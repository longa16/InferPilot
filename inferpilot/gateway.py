"""Groq-only gateway. Metrics are persisted before success is acknowledged."""
import asyncio
import codecs
import json
import copy
import logging
import time
from uuid import uuid4
from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
import httpx
from fastapi import HTTPException
from .telemetry import fingerprint, usage, price_for, timings

log = logging.getLogger(__name__)
URL = 'https://api.groq.com/openai/v1/chat/completions'


class Observer:
    def __init__(self, started):
        self.started = started
        self.ttft = None
        self.tokens = (None,None,'unavailable')
        self.finish = None
        self.done = False
        self.error = False
        self.timings = {}

    def inspect(self, frame):
        data = '\n'.join(line[5:].lstrip(' ') for line in frame.splitlines() if line.startswith('data:'))
        if not data:
            return
        if data == '[DONE]':
            self.done = True
            return
        value = json.loads(data)
        if value.get('error'):
            self.error = True
        self.timings.update(timings(value))
        if usage(value)[2] == 'provider':
            self.tokens = usage(value)
        choices = value.get('choices', [])
        if choices:
            choice = choices[0]
            content = (choice.get('delta') or {}).get('content')
            if content and self.ttft is None:
                self.ttft = (time.perf_counter()-self.started)*1000
            if choice.get('finish_reason'):
                self.finish = choice['finish_reason']


