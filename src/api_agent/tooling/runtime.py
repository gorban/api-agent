"""Borrow admitted named tools from the operation Runtime."""
import asyncio
import inspect

from api_agent.providers.loading import _task
from api_agent.providers.contracts import ContractError
from api_agent.workflows.tool_gate import ToolGate


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
    """Resolve lexical port bindings for one admitted workflow definition."""
    def __init__(self, runtime, definition, *, endpoints=None):
        admitted = next((item for item in runtime.tool_plan.definitions
                         if item.definition_id == definition.definition_id), None)
        if admitted is None:
            raise ContractError('Workflow definition has no admitted tool plan')
        self.runtime, self.definition = runtime, admitted
        self.endpoints = endpoints or {}

    def instance(self, *, scope_kind, scope, port):
        definition, bindings = self.definition, self.runtime.tool_plan.bindings
        steps = {step.id: step for step in definition.layout.steps}
        if (scope_kind == 'graph' and scope != definition.graph_id
                or scope_kind != 'graph' and (scope not in steps or steps[scope].kind != scope_kind)):
            raise ContractError('Selected workflow tool scope is unavailable')
        key = (definition.definition_id, scope_kind, scope, port)
        if key in bindings:
            return bindings[key]
        current = steps.get(scope) if scope_kind != 'graph' else None
        while current is not None and current.parent is not None:
            key = (definition.definition_id, 'component', current.parent, port)
            if key in bindings:
                return bindings[key]
            current = steps[current.parent]
        key = (definition.definition_id, 'graph', definition.graph_id, port)
        if key not in bindings:
            raise ContractError('Selected workflow tool port is unavailable')
        return bindings[key]

    def for_port(self, *, scope_kind, scope, port):
        return self.runtime.borrow_tool(self.instance(scope_kind=scope_kind, scope=scope, port=port))

    def gate(self, *, scope_kind, scope, port, invocation_id, timeout=30):
        return ToolGate(self.for_port(scope_kind=scope_kind, scope=scope, port=port),
                        invocation_id=invocation_id, timeout=timeout)
