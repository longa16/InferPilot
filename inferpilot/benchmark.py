"""Approved, isolated experiments. No live traffic configuration is modified."""
import asyncio
import hashlib
import json
import statistics
import re
import unicodedata
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from .telemetry import price_for

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT/'datasets'/'novamaison-v1.json'


def dataset():
    return json.loads(DATASET_PATH.read_text())


def corpus_digest():
    h = hashlib.sha256()
    for file in sorted((ROOT/'documents').glob('*.md')):
        h.update(file.name.encode()); h.update(file.read_bytes())
    return h.hexdigest()


def payload_for(case, model):
    allowed = {p.name:p for p in (ROOT/'documents').glob('*.md')}
    context = '\n\n'.join(allowed[name].read_text() for name in case['documents'])
    return {'model':model,'temperature':0,'max_completion_tokens':256,'stream':False,
        'messages':[{'role':'system','content':"Tu es NovaMaison, boutique fictive. Réponds en français uniquement avec les faits du contexte public. Si une information ou un produit n'y figure pas, reconnais l'absence d'information et oriente vers le support. N'invente pas de stock, de commande, de code promo ni de produit. Réponds en deux à quatre phrases."},
                    {'role':'user','content':'Contexte public :\n'+context+'\n\nQuestion : '+case['question']}]}


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',text.casefold()) if not unicodedata.combining(c))


def quality(text, expected):
    normalized = normalize(text)
    def matches(alternative):
        value=normalize(alternative)
        if value.isdigit() or len(value)<=2:
            return re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',normalized) is not None
        return value in normalized
    return all(any(matches(alternative) for alternative in group) for group in expected)


def percent_change(before, after):
    if before is None or after is None or before<=0:
        return None
    return 100*(before-after)/before


def summarize(rows):
    durations = [r['duration_ms'] for r in rows if r['duration_ms'] is not None]
    costs = [r['cost_usd'] for r in rows if r['cost_usd'] is not None]
    values = sorted(durations)
    p95 = None
    if len(values)>=20:
        index = .95*(len(values)-1)
        lo = int(index)
        p95 = values[lo] + (values[min(lo+1,len(values)-1)]-values[lo])*(index-lo)
    return {'cases':len(rows),'quality_passes':sum(r['quality_pass'] is True for r in rows),
        'errors':sum(r['quality_pass'] is None for r in rows),
        'cache_hits':sum(r['cache_hit'] for r in rows),
        'p50_duration_ms':statistics.median(values) if values else None,'p95_duration_ms':p95,
        'estimated_cost_usd':sum(float(c) for c in costs) if len(costs)==len(rows) and rows else None,
        'cost_known_cases':len(costs)}


async def report(db, run_id):
    rows = await db.all('SELECT * FROM benchmark_cases WHERE run_id=%s ORDER BY phase,case_id',(run_id,))
    phases = {phase:summarize([r for r in rows if r['phase']==phase]) for phase in ('baseline','warmup','optimized')}
    base,opt,warm = phases['baseline'],phases['optimized'],phases['warmup']
    total = opt['estimated_cost_usd']+warm['estimated_cost_usd'] if opt['estimated_cost_usd'] is not None and warm['estimated_cost_usd'] is not None else None
    regression = [r['case_id'] for r in rows if r['phase']=='optimized' and r['quality_pass'] is not True and
                  any(b['phase']=='baseline' and b['case_id']==r['case_id'] and b['quality_pass'] is True for b in rows)]
    expected_count = len(dataset()['cases'])
    complete = base['cases']==expected_count and opt['cases']==expected_count
    return {'phases':phases,'optimized_including_warmup_cost_usd':total,
        'steady_state_savings_pct':percent_change(base['estimated_cost_usd'],opt['estimated_cost_usd']) if complete else None,
        'including_warmup_savings_pct':percent_change(base['estimated_cost_usd'],total) if complete else None,
        'p50_gateway_latency_improvement_pct':percent_change(base['p50_duration_ms'],opt['p50_duration_ms']) if complete else None,
        'quality_regressions':regression,'comparison_complete':complete,
        'quality_gate_pass':complete and not regression and opt['errors']==0 and opt['quality_passes']==expected_count,
        'limitations':['Generation-only with fixed public corpus context; excludes retrieval/reranking/browser time.',
            'String fact/abstention checks on a small dataset are not a general quality guarantee.',
            'Fixed sequential baseline, then warm-up, then optimized: time/order effects are possible.',
            'Warm-up cost and request count are included separately; first warm-up request is cold.',
            'Known configured prices only; missing costs remain null, never zero. No conclusion from partial comparisons.']}


