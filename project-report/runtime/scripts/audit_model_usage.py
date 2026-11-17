#!/usr/bin/env python3
"""Account for actual invocation events, not copied response-cache files.

USD rates below are a dated pricing snapshot, not an account invoice or FX quote.
Failed requests without usage cannot be priced and are explicitly counted.
"""
import argparse
import collections
import json
from pathlib import Path

RATES={'deepseek-flash':(.006,.3,1.2),'deepseek-v4-pro':(.044,1.32,3.96)}
CNY_RATES={'deepseek-flash':(.04,2,8),'deepseek-v4-pro':(.3,9,27)}


def estimate_cny(calls):
    peak=0
    for call in calls:
        if call.get('cache_reused'): continue
        rates=CNY_RATES.get(call.get('model'))
        if rates is None: return None
        hit=call.get('prompt_cache_hit_tokens',call.get('prompt_tokens_details',{}).get('cached_tokens',0))
        miss=call.get('prompt_cache_miss_tokens',call.get('prompt_tokens',0)-hit)
        peak+=(hit*rates[0]+miss*rates[1]+call.get('completion_tokens',0)*rates[2])/1e6
    return {'currency':'CNY','range':[round(peak*.5,6),round(peak,6)],'pricingSnapshot':'2026-11-03',
        'source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'basis':'Successful new calls only; all-off-peak to all-peak estimate, not account invoice'}


def summarize(folders):
    logs={p.resolve() for folder in folders for p in folder.rglob('timing-events.jsonl')}
    jobs=[]; models=collections.defaultdict(collections.Counter)
    for log in sorted(logs):
        events=[json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        ends=[e for e in events if e['event']=='request_end']; totals=collections.Counter()
        for event in ends:
            u=event['usage']; prompt=u.get('prompt_tokens',0)
            hit=u.get('prompt_cache_hit_tokens',u.get('prompt_tokens_details',{}).get('cached_tokens',0))
            value=collections.Counter(calls=1,prompt=prompt,output=u.get('completion_tokens',0),hit=hit,
                miss=u.get('prompt_cache_miss_tokens',prompt-hit),seconds=u.get('wall_seconds',0))
            totals+=value; models[event.get('model','unknown')]+=value
        starts=sum(e['event']=='request_start' for e in events)
        retries=sum(e['event']=='request_retry' for e in events)
        job_file=log.parent/'job.json'; job=json.loads(job_file.read_text()) if job_file.exists() else {}
        jobs.append({'job':log.parent.name,'path':str(log.parent),'status':job.get('status'),
            **dict(totals),'startedRequests':starts,'retryEvents':retries,'unpricedIncompleteRequests':max(0,starts-len(ends)),
            'cachedSteps':sum(e['event']=='cache_reused' for e in events)})
    result={model:dict(stats) for model,stats in models.items()}
    for model,stats in result.items():
        if model in RATES:
            hit,miss,out=RATES[model]; peak=(stats['hit']*hit+stats['miss']*miss+stats['output']*out)/1e6
            stats.update(estimatedUSDRange=[round(peak*.5,6),round(peak,6)])
            ch,cm,co=CNY_RATES[model];cny=(stats['hit']*ch+stats['miss']*cm+stats['output']*co)/1e6
            stats.update(estimatedCNYRange=[round(cny*.5,6),round(cny,6)])
    return {'pricingSnapshot':'2026-11-03','pricingSource':'https://api-docs.deepseek.com/quick_start/pricing/',
        'note':'Actual successful request_end events only. Incomplete/retried requests and standalone probes may add cost. USD range uses all-off-peak to all-peak; no RMB reconciliation.',
        'cnyPricingSource':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'models':result,'jobs':jobs}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folders',nargs='+',type=Path)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    data=summarize(args.folders);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'models':data['models'],'jobs':len(data['jobs'])}))
