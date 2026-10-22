"""Configuration contains no provider prices or deploy-time credentials by default."""
from dataclasses import dataclass, field
import os
import json
from urllib.parse import urlparse


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL', ''))
    admin_secret: str = field(default_factory=lambda: os.getenv('ADMIN_AUTH_SECRET', ''))
    hmac_secret: str = field(default_factory=lambda: os.getenv('HMAC_SECRET', ''))
    groq_key: str = field(default_factory=lambda: os.getenv('GROQ_API_KEY', ''))
    model: str = field(default_factory=lambda: os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile'))
    agent_model: str = field(default_factory=lambda: os.getenv('AGENT_MODEL', os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')))
    environment: str = field(default_factory=lambda: os.getenv('ENVIRONMENT', 'development'))
    prices: dict = field(default_factory=lambda: json.loads(os.getenv('PRICING_JSON', '{}')))
    max_payload_bytes: int = 256_000
    max_concurrency: int = 8
    cache_ttl_seconds: int = 3600
    retention_days: int = field(default_factory=lambda: int(os.getenv('RETENTION_DAYS', '30')))
    benchmark_max_calls: int = 64
    provider_url: str = 'https://api.groq.com/openai/v1/chat/completions'

    def validate(self):
        if urlparse(self.database_url).scheme not in ('postgres', 'postgresql'):
            raise ValueError('DATABASE_URL must point to PostgreSQL; SQLite is not supported')
        if len(self.admin_secret) < 32 or len(self.hmac_secret) < 32:
            raise ValueError('ADMIN_AUTH_SECRET and HMAC_SECRET must each have at least 32 characters')
        if not 1 <= self.retention_days <= 365:
            raise ValueError('RETENTION_DAYS must be between 1 and 365')
        if self.environment == 'production' and 'sslmode=' not in self.database_url:
            raise ValueError('Production DATABASE_URL must explicitly require PostgreSQL TLS')
        if self.environment == 'production' and any(f'sslmode={x}' in self.database_url for x in ('disable','allow','prefer')):
            raise ValueError('Insecure PostgreSQL SSL mode in production')
        for model, price in self.prices.items():
            if not price.get('version') or not price.get('verified_at'):
                raise ValueError('Each price requires version and verified_at metadata')
            for key in ('input_per_million', 'output_per_million'):
                if isinstance(price.get(key), bool) or not isinstance(price.get(key), (int, float)) or not 0 <= price[key] < 10000:
                    raise ValueError('Invalid configured token price')
