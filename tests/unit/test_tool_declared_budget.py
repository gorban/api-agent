"""Tool calls run inside the running node's declared budget; they never add a hidden one."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.api.application import Runtime
from api_agent.providers.progress import component_timeout, progress_sink
from api_agent.providers.tools import ToolResult
from api_agent.tooling.runtime import OwnerTools, invocation_tools, node_tools
from api_agent.workflows.configuration import declared_timeouts
from api_agent.workflows.definition import ToolUsage
from test_tool_gate import Owner
from tool_operations_support import build, compose

RELEVANT = lambda _: {'relevant': True, 'grounded': True}


class SlowOwner(Owner):
    def __init__(self, delay, describe_delay=0):
        super().__init__('slow')
        self.delay, self.describe_delay = delay, describe_delay

    async def describe(self):
        await asyncio.sleep(self.describe_delay)
        return await super().describe()

    async def invoke(self, prepared):
        await asyncio.sleep(self.delay)
        return ToolResult('success', {'source': 'slow'})


async def fixed_call(node='run'):
    """Prepare, verify and execute one fixed-operation call through the node's declared port."""
    prepared = await node_tools(node)['primary'].prepare({'query': 'one'})
    grant = await node_tools(node)['primary'].verify(prepared, relevance=RELEVANT)
    return await node_tools(node)['primary'].execute(grant)


def fixed_definition():
    return SimpleNamespace(tool_usages=(ToolUsage(
        'run', 'primary', Path('/tool.toml'), 'node', Path('/flow.md'), 1,
        ('prepare', 'verify', 'execute')),))


@pytest.fixture(autouse=True)
def instant_schema_verdicts(monkeypatch):
    # Budget assertions must not depend on how fast the isolated schema process starts.
    async def verdict(schema, value):
        return True
    monkeypatch.setattr('api_agent.workflows.tool_gate.async_isolated_schema_verdict', verdict)


@pytest.fixture
def progress():
    reports = []
    with progress_sink(reports.append):
        yield reports


def timeouts_of(reports, name):
    return {report.timeout for report in reports if report.name == name}


@pytest.mark.asyncio
async def test_fixed_operation_scopes_inherit_the_node_budget_and_add_none(progress):
    selected = fixed_definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, SlowOwner(0))}):
        async with component_timeout('Workflow run', 5):
            assert (await fixed_call()).status == 'success'
    assert timeouts_of(progress, 'Tool preparation') == {None}
    assert timeouts_of(progress, 'Tool verification') == {None}
    assert timeouts_of(progress, 'Tool execution') == {None}


@pytest.mark.asyncio
async def test_fixed_operation_call_is_bounded_only_by_the_node_deadline():
    selected = fixed_definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, SlowOwner(1))}):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with component_timeout('Workflow run', 0.2):
                await fixed_call()
        assert time.monotonic() - started < 0.9


@pytest.mark.asyncio
async def test_fixed_operation_call_outlasting_the_old_default_needs_no_declaration():
    selected = fixed_definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, SlowOwner(0.3))}):
        async with component_timeout('Workflow run', 5):
            assert (await fixed_call()).status == 'success'


async def started_operations(tmp_path, delay=0, **kwargs):
    entry = build(tmp_path, 'older', **kwargs)
    (tmp_path / 'tool' / 'factory.py').write_text(
        'import asyncio\n\n\nclass Owner:\n    version = 1\n\n'
        '    async def invoke(self, operation, arguments):\n'
        f'        await asyncio.sleep({delay})\n        return {{"operation": operation}}\n\n'
        '    async def aclose(self):\n        pass\n\n\n'
        'def create_tool(local_config, services):\n    return Owner()\n')
    plan = compose(tmp_path, entry)
    runtime = Runtime('demo', tool_plan=plan, services={'events': []})
    await runtime.astart()
    return runtime, plan.definitions[0]


@pytest.mark.asyncio
async def test_operations_scope_inherits_the_node_budget_and_add_none(tmp_path, progress):
    runtime, definition = await started_operations(tmp_path)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
            async with component_timeout('Workflow reader', 5):
                await node_tools('reader')['lookup'].query()
    finally:
        await runtime.aclose()
    assert timeouts_of(progress, 'Tool execution') == {None}


@pytest.mark.asyncio
async def test_operations_call_is_bounded_only_by_the_node_deadline(tmp_path):
    runtime, definition = await started_operations(tmp_path, delay=1)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                async with component_timeout('Workflow reader', 0.2):
                    await node_tools('reader')['lookup'].query()
            assert time.monotonic() - started < 0.9
    finally:
        await runtime.aclose()


