"""Reuse raw responses across code-only iterations only when the full request key matches."""
from __future__ import annotations

import json
import shutil
import hashlib
from pathlib import Path
from review_common import atomic_write_bytes

REQUEST_PROTOCOL = 'llm-request-v2'


def request_identity(system, payload, config, tokens):
    """Hash the actual semantic API request, excluding transport and credentials."""
    from llm_client import request_payload
    body=request_payload(system,payload,config,tokens)
    body.pop('stream',None)
    value={'protocol':REQUEST_PROTOCOL,'provider':config.get('provider','siliconflow'),
        'endpoint':config['api_base'].rstrip('/')+'/chat/completions','request':body}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def restore_compatible_step(primary, key, work_run, root, stage, legacy_verifier):
    """Reuse only proven-identical old requests; never upgrade an old final report.

    The caller verifies the legacy provider/key, full prompt, payload and token
    budget. Changing unrelated global prompt versions does not force a paid call.
    Current validators and independent audits still run after raw-response reuse.
    """
    primary,work_run,root=Path(primary),Path(work_run),Path(root)
    if primary.exists(): return False
    if restore_sibling_step(primary,key,work_run,root): return True
    directories=[primary.parent]
    siblings=work_run.parent.parent
    if siblings.name=='pipelines' and siblings.resolve().is_relative_to((root/'runs').resolve()):
        relative=primary.parent.relative_to(work_run)
        directories += [folder/work_run.name/relative for folder in sorted(siblings.iterdir()) if folder.is_dir()]
    for directory in dict.fromkeys(directories):
        for candidate in sorted(directory.glob(stage+'*.json')):
            if not candidate.resolve().is_relative_to((root/'runs').resolve()) or candidate.resolve()==primary.resolve():continue
            try:
                item=json.loads(candidate.read_text())
                if not isinstance(item.get('answer'),dict) or not isinstance(item.get('usage'),dict):continue
                if item.get('cache_key')!=key and not legacy_verifier(item):continue
            except (ValueError,KeyError,TypeError,OSError):continue
            converted={**item,'cache_key':key,'cache_protocol':REQUEST_PROTOCOL,
                'origin_cache_key':item.get('origin_cache_key',item.get('cache_key')),
                'origin_cache_file':str(candidate.resolve().relative_to(root.resolve()))}
            primary.parent.mkdir(parents=True,exist_ok=True)
            atomic_write_bytes(primary, (json.dumps(converted,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
            return True
    return False


def restore_sibling_step(primary, key, work_run, root):
    primary, work_run, root = Path(primary), Path(work_run), Path(root)
    if primary.exists(): return False
    siblings = work_run.parent.parent
    if siblings.name != 'pipelines' or not siblings.resolve().is_relative_to((root/'runs').resolve()):
        return False
    relative = primary.relative_to(work_run)
    for folder in sorted(siblings.iterdir()):
        candidate = folder/work_run.name/relative
        if not candidate.is_file() or candidate.resolve() == primary.resolve():continue
        try:
            value=json.loads(candidate.read_text())
            if value.get('cache_key')!=key or not isinstance(value.get('answer'),dict) or not isinstance(value.get('usage'),dict):continue
        except (ValueError,OSError):continue
        primary.parent.mkdir(parents=True,exist_ok=True)
        atomic_write_bytes(primary, candidate.read_bytes())
        return True
    return False
