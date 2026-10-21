"""Real models: fails on missing network, poor retrieval or broken generation."""
import os
import asyncio
from pathlib import Path
from app.rag import Retriever, answer

r = Retriever(Path('documents'), Path('.cache/models'))
cases = [
    ('Can I put the Terra bottle in the dishwasher?', 'Lavage à la main'),
    ('Combien coûte la livraison standard ?', '4,90'),
    ('Wie viel kostet die Aurora Lampe?', '49,90'),
    ('Quelle est la capacité du sac Comète ?', '20 litres'),
]
for query, expected in cases:
    sources = r.search(query)
    assert any(expected in s['text'] for s in sources[:2]), (query, sources)
    print('PASS retrieval:', query, flush=True)
    if os.getenv('GENERATION_MODE', 'groq') == 'groq':
        result = asyncio.run(answer(query, sources))
        assert any(f"[{s['id']}]" in result for s in sources), result
        print(result, flush=True)
