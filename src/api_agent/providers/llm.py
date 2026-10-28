"""Small immutable text and tool vocabulary shared by inference adapters."""
import math
import json
from dataclasses import dataclass, field
from typing import Literal
from .control import OperationalError


LLM_FEATURES = frozenset({"tools", "structured", "temperature", "max_output_tokens", "cache_policy", "reasoning_effort", "cache_boundaries"})


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
class CacheBoundary:
    """Optional explicit prompt boundary; unsupported providers may ignore it."""
    lifetime_seconds: Literal[300, 3600] = 300

    def __post_init__(self):
        _require(type(self.lifetime_seconds) is int and self.lifetime_seconds in (300, 3600))


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: tuple[TextContent, ...] = field(default=(), repr=False)
    calls: tuple[ToolCall, ...] = field(default=(), repr=False)
    results: tuple[ToolResult, ...] = field(default=(), repr=False)
    continuation: str | None = field(default=None, repr=False)

    cache_boundary: CacheBoundary | None = None

    def __post_init__(self):
        _require(self.cache_boundary is None or (isinstance(self.cache_boundary, CacheBoundary) and bool(self.content) and any(part.text for part in self.content)))
        _require(type(self.role) is str and self.role in {"system", "user", "assistant", "tool"} and _items(self.content, TextContent) and _items(self.calls, ToolCall) and _items(self.results, ToolResult))
        _require(not self.calls or self.role == "assistant")
        _require(not self.results or self.role == "tool")
        _require(self.continuation is None or (type(self.continuation) is str and bool(self.continuation) and self.role == "assistant"))


@dataclass(frozen=True)
class Tool:
    name: str
    description: str = field(repr=False)
    input_schema: str = field(repr=False)
    strict: bool = True

    def __post_init__(self):
        _require(isinstance(self.name, str) and bool(self.name) and isinstance(self.description, str) and isinstance(self.input_schema, str))
        _require(type(self.strict) is bool)
        _schema(self.input_schema)


@dataclass(frozen=True)
class GenerationOptions:
    max_output_tokens: int | None = None
    temperature: float | None = None
    cache_policy: Literal["off", "medium", "high"] | None = None

    reasoning_effort: Literal["", "off", "low", "medium", "high"] | None = None

    def __post_init__(self):
        _require(self.reasoning_effort is None or (type(self.reasoning_effort) is str and self.reasoning_effort in ("", "off", "low", "medium", "high")))
        _require(self.max_output_tokens is None or (type(self.max_output_tokens) is int and self.max_output_tokens > 0))
        _require(self.temperature is None or (type(self.temperature) in {int, float} and math.isfinite(self.temperature) and self.temperature >= 0))
        _require(self.cache_policy is None or (type(self.cache_policy) is str and self.cache_policy in {"", "off", "medium", "high"}))
        if self.cache_policy == "":
            object.__setattr__(self, 'cache_policy', None)


@dataclass(frozen=True)
class StructuredOutput:
    name: str
    schema: str = field(repr=False)
    strict: bool = True

    def __post_init__(self):
        _require(isinstance(self.name, str) and bool(self.name) and isinstance(self.schema, str))
        _require(type(self.strict) is bool)
        _schema(self.schema)


OutputMode = Literal["structured", "json", "prompted_json", "text"]
FallbackPolicy = Literal["allow", "reject"]
OUTPUT_MODES = frozenset({"structured", "json", "prompted_json", "text"})
FALLBACK_REASONS = frozenset({"unsupported_mode", "unsupported_schema", "unsupported_combination", "unknown_capability"})


@dataclass(frozen=True)
class ResponsePolicy:
    mode: OutputMode
    fallback: FallbackPolicy
    schema: StructuredOutput | None = field(default=None, repr=False)

    def __post_init__(self):
        _require(type(self.mode) is str and self.mode in OUTPUT_MODES)
        _require(type(self.fallback) is str and self.fallback in {"allow", "reject"})
        _require(self.schema is None if self.mode == "text" else isinstance(self.schema, StructuredOutput))


@dataclass(frozen=True)
class ResponseEvidence:
    """Resolution metadata; strict is requested schema intent, not enforcement.

    Native strict enforcement requires actual_mode=structured and strict=True.
    JSON and prompted_json always rely on original-schema local validation.
    """
    requested_mode: OutputMode
    fallback: FallbackPolicy
    actual_mode: OutputMode
    schema_sha256: str | None
    strict: bool | None
    fallback_reason: str | None

    def __post_init__(self):
        _require(type(self.requested_mode) is str and self.requested_mode in OUTPUT_MODES)
        _require(type(self.actual_mode) is str and self.actual_mode in OUTPUT_MODES)
        _require(type(self.fallback) is str and self.fallback in {"allow", "reject"})
        _require(self.strict is None or type(self.strict) is bool)
        _require(self.schema_sha256 is None or (type(self.schema_sha256) is str and len(self.schema_sha256) == 64 and all(c in "0123456789abcdef" for c in self.schema_sha256)))
        _require((self.schema_sha256 is None and self.strict is None) if self.requested_mode == "text" else (self.schema_sha256 is not None and self.strict is not None))
        if self.requested_mode == self.actual_mode:
            _require(self.fallback_reason is None)
        else:
            _require(self.fallback == "allow" and (self.requested_mode, self.actual_mode) in {("structured", "json"), ("structured", "prompted_json"), ("json", "prompted_json")})
            _require(type(self.fallback_reason) is str and self.fallback_reason in FALLBACK_REASONS)


