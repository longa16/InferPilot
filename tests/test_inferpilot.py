"""Real PostgreSQL integration; Groq and Google Gemini are simulated only at their HTTP transports.
Never supplies synthetic monitoring data to a runtime endpoint.
"""
import asyncio
import hashlib
import json
import os
import time
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote
import httpx
import pytest
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
from inferpilot.api import create_app
from inferpilot.config import Settings
from inferpilot.gateway import Observer
from inferpilot.benchmark import dataset, quality, percent_change, summarize
from inferpilot.telemetry import usage, price_for, fingerprint

DB_URL = os.getenv('TEST_DATABASE_URL')


def test_usage_and_no_invented_cost():
    assert usage({'x_groq':{'usage':{'prompt_tokens':10,'completion_tokens':3}}}) == (10,3,'provider')
    assert usage({'usage':{'prompt_tokens':True,'completion_tokens':3}})[2] == 'unavailable'
    assert usage({}) == (None,None,'unavailable')
    assert price_for(Settings(), 'unknown',10,3) == (None,'unavailable',None)
    assert percent_change(None,1) is None and percent_change(0,1) is None
    assert percent_change(10,2) == 80
    assert summarize([{'duration_ms':1,'cost_usd':None,'quality_pass':True,'cache_hit':False}])['estimated_cost_usd'] is None


def test_fingerprints_are_scoped():
    payload={'model':'a','messages':[{'role':'user','content':'public FAQ'}],'temperature':0}
    app={'id':uuid4(),'corpus_version':'1'}
    original = fingerprint('test-secret',app,payload)
    assert original != fingerprint('test-secret',{**app,'id':uuid4()},payload)
    assert original != fingerprint('test-secret',{**app,'corpus_version':'2'},payload)
    assert original != fingerprint('test-secret',app,{**payload,'model':'b'})
    assert original != fingerprint('test-secret',app,{**payload,'temperature':.5})


