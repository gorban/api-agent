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
from api_agent.workflows.definition import ToolUsage
from test_tool_gate import Owner
from tool_operations_support import build, compose

RELEVANT = lambda _: {'relevant': True, 'grounded': True}


class SlowOwner(Owner):
    def __init__(self, delay):
        super().__init__('slow')
        self.delay = delay

    async def invoke(self, prepared):
        await asyncio.sleep(self.delay)
        return ToolResult('success', {'source': 'slow'})


async def fixed_call(tools, node='run'):
    """Prepare, verify and execute one fixed-operation call through the node's declared port."""
    prepared = await node_tools(node)['primary'].prepare({'query': 'one'})
    grant = await node_tools(node)['primary'].verify(prepared, relevance=RELEVANT)
    return await node_tools(node)['primary'].execute(grant)


def fixed_definition():
    return SimpleNamespace(tool_usages=(ToolUsage(
        'run', 'primary', Path('/tool.toml'), 'node', Path('/flow.md'), 1,
        ('prepare', 'verify', 'execute')),))


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
            assert (await fixed_call(None)).status == 'success'
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
                await fixed_call(None)
        assert time.monotonic() - started < 0.9


@pytest.mark.asyncio
async def test_fixed_operation_call_outlasting_the_old_default_needs_no_declaration():
    selected = fixed_definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, SlowOwner(0.3))}):
        async with component_timeout('Workflow run', 5):
            assert (await fixed_call(None)).status == 'success'


async def started_operations(tmp_path, delay=0):
    entry = build(tmp_path, 'older')
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