async def execute(db, gateway, settings, app, run_id, request):
    count,total_cost = 0,0.0
    namespace = 'benchmark:'+str(run_id)
    data = dataset()
    try:
        cases = data['cases']
        unique = {}
        for c in cases:
            if c['cacheable']:
                payload = payload_for(c,settings.model)
                unique.setdefault(json.dumps(payload,sort_keys=True),c)
        phases = [('baseline',cases,False),('warmup',list(unique.values()),True),('optimized',cases,True)]
        for phase,selected,cache in phases:
            for case in selected:
                state = await db.one('SELECT status FROM benchmark_runs WHERE id=%s',(run_id,))
                if state['status']=='cancel_requested':
                    raise asyncio.CancelledError()
                if count>=min(settings.benchmark_max_calls,request.max_calls):
                    raise RuntimeError('request_budget_exceeded')
                payload = payload_for(case,settings.model)
                # Conservative UTF-8 byte bound plus message overhead, not a claimed
                # token count. Dollar enforcement requires an explicit price table.
                upper_input = sum(len(m['content'].encode())+100 for m in payload['messages'])
                upper_cost,_,_ = price_for(settings,settings.model,upper_input,payload['max_completion_tokens'])
                if request.max_cost_usd is not None and (upper_cost is None or total_cost+float(upper_cost)>request.max_cost_usd):
                    raise RuntimeError('cost_budget_exceeded_or_unpriced')
                count += 1
                quality_pass,digest,event_id = None,None,None
                status,body,event_id = await gateway.complete(app,payload,run_id=run_id,
                    cache_scope=namespace if cache and case['cacheable'] else None)
                if status==200:
                    text = body['choices'][0]['message']['content']
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    quality_pass = quality(text,case['expected'])
                event = await db.one('SELECT duration_ms,estimated_cost_usd,cache_hit FROM inference_events WHERE id=%s',(event_id,))
                if event['estimated_cost_usd'] is not None:
                    total_cost += float(event['estimated_cost_usd'])
                await db.execute('''INSERT INTO benchmark_cases(id,run_id,event_id,case_id,scenario,phase,
                    expected_answer_facts,actual_response_digest,quality_pass,duration_ms,cost_usd,cache_hit)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (uuid4(),run_id,event_id,case['id'],case['scenario'],phase,Jsonb(case['expected']),digest,
                     quality_pass,event['duration_ms'],event['estimated_cost_usd'],event['cache_hit']))
                if status!=200:
                    raise RuntimeError('provider_error_'+str(status))
        result = await report(db,run_id)
        await db.execute("UPDATE benchmark_runs SET status='completed',finished_at=now(),report_json=%s WHERE id=%s",
                         (Jsonb(jsonable_encoder(result)),run_id))
    except asyncio.CancelledError:
        await db.execute("UPDATE benchmark_runs SET status='cancelled',finished_at=now(),notes='Stopped by user or service shutdown' WHERE id=%s",(run_id,))
    except Exception as error:
        # Only allowlisted error labels, not provider bodies or secrets, are persisted.
        label = str(error) if str(error).startswith(('request_budget','cost_budget','provider_error')) else 'benchmark_failed'
        result = await report(db,run_id)
        await db.execute("UPDATE benchmark_runs SET status='failed',finished_at=now(),notes=%s,report_json=%s WHERE id=%s",
                         (label,Jsonb(jsonable_encoder(result)),run_id))
