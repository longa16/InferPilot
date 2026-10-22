"""InferPilot service: public UI shell, authenticated data API and Groq gateway."""
import asyncio
import hashlib
import secrets
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
import httpx
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from fastapi import FastAPI, APIRouter, Depends, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from .config import Settings
from .db import Database
from .schemas import ApplicationCreate, Completion, AnalysisRequest, BenchmarkRequest, window
from .gateway import Gateway
from .tools import Tools, timeseries
from .agent import analyze
from .benchmark import dataset, corpus_digest, execute, DATASET_PATH
from .telemetry import purge

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent/'static'


class PayloadLimit:
    def __init__(self, app, limit): self.app,self.limit = app,limit
    async def __call__(self, scope, receive, send):
        if scope['type']!='http' or scope['method'] not in ('POST','PUT','PATCH'):
            return await self.app(scope,receive,send)
        parts,size = [],0
        while True:
            message = await receive()
            if message['type']=='http.disconnect': return
            size += len(message.get('body',b''))
            if size>self.limit:
                return await JSONResponse({'detail':'Payload exceeds gateway limit'},413)(scope,receive,send)
            parts.append(message.get('body',b''))
            if not message.get('more_body'): break
        sent = False
        async def replay():
            nonlocal sent
            if sent: return await receive()
            sent = True
            return {'type':'http.request','body':b''.join(parts),'more_body':False}
        return await self.app(scope,replay,send)


