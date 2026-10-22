from pathlib import Path
import hashlib
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[1]


class Database:
    def __init__(self, url):
        self.pool = AsyncConnectionPool(url, open=False, min_size=1, max_size=8,
                                        kwargs={'row_factory': dict_row, 'connect_timeout': 5})

    async def open(self):
        await self.pool.open()
        await self.pool.wait(timeout=12)
        await self.migrate()

    async def close(self):
        await self.pool.close()

    async def migrate(self):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(81492026)')
                await conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz DEFAULT now())')
                for path in sorted((ROOT / 'migrations').glob('*.sql')):
                    contents = path.read_text()
                    digest = hashlib.sha256(contents.encode()).hexdigest()
                    row = await (await conn.execute('SELECT checksum FROM schema_migrations WHERE version=%s', (path.name,))).fetchone()
                    if row:
                        if row['checksum'] != digest:
                            raise RuntimeError('Applied migration checksum changed: ' + path.name)
                    else:
                        await conn.execute(contents, prepare=False)
                        await conn.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)', (path.name,digest))

    async def one(self, sql, params=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def all(self, sql, params=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def execute(self, sql, params=()):
        async with self.pool.connection() as conn:
            result = await conn.execute(sql, params)
            return result.rowcount
