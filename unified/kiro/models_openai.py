"""Pydantic models for OpenAI-compatible request/response.

Ported from kiro-gateway models_openai.py.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------

class OpenAIModel(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "kiro"


class ModelList(BaseModel):
    object: str = "list"
    data: List[OpenAIModel] = []


# ---------------------------------------------------------------------------
# Chat Completion Request
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: Any = None  # str | list | None
    name: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    tool_call_id: Optional[str] = None
    model_config = {"extra": "allow"}


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class Tool(BaseModel):
    type: str = "function"
    function: Optional[ToolFunction] = None
    # Flat Cursor-style fields
    name: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Any] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    response_format: Optional[Any] = None
    reasoning_effort: Optional[str] = None  # none/minimal/low/medium/high/xhigh
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Chat Completion Response (non-streaming)
# ---------------------------------------------------------------------------

class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: List[ChatCompletionChoice] = []
    usage: Optional[ChatCompletionUsage] = None


# ---------------------------------------------------------------------------
# Chat Completion Chunk (streaming)
# ---------------------------------------------------------------------------

class ChatCompletionChunkDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Any]] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta = Field(default_factory=ChatCompletionChunkDelta)
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: List[ChatCompletionChunkChoice] = []
    usage: Optional[ChatCompletionUsage] = None