def test_observer_ignores_metadata_for_ttft():
    observer=Observer(time.perf_counter())
    observer.inspect('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
    assert observer.ttft is None
    observer.inspect('data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n')
    assert observer.ttft >= 0
    observer.inspect('data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n')
    observer.inspect('data: [DONE]\n\n')
    assert observer.done and observer.finish=='stop' and observer.tokens==(2,1,'provider')


def test_dataset_and_quality_limits():
    cases=dataset()['cases']
    assert 20<=len(cases)<=30 and len({c['id'] for c in cases})==len(cases)
    assert any(c['scenario']=='exact_repeat' for c in cases)
    assert all(not c['cacheable'] for c in cases if c['scenario'] in ('personal_not_cacheable','dynamic_not_cacheable','out_of_corpus'))
    assert quality('La garantie est de deux ans.',[['deux','2']])
    assert not quality('Inconnu', [['49,90','49.90']])


@pytest.fixture
def service():
    if not DB_URL: pytest.skip('TEST_DATABASE_URL required for PostgreSQL integration')
    schema='test_'+uuid4().hex
    with psycopg.connect(DB_URL,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts=urlsplit(DB_URL)
    url=urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query),('options','-c search_path='+schema)],quote_via=quote)))
    settings=Settings(database_url=url,admin_secret='test-admin-'+'a'*32,hmac_secret='test-hmac-'+'b'*32,
        groq_key='not-a-real-provider-key',gemini_key='not-a-real-google-key',model='test-model',agent_model='test-gemini-model',
        prices={'test-model':{'input_per_million':1,'output_per_million':2,'version':'TEST-ONLY','verified_at':'test fixture'}})
    state={'calls':0,'status':200,'missing_done':False,'missing_usage':False,'planner_calls':0}
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for text in ['Bonjour ', 'été €']:
                state['emitted']=state.get('emitted',0)+1
                yield ('data: '+json.dumps({'choices':[{'delta':{'content':text},'finish_reason':None}]})+'\n\n').encode()
                await asyncio.sleep(.002)
                if state.get('read_timeout'): raise httpx.ReadTimeout('test stream timeout')
            last={'choices':[{'delta':{},'finish_reason':'stop'}]}
            if not state['missing_usage']: last['x_groq']={'usage':{'prompt_tokens':10,'completion_tokens':3}}
            yield ('data: '+json.dumps(last)+'\n\n').encode()
            if not state['missing_done']: yield b'data: [DONE]\n\n'
    def handle(request):
        body=json.loads(request.content)
        if request.url.host=='generativelanguage.googleapis.com':
            state['planner_calls']+=1
            assert str(request.url)=='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
            assert request.headers['authorization']=='Bearer not-a-real-google-key'
            assert body['model']=='test-gemini-model'
            assert body['max_tokens']==2048
            if state.get('two_google_turns') and state['planner_calls']==1:
                return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{
                    'role':'assistant','content':None,'extra_content':{'google':{'opaque_state':'test-only-state'}},
                    'tool_calls':[{'id':'signed_read','type':'function',
                        'extra_content':{'google':{'thought_signature':'test-only-signature'}},
                        'function':{'name':'get_latency_outliers','arguments':'{}'}}]}}]})
            if state.get('two_google_turns'):
                previous=body['messages'][-2]
                assert previous['extra_content']['google']['opaque_state']=='test-only-state'
                assert previous['tool_calls'][0]['extra_content']['google']['thought_signature']=='test-only-signature'
                assert body['messages'][-1]['role']=='tool' and body['messages'][-1]['tool_call_id']=='signed_read'
                assert len(json.loads(body['messages'][-1]['content']))==3
            if state.get('google_no_tools'):
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'No experiment proposed.'}}]})
            if state.get('gemini_status'):
                return httpx.Response(state['gemini_status'],json={'error':{'message':'test Google error'}})
            if state.get('gemini_timeout'):
                raise httpx.ReadTimeout('test Gemini timeout',request=request)
            return httpx.Response(200,json={'model':'test-resolved-gemini','choices':[{
                'finish_reason':state.get('google_finish','tool_calls'),'message':{'role':'assistant','tool_calls':[{
                'id':'call_1','type':'function','function':{
                    'name':('execute_arbitrary_sql' if state.get('bad_tool') else 'propose_experiment'),
                    'arguments':'{"strategy":"exact_cache"}'}}]}}]})
        assert request.url.host=='api.groq.com'
        state['calls']+=1
        if state.get('timeout'): raise httpx.ReadTimeout('test timeout',request=request)
        assert request.headers['authorization']=='Bearer not-a-real-provider-key'
        if state['status']!=200:
            return httpx.Response(state['status'],json={'error':{'message':'test upstream error'}})
        if body.get('stream'): return httpx.Response(200,stream=Body())
        text='Bonjour, réponse de test.'
        for case in dataset()['cases']:
            if body['messages'][-1]['content'].endswith(case['question']):
                text=' '.join(group[0] for group in case['expected']);break
        data={'choices':[{'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}
        if not state['missing_usage']: data['usage']={'prompt_tokens':10,'completion_tokens':3}
        return httpx.Response(200,json=data)
    api=create_app(settings,transport=httpx.MockTransport(handle))
    try:
        with TestClient(api) as client:
            admin={'Authorization':'Bearer '+settings.admin_secret}
            created=client.post('/api/applications',headers=admin,json={'name':'TEST NovaMaison','corpus_version':'test-v1'})
            assert created.status_code==201,created.text
            app=created.json()
            key={'Authorization':'Bearer '+app['gateway_key']}
            yield client,admin,app,key,state
    finally:
        with psycopg.connect(DB_URL,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def payload(stream=False):
    return {'model':'test-model','messages':[{'role':'user','content':'Public test question'}],'temperature':0,'max_completion_tokens':32,'stream':stream}


def events(client,admin,app):
    result=client.get('/api/events',headers=admin,params={'app_id':app['id']})
    assert result.status_code==200,result.text
    return result.json()['items']


def test_gateway_auth_privacy_persistence_and_pagination(service):
    client,admin,app,key,state=service
    assert client.get('/ready').status_code==200
    assert client.get('/api/applications').status_code==401
    assert client.post('/v1/chat/completions',json=payload()).status_code==401
    result=client.post('/v1/chat/completions',headers=key,json=payload())
    assert result.status_code==200
    rows=events(client,admin,app)
    assert len(rows)==1 and rows[0]['token_source']=='provider'
    assert rows[0]['input_tokens']==10 and rows[0]['estimated_cost_usd']>0
    assert 'Public test question' not in json.dumps(rows)
    assert app['gateway_key'] not in json.dumps(client.get('/api/applications',headers=admin).json())
    assert client.post('/v1/chat/completions',headers={**key,'X-Request-ID':rows[0]['request_id']},json=payload()).status_code==409
    assert state['calls']==1
    assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==200
    page=client.get('/api/events',headers=admin,params={'app_id':app['id'],'limit':1}).json()
    assert page['next_cursor']
    page2=client.get('/api/events',headers=admin,params={'app_id':app['id'],'limit':1,'cursor':page['next_cursor']}).json()
    assert page2['items'][0]['id']!=page['items'][0]['id']
    overview=client.get('/api/overview',headers=admin,params={'app_id':app['id']}).json()
    assert overview['requests']==2 and overview['p95_latency_ms'] is None


@pytest.mark.parametrize('status',[401,429,500])
def test_gateway_upstream_errors(service,status):
    client,admin,app,key,state=service
    state['status']=status
    result=client.post('/v1/chat/completions',headers=key,json=payload())
    assert result.status_code==status
    assert events(client,admin,app)[0]['status']=='error'


def test_stream_success_and_incomplete(service):
    client,admin,app,key,state=service
    result=client.post('/v1/chat/completions',headers=key,json=payload(True))
    assert result.status_code==200 and '[DONE]' in result.text and 'Bonjour' in result.text
    row=events(client,admin,app)[0]
    assert row['status']=='success' and row['ttft_ms'] is not None and row['output_tokens']==3
    state['missing_done']=True
    result=client.post('/v1/chat/completions',headers=key,json=payload(True))
    assert '[DONE]' not in result.text
    assert events(client,admin,app)[0]['error_type']=='missing_done'


def test_null_usage_and_size_limit(service):
    client,admin,app,key,state=service
    state['missing_usage']=True
    assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==200
    row=events(client,admin,app)[0]
    assert row['input_tokens'] is None and row['estimated_cost_usd'] is None
    assert client.post('/v1/chat/completions',headers=key,json={**payload(),'messages':[{'role':'user','content':'x'*300_000}]}).status_code==413


def test_agent_uses_real_tools_and_no_auto_execution(service):
    client,admin,app,key,state=service
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='insufficient_data'
    assert len(result['tool_calls_json'])==4 and state['calls']==0 and state['planner_calls']==0
    for _ in range(3): assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==200
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='proposed'
    assert result['evidence_json']['planner_provider']=='google'
    assert result['evidence_json']['planner_model']=='test-gemini-model'
    assert result['evidence_json']['planner_resolved_model']=='test-resolved-gemini'
    assert state['planner_calls']==1 and state['calls']==3
    assert result['tool_calls_json'][-1]['tool']=='propose_experiment'
    assert result['recommendation_json']['evidence']['observed_requests']==3
    assert client.get('/api/benchmarks',headers=admin,params={'app_id':app['id']}).json()==[]


def test_benchmark_protocol_cache_and_quality(service):
    client,admin,app,key,state=service
    assert client.post('/api/benchmarks',headers=admin,json={'app_id':app['id'],'approved':False}).status_code==422
    created=client.post('/api/benchmarks',headers=admin,json={'app_id':app['id'],'approved':True,'max_calls':64,'max_cost_usd':1})
    assert created.status_code==202,created.text
    run_id=created.json()['id']
    for _ in range(300):
        result=client.get('/api/benchmarks/'+run_id,headers=admin).json()
        if result['status'] not in ('running','cancel_requested'): break
        time.sleep(.02)
    assert result['status']=='completed',result
    report=result['report_json']
    assert report['phases']['baseline']['cases']==25
    assert report['phases']['warmup']['cases']==12
    assert report['phases']['optimized']['cases']==25
    assert report['phases']['optimized']['cache_hits']==20
    assert report['optimized_including_warmup_cost_usd'] is not None
    assert report['quality_gate_pass'] is True
    assert report['including_warmup_savings_pct']<report['steady_state_savings_pct']
    assert state['calls']==42  # 25 baseline + 12 warm-up + 5 uncached dynamic/unknown.
    assert len(result['cases'])==62
    # Benchmark cache cannot affect ordinary traffic.
    assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==200
    assert state['calls']==43



def test_timeout_and_stream_http_error(service):
    client,admin,app,key,state=service
    state['timeout']=True
    assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==504
    assert events(client,admin,app)[0]['error_type']=='timeout'
    state['timeout']=False
    state['status']=429
    assert client.post('/v1/chat/completions',headers=key,json=payload(True)).status_code==429
    assert events(client,admin,app)[0]['http_status']==429


def test_agent_rejects_arbitrary_tool(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['bad_tool']=True
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='failed'
    assert len(result['tool_calls_json'])==4
    assert result['recommendation_json'] is None


def test_cache_expiry_and_application_isolation(service):
    client,admin,app,key,state=service
    gateway=client.app.state.gateway
    db=client.app.state.db
    scope='benchmark:unit-public-fixture'
    async def run():
        row=await db.one('SELECT id,name,corpus_version FROM applications WHERE id=%s',(app['id'],))
        before=state['calls']
        assert (await gateway.complete(row,payload(),cache_scope=scope))[0]==200
        _,hit,_=await gateway.complete(row,payload(),cache_scope=scope)
        assert state['calls']==before+1 and hit['usage']['completion_tokens']==0
        assert hit['x_inferpilot']['avoided_output_tokens_estimate']==3
        # Explicitly disable cache: provider must be contacted.
        await gateway.complete(row,payload())
        assert state['calls']==before+2
        row_v2={**row,'corpus_version':'test-v2'}
        await gateway.complete(row_v2,payload(),cache_scope=scope)
        assert state['calls']==before+3
        await db.execute("UPDATE cache_entries SET expires_at=now()-interval '1 second' WHERE app_id=%s",(row['id'],))
        await gateway.complete(row,payload(),cache_scope=scope)
        assert state['calls']==before+4
        return before
    before=client.portal.call(run)
    second=client.post('/api/applications',headers=admin,json={'name':'Second app','corpus_version':'test-v1'}).json()
    async def other():
        row=await db.one('SELECT id,name,corpus_version FROM applications WHERE id=%s',(second['id'],))
        await gateway.complete(row,payload(),cache_scope=scope)
    client.portal.call(other)
    assert state['calls']==before+5


def test_benchmark_request_budget_is_persisted(service):
    client,admin,app,key,state=service
    result=client.post('/api/benchmarks',headers=admin,json={'app_id':app['id'],'approved':True,'max_calls':1})
    run_id=result.json()['id']
    for _ in range(100):
        result=client.get('/api/benchmarks/'+run_id,headers=admin).json()
        if result['status']!='running': break
        time.sleep(.01)
    assert result['status']=='failed' and result['notes']=='request_budget_exceeded'
    assert state['calls']==1
    assert result['report_json']['including_warmup_savings_pct'] is None
    assert result['report_json']['comparison_complete'] is False


def test_rotation_and_retention(service):
    client,admin,app,key,state=service
    client.post('/v1/chat/completions',headers=key,json=payload())
    rotated=client.post('/api/applications/'+app['id']+'/rotate-key',headers=admin).json()
    assert client.post('/v1/chat/completions',headers=key,json=payload()).status_code==401
    assert client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+rotated['gateway_key']},json=payload()).status_code==200
    async def age():
        await client.app.state.db.execute("UPDATE inference_events SET started_at=now()-interval '100 days' WHERE app_id=%s",(app['id'],))
        await client.app.state.db.migrate()  # Checksums and repeated migration must be stable.
    client.portal.call(age)
    result=client.post('/api/maintenance/purge',headers=admin)
    assert result.json()['deleted_events']==2
    assert not events(client,admin,app)


def test_database_outage_fails_closed_without_provider():
    from inferpilot.gateway import Gateway
    class DB:
        async def execute(self,*args): raise RuntimeError('database unavailable')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail('provider must not be called'))) as client:
            gateway=Gateway(DB(),Settings(),client)
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as error:
                await gateway.complete({'id':uuid4(),'corpus_version':'1'},payload())
            assert error.value.status_code==503
    asyncio.run(run())


def test_usage_provider_tps_and_quality_numbers():
    from inferpilot.telemetry import timings
    assert timings({'usage':{'prompt_tokens':10,'completion_tokens':4,'completion_time':.5}})['provider_tps']==8
    assert 'provider_tps' not in timings({'usage':{'prompt_tokens':10,'completion_tokens':4}})
    assert not quality('La garantie dure 24 mois.',[['2']])
    assert quality('Charge maximale : 5 kg.',[['5']])


def test_novamaison_gateway_configuration(monkeypatch):
    from app.rag import completion_url,groq_request
    monkeypatch.setenv('LLM_BASE_URL','http://inferpilot:8080/v1')
    monkeypatch.setenv('LLM_GATEWAY_KEY','test-application-key')
    monkeypatch.delenv('GROQ_API_KEY',raising=False)
    assert completion_url()=='http://inferpilot:8080/v1/chat/completions'
    headers,_=groq_request('test',[],stream=True)
    assert headers['Authorization']=='Bearer test-application-key'



def test_gateway_streams_before_end_and_records_disconnect(service):
    client,admin,app,key,state=service
    async def run():
        gateway=client.app.state.gateway
        row=await gateway.db.one('SELECT id,name,corpus_version FROM applications WHERE id=%s',(app['id'],))
        status,_,response,attempt=await gateway.open_stream(row,payload(True))
        assert status==200
        stream=gateway.relay(response,attempt)
        first=await anext(stream)
        assert b'Bonjour' in first and state['emitted']==1
        assert not response.is_closed
        await stream.aclose()
        recorded=await gateway.db.one('SELECT status,error_type,ttft_ms FROM inference_events WHERE id=%s',(attempt['id'],))
        assert recorded['status']=='cancelled' and recorded['error_type']=='client_disconnect'
        assert recorded['ttft_ms'] is not None
        assert response.is_closed and gateway.slots._value==gateway.settings.max_concurrency
    client.portal.call(run)



def test_stream_timeout_retains_first_token_measurement(service):
    client,admin,app,key,state=service
    state['read_timeout']=True
    result=client.post('/v1/chat/completions',headers=key,json=payload(True))
    assert 'Bonjour' in result.text and '[DONE]' not in result.text
    row=events(client,admin,app)[0]
    assert row['status']=='error' and row['error_type']=='timeout' and row['ttft_ms'] is not None



@pytest.mark.parametrize('status',[401,404,429,500])
def test_google_planner_errors_are_recorded_without_fallback(service,status):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['gemini_status']=status
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='failed'
    assert result['evidence_json']['provider_http_status']==status
    assert result['evidence_json']['planner_provider']=='google'
    assert state['calls']==3 and state['planner_calls']==1
    assert 'not-a-real-google-key' not in json.dumps(result)


def test_google_planner_timeout_is_recorded(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['gemini_timeout']=True
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='failed'
    assert result['evidence_json']['provider_error']=='timeout'
    assert state['planner_calls']==1 and state['calls']==3



def test_google_planner_is_independent_of_groq_credentials(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    client.app.state.gateway.settings.groq_key=''
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='proposed'
    assert state['planner_calls']==1 and state['calls']==3
    ready=client.get('/ready').json()
    assert ready['provider_configured'] is False and ready['agent_provider_configured'] is True


def test_missing_google_key_does_not_fall_back_to_groq(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    client.app.state.gateway.settings.gemini_key=''
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='failed' and 'GEMINI_API_KEY' in result['evidence_json']['reason']
    assert result['evidence_json']['planner_invoked'] is False
    assert state['planner_calls']==0 and state['calls']==3


def test_google_tool_loop_preserves_signatures_and_real_results(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['two_google_turns']=True
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='proposed',result
    assert state['planner_calls']==2 and state['calls']==3
    assert [call['tool'] for call in result['tool_calls_json']][-2:]==['get_latency_outliers','propose_experiment']


def test_google_text_without_tool_call_does_not_create_experiment(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['google_no_tools']=True
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='no_experiment_proposed' and result['recommendation_json'] is None
    assert len(result['tool_calls_json'])==4


def test_google_truncated_tool_call_cannot_propose_experiment(service):
    client,admin,app,key,state=service
    for _ in range(3): client.post('/v1/chat/completions',headers=key,json=payload())
    state['google_finish']='length'
    result=client.post('/api/agent/analyze',headers=admin,json={'app_id':app['id']}).json()
    assert result['status']=='failed' and result['recommendation_json'] is None
    assert len(result['tool_calls_json'])==4


def test_google_model_default_does_not_follow_rag_model(monkeypatch):
    monkeypatch.delenv('AGENT_MODEL',raising=False)
    monkeypatch.setenv('GROQ_MODEL','some-rag-model')
    assert Settings().agent_model=='gemini-flash-latest'
