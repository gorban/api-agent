"""Eligibility is configuration-only; runtime unavailability cannot become skip."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing.provider_setup import IntegrationRequirements
from api_agent.providers.contracts import ProviderAccessError
from api_agent.llm_providers.gpt import factory


def module():
    root=Path(__file__).parents[1]/'integration'
    sys.path.insert(0,str(root))
    try:
        spec=importlib.util.spec_from_file_location('selection_contract',root/'test_studio_selection.py')
        result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
        return result
    finally: sys.path.remove(str(root))


@pytest.mark.parametrize('settings', [
    {}, {'GPT_AUTH_MODE':'subscription','GPT_API_MODE':'responses','GPT_BASE_URL':'https://ignored.example/v1'},
    {'GPT_AUTH_MODE':'api_key','GPT_API_MODE':'responses'},
    {'GPT_AUTH_MODE':'api_key','GPT_API_MODE':'completions'},
])
def test_supported_configuration_requires_no_provider_setup(settings):
    values={'LLM_PROVIDER':str(Path(factory.__file__))+':factory','LLM_ALLOWED_MODELS':'gpt-.*',**settings}
    module().require_selection_configuration(IntegrationRequirements(values))


@pytest.mark.parametrize('settings', [
    {'LLM_PROVIDER':str(Path(factory.__file__))+':other'},
    {'LLM_PROVIDER':__file__+':factory'},
    {'GPT_AUTH_MODE':'subscription','GPT_API_MODE':'completions'},
    {'GPT_AUTH_MODE':'api_key','GPT_BASE_URL':'https://compatible.example/v1'},
    {'GPT_AUTH_MODE':'api_key','GPT_API_MODE':'other'},
    {'GPT_AUTH_MODE':'unknown'},
])
def test_unsupported_route_is_explicit_precondition(settings):
    values={'LLM_PROVIDER':str(Path(factory.__file__))+':factory','LLM_ALLOWED_MODELS':'gpt-.*',**settings}
    with pytest.raises(pytest.skip.Exception):
        module().require_selection_configuration(IntegrationRequirements(values))


@pytest.mark.parametrize('allowed',['gpt-6-sol','gpt-5.6-luna'])
def test_both_exact_models_must_be_allowed(allowed):
    with pytest.raises(ProviderAccessError,match='permitted'):
        module().require_selection_configuration(IntegrationRequirements({
            'LLM_PROVIDER':str(Path(factory.__file__))+':factory','LLM_ALLOWED_MODELS':allowed}))


@pytest.mark.parametrize('operation',['describe','complete'])
def test_runtime_skip_becomes_failure_with_original_cause(operation):
    selected=module().SelectionProvider(SimpleNamespace(
        describe=Mock(side_effect=pytest.skip.Exception('unavailable')),
        complete=Mock(side_effect=pytest.skip.Exception('unavailable'))))
    failure = None
    try:
        getattr(selected,operation)(*(() if operation=='describe' else (object(),)))
    except BaseException as error:
        failure = error
    assert type(failure) is AssertionError
    assert 'setup was unavailable' in str(failure)
    assert isinstance(failure.__cause__,pytest.skip.Exception)
    assert selected.calls == []


def test_configuration_fixture_checks_before_provider_dependency():
    selected=module()
    requirements=Mock()
    selected.require_selection_configuration=lambda received: requirements(received)
    selected.selection_configuration.__wrapped__(None,'config')
    requirements.assert_called_once_with('config')


@pytest.mark.parametrize('debug',[False,True])
def test_backend_wrapper_preserves_original_calls_and_retained_ownership(monkeypatch, debug):
    import asyncio
    from contextlib import asynccontextmanager
    from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
    from api_agent.testing import provider_setup, studio_retained
    selected=module()
    shared=sys.modules['test_studio_request']
    result=LLMResult(Message('assistant',(TextContent('Maybe'),)))
    provider=SimpleNamespace(describe=Mock(return_value=(
            AvailableSelection('gpt-6-sol',family='gpt-sol',version='6',reasoning_efforts=('medium',)),
            AvailableSelection('gpt-5.6-luna',family='gpt-luna',version='5.6',reasoning_efforts=('off',)))),
        complete=Mock(return_value=result), close=Mock())
    captured=[]
    @asynccontextmanager
    async def endpoint(owner,*args,**kwargs):
        captured.append(owner)
        if debug:
            assert await owner.describe() == provider.describe.return_value
            assert await owner.complete('request') is result
        else:
            assert owner.describe() == provider.describe.return_value
            assert owner.complete('request') is result
        try: yield 'http://localhost:12345'
        finally:
            if debug: await owner.aclose()
    monkeypatch.setattr(shared,'agent_endpoint',endpoint)
    monkeypatch.setattr(provider_setup,'_read_snapshot',lambda:{})
    monkeypatch.setattr(provider_setup,'_worker_manager_registry',lambda _:None)
    monkeypatch.setattr(studio_retained,'retained_provider_factory',lambda *args:lambda _:pytest.fail('manual owner created'))
    observed=selected.selection_catalog.__wrapped__(None,provider)
    backend=selected.ui_provider_backend.__wrapped__(observed)
    async def run():
        async with backend({'DEBUG':debug,'UI_LISTENER_URL':'http://localhost:9443','LLM_ALLOWED_MODELS':'gpt-.*'}) as values:
            assert values['agent_url']=='http://localhost:12345'
            assert len(backend.observed.calls)==1
        backend.retire_test()
        if debug: assert captured[0].phase=='closed'
        else: assert captured[0] is backend.observed
    asyncio.run(run())
    provider.close.assert_not_called()
    provider.complete.assert_called_once_with('request')


@pytest.mark.parametrize('variation', ['missing','duplicate','family','version','unknown_efforts','unsupported_effort'])
def test_catalog_preparation_requires_exact_advertised_selection(variation):
    from dataclasses import replace
    from api_agent.providers.llm import AvailableSelection
    selected=module()
    sol=AvailableSelection('gpt-6-sol',family='gpt-sol',version='6',reasoning_efforts=('medium',))
    luna=AvailableSelection('gpt-5.6-luna',family='gpt-luna',version='5.6',reasoning_efforts=('off',))
    rows={'missing':(sol,), 'duplicate':(sol,luna,luna),
        'family':(sol,replace(luna,family='other')), 'version':(sol,replace(luna,version='6')),
        'unknown_efforts':(sol,replace(luna,reasoning_efforts=None)),
        'unsupported_effort':(sol,replace(luna,reasoning_efforts=('low',)))}[variation]
    with pytest.raises(AssertionError,match='Required'):
        selected.selection_catalog.__wrapped__(None,SimpleNamespace(describe=lambda:rows))
