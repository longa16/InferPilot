from contextlib import asynccontextmanager
from pathlib import Path
from functools import partial
import asyncio
import logging
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.rag import Retriever, answer

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
