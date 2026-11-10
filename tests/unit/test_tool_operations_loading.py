"""operations@1 owners are validated at load and borrow time, without the verified-read methods."""
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.providers.contracts import ContractError
from api_agent.tooling.admission import capture_tool_factory
from api_agent.tooling.operations import OPERATIONS_INTERFACE
from api_agent.tooling.runtime import OwnerTools, invocation_tools, node_tools
from api_agent.workflows.definition import ToolUsage

OWNER = '''class Owner:
    version = 1
    async def invoke(self, operation, arguments): return operation
    async def aclose(self): pass
def create_tool(local_config, services): return Owner()
'''


@dataclass(frozen=True)
class Selected:
    id: str
    admission: object
    local_values: object = field(default_factory=dict)
    interface: str = OPERATIONS_INTERFACE
    operations: object = field(default_factory=lambda: {'run': 'read'})
    pending: object = ()
    service_bindings: object = ()
    service_capabilities: object = ()
    executables: object = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    tools: tuple
    bindings: object = field(default_factory=dict)
    definitions: tuple = ()


def admit(tmp_path, source):
    (tmp_path / 'factory.py').write_text(source)
    return capture_tool_factory(tmp_path / 'factory.py', 'create_tool', tmp_path, OPERATIONS_INTERFACE)


def runtime(tmp_path, source):
    return Runtime('demo', tool_plan=Plan((Selected('demo/tool', admit(tmp_path, source)),)))


@pytest.mark.asyncio
async def test_minimal_owner_loads_without_verified_read_methods(tmp_path):
    loaded = runtime(tmp_path, OWNER)
    await loaded.astart()
    try:
        assert await loaded.borrow_tool('demo/tool').call('run', {}) == 'run'
    finally:
        await loaded.aclose()


@pytest.mark.asyncio
async def test_optional_async_start_runs(tmp_path):
    source = OWNER.replace('    async def aclose', '    async def astart(self): self.started = True\n    async def aclose')
    started = runtime(tmp_path, source)
    await started.astart()
    try:
        assert started.tool_owners['demo/tool'].implementation.started is True
    finally:
        await started.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('old, new, code', [
    ('version = 1', 'version = 2', 'tool_version_invalid'),
    ('version = 1', 'version = "1"', 'tool_version_invalid'),
    ('async def invoke', 'def invoke', 'tool_callable_invalid'),
    ('(self, operation, arguments)', '(self)', 'tool_callable_invalid'),
    ('async def aclose', 'def aclose', 'tool_callable_invalid'),
    ('    async def aclose', '    def astart(self): pass\n    async def aclose', 'tool_callable_invalid'),
    ('    async def aclose', '    async def astart(self, extra): pass\n    async def aclose', 'tool_callable_invalid'),
])
async def test_invalid_operations_owner_is_rejected_at_load(tmp_path, old, new, code):
    failing = runtime(tmp_path, OWNER.replace(old, new))

    with pytest.raises(RuntimeLifecycleError) as error:
        await failing.astart()

    assert str(error.value.primary_failure) == code


@pytest.mark.asyncio
async def test_missing_owner_is_rejected_at_load(tmp_path):
    failing = runtime(tmp_path, OWNER.replace('return Owner()', 'return None'))

    with pytest.raises(RuntimeLifecycleError) as error:
        await failing.astart()

    assert str(error.value.primary_failure) == 'tool_version_invalid'


@pytest.mark.asyncio
async def test_borrowed_view_needs_only_invoke_and_aclose(tmp_path):
    started = runtime(tmp_path, OWNER + '''
class View:
    async def invoke(self, operation, arguments): return 'view:' + operation
    async def aclose(self): pass
Owner.for_invocation = lambda self: View()
''')
    await started.astart()
    try:
        assert await started.borrow_tool('demo/tool').call('run', {}) == 'view:run'
    finally:
        await started.aclose()


@pytest.mark.asyncio
async def test_borrowed_view_without_invoke_is_rejected(tmp_path):
    started = runtime(tmp_path, OWNER + '''
class View:
    async def aclose(self): pass
Owner.for_invocation = lambda self: View()
''')
    await started.astart()
    try:
        with pytest.raises(RuntimeLifecycleError, match='^runtime_tool_view_invalid$'):
            started.borrow_tool('demo/tool')
    finally:
        await started.aclose()


@pytest.mark.asyncio
async def test_fixed_vocabulary_ports_reject_implementation_style_names():
    class Owner:
        calls = []
    usage = ToolUsage('plan', 'reader', Path('/tool.toml'), 'node', Path('/flow.md'), 1, ('introspect',))
    selected = SimpleNamespace(tool_usages=(usage,))
    async with invocation_tools(selected, {'tools': OwnerTools(selected, Owner())}):
        with pytest.raises(ContractError, match='not supported by the selected interface'):
            await node_tools('plan')['reader'].introspect()
