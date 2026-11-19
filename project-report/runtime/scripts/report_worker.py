"""Isolated report generation using packaged rules and source-bound checks."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import threading
from pathlib import Path

import build_achievement_reports as achievement
import build_contract_review as base
import build_project_reports as review
import build_reliable_review as reliable
import check_achievement_report_artifacts as achievement_check
import check_project_report_artifacts as review_check
import project_report_contract as contract
from docx import Document
from review_common import sha256
from llm_runtime import CONFIG as RUNTIME_CONFIG, model_config, call_scope, set_observer, emit, parallel_map, reset_cancellation, cancel_requests, check_cancelled
from pipeline_timing import PipelineTrace
from report_diagnostics import failure_details, exception_details, read_json, write_failure

ROOT = base.ROOT
VERSION = 'feishu-source-generation-v1.2'


def policy_path():
    private = ROOT / 'config/review_policy_v0.2.json'
    return private if private.exists() else ROOT / 'config/review_policy_v0.2.example.json'


def pipeline_fingerprint():
    """Includes rules, prompts, model config and methodology; excludes secrets."""
    paths = set((ROOT / 'scripts').glob('project_report*.py'))
    paths.update((ROOT / 'scripts').glob('achievement*.py'))
    paths.update((ROOT / 'scripts').glob('compact_report*.py'))
    paths.update(ROOT / 'scripts' / name for name in (
        'build_project_reports.py', 'build_achievement_reports.py', 'build_contract_review.py',
        'build_reliable_review.py', 'review_common.py', 'ocr_page.swift', 'pdf_ocr.py', 'requirements-ocr.txt', 'report_worker.py', 'siliconflow_stream.py',
        'check_project_report_artifacts.py', 'check_achievement_report_artifacts.py', 'report_diagnostics.py'))
    paths.update([policy_path(), base.CONFIG, RUNTIME_CONFIG, achievement.framework_path()])
    paths.update(ROOT / 'scripts' / name for name in ('llm_runtime.py','llm_client.py','pipeline_timing.py','llm_step_cache.py'))
    paths.add(ROOT / 'scripts/audit_model_usage.py')
    policy = json.loads(policy_path().read_text())
    paths.add(ROOT / policy['examples_file'])
    return contract.digest({'version': VERSION, 'files': {str(p.relative_to(ROOT)): sha256(p)
        for p in sorted(paths) if p.is_file()}})


def taskbook_identity(plan_path):
    plan = Document(plan_path)
    cover = '\n'.join(p.text for p in plan.paragraphs[:40])
    cover += '\n' + '\n'.join(cell.text for table in plan.tables[:1] for row in table.rows[:4] for cell in row.cells)
    ids = set(base.PROJECT_ID_PATTERN.findall(cover))
    title = next((match[1].strip() for p in plan.paragraphs[:40]
                  if (match := re.match(r'^\s*项\s*目\s*名\s*称(?:\s*[:：]\s*|\s+)(.+?)\s*$', p.text))), '')
    return ids, title


def project_identity(plan_path, report_path):
    """Read cover identity, keeping the selected row's two roles as the binding."""
    ids, title = taskbook_identity(plan_path)
    if len(ids) != 1:
        raise ValueError('任务书封面缺少唯一项目编号，无法建立独立项目档案。')
    pid = next(iter(ids))
    report_id = base.find_project_id(report_path)
    if report_id and report_id != pid:
        raise ValueError('任务书与总结报告封面的项目编号不同，请核对该行附件。')
    return pid, title or plan_path.stem


def retained_report(entry, kind, hashes, pid):
    path = (ROOT / entry['path']).resolve()
    if not path.is_relative_to(ROOT / 'runs') or sha256(path) != entry['sha256']:
        raise ValueError('已有报告版本校验失败，请同时勾选两类报告重新生成。')
    report = json.loads(path.read_text())
    source = report['review_scope'] if kind == 'review' else report['source']
    if report['project_id'] != pid or any(source[role + '_sha256'] != hashes[role] for role in hashes):
        raise ValueError('已有报告不属于当前材料，不能合并。')
    return report, path


