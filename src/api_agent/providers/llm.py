"""Small immutable text and tool vocabulary shared by inference adapters."""
import math
import json
from dataclasses import dataclass, field
from typing import Literal
from .control import OperationalError


LLM_FEATURES = frozenset({"tools", "structured", "temperature", "max_output_tokens", "cache_policy"})


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid LLM description")


def _items(value: object, kind: type) -> bool:
    return isinstance(value, tuple) and all(isinstance(item, kind) for item in value)


def _finite_json(value: object) -> bool:
    if isinstance(value, dict):
        return all(_finite_json(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    return type(value) is not float or math.isfinite(value)


def _schema(value: str) -> None:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError("invalid LLM schema") from None
    _require(isinstance(parsed, dict) and _finite_json(parsed))


@dataclass(frozen=True)
class TextContent:
    text: str = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.text, str))


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.call_id, str) and bool(self.call_id) and isinstance(self.name, str) and bool(self.name) and isinstance(self.arguments, str))


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: tuple[TextContent, ...] = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.call_id, str) and bool(self.call_id) and _items(self.content, TextContent))


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: tuple[TextContent, ...] = field(default=(), repr=False)
    calls: tuple[ToolCall, ...] = field(default=(), repr=False)
    results: tuple[ToolResult, ...] = field(default=(), repr=False)

    def __post_init__(self):
        _require(type(self.role) is str and self.role in {"system", "user", "assistant", "tool"} and _items(self.content, TextContent) and _items(self.calls, ToolCall) and _items(self.results, ToolResult))
        _require(not self.calls or self.role == "assistant")
        _require(not self.results or self.role == "tool")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str = field(repr=False)
    input_schema: str = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.name, str) and bool(self.name) and isinstance(self.description, str) and isinstance(self.input_schema, str))
        _schema(self.input_schema)


@dataclass(frozen=True)
class GenerationOptions:
    max_output_tokens: int | None = None
    temperature: float | None = None
    cache_policy: Literal["off", "medium", "high"] | None = None

    def __post_init__(self):
        _require(self.max_output_tokens is None or (type(self.max_output_tokens) is int and self.max_output_tokens > 0))
        _require(self.temperature is None or (type(self.temperature) in {int, float} and math.isfinite(self.temperature) and self.temperature >= 0))
        _require(self.cache_policy is None or (type(self.cache_policy) is str and self.cache_policy in {"", "off", "medium", "high"}))
        if self.cache_policy == "":
            object.__setattr__(self, 'cache_policy', None)


@dataclass(frozen=True)
class StructuredOutput:
    name: str
    schema: str = field(repr=False)

    def __post_init__(self):
        _require(isinstance(self.name, str) and bool(self.name) and isinstance(self.schema, str))
        _schema(self.schema)


@dataclass(frozen=True)
class CompletionRequest:
    messages: tuple[Message, ...] = field(repr=False)
    tools: tuple[Tool, ...] = field(default=(), repr=False)
    generation: GenerationOptions = field(default_factory=GenerationOptions)
    structured: StructuredOutput | None = field(default=None, repr=False)

    def __post_init__(self):
        _require(_items(self.messages, Message) and bool(self.messages) and _items(self.tools, Tool) and isinstance(self.generation, GenerationOptions) and (self.structured is None or isinstance(self.structured, StructuredOutput)))
        _require(len({tool.name for tool in self.tools}) == len(self.tools))


@dataclass(frozen=True)
class AvailableSelection:
    selected_llm: str
    description: str = field(default="", repr=False)
    features: tuple[str, ...] = ()
    display_name: str = ""
    version: str | None = None
    max_output_tokens: int | None = None
    context_length: int | None = None
    family: str | None = None

    def __post_init__(self):
        _require(isinstance(self.selected_llm, str) and bool(self.selected_llm) and isinstance(self.description, str) and isinstance(self.features, tuple) and all(type(feature) is str and feature in LLM_FEATURES for feature in self.features))
        _require(len(set(self.features)) == len(self.features))
        _require(type(self.display_name) is str)
        _require(self.version is None or type(self.version) is str)
        _require(self.family is None or (type(self.family) is str and bool(self.family)))
        _require(all(value is None or (type(value) is int and 0 < value <= 2147483647)
                     for value in (self.max_output_tokens, self.context_length)))

    def validate(self, request: "LLMRequest") -> None:
        required = {name for name, enabled in (("tools", bool(request.payload.tools) or any(message.calls or message.results for message in request.payload.messages)), ("structured", request.payload.structured is not None), ("temperature", request.payload.generation.temperature is not None), ("max_output_tokens", request.payload.generation.max_output_tokens is not None), ("cache_policy", request.payload.generation.cache_policy is not None)) if enabled}
        if request.selected_llm != self.selected_llm or not required.issubset(self.features):
            raise OperationalError("unsupported")


@dataclass(frozen=True)
class LLMRequest:
    selected_llm: str
    payload: CompletionRequest = field(repr=False)
    request_id: str = ""

    def __post_init__(self):
        _require(isinstance(self.selected_llm, str) and bool(self.selected_llm) and isinstance(self.payload, CompletionRequest) and isinstance(self.request_id, str))


@dataclass(frozen=True)
class Usage:
    """Input includes disjoint cache reads/writes; output includes reasoning."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_write_tokens: int | None = None

    def __post_init__(self):
        _require(all(value is None or (type(value) is int and value >= 0) for value in (self.input_tokens, self.output_tokens, self.cached_input_tokens, self.reasoning_tokens, self.cache_write_tokens)))

        _require(self.input_tokens is None or self.cached_input_tokens is None or self.cached_input_tokens <= self.input_tokens)
        _require(self.output_tokens is None or self.reasoning_tokens is None or self.reasoning_tokens <= self.output_tokens)
        _require(self.input_tokens is None or self.cache_write_tokens is None or
                 (self.cached_input_tokens or 0) + self.cache_write_tokens <= self.input_tokens)


@dataclass(frozen=True)
class LLMResult:
    payload: Message = field(repr=False)
    usage: Usage = field(default_factory=Usage)
    outcome: Literal["completed", "length", "tool_calls", "refused"] = "completed"
    actual_model: str | None = None
    service_tier: str | None = None
    serving_revision: str | None = None

    def __post_init__(self):
        _require(all(value is None or (type(value) is str and 0 < len(value) <= 200 and value.isprintable()) for value in (self.service_tier, self.serving_revision)))
        _require(self.actual_model is None or (type(self.actual_model) is str and 0 < len(self.actual_model) <= 200 and self.actual_model.isprintable()))
        _require(isinstance(self.payload, Message) and self.payload.role == "assistant" and isinstance(self.usage, Usage) and type(self.outcome) is str and self.outcome in {"completed", "length", "tool_calls", "refused"})


@dataclass(frozen=True)
class StreamEvent:
    payload: TextContent | ToolCall | LLMResult = field(repr=False)
    kind: Literal["text", "tool_call", "completed"] = "text"

    def __post_init__(self):
        expected = {"text": TextContent, "tool_call": ToolCall, "completed": LLMResult}
        _require(type(self.kind) is str and self.kind in expected and isinstance(self.payload, expected[self.kind]))

    @property
    def final(self) -> bool:
        return self.kind == "completed"
