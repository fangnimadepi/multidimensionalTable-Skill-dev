"""Two parallel writers + an independent, source-complete audit. No silent fallback.

The renderer, evidence excerpts and source dates are deterministic. A failed audit
permits one bounded repair of the affected report(s), followed by a fresh audit.
Uncertain reviewer issues are adjudicated separately before any rewrite. Legacy
reports remain readable; this schema is never passed off as a legacy audit.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

import achievement_sources
import build_achievement_reports as achievement
import build_contract_review as base
import build_project_reports as review
import build_reliable_review as reliable
import compact_report_prompts as prompts
import project_report_contract as contract
import project_report_presentation as presentation
import project_report_profile as profile
from llm_runtime import call_scope, emit, parallel_map, check_cancelled
from llm_step_cache import restore_sibling_step
from review_common import sha256
from audit_model_usage import estimate_cny

VERSION = 'compact-project-reports-v1'
CATEGORIES = ('进度与计划节点', '数量与规模', '技术路线与验收口径', '成果交付与应用状态', '报告内部表述不一致')
STATES = {'满足本期要求', '部分完成', '尚未完成', '需要确认', '后续节点'}
QUESTIONS = {'problem':'解决什么问题', 'delivered':'已经做成什么', 'increment':'核心增量是什么',
             'results':'取得什么结果', 'stage':'当前到哪一步'}
ROOT = base.ROOT


def source_catalog(pair, policy):
    """One shared catalog for both writers, using the existing section readers."""
    source = reliable.extract_source(pair, policy)
    source['administrative_profile'] = profile.extract(pair.plan_path,pair.project_id,ROOT)
    records = {}; required = []
    periods = {p['id']: p for p in source['plan_periods']}
    for ref, text in source['plan_blocks'].items():
        period = periods.get(ref)
        # Split only existing paragraph/line boundaries, preserving the complete
        # parent block as context. A completed subtask cannot cover its neighbours.
        parts=[line for line in text.splitlines() if line.strip() and line.strip()!=period['heading'].strip()] if period else [text]
        parts=parts or [text]
        for index,part in enumerate(parts,1):
            key=ref+f'-U{index}' if period else ref
            records[key] = {'side':'plan', 'role':'plan' if period else 'overall_reference',
                'file':source['plan_file'], 'location':ref, 'section':'里程碑、年度计划及年度目标' if period else '总体成果与考核要求',
                'text':part, 'context':text, 'node':period['heading'] if period else '',
                'due':period['due']['date'] if period else None,
                'scope':period['checkpoint_scope'] if period else '总体目标参考'}
            if period and period['checkpoint_scope']=='本轮节点': required.append(key)
    for ref, value in contract.report_sources(source).items():
        records[ref] = {'side':'report', 'file':source['report_file'], 'section':'进展与总结',
            'location':value['base_ref'], **value}
    # Background and prior methods are needed by the value report. Reuse the
    # already parsed progress rather than independently parsing it a second time.
    extra = achievement_sources.docx_sections(pair.plan_path, 'plan')
    if pair.report_path.suffix.lower() == '.pdf':
        more, _ = achievement_sources.pdf_sections(pair.report_path)
    else:
        more = achievement_sources.docx_sections(pair.report_path, 'report')
    existing = {r.get('base_ref', ref) for ref, r in records.items() if r['side']=='report'}
    compact = lambda text: re.sub(r'\s+', '', text)
    plan_text = '\n'.join(compact(r['text']) for r in records.values() if r['side']=='plan')
    report_text = '\n'.join(compact(r['text']) for r in records.values() if r['side']=='report')
    for ref, record in {**extra, **more}.items():
        # Never reintroduce merged target/actual/self-assessment table cells.
        if record['side']=='report' and (ref in existing or ref.split('-S')[0] in existing): continue
        if compact(record['text']) in (plan_text if record['side']=='plan' else report_text): continue
        if record['side']=='plan' and record['section'] in {'里程碑计划','成果与评测','追加支持条件'}: continue
        records['B-'+ref] = {**record, 'role':'background' if record['side']=='plan' else 'actual_or_body'}
    if not required or not any(r['side']=='report' for r in records.values()):
        raise ValueError('未提取到完整计划和报告内容，不能按无差异发布。')
    return {'project_id':pair.project_id, 'project_name':pair.plan_path.stem,
        'plan_file':source['plan_file'], 'report_file':source['report_file'],
        'plan_sha256':source['plan_sha256'], 'report_sha256':source['report_sha256'],
        'checkpoint_date':policy['checkpoint_date'], 'review_stage':policy['review_stage'],
        'evidence_as_of':source['evidence_as_of'], 'administrative_profile':source['administrative_profile'],
        'scanned_report':source.get('ocr',{}).get('scanned',False), 'required_plan_refs':required, 'records':records}


def statements(kind, draft):
    """Stable structural IDs are assigned by code, never by a model."""
    values = []
    def add(path, value):
        form=('提问' if path.startswith('pending.') or path.endswith('.confirmation') else
              '价值分析' if kind=='achievement' and (path.startswith('value_analysis.') or path in {'overall_judgement','value_title','executive_summary'}) else
              '对照判断' if path in {'overall_situation','overall_opinion'} or path.endswith('.analysis') else '事实归纳')
        values.append({**value, 'id':kind+'.'+path,'kind':form} if isinstance(value, dict) else {'id':kind+'.'+path,'kind':form})
    if kind=='review':
        for field in ('overview','plan_summary','progress_summary','overall_situation','overall_opinion'):
            add(field,draft.get(field))
        for i, row in enumerate(draft.get('comparisons',[])):
            if row.get('comparable') is True and row.get('status') in {'满足本期要求','部分完成','尚未完成'}:
                add(f'comparisons.{i}.progress',row.get('progress'))
        for i, row in enumerate(draft.get('differences',[])):
            for field in ('analysis','confirmation'): add(f'differences.{i}.{field}',row.get(field))
        for i, value in enumerate(draft.get('pending',[])): add(f'pending.{i}',value)
    else:
        for field in ('value_title','executive_summary','overall_judgement'): add(field,draft.get(field))
        for key in QUESTIONS: add('fact_sheet.'+key,draft.get('fact_sheet',{}).get(key))
        for i,value in enumerate(draft.get('value_analysis',[])): add(f'value_analysis.{i}',value)
    return values


def validate_draft(kind, draft, source):
    """Literal registry, numeric and role checks precede the semantic audit."""
    errors = []; records=source['records']
    if not isinstance(draft,dict): return ['draft_not_object']
    if kind=='review':
        for key in ('comparisons','differences','pending'):
            if not isinstance(draft.get(key),list): return ['missing_array:'+key]
        required=set(source['required_plan_refs']); seen=set()
        for i,row in enumerate(draft['comparisons']):
            if not isinstance(row,dict): return ['invalid_comparison']
            ref=row.get('plan_ref'); state=row.get('status'); comparable=row.get('comparable'); basis=row.get('basis')
            if not isinstance(ref,str): return ['invalid_plan_ref']
            if ref not in required: errors.append(f'unknown_plan_ref:{i}')
            else:
                seen.add(ref)
                if records[ref]['scope']=='后续节点' and state!='后续节点': errors.append(f'future_as_current:{i}')
                if records[ref]['scope']!='后续节点' and state=='后续节点': errors.append(f'current_as_future:{i}')
            if not isinstance(state,str) or not isinstance(basis,str): return ['invalid_comparison_state']
            if state not in STATES or type(comparable) is not bool: errors.append(f'invalid_comparison_state:{i}')
            permitted={'满足本期要求':{'报告明确完成'},'部分完成':{'报告明确未完成','量化缺口'},
                '尚未完成':{'报告明确未完成','量化缺口'},'需要确认':{'不可比','披露不足','报告明确未完成'},'后续节点':{'后续节点'}}
            if basis not in permitted.get(state,set()): errors.append(
                f'comparisons.{i}:status={state},basis={basis}不匹配；披露不足/不可比必须为需要确认，'
                '部分完成/尚未完成必须有报告明确未完成或量化缺口；请修正status/basis/comparable及相应叙述')
            if comparable is False and state not in {'需要确认','后续节点'}: errors.append(f'assertive_incomparable:{i}')
            if not isinstance(row.get('progress'),dict): return ['invalid_progress']
            if state in {'满足本期要求','部分完成','尚未完成'} and not row['progress'].get('refs'):
                errors.append(f'comparison_without_report:{i}')
        if required-seen: errors.append('uncovered_plan_refs:'+','.join(sorted(required-seen)))
        for i,row in enumerate(draft['differences']):
            if not isinstance(row,dict): return ['invalid_difference']
            refs=row.get('plan_refs')
            if (row.get('category') not in CATEGORIES or not isinstance(row.get('title'),str) or
                    not row['title'].strip() or not isinstance(refs,list) or not refs or any(not isinstance(r,str) or r not in required for r in refs)):
                errors.append(f'invalid_difference:{i}')
            elif all(records[r]['scope']=='后续节点' for r in refs): errors.append(f'future_difference:{i}')
            if row.get('basis') not in {'报告明确未完成','量化缺口','报告明确规格不同','报告内部冲突'}:
                errors.append(f'difference_without_explicit_basis:{i}')
    elif (not isinstance(draft.get('value_analysis'),list) or not 2<=len(draft['value_analysis'])<=4
            or not isinstance(draft.get('fact_sheet'),dict)):
        return ['achievement_sections_missing']
    for claim in statements(kind,draft):
        key=claim['id']; text=claim.get('text'); refs=claim.get('refs')
        if (not isinstance(text,str) or not text.strip() or not isinstance(refs,list) or
                not refs or any(not isinstance(r,str) or r not in records for r in refs)):
            errors.append('claim_or_ref_invalid:'+key); continue
        if len(set(refs))!=len(refs): errors.append('duplicate_ref:'+key)
        support='\n'.join(records[r].get('context',records[r]['text']) for r in refs)
        if not presentation.number_support(text,support): errors.append('unsupported_number:'+key)
        if re.search(r'\]\(|<\s*(?:a|script|iframe)\b|^#{1,6} ',text,re.I|re.M): errors.append('model_markup:'+key)
        sides={records[r]['side'] for r in refs}
        if key.endswith(('progress_summary','overall_situation','executive_summary','overall_judgement')) and 'report' not in sides:
            errors.append('actual_without_report:'+key)
        if key.endswith('plan_summary') and 'plan' not in sides: errors.append('plan_without_taskbook:'+key)
        if key.endswith('overall_opinion') and sides!={'plan','report'}: errors.append('opinion_without_both_sides:'+key)
        current_comparison=('.comparisons.' in key and draft['comparisons'][int(key.split('.')[2])]['status']!='后续节点')
        if current_comparison or key.endswith(('fact_sheet.delivered','fact_sheet.increment','fact_sheet.results','fact_sheet.stage')):
            if ('report' not in sides or (not current_comparison and sides!={'report'})):
                errors.append('progress_requires_report:'+key)
            if not any(records[r]['side']=='report' and records[r]['role'] not in {'target_restatement','self_assessment','proof_label'} for r in refs):
                errors.append('target_or_label_as_fact:'+key)
        if key.endswith('.analysis') and sides!={'plan','report'}: errors.append('difference_without_both_sides:'+key)
        if contract.asserts_delay(text) and not source['evidence_as_of'].get('date'):
            errors.append('delay_without_fact_time:'+key)
    return errors


def audit_errors(audit, claims):
    expected=[c['id'] for c in claims]
    if not isinstance(audit,dict): return [{'id':'$structure','reason':'audit_not_object'}]
    ids=audit.get('checked_ids',[]); issues=audit.get('issues')
    if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)) or set(ids)!=set(expected):
        return [{'id':'$structure','reason':'audit_claim_coverage_mismatch'}]
    if not isinstance(issues,list) or any(not isinstance(i,dict) or not isinstance(i.get('reason'),str) or
            i.get('id') not in set(expected)|{'$coverage','$paired','$structure'} for i in issues):
        return [{'id':'$structure','reason':'invalid_audit_issues'}]
    if audit.get('coverage_ok') is not True: issues=issues+[{'id':'$coverage','reason':'plan_coverage_not_passed'}]
    if audit.get('paired_ok') is not True: issues=issues+[{'id':'$paired','reason':'paired_consistency_not_passed'}]
    return issues


def model_input(source, policy):
    return {'project_id':source['project_id'], 'project_name':source['project_name'],
        'checkpoint_date':source['checkpoint_date'], 'evidence_as_of':source['evidence_as_of'],
        'required_plan_refs':source['required_plan_refs'], 'rules':policy['rules'],
        # Paths and cover contact details are not needed by the model.
        'source_records':{ref:{k:v for k,v in r.items() if k not in {'file','location','base_ref'}}
                          for ref,r in source['records'].items()}}


def bind_citations(kind, raw, source):
    """Propose original source locations, never change a model's statement.

    Repairs only unknown reference labels or a number missing from selected
    references. Suggestions are NOT approval: all final text and these locations
    must still pass the independent whole-context audit. No new facts or numbers
    are generated here. Keep the raw response and a separate change record.
    """
    draft=copy.deepcopy(raw); changes=[]; records=source['records']
    if not isinstance(draft,dict): return draft,changes
    try: claims=statements(kind,draft)
    except (TypeError,AttributeError): return draft,changes
    def grams(text):
        text=re.sub(r'[^\w\u4e00-\u9fff]','',text.lower())
        return {text[i:i+2] for i in range(len(text)-1)}
    for claim in claims:
        refs=claim.get('refs'); text=claim.get('text')
        if not isinstance(refs,list) or not isinstance(text,str) or any(not isinstance(r,str) for r in refs): continue
        valid=list(dict.fromkeys(r for r in refs if r in records)); added=[]
        support='\n'.join(records[r].get('context',records[r]['text']) for r in valid)
        if not presentation.number_support(text,support):
            missing=contract.numeric_tokens(text)-contract.numeric_tokens(support); query=grams(text)
            report_only=claim['id'].endswith(('fact_sheet.delivered','fact_sheet.increment','fact_sheet.results','fact_sheet.stage'))
            candidates=[]
            for ref,r in records.items():
                if ref in valid or not (missing & contract.numeric_tokens(r['text'])): continue
                if report_only and r['side']!='report': continue
                if r['side']=='report' and r['role'] in {'target_restatement','self_assessment','proof_label'}: continue
                overlap=len(query & grams(r['text']))
                if overlap>=4: candidates.append((overlap,ref))
            for _,ref in sorted(candidates,reverse=True):
                if len(added)>=2 or presentation.number_support(text,support): break
                valid.append(ref); added.append(ref); support+='\n'+records[ref].get('context',records[ref]['text'])
        if valid!=refs:
            target=draft
            for part in claim['id'].split('.')[1:]: target=target[int(part)] if isinstance(target,list) else target[part]
            target['refs']=valid
            changes.append({'id':claim['id'],'removed':[r for r in refs if r not in valid],'added':added,
                'status':'proposed_locations_require_independent_audit'})
    return draft,changes


def literal_progress(raw, source):
    """A comparison cell displays report wording, not a newly expanded capability.

    Model-selected short excerpts are recovered only when literal; otherwise the
    full selected report block is used, as in the legacy fact citation fallback.
    The model's status and the choice of source still require independent audit.
    """
    draft=copy.deepcopy(raw)
    if not isinstance(draft,dict) or not isinstance(draft.get('comparisons'),list): return draft
    for row in draft['comparisons']:
        if not isinstance(row,dict) or not isinstance(row.get('progress'),dict): continue
        claim=row['progress']; refs=claim.get('refs',[])
        if not isinstance(refs,list): continue
        available=[r for r in refs if isinstance(r,str) and r in source['records']
            and source['records'][r]['side']=='report'
            and source['records'][r]['role'] not in {'target_restatement','self_assessment','proof_label'}]
        if not available: continue
        exact=next(((r,contract.literal_quote(claim.get('text'),source['records'][r]['text'])) for r in available
            if contract.literal_quote(claim.get('text'),source['records'][r]['text'])),None)
        ref,quote=exact or (available[0],source['records'][available[0]]['text'])
        claim.update(text=quote,refs=[ref])
    return draft


def gate_findings(raw):
    """Abstain on a proposed difference when its own comparison is unconfirmed.

    The model's existing PI question is retained verbatim. No new project
    assessment is written by this clerical gate; rejected candidates stay local.
    """
    draft=copy.deepcopy(raw);rejected=[]
    if not isinstance(draft,dict) or not all(isinstance(draft.get(k),list) for k in ('comparisons','differences','pending')):
        return draft,rejected
    kept=[]
    for value in draft['differences']:
        if not isinstance(value,dict) or not isinstance(value.get('plan_refs'),list): kept.append(value);continue
        rows=[r for r in draft['comparisons'] if isinstance(r,dict) and r.get('plan_ref') in value['plan_refs']]
        binding=any(r.get('comparable') is True and r.get('status') in {'部分完成','尚未完成'} for r in rows)
        conflict=value.get('category')=='报告内部表述不一致' and value.get('basis')=='报告内部冲突'
        if binding or conflict: kept.append(value);continue
        question=value.get('confirmation')
        if isinstance(question,dict) and question not in draft['pending']:draft['pending'].append(question)
        rejected.append({'title':value.get('title'),'reason':'no_assertive_comparable_row','candidate':value})
    draft['differences']=kept
    return draft,rejected


class Caller:
    def __init__(self, work, config):
        self.work=work; self.config=config; self.calls=[]; self.lock=threading.Lock(); self.requests=0
        self.started=time.monotonic(); self.budget=float(config.get('generation_budget_seconds',75))
        if not 1<=self.budget<=300: raise ValueError('Invalid model generation budget')

    def __call__(self, stage, prompt, payload, model, tokens):
        config={**self.config,'text_model':model,'request_attempts':1}
        # A content-addressed stage survives unrelated UI or prompt edits.
        key=contract.digest({'protocol':VERSION,'system':prompt,'payload':payload,'model':model,
            'api_base':config['api_base'],'provider':config.get('provider'),'max_tokens':tokens,'thinking':False})
        path=self.work/'calls'/f'{stage}-{key[:16]}.json'
        restore_sibling_step(path,key,self.work,ROOT)
        with call_scope(report='compact',stage=stage):
            check_cancelled()
            value=None
            if path.exists():
                candidate=json.loads(path.read_text())
                if candidate.get('cache_key')==key and isinstance(candidate.get('answer'),dict): value=candidate
            if value:
                usage={**value['usage'],'cache_reused':True}; emit('cache_reused')
            else:
                with self.lock:
                    if self.requests>=7: raise ValueError('已达到本轮模型调用预算，停止自动重写，保留已有报告。')
                    left=self.budget-(time.monotonic()-self.started)
                    if left<1: raise ValueError('本轮模型处理已达到等待上限，已保留完成步骤和原报告。')
                    self.requests+=1
                config.update(request_timeout_seconds=min(float(config.get('request_timeout_seconds',90)),left),
                    request_total_budget_seconds=left,
                    request_idle_timeout_seconds=min(float(config.get('request_idle_timeout_seconds',30)),left))
                answer,usage=base.call_json_model(prompt,payload,config,tokens)
                review.write_json(path,{'cache_key':key,'model':model,'stage':stage,'system_prompt':prompt,
                    'request':payload,'answer':answer,'usage':usage})
                value={'answer':answer}; usage={**usage,'cache_reused':False}
            with self.lock: self.calls.append({'stage':stage,**usage})
            return value['answer']


def comparison_metadata(drafts):
    if 'review' not in drafts: return {}
    draft=drafts['review']
    return {'review':{'comparisons':[{**{k:row[k] for k in ('plan_ref','status','basis','comparable')},
        'claim_id':f'review.comparisons.{i}.progress' if row['comparable'] is True and row['status'] in {'满足本期要求','部分完成','尚未完成'} else None}
        for i,row in enumerate(draft['comparisons'])],
        'differences':[{**{k:row[k] for k in ('category','basis','plan_refs')},
            'analysis_id':f'review.differences.{i}.analysis','confirmation_id':f'review.differences.{i}.confirmation'}
            for i,row in enumerate(draft['differences'])]}}


def adjudicated_errors(raw, issues, claims, records):
    checks=raw.get('checks') if isinstance(raw,dict) else None
    expected={str(i+1):issue for i,issue in enumerate(issues)}
    if not isinstance(checks,list) or len(checks)!=len(expected): raise ValueError('复核意见裁定未完整覆盖，停止发布。')
    found=set(); confirmed=[]; texts={c['id']:c['text'] for c in claims}
    for check in checks:
        if not isinstance(check,dict): raise ValueError('复核意见裁定格式错误。')
        key=check.get('issue_id')
        if not isinstance(key,str) or key not in expected or key in found or type(check.get('confirmed')) is not bool:
            raise ValueError('复核意见裁定编号不完整或重复，停止发布。')
        found.add(key)
        # A second model may help reject a style suggestion; it cannot waive
        # explicit findings about these conservative business boundaries.
        protected=protected_issue(expected[key])
        if not check['confirmed'] and protected:
            confirmed.append(expected[key]);continue
        if check['confirmed']:
            issue=expected[key]; refs=check.get('source_refs'); quote=check.get('claim_quote')
            if (not isinstance(refs,list) or not refs or any(not isinstance(r,str) or r not in records for r in refs)
                    or not isinstance(check.get('reason'),str) or not check['reason'].strip()):
                raise ValueError('实质复核问题缺少原文位置或具体说明，停止发布。')
            if not issue['id'].startswith('$') and not contract.literal_quote(quote,texts.get(issue['id'],'')):
                raise ValueError('实质复核问题未定位到草稿原句，停止发布。')
            confirmed.append({**issue,'reason':check['reason']})
    return confirmed


def protected_issue(issue):
    text=issue['reason']
    hard=('.comparisons.' in issue['id'] or '.differences.' in issue['id']) or bool(re.search(
        r'阶段错配|状态升级|性能范围|范围的扩大|将.{0,24}扩大为|'
        r'对象不对应|不可比|比较口径|量化缺口|量化差异|无法构成.{0,12}(?:差异|缺口)|'
        r'(?:终期|总体).{0,24}(?:误当|误作|错误地?当|误用于|当作本期|当作中期)|'
        r'(?:数字|数量|单位|指标口径).{0,15}(?:错误|不符|混用)|'
        r'(?:未来|下一步|计划).{0,15}(?:写成|改写为|当作).{0,12}(?:已完成|已实现|当前事实)|'
        r'无依据的?(?:未完成|未开展)|'
        r'尚未(?:开展|进入|实施)|'
        r'(?:未提及|未说明).{0,12}(?:等同|推断|写成|认定).{0,8}(?:未完成|未开展|已完成)',text))
    # Unknown objections are substantive by default. Only explicitly editorial
    # suggestions are eligible for a second model's dismissal.
    editorial=bool(re.search(r'同义|措辞|重复叙述|排版|阅读顺序|语言风格|表述方式|wording|stylistic|formatting',text,re.I))
    return hard or not editorial


def checked_audit(record, claims, records):
    errors=audit_errors(record.get('raw'),claims)
    return adjudicated_errors(record['adjudication'],errors,claims,records) if record.get('adjudication') is not None else errors


def generate(source, policy, config, work, retained=None):
    retained=retained or {}; caller=Caller(work,config)
    context=model_input(source,policy)
    if sum(len(r['text']) for r in source['records'].values())>160000:
        raise ValueError('当前材料超出快速流程的完整语境容量，需要分批处理；未截断原文或生成简版报告。')
    writer=config.get('compact_writer_model',config['text_model']); auditor=config.get('compact_audit_model',config['text_model'])
    types=[k for k in ('review','achievement') if k not in retained and k in config['requested_types']]
    drafts={kind:r['draft'] for kind,r in retained.items()}
    writer_prompts={'review':prompts.REVIEW,'achievement':prompts.ACHIEVEMENT}
    def draft_one(kind):
        extra={'method':achievement.framework()['method_only']} if kind=='achievement' else {}
        return kind,caller(kind+'-draft',writer_prompts[kind],{**context,**extra},writer,6000 if kind=='review' else 3600)
    drafts.update(dict(parallel_map(draft_one,types,2)))
    # Only one repair pass. Unchanged valid reports/paragraphs are not rewritten.
    # Audit includes both reports together, eliminating a separate paired call.
    for attempt in range(2):
        bindings=[]
        for kind in types:
            if kind=='review':
                drafts[kind]=literal_progress(drafts[kind],source)
                drafts[kind],gated=gate_findings(drafts[kind])
                if gated:review.write_json(work/f'gated-findings-{attempt}.json',gated)
            drafts[kind],changes=bind_citations(kind,drafts[kind],source); bindings+=changes
        if bindings: review.write_json(work/f'citation-proposals-{attempt}.json',bindings)
        local={kind:validate_draft(kind,draft,source) for kind,draft in drafts.items()}
        errors=[{'id':kind,'reason':';'.join(values)} for kind,values in local.items() if values]
        if not errors:
            claims=[c for kind,draft in drafts.items() for c in statements(kind,draft)]
            metadata=comparison_metadata(drafts)
            audit=caller('independent-audit',prompts.AUDIT,{**context,'claims':claims,'comparison_metadata':metadata},auditor,1800)
            errors=audit_errors(audit,claims)
            audit_record={'raw':audit}
            # No rewriting on a reviewer's unverified style suggestions. A
            # separate call can confirm material errors, not create new ones.
            if (errors and any(not protected_issue(e) for e in errors) and isinstance(audit.get('checked_ids'),list) and
                    set(audit['checked_ids'])=={c['id'] for c in claims} and
                    not any(e['reason'] in {'audit_claim_coverage_mismatch','invalid_audit_issues'} for e in errors)):
                tagged=[{**issue,'issue_id':str(i+1)} for i,issue in enumerate(errors)]
                judgement=caller('audit-adjudication',prompts.ADJUDICATE,{**context,'claims':claims,
                    'comparison_metadata':metadata,'issues':tagged},config.get('compact_adjudicator_model',auditor),1600)
                errors=adjudicated_errors(judgement,errors,claims,source['records'])
                audit_record['adjudication']=judgement
            if not errors: return drafts,audit_record,caller.calls
        review.write_json(work/f'rejected-{attempt}.json',{'issues':errors,'drafts':drafts})
        if attempt==1: break
        affected=set()
        for error in errors:
            key=error['id']; affected.update(types if key.startswith('$') else [key.split('.')[0]])
        if affected-set(types): raise ValueError('本次结果与保留报告存在待核对内容，请同时选择两类报告更新。')
        # One targeted patch request can fix either or both selected reports.
        # JSON paths identify individual text/refs pairs or structural arrays.
        patch_prompt=prompts.BOUNDARY+'''只修复issues指出的部分，保留其余内容。返回JSON {"patches":[{"report":"review|achievement","path":"overview 或 comparisons.0.progress 等已有路径","value":"该处完整对象或数组"}]}。不能改未选中的报告。若对照行的status/basis/comparable有误，必须修改这些字段，不能只改progress文字。没有依据判断完成的行改为需要确认/披露不足/comparable=false。可以整体替换comparisons/differences/pending，但不能改写无关叙述，不得省略未改字段。'''
        patch=caller('targeted-repair',patch_prompt,{**context,'issues':errors,'drafts':drafts,'editable_reports':sorted(affected),
            'output_contracts':{kind:writer_prompts[kind] for kind in affected}},config.get('compact_repair_model',auditor),4800)
        drafts=apply_patches(drafts,patch,affected)
    raise ValueError('报告未通过完整原文与独立复核，已停止自动重写；已有报告附件保留。')


def apply_patches(drafts, raw, allowed):
    values=raw.get('patches') if isinstance(raw,dict) else None
    if not isinstance(values,list) or not values or len(values)>30: raise ValueError('局部修订格式不完整，停止发布。')
    output=copy.deepcopy(drafts); seen=set()
    for patch in values:
        kind=patch.get('report'); path=patch.get('path','')
        if kind not in allowed or not isinstance(path,str) or not re.fullmatch(r'[a-z_]+(?:\.(?:[a-z_]+|\d+))*',path):
            raise ValueError('局部修订越出已选报告或有效路径。')
        key=(kind,path)
        if key in seen or any(k==kind and (p.startswith(path+'.') or path.startswith(p+'.')) for k,p in seen):
            raise ValueError('局部修订路径重复或相互覆盖。')
        seen.add(key); target=output[kind]; parts=path.split('.')
        try:
            for part in parts[:-1]: target=target[int(part)] if isinstance(target,list) else target[part]
            leaf=int(parts[-1]) if isinstance(target,list) else parts[-1]
            target[leaf]  # Existing keys only, including array bounds.
            target[leaf]=patch['value']
        except (KeyError,IndexError,TypeError,ValueError): raise ValueError('局部修订引用了不存在的报告路径。') from None
    return output


def render(kind, draft, source):
    claims=statements(kind,draft); records=source['records']; used=[]; uses={}
    # Comparison requirements are literal source text, including their conditions.
    for c in claims:
        for ref in c['refs']:
            if ref not in used: used.append(ref)
            uses.setdefault(ref,[]).append(c['text'])
    if kind=='review':
        for row in draft['comparisons']:
            ref=row['plan_ref']
            if ref not in used: used.append(ref)
            uses.setdefault(ref,[]).append('用于对照本项里程碑的计划要求及计划节点。')
    # A single full parent paragraph can support several subconditions. Cite it
    # once in the appendix while preserving all uses and all inline destinations.
    canonical={}; first={}
    for ref in used:
        r=records[ref]; key=(r['side'],r['file'],r['location'],r.get('context',r['text']))
        owner=first.setdefault(key,ref); canonical[ref]=owner
        if owner!=ref: uses.setdefault(owner,[]).extend(uses.get(ref,[]))
    distinct=list(dict.fromkeys(canonical[r] for r in used))
    prefix=('evidence-' if kind=='review' else 'achievement-')+source['project_id'].lower()+'-'
    names={r:('依据' if kind=='review' else '成果依据')+f'{i:02d}' for i,r in enumerate(distinct,1)}
    anchors={r:prefix+f'{i:02d}' for i,r in enumerate(distinct,1)}
    def links(refs): return '、'.join(f'[{names[r]}](#{anchors[r]})' for r in dict.fromkeys(canonical[ref] for ref in refs))
    def text(c): return c['text']+'（'+links(c['refs'])+'）'
    title='项目阶段履约审查报告' if kind=='review' else '项目阶段成果与后续推进分析报告'
    lines=['# '+title,'','**项目：'+source['project_name']+'**','']
    lines+=profile.header_lines(source['administrative_profile'])
    if kind=='review':
        lines += [f"审查阶段：{source['review_stage']}　｜　审查基准日：{source['checkpoint_date']}（暂定）",'',
            '## 一、项目概况与审查口径','',text(draft['overview']),'']
    else: lines += ['**'+text(draft['value_title'])+'**','',text(draft['executive_summary']),'']
    lines += ['- 任务书：'+Path(source['plan_file']).name,'- 总结报告：'+Path(source['report_file']).name,'',
        '文中“依据”编号可在本报告末尾查阅文件位置及原文。','']
    if kind=='review':
        lines += ['## 二、计划与实际进展总览','','**本期计划重点：** '+text(draft['plan_summary']),'',
            '**当前工作进展：** '+text(draft['progress_summary']),'','### 计划与进展对照','',
            '| 序号 | 任务书要求 | 计划节点 | 报告进展 | 完成情况及认定依据 | 来源 |','| --- | --- | --- | --- | --- | --- |']
        displayed=0
        for row in draft['comparisons']:
            if row['comparable'] is not True or row['status'] in {'需要确认','后续节点'}: continue
            r=records[row['plan_ref']]
            displayed+=1
            lines.append('| '+' | '.join(contract.cell(v) for v in (str(displayed),r['text'],r.get('node',''),
                row['progress']['text'],row['status'],links([row['plan_ref']]+row['progress']['refs'])))+' |')
        if not displayed:
            lines=lines[:-2]+['各项计划的对应进展尚需明确，具体问题见下方与 PI 老师确认事项。']
        lines += ['','## 三、项目出入点与对接重点','']
        if not draft['differences']: lines+=['本节暂无单列的出入事项。需明确的内容见下方与 PI 老师确认事项。','']
        for category in CATEGORIES:
            differences=[d for d in draft['differences'] if d['category']==category]
            if not differences: continue
            lines+=['### '+category,'']
            for i,d in enumerate(differences,1):
                planned='；'.join(records[r]['text'] for r in d['plan_refs'])
                progress=[row['progress'] for row in draft['comparisons'] if row['plan_ref'] in d['plan_refs']]
                lines+=['#### '+str(i)+'. '+d['title'],'','**任务书要求：** '+planned+'（'+links(d['plan_refs'])+'）','',
                    '**完成情况：** '+'；'.join(text(c) for c in progress),'','**出入情况：** '+text(d['analysis']),'']
        lines+=['### 综合审查意见','',text(draft['overall_situation']),'',text(draft['overall_opinion']),'']
        pending=[d['confirmation'] for d in draft['differences']]+draft['pending']
        if pending:
            lines+=['### 与 PI 老师确认事项','']
            for i,c in enumerate(pending,1): lines+=[f'{i}. '+text(c),'']
    else:
        lines+=['## 一、成果事实提炼','','| 提炼维度 | 当前成果与进展 |','| --- | --- |']
        for key,label in QUESTIONS.items(): lines.append('| '+label+' | '+contract.cell(text(draft['fact_sheet'][key]))+' |')
        lines+=['','## 二、领域背景与成果价值','']
        for c in draft['value_analysis']: lines += [text(c),'']
        lines += ['### 综合判断','',text(draft['overall_judgement']),'']
    lines+=['## 附录：判断依据','']
    if source['scanned_report']: lines+=['总结报告为扫描 PDF，以下摘录为本地 OCR 文本，位置按原文件页码标注。','']
    for ref in distinct:
        r=records[ref]
        location=contract.location(r['location']) if re.match(r'^[PR]-',r['location']) else r['location']
        lines += [f'<a id="{anchors[ref]}" name="{anchors[ref]}"></a>','',f"### {names[ref]}｜"+('任务书' if r['side']=='plan' else '总结报告'),'',
            '- 文件：'+Path(r['file']).name,'- 位置：'+location,'- 原文栏目：'+r['section'],'',
            '**引用用途：** '+'；'.join(dict.fromkeys(uses[ref])),'',
            '**原文及同段语境：**','']
        lines += ['> '+line for line in r.get('context',r['text']).splitlines()]
        lines += ['']
    return '\n'.join(lines)+'\n'


def run(manifest, folder, pair, policy, config, trace, title):
    work=Path(manifest['workDir'])/'compact'; work.mkdir(parents=True,exist_ok=True)
    with call_scope(report='compact',stage='extract'):
        source=source_catalog(pair,policy); source['project_name']=title
    trace.phase('两份材料已解析，正在生成项目报告','compact/prepare')
    review.write_json(work/'source.json',source)
    retained={}
    for kind,entry in manifest.get('retained',{}).items():
        path=(ROOT/entry['path']).resolve()
        if not path.is_relative_to(ROOT/'runs') or sha256(path)!=entry['sha256']: raise ValueError('已有报告版本校验失败。')
        old=json.loads(path.read_text())
        if old.get('schema_version')!=VERSION or old.get('project_id')!=pair.project_id or any(
                old['source'][role+'_sha256']!=manifest['hashes'][role] for role in manifest['hashes']):
            raise ValueError('已有报告的材料或生成协议不同，请同时勾选两类报告更新。')
        retained[kind]=old
    drafts,audit,calls=generate(source,policy,{**config,'requested_types':manifest['types']},work,retained)
    if checked_audit(audit,[c for k,d in drafts.items() for c in statements(k,d)],source['records']):
        raise ValueError('最终独立复核记录未通过。')
    markdown={}; files={}; checks={}
    for kind,draft in drafts.items():
        if validate_draft(kind,draft,source): raise ValueError('最终原文与结构检查未通过。')
        value={'schema_version':VERSION,'project_id':pair.project_id,'kind':kind,'source':source,'draft':draft,
            'validation':{'passed':True,'independent_audit':audit},'run_metadata':{'calls':calls}}
        path=folder/'reports'/kind/(pair.project_id+'.json'); review.write_json(path,value)
        markdown[kind]=render(kind,draft,source); files[kind]={'path':str(path.relative_to(ROOT)),'sha256':sha256(path)}
        (work/(kind+'.md')).write_text(markdown[kind]); checks[kind]={'passed':True,'schema':VERSION}
    for role,path in manifest['paths'].items():
        if sha256(Path(path))!=manifest['hashes'][role]: raise ValueError('项目材料在生成期间发生变化，停止发布。')
    if len(drafts)==2: checks['paired']={'passed':True}
    actual=[c for c in calls if not c.get('cache_reused')]
    result={'projectId':pair.project_id,'projectName':title,'reports':markdown,'reportFiles':files,
        'materialHashes':manifest['hashes'],'pipelineFingerprint':manifest['pipelineFingerprint'],'checks':checks,
        'runMetadata':{'pipeline':VERSION,'model':config.get('compact_writer_model',config['text_model']),
            'auditModel':config.get('compact_audit_model',config['text_model']),'provider':config.get('provider'),
            'adjudicatorModel':config.get('compact_adjudicator_model',config['text_model']),
            'maxModelRequests':7,'modelBudgetSeconds':config.get('generation_budget_seconds',75),
            'estimatedCost':estimate_cny(actual),
            'checkpointDate':policy['checkpoint_date'],'reviewStage':policy['review_stage'],
            'calls':len(calls),'newModelCalls':len(actual),'promptTokens':sum(c.get('prompt_tokens',0) for c in actual),
            'completionTokens':sum(c.get('completion_tokens',0) for c in actual),
            'cachedSteps':len(calls)-len(actual),'timing':trace.summary()}}
    trace.phase('原文与报告复核完成，正在制作详细文档','assembled')
    review.write_json(folder/'generated.json',result); trace.write('timing-summary.json',trace.summary())
    return result
