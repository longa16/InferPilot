from pathlib import Path
import asyncio
import numpy as np
import pytest
from app.rag import BM25, Chunk, Retriever, answer, chunk_documents, reciprocal_rank_fusion

ROOT = Path(__file__).resolve().parents[1]


def test_corpus_sections_and_overlap(tmp_path):
    (tmp_path / 'a.md').write_text('# Test\n\n## Long\n' + ' '.join(f'w{i}' for i in range(20)))
    chunks = chunk_documents(tmp_path, size=10, overlap=2)
    assert len(chunks) == 3
    assert 'w8 w9' in chunks[0].text and 'w8 w9' in chunks[1].text
    assert all('Test — Long' in c.text for c in chunks)
    corpus = chunk_documents(ROOT / 'documents')
    assert len(corpus) >= 18
    assert len({c.id for c in corpus}) == len(corpus)
    assert any('49,90' in c.text for c in corpus)


def test_bm25_and_fusion():
    bm = BM25(['gourde Terra lavage main', 'lampe Aurora éclairage', 'livraison standard gratuite'])
    assert int(np.argmax(bm.scores('Terra gourde'))) == 0
    assert int(np.argmax(bm.scores('livraison'))) == 2
    assert not bm.scores('inconnu').any()
    assert reciprocal_rank_fusion([[0, 1], [1, 2]])[0] == 1


def test_retriever_really_uses_reranker():
    class Encoder:
        def query_embed(self, query):
            yield np.array([1., 0.])
    class Reranker:
        def rerank(self, query, docs):
            return [10 if 'gourde' in d else -10 for d in docs]
    r = Retriever.__new__(Retriever)
    r.chunks = [Chunk('S1', 'a.md', 'lampe'), Chunk('S2', 'b.md', 'gourde')]
    r.matrix = np.array([[1., 0.], [0., 1.]])
    r.encoder, r.reranker = Encoder(), Reranker()
    r.bm25 = BM25([c.text for c in r.chunks])
    assert r.search('lampe', top_k=1)[0]['id'] == 'S2'


def test_extractive_and_invalid_mode(monkeypatch):
    monkeypatch.setenv('GENERATION_MODE', 'extractive')
    response = asyncio.run(answer('prix ?', [{'id': 'S1', 'text': 'Prix : 10 €'}]))
    assert '[S1]' in response and '10 €' in response and 'extractif' in response
    monkeypatch.setenv('GENERATION_MODE', 'typo')
    with pytest.raises(ValueError):
        asyncio.run(answer('prix ?', []))


def test_api_validation_and_sources(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    class Stub:
        def __init__(self, *args):
            self.chunks = [Chunk('S1', 'catalogue.md', 'Aurora : 49,90 €')]
        def search(self, query):
            return [{'id':'S1', 'source':'catalogue.md', 'text':self.chunks[0].text,
                     'dense_score':.8, 'rerank_score':1.}]
    monkeypatch.setattr(main, 'Retriever', Stub)
    monkeypatch.setenv('GENERATION_MODE', 'extractive')
    with TestClient(main.app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/health').json()['chunks'] == 1
        assert client.post('/api/chat', json={'question':'  '}).status_code == 422
        assert client.post('/api/chat', json={'question':'x'*2001}).status_code == 422
        result = client.post('/api/chat', json={'question':'Prix Aurora ?'})
        assert result.status_code == 200
        assert result.json()['sources'][0]['id'] == 'S1'
        assert '49,90' in result.json()['answer']


def test_groq_payload_and_citation_validation(monkeypatch):
    import httpx
    monkeypatch.delenv('GENERATION_MODE', raising=False)  # Groq is the default.
    monkeypatch.setenv('GROQ_API_KEY', 'test-key-not-a-credential')
    monkeypatch.setenv('GROQ_MODEL', 'test-model')
    replies = iter(['Le prix est 10 € [S1].', 'Le prix est 10 € [S999].'])
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json, headers):
            assert url == 'https://api.groq.com/openai/v1/chat/completions'
            assert headers['Authorization'] == 'Bearer test-key-not-a-credential'
            assert json['model'] == 'test-model'
            assert json['temperature'] == 0
            assert json['max_completion_tokens'] == 800
            assert json['messages'][0]['role'] == 'system'
            assert '[S1]' in json['messages'][1]['content']
            return httpx.Response(200, json={'choices':[{'message':{'content':next(replies)},
                                                        'finish_reason':'stop'}]},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    sources = [{'id':'S1', 'text':'Prix 10 €'}]
    assert '[S1]' in asyncio.run(answer('prix ?', sources))
    with pytest.raises(ValueError, match='références'):
        asyncio.run(answer('prix ?', sources))


def test_groq_missing_key_fails_before_network(monkeypatch):
    monkeypatch.setenv('GENERATION_MODE', 'groq')
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    with pytest.raises(ValueError, match='GROQ_API_KEY'):
        asyncio.run(answer('prix ?', []))


@pytest.mark.parametrize('status', [401, 429, 500])
def test_groq_http_errors(monkeypatch, status):
    import httpx
    monkeypatch.setenv('GENERATION_MODE', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'test-key-not-a-credential')
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(status, json={'error':{'message':'failure'}},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    with pytest.raises(httpx.HTTPStatusError) as error:
        asyncio.run(answer('prix ?', []))
    assert error.value.response.status_code == status


@pytest.mark.parametrize('content,reason', [(None,'stop'), ('','stop'), ('Texte incomplet','length')])
def test_groq_invalid_output(monkeypatch, content, reason):
    import httpx
    monkeypatch.setenv('GENERATION_MODE', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'test-key-not-a-credential')
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={'choices':[{'message':{'content':content},
                                                        'finish_reason':reason}]},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    with pytest.raises(ValueError):
        asyncio.run(answer('prix ?', []))


def test_chunks_respect_token_budget(tmp_path):
    (tmp_path / 'a.md').write_text('# T\n\n## S\n' + ' '.join(f'w{i}' for i in range(200)))
    chunks = chunk_documents(tmp_path, token_count=lambda t: len(t.split()) * 3)
    assert all(len(c.text.split()) * 3 <= 128 for c in chunks)
    assert 'w199' in chunks[-1].text
