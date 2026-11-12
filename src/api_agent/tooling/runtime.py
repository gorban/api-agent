"""Borrow admitted named tools from the operation Runtime."""
import asyncio
import inspect

from api_agent.providers.loading import _task
from api_agent.providers.contracts import ContractError
from api_agent.providers.sessions import _Owner, _drain
from api_agent.providers.control import finish_cleanup
from api_agent.workflows.tool_gate import ToolGate, ToolGateError, VerifiedCall
from api_agent.providers.tools import PreparedCall
from api_agent.providers.progress import component_timeout
from .operations import FIXED_IDEMPOTENT, FIXED_OPERATIONS, OPERATIONS_INTERFACE


class BorrowedTool:
    def __init__(self, runtime, owner, view):
        self.runtime, self.owner, self.view = runtime, owner, view
        self.closed = False
        self._closing = False
        self._close_task = None
        self._rejected_awaitable = None
        self._tasks, self._cancel_requested = set(), set()

    def _check(self):
        self.runtime._context()
        if self.closed or self._closing or self.runtime.draining:
            raise ContractError('Tool invocation is closed')

    async def _run(self, operation):
        self._check()
        task = _task(operation())
        self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.discard(task)

    async def describe(self):
        return await self._run(self.view.describe)

    async def validate(self, prepared):
        return await self._run(lambda: self.view.validate(prepared))

    async def invoke(self, prepared):
        return await self._run(lambda: self.view.invoke(prepared))

    async def call(self, operation, arguments):
        """Invoke one named operation on an operations@1 view."""
        return await self._run(lambda: self.view.invoke(operation, arguments))

    async def _close_owned(self):
        from api_agent.providers.sessions import _drain
        await _drain(self._tasks, self.runtime.timeout, self._cancel_requested)
        if self.view is not self.owner:
            close = getattr(self.view, 'aclose', None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
            elif inspect.iscoroutine(self.view):
                self.view.close()
            elif inspect.isawaitable(self.view):
                from api_agent.providers.loading import _ReturnedAwaitable
                if self._rejected_awaitable is None:
                    self._rejected_awaitable = _ReturnedAwaitable(self.view, '')
                await self._rejected_awaitable.aclose()
        self.closed = True
        self.runtime._tool_views[:] = [view for view in self.runtime._tool_views if view is not self]

    async def aclose(self):
        if self.closed:
            return
        self._closing = True
        if self._close_task is None or self._close_task.done() and not self.closed:
            self._close_task = _task(self._close_owned())
        finished, _ = await asyncio.wait({self._close_task}, timeout=self.runtime.timeout)
        if not finished:
            from api_agent.providers.control import CleanupPendingError
            raise CleanupPendingError((self,), self.aclose)
        await asyncio.shield(self._close_task)


class WorkflowTools:
    """Admitted tool bindings of one workflow; only the engine resolves ports, per running node."""
    def __init__(self, runtime, definition, *, endpoints=None):
        admitted = next((item for item in runtime.tool_plan.definitions
                         if item.definition_id == definition.definition_id), None)
        if admitted is None:
            raise ContractError('Workflow definition has no admitted tool plan')
        self._runtime, self.definition = runtime, admitted
        self.endpoints = endpoints or {}

    def for_node(self, node):
        step = next((item for item in self.definition.layout.steps if item.id == node), None)
        if step is None or step.kind != 'node':
            raise ContractError('Selected workflow tool scope is unavailable')
        return NodeResolver(self._runtime, self.definition, node)


class NodeResolver:
    """Ports of one declared node; the node is fixed at creation, never by the caller."""
    def __init__(self, runtime, definition, node):
        self._runtime, self._definition, self._node = runtime, definition, node

    def instance(self, port):
        definition, bindings = self._definition, self._runtime.tool_plan.bindings
        steps = {step.id: step for step in definition.layout.steps}
        key = (definition.definition_id, 'node', self._node, port)
        if key in bindings:
            return bindings[key]
        current = steps[self._node]
        while current.parent is not None:
            key = (definition.definition_id, 'component', current.parent, port)
            if key in bindings:
                return bindings[key]
            current = steps[current.parent]
        key = (definition.definition_id, 'graph', definition.graph_id, port)
        if key not in bindings:
            raise ContractError('Selected workflow tool port is unavailable')
        return bindings[key]

    def borrow(self, port):
        """A raw view of a fixed-operation tool; implementation-declared operations stay gated."""
        instance = self.instance(port)
        if any(item.id == instance and item.interface == OPERATIONS_INTERFACE
               for item in self._runtime.tool_plan.tools):
            raise ContractError('Implementation-declared operations are reached through context.tools[port]')
        return self._runtime.borrow_tool(instance)

    def gate(self, port, *, invocation_id, timeout=None):
        instance = self.instance(port)
        borrowed = self._runtime.borrow_tool(instance)
        tool = next(item for item in self._runtime.tool_plan.tools if item.id == instance)
        if tool.interface == OPERATIONS_INTERFACE:
            return OperationsGate(borrowed, tool.operations, timeout=timeout, tool_alias=port,
                                  tool_instance=instance)
        return ToolGate(borrowed, invocation_id=invocation_id, timeout=timeout, tool_alias=port,
                        tool_instance=instance)


class OperationsGate:
    """One borrowed operations@1 view; the implementation's declared operations bound its calls."""
    def __init__(self, owner, operations, *, timeout=None, tool_alias=None, tool_instance=None):
        self.owner, self.operations, self.timeout = owner, operations, timeout
        self.tool_alias, self.tool_instance = tool_alias, tool_instance

    async def invoke(self, operation, arguments):
        if operation not in self.operations:
            raise ContractError('Tool operation is not declared by its implementation')
        async with component_timeout('Tool execution', self.timeout):
            if not self.operations[operation]:
                from api_agent.workflows.execution import mark_active_effect
                mark_active_effect()
            return await self.owner.call(operation, arguments)


# Invocation state follows async task context, never an executor or operation owner.
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from uuid import uuid4

_CURRENT_TOOLS = ContextVar('workflow_node_tools', default=None)
_ADMITTED_TOOLS = ContextVar('workflow_admitted_tools', default=None)


class NodeTools(Mapping):
    """Capabilities explicitly consumed by one node in the captured declaration."""
    def __init__(self, invocation, node):
        self._invocation, self._node = invocation, node
        self._uses = {use.port: use for use in getattr(invocation.definition, 'tool_usages', ())
                      if use.scope_kind == 'node' and use.scope == node and use.operations}

    def __iter__(self):
        return iter(self._uses)

    def __len__(self):
        return len(self._uses)

    def __getitem__(self, port):
        self._invocation.check()
        if port not in self._uses:
            raise ContractError('Node tool port is not declared')
        return NodeTool(self._invocation, self._uses[port])


class NodeTool:
    """A borrowed operation view; disposal remains with the invocation."""
    def __init__(self, invocation, use):
        self._invocation, self._use = invocation, use

    def __getattr__(self, operation):
        if operation not in self._use.operations:
            raise ContractError('Node tool operation is not declared')
        async def call(*args, **kwargs):
            return await self._invocation.dispatch(self._use, operation, *args, **kwargs)
        return call


def _matches(resolver, definition):
    admitted = getattr(resolver, 'definition', None)
    return admitted is definition or (
        admitted is not None and getattr(admitted, 'definition_id', None) is not None
        and admitted.definition_id == getattr(definition, 'definition_id', None))


class ToolInvocation:
    def __init__(self, definition, resolver, timeouts=None):
        self.definition, self.resolver = definition, resolver
        # Resolved Timeouts values by owner; ``<node>.<port>`` owners bound that port's calls.
        self.timeouts = timeouts or {}
        if resolver is not None and not _matches(resolver, definition):
            raise ContractError('Tool resolver does not match workflow definition')
        self.id, self.closed, self.gates = str(uuid4()), False, {}
        self.calls = {}
        self.effects = []

    def check(self):
        if self.closed:
            raise ContractError('Tool invocation is closed')

    def timeout(self, use):
        return self.timeouts.get(f'{use.scope}.{use.port}')

    def gate(self, use):
        self.check()
        resolver = self.resolver.for_node(use.scope)
        instance = resolver.instance(use.port)
        if instance not in self.gates:
            self.gates[instance] = resolver.gate(use.port, invocation_id=self.id, timeout=self.timeout(use))
        return instance, self.gates[instance]

    async def dispatch(self, use, operation, /, *args, **kwargs):
        instance, base = self.gate(use)
        if isinstance(base, OperationsGate):
            if args:
                raise ContractError('Tool operations accept keyword arguments only')
            if base.operations.get(operation) is False:
                self.effects.append(use.scope)
            return await base.invoke(operation, kwargs)
        if operation not in FIXED_OPERATIONS:
            raise ContractError('Tool operation is not supported by the selected interface')
        # Each selected instance has one borrowed view, while each preparation
        # retains its own existing ToolGate state until invocation disposal.
        calls = self.calls.setdefault(instance, {})
        if operation == 'describe':
            return await base.describe(*args, **kwargs)
        if operation == 'prepare':
            async with component_timeout('Tool preparation', base.timeout):
                gate = await base._for_call()
                prepared = await gate.prepare(*args, **kwargs)
                calls[prepared.digest] = gate
                return prepared
        candidate = args[0] if args else kwargs.get('prepared' if operation == 'verify' else 'verified')
        if operation == 'execute':
            if not isinstance(candidate, VerifiedCall):
                raise ToolGateError('Tool verification is missing')
            candidate = candidate.prepared
        if (not isinstance(candidate, PreparedCall) or type(candidate.digest) is not str
                or candidate.digest not in calls):
            raise ToolGateError('Tool verification is missing')
        gate = calls[candidate.digest]
        if operation == 'execute':
            if operation not in FIXED_IDEMPOTENT:
                self.effects.append(use.scope)
            kwargs['_tool_alias'] = use.port
        return await getattr(gate, operation)(*args, **kwargs)

    async def aclose(self):
        self.closed = True
        # All views are attempted even when one cleanup requires a retry. Runtime
        # retains failed borrowed views for its existing retryable owner cleanup.
        errors = []
        for gate in self.gates.values():
            try:
                await gate.owner.aclose()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise errors[0]


def capture_node_tools(node):
    invocation = _CURRENT_TOOLS.get()
    return None if invocation is None else NodeTools(invocation, node)


def node_tools(node):
    invocation = _CURRENT_TOOLS.get()
    if invocation is None:
        raise ContractError('Node tools require an active workflow invocation')
    return NodeTools(invocation, node)


@contextmanager
def bind_workflow_tools(resolver):
    """Install the admitted resolver for workflows run here; applications never receive it."""
    token = _ADMITTED_TOOLS.set(resolver)
    try:
        yield
    finally:
        _ADMITTED_TOOLS.reset(token)


def _require_bindings(definition, resolver, *, other_definition):
    """Resolve every declared node port before the workflow's first node can spend inference."""
    for use in getattr(definition, 'tool_usages', ()):
        if use.scope_kind != 'node':
            continue
        where = f'Workflow node {use.scope} port {use.port} has no admitted tool binding: '
        if resolver is None:
            reason = ('the bound tools belong to another workflow definition' if other_definition
                      else 'no tools are bound')
            raise ContractError(where + reason + "; bind this definition's tools with "
                                "bind_workflow_tools() or services['tools']")
        try:
            resolver.for_node(use.scope).instance(use.port)
        except ContractError as error:
            raise ContractError(where + str(error)) from None


@asynccontextmanager
async def invocation_tools(definition, services, *, timeouts=None):
    admitted, supplied = _ADMITTED_TOOLS.get(), services.get('tools')
    other_definition = admitted is not None and not _matches(admitted, definition)
    if other_definition:
        admitted = None
    if admitted is not None and supplied is not None:
        raise ContractError('Workflow tools are supplied by the engine, not the application')
    resolver = admitted if admitted is not None else supplied
    invocation = ToolInvocation(definition, resolver, timeouts)
    _require_bindings(definition, resolver, other_definition=other_definition)
    token = _CURRENT_TOOLS.set(invocation)
    try:
        yield invocation
    finally:
        try:
            await finish_cleanup(invocation.aclose())
        finally:
            _CURRENT_TOOLS.reset(token)


class OwnerTools:
    """Compatibility adapter for an application supplied single tool owner."""
    def __init__(self, definition, owner):
        instances = {use.instance for use in definition.tool_usages}
        if len(instances) != 1:
            raise ContractError('A single owner requires exactly one declared tool instance')
        self.definition = definition
        self._instance = next(iter(instances))
        self.owner = owner

    def for_node(self, node):
        return self

    def instance(self, port=None):
        import tomllib
        body = getattr(self.definition, 'contents', {}).get(self._instance)
        return tomllib.loads(body.decode('utf-8'))['id'] if body is not None else str(self._instance)

    def gate(self, port, *, invocation_id, timeout=None):
        factory = getattr(self.owner, 'for_invocation', None)
        if factory is not None and (not callable(factory) or inspect.iscoroutinefunction(factory)):
            raise ContractError('Tool invocation factory must be synchronous')
        view = factory() if factory is not None else self.owner
        # A direct owner is borrowed and must never be disposed by a node turn.
        return ToolGate(_OwnerView(view, self.owner), invocation_id=invocation_id, timeout=timeout,
                        tool_alias=port, tool_instance=self.instance())


class _OwnerView(_Owner):
    def __init__(self, view, owner, timeout=30):
        super().__init__(timeout)
        self._view, self._owner = view, owner
        self._tasks = set()
        self._valid = all(inspect.iscoroutinefunction(getattr(view, name, None))
                          for name in ('describe', 'validate', 'invoke', 'aclose'))
        if view is not owner:
            if inspect.iscoroutine(view):
                view.close()
            elif inspect.isawaitable(view):
                from api_agent.providers.loading import _ReturnedAwaitable
                self._resources.append((_ReturnedAwaitable(view, ''), 'aclose'))
            elif callable(getattr(view, 'aclose', None)):
                self._resources.append((view, 'aclose'))

    async def _run(self, operation):
        self._check()
        if not self._valid:
            raise ContractError('Tool invocation view requires async tool methods')
        task = _task(operation())
        self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.discard(task)

    async def describe(self):
        return await self._run(lambda: self._view.describe())

    async def validate(self, prepared):
        return await self._run(lambda: self._view.validate(prepared))

    async def invoke(self, prepared):
        return await self._run(lambda: self._view.invoke(prepared))

    async def _dispose(self):
        await _drain(self._tasks, self._timeout, self._cancel_requested)
        await self._release()
