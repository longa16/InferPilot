"""Bounded, parameterized read tools. No caller or LLM supplied SQL is executed."""
from datetime import datetime, timezone, timedelta


class Tools:
    def __init__(self, db, app_id, start, end):
        self.db,self.app_id,self.start,self.end = db,app_id,start,end

    async def get_overview(self):
        row = await self.db.one('''SELECT count(*)::int AS requests,
          count(*) FILTER(WHERE status='success')::int AS successes,
          count(*) FILTER(WHERE status IN ('error','cancelled'))::int AS errors,
          count(*) FILTER(WHERE status='running')::int AS in_flight,
          count(*) FILTER(WHERE cache_hit)::int AS cache_hits,
          avg(duration_ms) FILTER(WHERE status='success') AS mean_latency_ms,
          CASE WHEN count(duration_ms) FILTER(WHERE status='success')>=20
            THEN percentile_cont(.95) WITHIN GROUP(ORDER BY duration_ms) FILTER(WHERE status='success') END AS p95_latency_ms,
          avg(ttft_ms) FILTER(WHERE status='success') AS mean_ttft_ms,
          avg(provider_tps) FILTER(WHERE status='success' AND NOT cache_hit) AS mean_provider_tps,
          sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
          sum(estimated_cost_usd) AS known_estimated_cost_usd,
          count(estimated_cost_usd)::int AS cost_known_requests,
          count(input_tokens)::int AS tokens_known_requests
          FROM inference_events WHERE app_id=%s AND started_at BETWEEN %s AND %s''',
          (self.app_id,self.start,self.end))
        row['error_rate'] = row['errors']/row['requests'] if row['requests'] else None
        row['cache_hit_rate'] = row['cache_hits']/row['requests'] if row['requests'] else None
        row['window'] = {'from':self.start,'to':self.end}
        row['source'] = 'PostgreSQL inference_events; gateway scope, not internal RAG timings'
        return row

    async def get_repeated_patterns(self):
        return await self.db.all('''SELECT request_fingerprint,model,count(*)::int AS occurrences,
          min(started_at) AS first_seen,max(started_at) AS last_seen,
          sum(estimated_cost_usd) AS known_estimated_cost_usd,
          count(estimated_cost_usd)::int AS priced_observations,
          (array_agg(request_id ORDER BY started_at DESC))[1:5] AS example_request_ids
          FROM inference_events WHERE app_id=%s AND started_at BETWEEN %s AND %s
            AND status='success' AND NOT cache_hit
          GROUP BY request_fingerprint,model HAVING count(*)>1
          ORDER BY count(*) DESC LIMIT 10''',(self.app_id,self.start,self.end))

    async def get_latency_outliers(self):
        return await self.db.all('''SELECT request_id,model,duration_ms,ttft_ms,status,cache_hit
          FROM inference_events WHERE app_id=%s AND started_at BETWEEN %s AND %s AND duration_ms IS NOT NULL
          ORDER BY duration_ms DESC LIMIT 10''',(self.app_id,self.start,self.end))

    async def get_previous_benchmarks(self):
        return await self.db.all('''SELECT id,dataset_version,status,started_at,finished_at,report_json
          FROM benchmark_runs WHERE app_id=%s ORDER BY started_at DESC LIMIT 5''',(self.app_id,))


async def timeseries(db, app_id, start, end, metric='requests', interval='hour'):
    expressions = {
        'requests':'count(*)',
        'errors':"count(*) FILTER(WHERE status IN ('error','cancelled'))",
        'latency':'avg(duration_ms) FILTER(WHERE status=\'success\')',
        'cost':'sum(estimated_cost_usd)',
        'ttft':'avg(ttft_ms) FILTER(WHERE status=\'success\')',
    }
    if metric not in expressions or interval not in ('hour','day'):
        raise ValueError('Unsupported metric or interval')
    # Only server-defined SQL expressions are interpolated. All values are bound.
    sql = f'''SELECT date_trunc(%s,started_at) AS bucket,{expressions[metric]} AS value,
      count(*)::int AS observations FROM inference_events WHERE app_id=%s AND started_at BETWEEN %s AND %s
      GROUP BY bucket ORDER BY bucket LIMIT 2161'''
    return await db.all(sql,(interval,app_id,start,end))
