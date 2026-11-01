from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any

Purpose = Literal['chat', 'extraction', 'embedding', 'rerank']
Category = Literal['profile', 'preferences', 'experiences', 'projects', 'relationships', 'procedures', 'support']


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=300)


class ProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default='', max_length=2000)
    allow_private: bool = False
    enabled: bool = True
    version: int | None = None


class ModelInput(BaseModel):
    provider_id: str
    name: str = Field(min_length=1, max_length=150)
    purpose: Purpose = 'chat'
    context_window: int = Field(default=8192, ge=2048, le=2000000)


class PolicyInput(BaseModel):
    model_id: str
    version: int = Field(default=0, ge=0)


class ConversationInput(BaseModel):
    title: str = Field(default='新的对话', min_length=1, max_length=200)
    version: int = 1


class ChatInput(BaseModel):
    conversation_id: str
    request_id: str = Field(min_length=8, max_length=100)
    content: str = Field(min_length=1, max_length=12000)


class MemoryFields(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: Category = 'preferences'
    subject: str = Field(default='self', min_length=1, max_length=150)
    event_at: str | None = Field(default=None, max_length=100, description='事件发生日期或带时区的 ISO 8601 时间；null 表示未说明')
    valid_from: str | None = Field(default=None, max_length=100, description='事实适用期间起点；不作为保留或授权开关')
    valid_to: str | None = Field(default=None, max_length=100, description='事实适用期间终点；不是自动删除时间')
    version: int = Field(default=1, ge=1)


class MemoryInput(MemoryFields):
    @field_validator('event_at', 'valid_from', 'valid_to')
    @classmethod
    def temporal_value(cls, value):
        from .memory_time import instant
        instant(value)
        return value

    @model_validator(mode='after')
    def temporal_period(self):
        from .memory_time import check_period
        check_period(self.model_dump())
        return self


class CandidateAction(BaseModel):
    action: Literal['confirm', 'reject']
    review_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class PreferenceInput(BaseModel):
    auto_memory: bool


class ExportInput(BaseModel):
    format: Literal['markdown', 'json'] = 'markdown'


class SessionView(BaseModel):
    id: str
    username: str
    csrf: str
    auto_memory: bool


class ProviderView(BaseModel):
    id: str
    name: str
    base_url: str
    key_mask: str
    allow_private: bool
    enabled: bool
    version: int


class ModelView(ModelInput):
    id: str
    status: str
    verified_at: float | None


class ConversationView(BaseModel):
    id: str
    title: str
    version: int
    created_at: float


class MessageView(BaseModel):
    id: str
    role: str
    content: str
    status: str
    run_id: str | None
    created_at: float


class ChatRunView(BaseModel):
    model_id: str
    id: str
    conversation_id: str
    status: str
    error: str | None
    memory_status: str
    memory_refs: list[dict[str, Any]]
    config: dict[str, Any]
    next_seq: int


class MemoryView(MemoryFields):
    id: str
    status: str
    index_status: str
    evidence: str
    source_ids: list[str]
    created_at: float
    revisions: list[dict[str, Any]]


class CandidateView(BaseModel):
    id: str
    source_id: str
    content: str
    category: str
    subject: str
    evidence: str
    reason: str
    status: str
    memory_id: str | None
    origin: dict[str, Any] = Field(default_factory=dict)


class OperationView(BaseModel):
    id: str
    kind: str
    status: str
    target_id: str | None
    result: dict[str, Any]
    error: str | None
    created_at: float