@dataclass(frozen=True)
class CompletionRequest:
    messages: tuple[Message, ...] = field(repr=False)
    tools: tuple[Tool, ...] = field(default=(), repr=False)
    generation: GenerationOptions = field(default_factory=GenerationOptions)
    structured: StructuredOutput | None = field(default=None, repr=False)
    response: ResponsePolicy | None = field(default=None, repr=False)

    def __post_init__(self):
        _require(_items(self.messages, Message) and bool(self.messages) and _items(self.tools, Tool) and isinstance(self.generation, GenerationOptions) and (self.structured is None or isinstance(self.structured, StructuredOutput)))
        _require(self.response is None or isinstance(self.response, ResponsePolicy))
        _require(self.structured is None or self.response is None)
        _require(sum(message.cache_boundary is not None for message in self.messages) <= 4)
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
    reasoning_efforts: tuple[str, ...] | None = None

    cache_boundary_lifetimes: tuple[int, ...] = ()

    def __post_init__(self):
        _require(type(self.cache_boundary_lifetimes) is tuple and len(set(self.cache_boundary_lifetimes)) == len(self.cache_boundary_lifetimes) and all(type(value) is int and value in (300, 3600) for value in self.cache_boundary_lifetimes))
        _require(not self.cache_boundary_lifetimes or "cache_boundaries" in self.features)
        _require(isinstance(self.selected_llm, str) and bool(self.selected_llm) and isinstance(self.description, str) and isinstance(self.features, tuple) and all(type(feature) is str and feature in LLM_FEATURES for feature in self.features))
        _require(len(set(self.features)) == len(self.features))
        _require(self.reasoning_efforts is None or (type(self.reasoning_efforts) is tuple and all(type(value) is str and value in ("off", "low", "medium", "high") for value in self.reasoning_efforts) and len(set(self.reasoning_efforts)) == len(self.reasoning_efforts)))
        _require(type(self.display_name) is str)
        _require(self.version is None or type(self.version) is str)
        _require(self.family is None or (type(self.family) is str and bool(self.family)))
        _require(all(value is None or (type(value) is int and 0 < value <= 2147483647)
                     for value in (self.max_output_tokens, self.context_length)))

    def validate(self, request: "LLMRequest") -> None:
        required = {name for name, enabled in (("tools", bool(request.payload.tools) or any(message.calls or message.results for message in request.payload.messages)), ("structured", request.payload.structured is not None), ("temperature", request.payload.generation.temperature is not None), ("max_output_tokens", request.payload.generation.max_output_tokens is not None), ("cache_policy", request.payload.generation.cache_policy is not None)) if enabled}
        maximum = request.payload.generation.max_output_tokens
        if maximum is not None and self.max_output_tokens is not None and maximum > self.max_output_tokens:
            raise OperationalError("unsupported")
        effort = request.payload.generation.reasoning_effort
        if effort and ("reasoning_effort" not in self.features or self.reasoning_efforts is None or effort not in self.reasoning_efforts):
            raise OperationalError("unsupported")
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


def known_usage_lower_bound(input_tokens, output_tokens, cached_input_tokens,
                            cache_write_tokens, reasoning_tokens):
    """Minimum disjoint input and output implied by one usage observation."""
    parts = (input_tokens, output_tokens, cached_input_tokens,
             cache_write_tokens, reasoning_tokens)
    if all(value is None for value in parts):
        return None
    input_min = (input_tokens if input_tokens is not None else
                 (cached_input_tokens or 0) + (cache_write_tokens or 0))
    output_min = output_tokens if output_tokens is not None else (reasoning_tokens or 0)
    return input_min + output_min


@dataclass(frozen=True)
class LLMResult:
    payload: Message = field(repr=False)
    usage: Usage = field(default_factory=Usage)
    outcome: Literal["completed", "length", "tool_calls", "refused"] = "completed"
    actual_model: str | None = None
    service_tier: str | None = None
    serving_revision: str | None = None
    billing: dict | None = field(default=None, repr=False)

    applied_cache_boundaries: tuple[int, ...] = ()
    response: ResponseEvidence | None = None

    def __post_init__(self):
        _require(self.response is None or isinstance(self.response, ResponseEvidence))
        _require(type(self.applied_cache_boundaries) is tuple and len(self.applied_cache_boundaries) <= 4 and len(set(self.applied_cache_boundaries)) == len(self.applied_cache_boundaries) and all(type(value) is int and 0 <= value < 64 for value in self.applied_cache_boundaries))
        if self.billing is not None:
            from .costs import validate_billing
            validate_billing(self.billing)
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


def cache_boundary_indexes(request: LLMRequest, selection: AvailableSelection) -> tuple[int, ...]:
    """Return only requested boundaries supported by this advertised selection."""
    if request.selected_llm != selection.selected_llm or "cache_boundaries" not in selection.features:
        return ()
    return tuple(index for index, message in enumerate(request.payload.messages)
                 if message.cache_boundary is not None
                 and message.cache_boundary.lifetime_seconds in selection.cache_boundary_lifetimes)
