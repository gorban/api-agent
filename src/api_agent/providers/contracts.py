"""Version-one provider boundaries; payload semantics belong to adapters."""
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol


class ContractError(ValueError):
    """A neutral boundary failure, without payload or credential diagnostics."""


class SessionClosedError(RuntimeError):
    """An authentication/session owner has finished disposal."""


@dataclass(frozen=True)
class ProviderCapabilities:
    kind: str
    operations: tuple[str, ...] = ()
    credential_roles: tuple[str, ...] = ()
    llm_role: str | None = None
    version: int = 1


@dataclass(frozen=True)
class APIRequest:
    payload: object = field(repr=False)
    operation: str = "execute"
    request_id: str = ""


@dataclass(frozen=True)
class APIResult:
    payload: object = field(repr=False)
    evidence: tuple[object, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class LLMRequest:
    selected_llm: str
    payload: object = field(repr=False)
    request_id: str = ""


@dataclass(frozen=True)
class LLMResult:
    payload: object = field(repr=False)
    usage: Mapping[str, object] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class StreamEvent:
    payload: object = field(repr=False)
    final: bool = False


@dataclass
class BrowserSession:
    browser: object = field(repr=False)
    context: object = field(repr=False)
    page: object = field(repr=False)


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
    async def adescribe(self, *, auth: object = None) -> object: ...
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


OPERATION_METHODS = {
    "api": {"invoke": "ainvoke"},
    "llm": {"describe": "adescribe", "complete": "acomplete", "stream": "astream", "usage": "aread_usage"},
    "ui": {"launch": "astart"},
    "upstream": {"validate": "avalidate"},
}
