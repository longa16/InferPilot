import hashlib
import hmac
import json
import math
from decimal import Decimal
from datetime import datetime, timezone


def fingerprint(secret, app, payload, namespace='observed'):
    canonical = json.dumps({'app':str(app['id']), 'corpus':app['corpus_version'],
                            'rules':'exact-public-faq-v1', 'namespace':namespace,
                            'payload':{k:v for k,v in payload.items() if k != 'stream'}},
                           sort_keys=True, separators=(',',':'), ensure_ascii=False)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def usage(event):
    value = event.get('usage') or (event.get('x_groq') or {}).get('usage') or {}
    a, b = value.get('prompt_tokens'), value.get('completion_tokens')
    if all(isinstance(x,int) and not isinstance(x,bool) and x >= 0 for x in (a,b)):
        return a,b,'provider'
    return None,None,'unavailable'


def timings(event):
    value = event.get('usage') or (event.get('x_groq') or {}).get('usage') or {}
    result = {}
    for source,target in [('prompt_time','provider_prompt_ms'),('queue_time','provider_queue_ms'),('completion_time','provider_completion_ms')]:
        number = value.get(source)
        if isinstance(number,(float,int)) and not isinstance(number,bool) and math.isfinite(number) and number>=0:
            result[target] = number*1000
    output = usage(event)[1]
    duration = result.get('provider_completion_ms')
    if output is not None and duration and duration>0:
        result['provider_tps'] = output/(duration/1000)
    return result


def price_for(settings, model, a, b):
    price = settings.prices.get(model)
    if not price or a is None or b is None:
        return None,'unavailable',None
    result = (Decimal(a)*Decimal(str(price['input_per_million'])) + Decimal(b)*Decimal(str(price['output_per_million']))) / Decimal(1_000_000)
    return result,'configured_price_estimate',price['version']


async def purge(db, days):
    # No prompts are stored. Public benchmark cache responses expire separately.
    events = await db.execute("DELETE FROM inference_events WHERE started_at < now() - %s * interval '1 day' AND status <> 'running'", (days,))
    await db.execute("DELETE FROM cache_entries WHERE expires_at < now()")
    await db.execute("DELETE FROM agent_runs WHERE started_at < now() - %s * interval '1 day'", (days,))
    await db.execute("DELETE FROM benchmark_runs WHERE started_at < now() - %s * interval '1 day' AND status NOT IN ('running','cancel_requested')", (days,))
    return {'deleted_events': events, 'retention_days':days}
