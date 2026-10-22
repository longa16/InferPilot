from datetime import datetime, timezone, timedelta
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ApplicationCreate(Strict):
    name: str = Field(min_length=1, max_length=100)
    corpus_version: str = Field(min_length=1, max_length=100)


class Completion(BaseModel):
    model_config = ConfigDict(extra='allow')
    model: str = Field(min_length=1, max_length=150)
    messages: list[dict] = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0, ge=0, le=2)
    max_completion_tokens: int = Field(default=800, ge=1, le=8192)
    stream: bool = False

    @model_validator(mode='after')
    def validate_messages(self):
        for message in self.messages:
            if message.get('role') not in ('system','user','assistant') or not isinstance(message.get('content'), str):
                raise ValueError('Only text system/user/assistant messages are supported')
        if self.model_extra:
            raise ValueError('Unsupported completion fields for this MVP')
        return self


class AnalysisRequest(Strict):
    app_id: UUID
    hours: int = Field(default=24, ge=1, le=2160)


class BenchmarkRequest(Strict):
    app_id: UUID
    approved: Literal[True]
    agent_run_id: UUID | None = None
    max_calls: int = Field(default=64, ge=1, le=64)
    max_cost_usd: float | None = Field(default=None, gt=0, le=100)


def window(start=None, end=None):
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None or not start < end or end-start > timedelta(days=90):
        raise ValueError('Use timezone-aware dates, from < to, and a window at most 90 days')
    return start, end
