"""Content-bound, invocation-local validation and execution order for tools."""
from dataclasses import dataclass
from hashlib import sha256
import inspect
from uuid import uuid4

from api_agent.providers.bounded_schema import async_isolated_valid_schema
from api_agent.providers.progress import component_timeout
from api_agent.providers.tools import (MAX_ARGUMENT_BYTES, PreparedCall, ToolResult,
                                       ToolSpec, canonical)


class ToolGateError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedCall:
    prepared: PreparedCall
    gate_id: str


class ToolGate:
    """One operation per selected owner; grants are ordering markers, not security tokens."""
    def __init__(self, owner, *, invocation_id: str, timeout: float = 30):
        if not isinstance(invocation_id, str) or not invocation_id or len(invocation_id) > 200:
            raise ToolGateError('Invalid invocation identity')
        if any(not callable(getattr(owner, name, None))
               for name in ('describe', 'validate', 'invoke', 'aclose')):
            raise ToolGateError('Invalid tool owner')
        self.owner, self.invocation_id, self.timeout = owner, invocation_id, timeout
        self._id = str(uuid4())
        self._spec = None
        self._grants = set()
        self._used = False

    async def describe(self) -> ToolSpec:
        async with component_timeout('Tool preparation', self.timeout):
            spec = self._spec
            if spec is None:
                spec = await self.owner.describe()
                if not isinstance(spec, ToolSpec):
                    raise ToolGateError('Tool owner returned invalid specification')
                self._spec = spec
            return spec

    async def prepare(self, arguments: dict) -> PreparedCall:
        async with component_timeout('Tool preparation', self.timeout):
            spec = await self.describe()
            if type(arguments) is not dict:
                raise ToolGateError('Tool arguments must be an object')
            encoded = canonical(arguments, MAX_ARGUMENT_BYTES)
            if not await async_isolated_valid_schema(spec.input_schema, arguments):
                raise ToolGateError('Tool arguments do not match the selected schema')
            digest = sha256((self._id + '\0' + self.invocation_id + '\0' + spec.digest + '\0' + encoded).encode()).hexdigest()
            return PreparedCall(spec, arguments, self.invocation_id, digest)

    def _check(self, prepared):
        if not isinstance(prepared, PreparedCall) or prepared.spec is not self._spec:
            raise ToolGateError('Tool selection changed')
        if prepared.invocation_id != self.invocation_id:
            raise ToolGateError('Tool invocation changed')
        encoded = canonical(prepared.arguments, MAX_ARGUMENT_BYTES)
        expected = sha256((self._id + '\0' + self.invocation_id + '\0' + self._spec.digest + '\0' + encoded).encode()).hexdigest()
        if prepared.digest != expected:
            raise ToolGateError('Tool arguments changed')

    async def verify(self, prepared: PreparedCall, *, relevance) -> VerifiedCall:
        """Owner validity precedes independent LLM relevance/grounding acceptance."""
        self._check(prepared)
        async with component_timeout('Tool verification', self.timeout):
            await self.owner.validate(prepared)
            verdict = relevance(prepared)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            if (type(verdict) is not dict or verdict.get('relevant') is not True
                    or verdict.get('grounded') is not True):
                raise ToolGateError('Tool candidate failed relevance or grounding')
            self._check(prepared)
            grant = VerifiedCall(prepared, self._id)
            self._grants.add(prepared.digest)
            return grant

    async def execute(self, verified: VerifiedCall) -> ToolResult:
        if not isinstance(verified, VerifiedCall) or verified.gate_id != self._id:
            raise ToolGateError('Tool verification is missing')
        prepared = verified.prepared
        self._check(prepared)
        if prepared.digest not in self._grants or self._used:
            raise ToolGateError('Tool verification is stale or already used')
        self._used = True
        async with component_timeout('Tool execution', self.timeout):
            result = await self.owner.invoke(prepared)
            if not isinstance(result, ToolResult):
                raise ToolGateError('Tool owner returned invalid result')
            if result.status != 'failure' and not await async_isolated_valid_schema(self._spec.output_schema, result.data):
                raise ToolGateError('Tool result does not match the selected schema')
            return result
