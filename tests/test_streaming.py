import asyncio
import json
import httpx
import pytest
from app.rag import stream_answer, sse_data

SOURCES = [{'id': 'S1', 'text': 'Prix : 10 €'}]


def delta(text=None, finish=None):
    return 'data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': finish}]}) + '\r\n\r\n'


def install_groq(monkeypatch, events, status=200):
    real_client = httpx.AsyncClient
    state = {'consumed': 0, 'closed': False}
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for event in events:
                state['consumed'] += 1
                # Deliberately split UTF-8 and SSE lines over network packets.
                raw = event.encode()
                for offset in range(0, len(raw), 7):
                    yield raw[offset:offset+7]
        async def aclose(self):
            state['closed'] = True
    def handle(request):
        assert request.url == 'https://api.groq.com/openai/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer not-a-real-key'
        payload = json.loads(request.content)
        assert payload['stream'] is True
        assert payload['model'] == 'stream-test-model'
        assert '[S1]' in payload['messages'][1]['content']
        return httpx.Response(status, stream=Body())
    monkeypatch.setenv('GENERATION_MODE', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'not-a-real-key')
    monkeypatch.setenv('GROQ_MODEL', 'stream-test-model')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    return state


def test_groq_yields_before_completion(monkeypatch):
    events = [': keepalive\r\n\r\n', delta('Prix : '), delta('10 € [S1].'),
              delta(finish='stop'), 'data: [DONE]\r\n\r\n']
    state = install_groq(monkeypatch, events)
    async def run():
        stream = stream_answer('Prix ?', SOURCES)
        assert await anext(stream) == 'Prix : '
        assert state['consumed'] == 2  # Later tokens have not been read yet.
        assert not state['closed']
        remaining = [text async for text in stream]
        assert remaining == ['10 € [S1].']
        assert state['closed']
    asyncio.run(run())


def test_groq_closes_upstream_on_cancel(monkeypatch):
    state = install_groq(monkeypatch, [delta('Bonjour'), delta('Suite')])
    async def run():
        stream = stream_answer('Bonjour', SOURCES)
        assert await anext(stream) == 'Bonjour'
        await stream.aclose()
        assert state['closed']
        assert state['consumed'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('events', [
    [delta('incomplet')],
    [delta('tronqué'), delta(finish='length'), 'data: [DONE]\n\n'],
    [delta('Source inconnue [S99]'), delta(finish='stop'), 'data: [DONE]\n\n'],
    [delta(finish='stop'), 'data: [DONE]\n\n'],
    ['data: {"error":{"message":"failure"}}\n\n'],
    ['data: {malformed}\n\n'],
])
def test_groq_rejects_incomplete_or_invalid_stream(monkeypatch, events):
    state = install_groq(monkeypatch, events)
    async def run():
        with pytest.raises(ValueError):
            async for _ in stream_answer('Prix ?', SOURCES):
                pass
        assert state['closed']
    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 404, 429, 500])
def test_stream_http_errors(monkeypatch, status):
    state = install_groq(monkeypatch, [], status)
    async def run():
        with pytest.raises(httpx.HTTPStatusError) as error:
            async for _ in stream_answer('Prix ?', SOURCES):
                pass
        assert error.value.response.status_code == status
        assert state['closed']
    asyncio.run(run())


def test_extractive_stream(monkeypatch):
    monkeypatch.setenv('GENERATION_MODE', 'extractive')
    async def run():
        return [text async for text in stream_answer('Prix ?', SOURCES)]
    assert '10 €' in asyncio.run(run())[0]


def test_sse_multiline_and_final_event():
    async def lines():
        for line in [': comment', 'event: completion', 'data: {', 'data: "x": 1}', '', 'data: [DONE]']:
            yield line
    async def run():
        return [event async for event in sse_data(lines())]
    assert asyncio.run(run()) == ['{\n"x": 1}', '[DONE]']


@pytest.mark.parametrize('failure', [None, 'retrieval', 'upstream'])
def test_stream_api_contract_and_lock_release(monkeypatch, failure):
    import app.main as main
    from fastapi.testclient import TestClient
    class Retriever:
        def __init__(self, *args): self.chunks = SOURCES
        def search(self, question):
            if failure == 'retrieval': raise RuntimeError('private retrieval error')
            return SOURCES
    async def stream(question, sources):
        yield 'Prix : '
        if failure == 'upstream': raise RuntimeError('private API details')
        yield '10 € [S1].'
    monkeypatch.setattr(main, 'Retriever', Retriever)
    monkeypatch.setattr(main, 'stream_answer', stream)
    monkeypatch.setenv('GENERATION_MODE', 'groq')
    with TestClient(main.app) as client:
        assert client.post('/api/chat/stream', json={'question': '  '}).status_code == 422
        assert client.post('/api/chat/stream', json={'question': 'x'*2001}).status_code == 422
        result = client.post('/api/chat/stream', json={'question': 'Prix ?'})
        assert result.status_code == 200
        assert result.headers['content-type'].startswith('text/event-stream')
        assert result.headers['x-accel-buffering'] == 'no'
        events = []
        for block in result.text.strip().split('\n\n'):
            name, data = block.split('\n')
            events.append((name.removeprefix('event: '), json.loads(data.removeprefix('data: '))))
        assert events[0] == ('start', {'mode':'groq'})
        if failure:
            assert events[-1][0] == 'error'
            assert 'done' not in [name for name, data in events]
            assert 'private' not in result.text
        else:
            assert [name for name, data in events] == ['start','token','token','done']
            assert ''.join(data['text'] for name, data in events if name == 'token') == 'Prix : 10 € [S1].'
        assert not main.app.state.lock.locked()
