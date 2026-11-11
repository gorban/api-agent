"""Budgets inside a flow come from the declared Timeouts hierarchy, never from a hidden constant."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.progress import component_timeout, progress_sink
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.capture_authority import capture_node
from api_agent.workflows.execution import project_correction
from api_agent.workflows.journal import WorkflowJournal


def authority(delay=0):
    @asynccontextmanager
    async def capability():
        await asyncio.sleep(delay)
        yield EmissionBoundary(ContentPolicy(True))
    return capability


def reported(rows, name):
    return [row.timeout for row in rows if row.name == name]


def journal():
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    layout = SimpleNamespace(steps=[node], tables={'Edges': [], 'Functions': [{'ID': 'Make', 'Kind': 'deterministic'}]})
    resource = SimpleNamespace(kind='declaration', logical_path='flows/main.md.html', sha256='a' * 64,
                               content_bytes=b'# Workflow')
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base', layout=layout,
                                 resources=[resource], replacements=[])
    return WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True),
                           boundary=EmissionBoundary(ContentPolicy(True)))


def test_correction_authority_declares_no_budget_of_its_own():
    async def scenario():
        rows = []
        with progress_sink(rows.append):
            await project_correction({'reason': 'x'}, services={'capture_authority': authority()})
        return rows
    assert reported(asyncio.run(scenario()), 'Workflow correction authority') == [None, None]


def test_correction_authority_is_bounded_by_the_enclosing_declared_budget():
    async def scenario():
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with component_timeout('Declared node', 0.1):
                await project_correction({'reason': 'x'}, services={'capture_authority': authority(30)})
        return asyncio.get_running_loop().time() - started
    assert asyncio.run(scenario()) < 5


def test_capture_authority_declares_no_budget_of_its_own():
    async def scenario():
        rows = []
        with progress_sink(rows.append):
            await capture_node(journal(), {'capture_authority': authority()}, visit_id='visit',
                               direction='input', values={'text': 'x'}, schema={'type': 'object'})
        return rows
    assert reported(asyncio.run(scenario()), 'Workflow capture authority') == [None, None]


def test_capture_authority_is_bounded_by_the_enclosing_declared_budget():
    async def scenario():
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with component_timeout('Declared node', 0.1):
                await capture_node(journal(), {'capture_authority': authority(30)}, visit_id='visit',
                                   direction='input', values={'text': 'x'}, schema={'type': 'object'})
        return asyncio.get_running_loop().time() - started
    assert asyncio.run(scenario()) < 5


def test_gpt_provider_declares_no_hardcoded_inference_timeout():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    assert GPTProvider.inference_timeout is None


@pytest.mark.parametrize('declared', [None, 7])
def test_llm_inference_budget_is_the_declared_provider_value_or_inherited(declared):
    async def scenario():
        rows = []
        async def invoke(callback):
            return await callback(auth='selected')
        async def physical(request, *, auth):
            raise ProviderAccessError('unavailable', 'Synthetic failure')
        owner = SharedSetupOwner('op', 'selected', lambda _: SimpleNamespace(call=invoke, aclose=AsyncMock()),
                                 complete=physical, inference_timeout=declared)
        request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('test'),)),)))
        with progress_sink(rows.append), bind_transport_attempt_ledger(TransportAttemptLedger(5)):
            with pytest.raises(ProviderAccessError):
                await owner.complete(request)
        await owner.aclose()
        return rows
    assert set(reported(asyncio.run(scenario()), 'LLM inference')) == {declared}
