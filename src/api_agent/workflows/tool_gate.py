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
    def __init__(self, owner, *, invocation_id: str, timeout: float = 30,
                 tool_alias=None, tool_instance=None):
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
        self.tool_alias, self.tool_instance = tool_alias, tool_instance

    async def describe(self) -> ToolSpec:
        async with component_timeout('Tool preparation', self.timeout):
            spec = self._spec
            if spec is None:
                spec = await self.owner.describe()
                if not isinstance(spec, ToolSpec):
                    raise ToolGateError('Tool owner returned invalid specification')
                self._spec = spec
            return spec

    async def _for_call(self):
        """Borrow this view/specification for an independently single-use call."""
        spec = await self.describe()
        gate = ToolGate(self.owner, invocation_id=self.invocation_id, timeout=self.timeout,
                        tool_alias=self.tool_alias, tool_instance=self.tool_instance)
        gate._spec = spec
        return gate

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

    async def execute(self, verified: VerifiedCall, *, _tool_alias=None) -> ToolResult:
        if not isinstance(verified, VerifiedCall) or verified.gate_id != self._id:
            raise ToolGateError('Tool verification is missing')
        prepared = verified.prepared
        self._check(prepared)
        if prepared.digest not in self._grants or self._used:
            raise ToolGateError('Tool verification is stale or already used')
        self._used = True
        # Preserve the selected ToolSpec identity for owners, but never dispatch
        # caller-owned mutable arguments after asynchronous evidence admission.
        import asyncio
        import json
        arguments = json.loads(canonical(prepared.arguments, MAX_ARGUMENT_BYTES))
        dispatch = PreparedCall(prepared.spec, arguments, prepared.invocation_id, prepared.digest)
        from .tool_observations import ToolObservation, tool_payload_schema
        observation = ToolObservation(prepared.spec, alias=_tool_alias or self.tool_alias, instance=self.tool_instance)
        request_schema = {'type': 'object', 'properties': {
            'tool_name': {'type': 'string'}, 'tool_instance': {'type': ['string', 'null']},
            'arguments': tool_payload_schema(prepared.spec.input_schema, 'arguments')}, 'additionalProperties': False}
        def before_dispatch():
            # Run after credential enrollment and projection, while the same
            # lease still protects atomic tool-start/input retention.
            self._check(prepared)
            self._check(dispatch)
            if prepared.digest not in self._grants:
                raise ToolGateError('Tool verification is stale or already used')
        await observation.capture('input', {'tool_name': observation.alias,
            'tool_instance': observation.instance, 'arguments': arguments}, request_schema,
            before_dispatch=before_dispatch)
        outcome = 'failure'
        try:
            async with component_timeout('Tool execution', self.timeout):
                result = await self.owner.invoke(dispatch)
                if not isinstance(result, ToolResult):
                    raise ToolGateError('Tool owner returned invalid result')
                if result.status != 'failure' and not await async_isolated_valid_schema(self._spec.output_schema, result.data):
                    raise ToolGateError('Tool result does not match the selected schema')
            outcome = result.status
        except BaseException as error:
            import asyncio
            outcome = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failure'
            try:
                await observation.capture('output', {}, {'type': 'object'}, unavailable=True)
            except BaseException:
                observation.loss()
            observation.end(outcome)
            raise
        try:
            response_schema = {'type': 'object', 'properties': {
                'status': {'type': 'string'},
                'data': {} if result.status == 'failure' else tool_payload_schema(self._spec.output_schema, 'data'),
                'errors': {'type': 'array', 'items': {}},
                'diagnostic': {'type': ['string', 'null']}},
                'additionalProperties': False}
            await observation.capture('output',
                {'status': result.status, 'data': result.data, 'errors': list(result.errors),
                 'diagnostic': result.diagnostic}, response_schema)
        except asyncio.CancelledError:
            observation.loss()
            observation.end('cancelled')
            raise
        except Exception:
            observation.loss()
        observation.end(outcome)
        return result
