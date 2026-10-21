from contextlib import asynccontextmanager, aclosing
from pathlib import Path
from functools import partial
import asyncio
import logging
import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from app.rag import Retriever, answer, stream_answer

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup fails visibly if model downloads or indexing fail; no fake readiness.
    app.state.retriever = await asyncio.to_thread(Retriever, ROOT / 'documents', ROOT / '.cache' / 'models')
    app.state.lock = asyncio.Lock()
    yield


app = FastAPI(title='InferPilot — NovaMaison', lifespan=lifespan)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


@app.get('/')
def home():
    return FileResponse(ROOT / 'app' / 'static' / 'index.html')


@app.get('/health')
def health():
    return {'status': 'ready', 'chunks': len(app.state.retriever.chunks),
            'generation_mode': os.getenv('GENERATION_MODE', 'groq'),
            'generation_service_checked': False}


@app.post('/api/chat')
async def chat(body: Question):
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(422, 'Veuillez poser une question.')
    # Bound concurrent CPU inference and LLM work for this small deployment.
    async with app.state.lock:
        try:
            sources = await asyncio.to_thread(partial(app.state.retriever.search, question))
            response = await answer(question, sources)
        except Exception:
            logger.exception('Échec du pipeline RAG')
            raise HTTPException(503, 'Service indisponible. Vérifiez les modèles, la configuration Groq et les quotas API.')
    return {'answer': response, 'sources': sources,
            'mode': os.getenv('GENERATION_MODE', 'groq')}


@app.post('/api/chat/stream')
async def chat_stream(body: Question):
    question = body.question.strip()
    if len(question) < 2:
        raise HTTPException(422, 'Veuillez poser une question.')

    def event(name: str, data: dict) -> str:
        return f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

    async def events():
        # The lock stays held until the stream ends, including on disconnect.
        async with app.state.lock:
            try:
                mode = os.getenv('GENERATION_MODE', 'groq')
                yield event('start', {'mode': mode})
                sources = await asyncio.to_thread(partial(app.state.retriever.search, question))
                async with aclosing(stream_answer(question, sources)) as stream:
                    async for text in stream:
                        yield event('token', {'text': text})
                yield event('done', {})
            except asyncio.CancelledError:
                raise  # Close the upstream HTTP stream when the browser disconnects.
            except Exception:
                logger.exception('Échec du pipeline RAG en streaming')
                yield event('error', {'message': 'La réponse n’a pas pu être terminée. Veuillez réessayer.'})

    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})