class Gateway:
    def __init__(self, db, settings, client):
        self.db, self.settings, self.client = db,settings,client
        self.slots = asyncio.Semaphore(settings.max_concurrency)

    async def begin(self, app, payload, request_id=None, run_id=None):
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=.05)
        except asyncio.TimeoutError:
            raise HTTPException(429,'Gateway concurrency limit reached',headers={'Retry-After':'1'})
        started = time.perf_counter()
        request_id = request_id or uuid4()
        attempt = {'id':uuid4(), 'request_id':request_id, 'started':started, 'app':app,
                   'model':payload['model'], 'stream':bool(payload.get('stream')), 'done':False}
        attempt['fingerprint'] = fingerprint(self.settings.hmac_secret, app, payload)
        try:
            await self.db.execute('''INSERT INTO inference_events(id,app_id,request_id,model,status,stream,request_fingerprint,benchmark_run_id)
                VALUES(%s,%s,%s,%s,'running',%s,%s,%s)''',
                (attempt['id'],app['id'],request_id,payload['model'],attempt['stream'],attempt['fingerprint'],run_id))
        except UniqueViolation:
            self.slots.release()
            raise HTTPException(409,'request_id already recorded; upstream not called again')
        except Exception:
            self.slots.release()
            raise HTTPException(503,'Telemetry database unavailable; upstream was not called')
        return attempt

    async def finish(self, attempt, status, http_status, *, tokens=(None,None,'unavailable'), ttft=None,
                     error=None, cache_hit=False, avoided=(None,None), provider_timings=None):
        a,b,source = tokens
        provider_timings = {} if cache_hit else (provider_timings or {})
        cost,cost_source,version = price_for(self.settings, attempt['model'], a,b)
        if cache_hit:
            a,b,source,cost,cost_source = 0,0,'cache_no_upstream',0,'cache_no_upstream'
        await self.db.execute('''UPDATE inference_events SET finished_at=now(),status=%s,http_status=%s,
            duration_ms=%s,ttft_ms=%s,input_tokens=%s,output_tokens=%s,token_source=%s,
            estimated_cost_usd=%s,cost_source=%s,pricing_version=%s,error_type=%s,cache_hit=%s,
            avoided_input_tokens=%s,avoided_output_tokens=%s,provider_prompt_ms=%s,provider_queue_ms=%s,
            provider_completion_ms=%s,provider_tps=%s WHERE id=%s AND status='running' ''',
            (status,http_status,(time.perf_counter()-attempt['started'])*1000,ttft,a,b,source,cost,
             cost_source,version,error,cache_hit,*avoided,provider_timings.get('provider_prompt_ms'),
             provider_timings.get('provider_queue_ms'),provider_timings.get('provider_completion_ms'),
             provider_timings.get('provider_tps'),attempt['id']))
        attempt['done'] = True

    async def interrupted(self, attempt, error, status='error', **measures):
        if attempt['done']:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.finish(attempt,status,None,error=error,**measures)),3)
        except Exception:
            # No request contents, authorization headers or connection strings in logs.
            log.error('Telemetry finalization failed request_id=%s; event remains running',attempt['request_id'])

    def headers(self):
        if not self.settings.groq_key:
            raise HTTPException(503,'GROQ_API_KEY is not configured on InferPilot')
        return {'Authorization':'Bearer '+self.settings.groq_key,'Accept-Encoding':'identity'}

    async def complete(self, app, payload, *, request_id=None, run_id=None, cache_scope=None):
        attempt = await self.begin(app,payload,request_id,run_id)
        try:
            # Only the approved benchmark service supplies cache_scope. Public gateway
            # callers cannot opt in by forging a header or placing a flag in a prompt.
            cache_key = fingerprint(self.settings.hmac_secret,app,payload,cache_scope) if cache_scope else None
            if cache_key:
                row = await self.db.one('SELECT response_json FROM cache_entries WHERE app_id=%s AND key_hash=%s AND expires_at>now() AND corpus_version=%s',
                                        (app['id'],cache_key,app['corpus_version']))
                if row:
                    data = copy.deepcopy(row['response_json'])
                    a,b,_ = usage(data)
                    await self.finish(attempt,'success',200,cache_hit=True,avoided=(a,b))
                    for container in (data, data.get('x_groq') or {}):
                        if 'usage' in container:
                            container['usage'] = {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
                    data['x_inferpilot'] = {'cache_hit':True,'upstream_tokens':0,
                        'avoided_input_tokens_estimate':a,'avoided_output_tokens_estimate':b}
                    return 200,data,attempt['id']
            response = await self.client.post(URL,headers=self.headers(),json=payload)
            try:
                data = response.json()
            except ValueError:
                data = {'error':{'message':'Invalid JSON from provider'}}
            tokens = usage(data)
            choice = (data.get('choices') or [{}])[0]
            success = response.is_success and choice.get('finish_reason') == 'stop' and isinstance((choice.get('message') or {}).get('content'),str) and bool(choice['message']['content'].strip())
            status = 'success' if success else 'error'
            code = response.status_code if not response.is_success or success else 502
            if success and cache_key:
                # Cache contains only trusted, public fixture responses; never raw prompts.
                await self.db.execute('''INSERT INTO cache_entries(id,app_id,key_hash,response_json,expires_at,corpus_version)
                   VALUES(%s,%s,%s,%s,now()+%s*interval '1 second',%s)
                   ON CONFLICT(key_hash) DO UPDATE SET response_json=excluded.response_json,
                     created_at=now(),expires_at=excluded.expires_at''',
                   (uuid4(),app['id'],cache_key,Jsonb(data),self.settings.cache_ttl_seconds,app['corpus_version']))
            await self.finish(attempt,status,code,tokens=tokens,provider_timings=timings(data),error=None if success else ('upstream_http_'+str(code) if not response.is_success else 'incomplete_response'))
            if code == 502:
                data = {'error':{'message':'Provider response was incomplete'}}
            return code,data,attempt['id']
        except httpx.TimeoutException:
            await self.interrupted(attempt,'timeout')
            raise HTTPException(504,'Groq request timed out')
        except asyncio.CancelledError:
            await self.interrupted(attempt,'client_disconnect','cancelled')
            raise
        except HTTPException:
            await self.interrupted(attempt,'configuration_error')
            raise
        except httpx.RequestError:
            await self.interrupted(attempt,'provider_connection')
            raise HTTPException(502,'Groq connection failed')
        except Exception:
            await self.interrupted(attempt,'persistence_or_gateway_error')
            raise HTTPException(503,'Gateway could not persist or process this request')
        finally:
            self.slots.release()

    async def open_stream(self, app, payload, request_id=None):
        attempt = await self.begin(app,payload,request_id)
        response = None
        try:
            request = self.client.build_request('POST',URL,headers=self.headers(),json=payload)
            response = await self.client.send(request,stream=True)
            if not response.is_success:
                await response.aread()
                try: data = response.json()
                except ValueError: data = {'error':{'message':'Provider rejected request'}}
                await response.aclose()
                await self.finish(attempt,'error',response.status_code,error='upstream_http_'+str(response.status_code))
                self.slots.release()
                return response.status_code,data,None,attempt
            return 200,None,response,attempt
        except BaseException as error:
            if response is not None:
                await response.aclose()
            await self.interrupted(attempt,'timeout' if isinstance(error,httpx.TimeoutException) else 'stream_open_error',
                                   'cancelled' if isinstance(error,asyncio.CancelledError) else 'error')
            self.slots.release()
            if isinstance(error,asyncio.CancelledError): raise
            if isinstance(error,HTTPException): raise
            raise HTTPException(504 if isinstance(error,httpx.TimeoutException) else 502,'Cannot open Groq stream')

    async def relay(self, response, attempt):
        observer = Observer(attempt['started'])
        decoder = codecs.getincrementaldecoder('utf-8')()
        buffer = ''
        try:
            async for chunk in response.aiter_bytes():
                buffer += decoder.decode(chunk)
                if len(buffer)>1_000_000:
                    raise ValueError('SSE frame too large')
                # Buffer a single SSE frame, never the complete completion. Groq uses
                # LF or CRLF; forwarding the frame keeps all data/metadata fields.
                while True:
                    lf, crlf = buffer.find('\n\n'), buffer.find('\r\n\r\n')
                    candidates = [(i,n) for i,n in ((lf,2),(crlf,4)) if i>=0]
                    if not candidates: break
                    index,sep = min(candidates)
                    frame,buffer = buffer[:index+sep],buffer[index+sep:]
                    observer.inspect(frame)
                    if observer.done:
                        success = observer.finish == 'stop' and observer.ttft is not None and not observer.error
                        await self.finish(attempt,'success' if success else 'error',200,tokens=observer.tokens,
                                          ttft=observer.ttft,provider_timings=observer.timings,error=None if success else 'incomplete_stream')
                        if not success:
                            yield self.error_frame('Provider stream was incomplete')
                        else:
                            yield frame.encode()
                        return
                    yield frame.encode()
                    if len(buffer)>1_000_000:
                        raise ValueError('SSE frame too large')
            await self.finish(attempt,'error',200,tokens=observer.tokens,ttft=observer.ttft,error='missing_done')
            yield self.error_frame('Provider stream ended without completion')
        except (asyncio.CancelledError, GeneratorExit):
            await self.interrupted(attempt,'client_disconnect','cancelled',tokens=observer.tokens,
                                   ttft=observer.ttft,provider_timings=observer.timings)
            raise
        except Exception as error:
            await self.interrupted(attempt,'timeout' if isinstance(error,httpx.TimeoutException) else 'stream_or_persistence_error',
                                   tokens=observer.tokens,ttft=observer.ttft,provider_timings=observer.timings)
            yield self.error_frame('Gateway stream failed; request is not confirmed recorded')
        finally:
            await response.aclose()
            self.slots.release()

    @staticmethod
    def error_frame(message):
        return ('data: '+json.dumps({'error':{'message':message}})+'\n\n').encode()