def run_forecast(manifest, folder, work, path, config, trace, notify):
    from achievement_forecast import build

    ids, cover_title = taskbook_identity(path)
    project_number = next(iter(ids)) if len(ids) == 1 else ''
    pid = project_number or manifest['recordId']
    title = cover_title or manifest.get('projectName') or path.stem
    notify('提炼目标成果，撰写项目概述与用途', 'forecast/extract')
    calls = []

    def request(system, payload):
        with call_scope(report='forecast', stage='write'):
            answer, usage = achievement.cached_call(work / 'forecast', pid, 'forecast-write', system, payload, config, True)
            calls.append(usage)
            if usage.get('cache_reused'):
                emit('cache_reused')
            return answer, usage

    markdown, _ = build(path, title, project_number, request)
    if sha256(path) != manifest['hashes']['plan']:
        raise ValueError('项目任务书在生成期间发生变化，停止发布。')
    notify('已核对任务书依据，正在制作预期成果报告', 'assembled')
    result = {'projectId': pid, 'displayProjectId': project_number, 'projectName': title,
        'reports': {'forecast': markdown}, 'reportFiles': {},
        'materialHashes': manifest['hashes'], 'pipelineFingerprint': manifest['pipelineFingerprint'],
        'checks': {'forecast': {'passed': True}}, 'runMetadata': {
            'model': config['text_model'], 'provider': config.get('provider', 'siliconflow'),
            'calls': len(calls), 'newModelCalls': sum(not usage.get('cache_reused') for usage in calls),
            'responseTokens': sum(usage.get('total_tokens', 0) for usage in calls),
            'timing': trace.summary(), 'transferTiming': manifest.get('transferTiming')}}
    trace.write('timing-summary.json', trace.summary())
    review.write_json(folder / 'generated.json', result)
    return result


