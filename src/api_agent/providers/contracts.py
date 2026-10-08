"""Version-one provider boundaries with normalized inference data."""
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol
from .control import CleanupPendingError, OperationalError, OperationContext
from .llm import (LLM_FEATURES, AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, StreamEvent, StructuredOutput, TextContent, Tool, ToolCall, ToolResult, Usage)


class ContractError(ValueError):
    """A neutral boundary failure, without payload or credential diagnostics."""


class SetupPrerequisiteError(ContractError):
    """A safe reason that required setup configuration/capability is unavailable.

    Operational integrations skip this condition; rejection tests may assert it.
    Authentication, transport and implementation failures must use other errors.
    """


class InteractionRequiredError(ContractError):
    """A safe, actionable message requesting attended provider setup."""


class SessionClosedError(RuntimeError):
    """An authentication/session owner has finished disposal."""


@dataclass(frozen=True)
class ProviderCapabilities:
    kind: str
    operations: tuple[str, ...] = ()
    credential_roles: tuple[str, ...] = ()
    llm_role: str | None = None
    version: int = 1
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class APIRequest:
    payload: object = field(repr=False)
    operation: str = "execute"
    request_id: str = ""


@dataclass(frozen=True)
class APIResult:
    payload: object = field(repr=False)
    evidence: tuple[object, ...] = field(default=(), repr=False)


@dataclass
class BrowserSession:
    browser: object = field(repr=False)
    context: object = field(repr=False)
    page: object = field(repr=False)
    inspection_endpoint: str | None = field(default=None, repr=False)


class RoleCredentialReader(Protocol):
    async def __call__(self, role: str, index: int, kind: str) -> str: ...


class LLMCredentialReader(Protocol):
    async def __call__(self, index: int, kind: str) -> str: ...


class LLMAuthSession(Protocol):
    async def __call__(self) -> object: ...
    async def aclose(self) -> None: ...


class LLMAuthenticator(Protocol):
    async def authenticate(self, *, session: BrowserSession, user_index: int,
                           credentials: LLMCredentialReader) -> LLMAuthSession: ...


class LLMSetup(Protocol):
    async def asetup(self, *, interactive: bool, user_index: int | None,
                     credentials: LLMCredentialReader | None) -> LLMAuthSession: ...


class ResourcePackAuthenticator(Protocol):
    async def authenticate(self, *, session: BrowserSession, tui_mode: str,
                           user_index: int | None, credentials: RoleCredentialReader) -> None: ...


class APIProvider(Protocol):
    version: int
    capabilities: ProviderCapabilities
    async def ainvoke(self, request: APIRequest, *, auth: object = None) -> APIResult: ...


class LLMProvider(Protocol):
    version: int
    capabilities: ProviderCapabilities
    async def adescribe(self, *, auth: object = None) -> tuple[AvailableSelection, ...]: ...
    async def acomplete(self, request: LLMRequest, *, auth: object = None) -> LLMResult: ...
    def astream(self, request: LLMRequest, *, auth: object = None) -> AsyncIterator[StreamEvent]: ...
    async def aread_usage(self, *, auth: object = None) -> object: ...


class UIProvider(Protocol):
    version: int
    capabilities: ProviderCapabilities
    async def astart(self) -> None: ...
    async def aclose(self) -> None: ...


class UpstreamAPIProvider(Protocol):
    version: int
    capabilities: ProviderCapabilities
    async def avalidate(self, request: APIRequest, *, auth: object = None) -> object: ...


class AccessProvider(Protocol):
    version: int
    capabilities: ProviderCapabilities
    async def adescribe(self, *, auth: object = None) -> object: ...
    async def aread_usage(self, *, auth: object = None) -> object: ...


OPERATION_METHODS = {
    "api": {"invoke": "ainvoke"},
    "access": {"describe": "adescribe", "usage": "aread_usage"},
    "llm": {"describe": "adescribe", "complete": "acomplete", "stream": "astream", "usage": "aread_usage"},
    "ui": {"launch": "astart"},
    "upstream": {"validate": "avalidate"},
}
