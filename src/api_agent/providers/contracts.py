"""Version-one provider boundaries with normalized inference data."""
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol
from .control import CleanupPendingError, OperationalError, OperationContext
from .llm import (LLM_FEATURES, OutputMode, FallbackPolicy, ResponsePolicy, ResponseEvidence, CacheBoundary, cache_boundary_indexes, AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, StreamEvent, StructuredOutput, TextContent, Tool, ToolCall, ToolResult, Usage)


class ContractError(ValueError):
    """A neutral boundary failure, without payload or credential diagnostics."""


class ProviderAccessError(OperationalError):
    """Provider-sanitized, bounded upstream rejection details for consumer reports."""
    response: ResponseEvidence | None = None

    def __init__(self, kind, message, *, status=None, code=None, type=None, request_id=None, usage=None, response=None):
        super().__init__(kind)
        if status is not None and (status.__class__ is not int or not 100 <= status <= 599):
            raise ContractError('Invalid provider error status')
        for value, limit in ((message, 512), (code, 100), (type, 100), (request_id, 100)):
            if value is not None and (value.__class__ is not str or not value or len(value) > limit or not value.isprintable()):
                raise ContractError('Invalid provider error detail')
        if message is None:
            raise ContractError('Invalid provider error detail')
        self.status, self.code, self.type, self.request_id = status, code, type, request_id
        self.message = message
        from .llm import Usage
        if usage is not None and (not isinstance(usage, Usage) or any(
                value is not None and value > 2147483647 for value in (usage.input_tokens,
                usage.output_tokens, usage.cached_input_tokens, usage.reasoning_tokens, usage.cache_write_tokens))):
            raise ContractError('Invalid provider error usage')
        from .llm import ResponseEvidence
        if response is not None and not isinstance(response, ResponseEvidence):
            raise ContractError('Invalid provider response evidence')
        if response is not None:
            self.response = response
        self.usage = usage
        self.args = (message,)

    def __str__(self):
        details = [f'{label}: {value}' for label, value in (
            ('HTTP status', self.status), ('code', self.code),
            ('type', self.type), ('request ID', self.request_id)) if value is not None]
        return self.message + ('\n' + ' · '.join(details) if details else '')


class SetupPrerequisiteError(ContractError):
    """A safe reason that required setup configuration/capability is unavailable.

    Operational integrations skip this condition; rejection tests may assert it.
    Authentication, transport and implementation failures must use other errors.
    """


class InteractionRequiredError(ContractError):
    """A safe, actionable message requesting attended provider setup."""


class ConfigurationRequiredError(InteractionRequiredError):
    """A bounded request for declared settings, containing no setting values."""

    def __init__(self, fields):
        if (type(fields) not in (tuple, list) or not 1 <= len(fields) <= 32
                or any(type(name) is not str or len(name) > 128
                       or not name.isidentifier() or name != name.upper() for name in fields)
                or len(set(fields)) != len(fields)):
            raise ContractError('Invalid configuration requirement fields')
        self.fields = tuple(fields)
        super().__init__('Additional configuration is required. Open Review configuration.')


class UnattendedCredentialError(InteractionRequiredError):
    """Credential access failed; never silently replace the selected identity."""

    def __init__(self, message, *, index_source=None):
        self.detail = message
        self.index_source = index_source
        guidance = ('Failed to get credentials for unattended access. Fix credential access, '
                    'or switch to attended login with [ and ] to cycle to "No unattended user set"')
        if index_source == 'dotenv' or isinstance(index_source, str) and index_source.endswith('/.env'):
            guidance += (' or use Clear .env configuration from the main menu and delete any '
                         '.env assignment to UNATTENDED_USER_INDEX.')
        else:
            guidance += (' or stop passing the override for UNATTENDED_USER_INDEX '
                         '(environment variable or command-line option).')
        super().__init__(guidance + '\n\n' + message)


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
    "ui": {"launch": "astart", "prepare_session": "aprepare_session",
           "capture_evidence": "acapture_evidence"},
    "upstream": {"validate": "avalidate"},
}