def create_app(settings=None, db=None, transport=None):
    settings = settings or Settings()
    database = db or Database(settings.database_url)
    @asynccontextmanager
    async def lifespan(api):
        settings.validate()
        await database.open()
        # Single-instance MVP; abandoned work from an earlier process is explicit.
        await database.execute("UPDATE inference_events SET status='error',error_type='stale_unfinished',finished_at=now() WHERE status='running' AND started_at<now()-interval '15 minutes'")
        await database.execute("UPDATE benchmark_runs SET status='failed',finished_at=now(),notes='stale_interrupted_run' WHERE status IN ('running','cancel_requested') AND started_at<now()-interval '15 minutes'")
        client = httpx.AsyncClient(timeout=httpx.Timeout(60,connect=10),transport=transport,
                                   limits=httpx.Limits(max_connections=16,max_keepalive_connections=8))
        api.state.db,api.state.gateway = database,Gateway(database,settings,client)
        api.state.tasks = set()
        async def retention_loop():
            while True:
                try: await purge(database,settings.retention_days)
                except Exception: log.error('Retention cleanup unavailable; no data contents logged')
                await asyncio.sleep(3600)
        retention = asyncio.create_task(retention_loop())
        try: yield
        finally:
            retention.cancel()
            for task in list(api.state.tasks): task.cancel()
            await asyncio.gather(retention,*api.state.tasks,return_exceptions=True)
            await client.aclose()
            await database.close()
    api = FastAPI(title='InferPilot',version='1.0.0',lifespan=lifespan,
                  docs_url=None,redoc_url=None,openapi_url=None)
    api.add_middleware(PayloadLimit,limit=settings.max_payload_bytes)

    def admin(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization,'Bearer '+settings.admin_secret):
            raise HTTPException(401,'Admin authentication required',headers={'WWW-Authenticate':'Bearer'})

    async def require_app(app_id):
        row = await database.one('SELECT id,name,corpus_version,is_active,created_at FROM applications WHERE id=%s',(app_id,))
        if not row: raise HTTPException(404,'Application not found')
        return row

    async def gateway_app(authorization: str = Header(default='')):
        token = authorization.removeprefix('Bearer ')
        if token == authorization or not token:
            raise HTTPException(401,'Application gateway key required')
        try:
            row = await database.one('SELECT id,name,corpus_version FROM applications WHERE gateway_key_hash=%s AND is_active',
                                     (hashlib.sha256(token.encode()).hexdigest(),))
        except Exception:
            raise HTTPException(503,'Telemetry database unavailable')
        if not row: raise HTTPException(401,'Invalid application gateway key')
        return row

    @api.get('/')
    def home(): return FileResponse(STATIC/'index.html',headers={'Cache-Control':'no-cache'})

    @api.get('/health')
    def health(): return {'status':'alive','service':'InferPilot','groq_checked':False}

    @api.get('/ready')
    async def ready():
        try:
            await database.one('SELECT 1 AS ok')
            return {'status':'ready','database':'postgresql','provider_configured':bool(settings.groq_key),'provider_checked':False}
        except Exception:
            raise HTTPException(503,'PostgreSQL is unavailable')

    @api.post('/v1/chat/completions')
    async def completions(body: Completion, app=Depends(gateway_app), x_request_id: UUID | None=Header(default=None)):
        payload = body.model_dump()
        request_id = x_request_id or uuid4()
        headers = {'X-Request-ID':str(request_id),'Cache-Control':'no-store'}
        if not body.stream:
            status,data,_ = await api.state.gateway.complete(app,payload,request_id=request_id)
            return JSONResponse(data,status_code=status,headers=headers)
        status,data,response,attempt = await api.state.gateway.open_stream(app,payload,request_id)
        if status!=200:
            return JSONResponse(data,status_code=status,headers=headers)
        headers.update({'X-Accel-Buffering':'no','Cache-Control':'no-cache, no-transform'})
        return StreamingResponse(api.state.gateway.relay(response,attempt),media_type='text/event-stream',headers=headers)

    router = APIRouter(prefix='/api',dependencies=[Depends(admin)])

    @router.get('/schema')
    def schema(): return api.openapi()

    @router.get('/applications')
    async def applications():
        return await database.all('SELECT id,name,corpus_version,is_active,created_at FROM applications ORDER BY created_at')

    @router.post('/applications',status_code=201)
    async def create_application(body: ApplicationCreate):
        key = 'ip_'+secrets.token_urlsafe(32)
        app_id = uuid4()
        await database.execute('INSERT INTO applications(id,name,gateway_key_hash,corpus_version) VALUES(%s,%s,%s,%s)',
                               (app_id,body.name,hashlib.sha256(key.encode()).hexdigest(),body.corpus_version))
        return {'id':app_id,'name':body.name,'corpus_version':body.corpus_version,'gateway_key':key,
                'notice':'Shown once. Store securely; only its hash is persisted.'}

    @router.post('/applications/{app_id}/rotate-key')
    async def rotate_key(app_id: UUID):
        await require_app(app_id)
        key = 'ip_'+secrets.token_urlsafe(32)
        await database.execute('UPDATE applications SET gateway_key_hash=%s WHERE id=%s',
                               (hashlib.sha256(key.encode()).hexdigest(),app_id))
        return {'gateway_key':key,'notice':'Previous key revoked; shown once.'}

    @router.get('/overview')
    async def overview(app_id: UUID, start:datetime|None=Query(None,alias='from'), end:datetime|None=Query(None,alias='to')):
        await require_app(app_id)
        try: start,end = window(start,end)
        except ValueError as error: raise HTTPException(422,str(error))
        return await Tools(database,app_id,start,end).get_overview()

    @router.get('/events')
    async def events(app_id: UUID, limit:int=Query(25,ge=1,le=100), cursor:UUID|None=None,
                     model:str|None=Query(None,max_length=150), status:str|None=Query(None,pattern='^(running|success|error|cancelled)$'),
                     cache_hit:bool|None=None, start:datetime|None=Query(None,alias='from'),end:datetime|None=Query(None,alias='to')):
        await require_app(app_id)
        try: start,end = window(start,end)
        except ValueError as error: raise HTTPException(422,str(error))
        where = ['app_id=%s','started_at BETWEEN %s AND %s']
        params = [app_id,start,end]
        for column,value in (('model',model),('status',status),('cache_hit',cache_hit)):
            if value is not None:
                where.append(column+'=%s'); params.append(value)
        if cursor:
            where.append('(started_at,id)<(SELECT started_at,id FROM inference_events WHERE id=%s AND app_id=%s)')
            params.extend([cursor,app_id])
        rows = await database.all('SELECT * FROM inference_events WHERE '+' AND '.join(where)+' ORDER BY started_at DESC,id DESC LIMIT %s',(*params,limit+1))
        return {'items':rows[:limit],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None,
                'scope':'gateway; no prompts or raw responses recorded'}

    @router.get('/timeseries')
    async def series(app_id:UUID, metric:str='requests',interval:str='hour',
                     start:datetime|None=Query(None,alias='from'),end:datetime|None=Query(None,alias='to')):
        await require_app(app_id)
        try:
            start,end = window(start,end)
            return await timeseries(database,app_id,start,end,metric,interval)
        except ValueError as error: raise HTTPException(422,str(error))

    @router.post('/agent/analyze')
    async def agent_analyze(body:AnalysisRequest):
        app = await require_app(body.app_id)
        # No background optimization: tool loop can only produce a proposal.
        return await analyze(database,settings,api.state.gateway.client,app,body.hours)

    @router.get('/agent/runs')
    async def agent_runs(app_id:UUID):
        return await database.all('SELECT * FROM agent_runs WHERE app_id=%s ORDER BY started_at DESC LIMIT 20',(app_id,))

    @router.get('/agent/runs/{run_id}')
    async def agent_run(run_id:UUID):
        row = await database.one('SELECT * FROM agent_runs WHERE id=%s',(run_id,))
        if not row: raise HTTPException(404,'Agent run not found')
        return row

    @router.get('/dataset')
    def get_dataset(): return {**dataset(),'corpus_digest':corpus_digest()}

    @router.post('/benchmarks',status_code=202)
    async def benchmark_create(body:BenchmarkRequest):
        app = await require_app(body.app_id)
        if not settings.groq_key: raise HTTPException(503,'Configure GROQ_API_KEY before running a benchmark')
        if body.max_cost_usd is not None and settings.model not in settings.prices:
            raise HTTPException(422,'A verified configured model price is required to enforce a dollar budget')
        if body.agent_run_id:
            agent = await database.one('SELECT * FROM agent_runs WHERE id=%s AND app_id=%s',(body.agent_run_id,body.app_id))
            if not agent or not agent['recommendation_json']:
                raise HTTPException(422,'Selected agent run has no experiment proposal')
        run_id = uuid4()
        config = {'approval':True,'scope':'isolated_benchmark_only','agent_run_id':body.agent_run_id,
            'corpus_version':app['corpus_version'],'corpus_sha256':corpus_digest(),
            'dataset_sha256':hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest(),
            'model':settings.model,'order':'fixed baseline -> warmup unique static inputs -> optimized',
            'max_calls':body.max_calls,'max_cost_usd':body.max_cost_usd,
            'pricing':settings.prices.get(settings.model),'cost_budget_available':settings.model in settings.prices,
            'cache_ttl_seconds':settings.cache_ttl_seconds,'corpus_scope':'frozen public context; generation-only',
            'quality':'lexical facts/abstention guard, not a semantic guarantee'}
        try:
            await database.execute('INSERT INTO benchmark_runs(id,app_id,strategy,dataset_version,configuration_json,status) VALUES(%s,%s,%s,%s,%s,%s)',
                (run_id,app['id'],'exact_cache_comparison',dataset()['version'],Jsonb(jsonable_encoder(config)),'running'))
        except UniqueViolation:
            raise HTTPException(409,'A benchmark is already running; stop or wait for it')
        task = asyncio.create_task(execute(database,api.state.gateway,settings,app,run_id,body))
        api.state.tasks.add(task)
        task.add_done_callback(api.state.tasks.discard)
        return {'id':run_id,'status':'running','configuration':config}

    @router.get('/benchmarks')
    async def benchmarks(app_id:UUID):
        return await database.all('SELECT * FROM benchmark_runs WHERE app_id=%s ORDER BY started_at DESC LIMIT 20',(app_id,))

    @router.get('/benchmarks/{run_id}')
    async def benchmark_get(run_id:UUID):
        row = await database.one('SELECT * FROM benchmark_runs WHERE id=%s',(run_id,))
        if not row: raise HTTPException(404,'Benchmark not found')
        row['cases'] = await database.all('SELECT * FROM benchmark_cases WHERE run_id=%s ORDER BY phase,case_id',(run_id,))
        return row

    @router.post('/benchmarks/{run_id}/stop')
    async def benchmark_stop(run_id:UUID):
        count = await database.execute("UPDATE benchmark_runs SET status='cancel_requested' WHERE id=%s AND status='running'",(run_id,))
        return {'stop_requested':bool(count),'notice':'An in-flight provider call may finish before cancellation.'}

    @router.post('/maintenance/purge')
    async def purge_data(): return await purge(database,settings.retention_days)

    @router.delete('/applications/{app_id}')
    async def delete_application(app_id:UUID):
        await database.execute('DELETE FROM applications WHERE id=%s',(app_id,))
        return {'deleted':True}

    api.include_router(router)
    return api


app = create_app()