PORT_ROWS = [('reader.lookup', 'READER_TOOL_TIMEOUT', 0.2)]


@pytest.mark.asyncio
async def test_operations_port_row_caps_the_call_below_the_node_budget(tmp_path, progress):
    runtime, definition = await started_operations(tmp_path, delay=1, timeouts=PORT_ROWS)
    values = declared_timeouts(definition)
    assert values == {'reader.lookup': 0.2}
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}, timeouts=values):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                async with component_timeout('Workflow reader', 5):
                    await node_tools('reader')['lookup'].query()
            assert time.monotonic() - started < 0.9
    finally:
        await runtime.aclose()
    assert timeouts_of(progress, 'Tool execution') == {0.2}


@pytest.mark.asyncio
async def test_node_budget_still_clips_a_longer_port_row(tmp_path, progress):
    runtime, definition = await started_operations(tmp_path, delay=1, timeouts=[('reader.lookup', 'READER_TOOL_TIMEOUT', 30)])
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)},
                                    timeouts=declared_timeouts(definition)):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                async with component_timeout('Workflow reader', 0.2):
                    await node_tools('reader')['lookup'].query()
            assert time.monotonic() - started < 0.9
    finally:
        await runtime.aclose()
    clipped = timeouts_of(progress, 'Tool execution')
    assert clipped and max(clipped) <= 0.2


@pytest.mark.asyncio
async def test_port_row_applies_only_to_its_own_port(tmp_path, progress):
    runtime, definition = await started_operations(tmp_path, timeouts=PORT_ROWS)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)},
                                    timeouts=declared_timeouts(definition)):
            await node_tools('writer')['store'].mutate()
    finally:
        await runtime.aclose()
    assert timeouts_of(progress, 'Tool execution') == {None}


@pytest.mark.asyncio
async def test_port_row_resolves_from_configuration_and_may_be_omitted(tmp_path):
    definition = (await started_operations(tmp_path, timeouts=PORT_ROWS))[1]
    assert declared_timeouts(definition, {'READER_TOOL_TIMEOUT': 3}) == {'reader.lookup': 3}


FIXED = ('run', 'primary')


@pytest.mark.asyncio
async def test_fixed_operation_port_row_caps_preparation_and_execution(progress):
    selected = fixed_definition()
    for owner in (SlowOwner(1), SlowOwner(0, describe_delay=1)):
        async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)},
                                    timeouts={'run.primary': 0.2}):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                async with component_timeout('Workflow run', 5):
                    await fixed_call()
            assert time.monotonic() - started < 0.9
    assert timeouts_of(progress, 'Tool preparation') >= {0.2}
    assert timeouts_of(progress, 'Tool execution') == {0.2}


@pytest.mark.asyncio
async def test_fixed_operation_verification_is_not_capped_by_the_port_row(progress):
    selected = fixed_definition()
    async def slow_relevance(_):
        await asyncio.sleep(0.4)
        return {'relevant': True, 'grounded': True}
    async with invocation_tools(selected, {'tools': OwnerTools(selected, SlowOwner(0))},
                                timeouts={'run.primary': 0.2}):
        prepared = await node_tools('run')['primary'].prepare({'query': 'one'})
        await node_tools('run')['primary'].verify(prepared, relevance=slow_relevance)
    assert timeouts_of(progress, 'Tool verification') == {None}


@pytest.mark.asyncio
async def test_legacy_port_call_honors_its_row_and_otherwise_inherits(progress):
    from api_agent.workflows.execution import NodeContext
    selected = SimpleNamespace(definition_id='d', tool_usages=(ToolUsage(
        'run', 'primary', Path('/tool.toml'), 'node', Path('/flow.md'), 1),))
    for delay, timeouts, expected in ((0, {}, {None}), (1, {'run.primary': 0.2}, {0.2})):
        async def invoke(prepared, delay=delay):
            await asyncio.sleep(delay)
            return 'done'
        resolver = SimpleNamespace(definition=selected, for_node=lambda node: SimpleNamespace(
            borrow=lambda port: SimpleNamespace(invoke=invoke)))
        progress.clear()
        async with invocation_tools(selected, {'tools': resolver}, timeouts=timeouts):
            tool = NodeContext('run', (), (), {}).tool('primary')
            if delay:
                with pytest.raises(TimeoutError):
                    await tool.invoke('request')
            else:
                assert await tool.invoke('request') == 'done'
        assert timeouts_of(progress, 'Tool execution') == expected