def run(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    work = Path(manifest['workDir']).resolve(); folder = manifest_path.parent
    if not work.is_relative_to(ROOT / 'runs') or manifest['pipelineFingerprint'] != pipeline_fingerprint():
        raise ValueError('生成流程版本已改变，请重新提交任务。')
    reset_cancellation()
    trace = PipelineTrace(folder, manifest.get('attemptId')); set_observer(trace.event)
    stages = []; calls = []

    def notify(message, stage):
        stages.append({'stage': stage, 'time': time.time()})
        trace.phase(message, stage)

    notify('读取项目材料，检查文件版本', 'inputs')
    paths = {role: Path(value).resolve() for role, value in manifest['paths'].items()}
    for role, path in paths.items():
        if not path.is_relative_to(ROOT / 'runs') or sha256(path) != manifest['hashes'][role]:
            raise ValueError('项目材料在处理前发生变化，请重新上传。')
    config = manifest.get('modelConfig') or model_config()
    if manifest['types'] == ['forecast']:
        if set(paths) != {'plan'}:
            raise ValueError('预期成果分析只接收一份项目任务书。')
        return run_forecast(manifest, folder, work, paths['plan'], config, trace, notify)

    original_review_call, original_achievement_call = review.cached_call, achievement.cached_call

    def review_call(pid, stage, *args, **kwargs):
        check_cancelled()
        with call_scope(report='review', stage=stage):
            result = original_review_call(pid, stage, *args, **kwargs)
            calls.append({'report': 'review', 'stage': stage, **result[1]})
            if result[1].get('cache_reused'): emit('cache_reused')
            return result

    def achievement_call(run_dir, pid, stage, *args, **kwargs):
        check_cancelled()
        with call_scope(report='achievement', stage=stage):
            result = original_achievement_call(run_dir, pid, stage, *args, **kwargs)
            calls.append({'report': 'achievement', 'stage': stage, **result[1]})
            if result[1].get('cache_reused'): emit('cache_reused')
            return result

    review.cached_call, achievement.cached_call = review_call, achievement_call
    pid, title = project_identity(paths['plan'], paths['report'])
    pair = base.Pair(pid, paths['plan'], paths['report'])
    policy = reliable.policy_data(policy_path())
    if config.get('generation_mode') == 'compact':
        from compact_report_pipeline import run as run_compact
        return run_compact(manifest, folder, pair, policy, config, trace, title)
    bank_path = ROOT / policy['examples_file']
    bank = json.loads(bank_path.read_text()) if bank_path.exists() else None
    review.RUN = work / 'review'
    achievement_run = work / 'achievement'
    reports = {}; files = {}; checks = {}; requested = manifest['types']
    achievement_ready = threading.Event()
    grouped_pair = config.get('review_workflow') == 'grouped' and set(requested) == {'review','achievement'}
    def paired_achievement():
        while not achievement_ready.wait(.2):
            check_cancelled()
        check_cancelled()
        report = reports.get('achievement')
        if not report or report.get('validation',{}).get('status') != 'passed':
            raise ValueError('Grouped review paired achievement report not ready')
        return {'achievement_draft':report['draft'],'checked_context':achievement.checked_context(report),
            'draft_sha256':contract.digest(report['draft'])}
    for kind, entry in manifest.get('retained', {}).items():
        reports[kind], path = retained_report(entry, kind, manifest['hashes'], pid)
        files[kind] = {'path': str(path.relative_to(ROOT)), 'sha256': sha256(path)}
    def generate_review(_):
        notify('解析任务书计划与总结报告', 'review/extract')
        with call_scope(report='review', stage='extract'):
            extracted = review.source_for(pair, policy)
            if (any((ROOT/extracted[role+'_file']).resolve() != paths[role] or
                    extracted[role+'_sha256'] != manifest['hashes'][role] for role in ('plan','report')) or
                    extracted['administrative_profile']['source_file'] != extracted['plan_file']):
                raise ValueError('提取缓存的材料路径与当前附件不一致，已在模型调用前停止。')
        options = {'paired_provider':paired_achievement} if grouped_pair else {}
        review.run_project(pair, policy, config, bank, True, False, 5, work / 'review-markdown', **options)
        path = review.RUN / 'reports' / (pid + '.json')
        report = json.loads(path.read_text()); md = contract.render_markdown(report)
        source = json.loads((review.RUN / 'sources' / (pid + '.json')).read_text())
        errors = review_check.check_report(report, md, source, ROOT)
        if errors:
            feedback_file = folder / 'artifact-errors.json'
            review.write_json(feedback_file, {'review': errors})
            raise contract.ReportValidationError('履约报告未通过原文与结构完整性检查，已保留原报告附件。',
                                                 stage='review/artifacts', feedback_file=feedback_file)
        reports['review'] = report; checks['review'] = {'passed': True}
        # Each published job owns an immutable JSON snapshot. Future retries
        # may update the working cache without invalidating prior reports.
        snapshot = folder / 'reports/review' / path.name
        snapshot.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, snapshot)
        files['review'] = {'path': str(snapshot.relative_to(ROOT)), 'sha256': sha256(snapshot)}
    def generate_achievement(_):
        notify('解析成果与背景材料', 'achievement/extract')
        achievement.run_project(pair, achievement_run, work / 'achievement-markdown', config, True)
        path = achievement_run / 'reports' / (pid + '.json')
        reports['achievement'] = json.loads(path.read_text())
        files['achievement'] = {'path': str(path.relative_to(ROOT)), 'sha256': sha256(path)}
        achievement_ready.set()
    tasks = [generate_review] if 'review' in requested else []
    if 'achievement' in requested: tasks.append(generate_achievement)
    def generate_one(function):
        try: return function(None)
        except Exception as error:
            review.write_json(folder / (function.__name__ + '-failure.json'),
                {'type':type(error).__name__, 'message':str(error)[:500]})
            cancel_requests()
            achievement_ready.set()
            raise
    parallel_map(generate_one, tasks, 2 if grouped_pair else config.get('report_workers', 1))
    if set(reports) == {'review', 'achievement'}:
        notify('交叉核对履约审查与成果分析的结论', 'paired')
        merged = reports['review'].get('validation',{}).get('grouped_paired') if grouped_pair else None
        if merged:
            from project_report_grouped import validate_paired_proof
            validate_paired_proof(reports['review'], reports['achievement'], ROOT)
            report = reports['achievement']
            report['validation']['paired_report_consistency'] = {**merged,'checked_in':'grouped_review',
                'review_report_file':files['review']['path'],'review_report_sha256':files['review']['sha256'],
                'final_draft_sha256':contract.digest(report['draft'])}
            achievement.write_json(ROOT/files['achievement']['path'],report)
            files['achievement']['sha256']=sha256(ROOT/files['achievement']['path'])
        elif 'achievement' in requested:
            # The generator may repair the selected achievement narrative; it
            # never rewrites an unselected, retained fulfillment report.
            achievement.check_paired_report(pair, achievement_run, work / 'achievement-markdown',
                (ROOT / files['review']['path']).parent, config, True)
            path = ROOT / files['achievement']['path']
            reports['achievement'] = json.loads(path.read_text())
            files['achievement']['sha256'] = sha256(path)
        else:
            # Updating only review must not silently edit retained achievement.
            report = reports['achievement']; draft = report['draft']
            from achievement_expression import is_expression, prompts as expression_prompts
            if is_expression(report):
                system = expression_prompts.PAIRED
                payload = {**achievement.checked_context(report),
                    'paired_review': contract.render_markdown(reports['review']).split('## 附录：', 1)[0],
                    'achievement_draft': draft}
            else:
                system = achievement.prompts.CONSISTENCY
                payload = {**achievement.checked_context(report),
                    'review_report': contract.render_markdown(reports['review']).split('## 附录：', 1)[0],
                    'achievement_report_statements': achievement.statements(draft)}
            cross, _ = achievement.cached_call(achievement_run, pid, 'paired-retained', system, payload, config, True)
            if cross.get('consistent') is not True or cross.get('issues') != []:
                review.write_json(folder / 'paired-conflict.json', cross)
                raise ValueError('新履约报告与保留的成果报告存在待核对表述，请同时勾选两类报告更新。')
        checks['paired'] = {'passed': True}
    if 'achievement' in requested:
        report = reports['achievement']; md = achievement.render(report)
        errors = achievement_check.check(report, md, ROOT, require_paired='review' in reports)
        if errors:
            feedback_file = folder / 'artifact-errors.json'
            review.write_json(feedback_file, {'achievement': errors})
            raise contract.ReportValidationError('成果报告未通过事实与原文完整性检查，已保留原报告附件。',
                                                 stage='achievement/artifacts', feedback_file=feedback_file)
        checks['achievement'] = {'passed': True}
        path = ROOT / files['achievement']['path']
        snapshot = folder / 'reports/achievement' / path.name
        snapshot.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, snapshot)
        files['achievement'] = {'path': str(snapshot.relative_to(ROOT)), 'sha256': sha256(snapshot)}
    markdown = {kind: contract.render_markdown(value) if kind == 'review' else achievement.render(value)
                for kind, value in reports.items()}
    for role, path in paths.items():
        if sha256(path) != manifest['hashes'][role]:
            raise ValueError('项目材料在生成期间发生变化，停止发布。')
    notify('报告及依据核验完成，正在整理详细文档', 'assembled')
    result = {'projectId': pid, 'projectName': title, 'reports': markdown, 'reportFiles': files,
        'materialHashes': manifest['hashes'], 'pipelineFingerprint': manifest['pipelineFingerprint'],
        'checks': checks, 'runMetadata': {'model': config['text_model'], 'provider': config.get('provider', 'siliconflow'),
            'checkpointDate': policy['checkpoint_date'], 'reviewStage': policy['review_stage'],
            'calls': len(calls), 'newModelCalls': sum(not c.get('cache_reused', False) for c in calls),
            'responseTokens': sum(c.get('total_tokens', 0) for c in calls), 'stages': stages,
            'timing': trace.summary(), 'transferTiming': manifest.get('transferTiming')}}
    trace.write('timing-summary.json', trace.summary())
    review.write_json(folder / 'generated.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path); args = parser.parse_args()
    try:
        run(args.manifest.resolve())
    except Exception as error:
        # Detailed errors stay local; no HTTP response bodies or key values.
        detail = failure_details(args.manifest.parent, error)
        detail.pop('worker', None)
        detail.update(exception_details(error))
        if isinstance(error, contract.ReportValidationError):
            detail.update(validation_stage=error.validation_stage, validation_file=str(error.validation_file))
        write_failure(args.manifest.parent / 'worker-failure.json', detail)
        progress = args.manifest.parent / 'progress.json'
        if progress.exists():
            write_failure(args.manifest.parent / 'timing-summary.json', read_json(progress).get('timing', {}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
