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


CHOICES = ('gpt-6-sol', 'gpt-5.6-luna', 'medium', 'off')
VALUES = dict(zip(('LLM_STUDIO_TEST_DEFAULT_MODEL', 'LLM_STUDIO_TEST_SELECTED_MODEL',
    'LLM_STUDIO_TEST_DEFAULT_EFFORT', 'LLM_STUDIO_TEST_SELECTED_EFFORT'), CHOICES))


def test_supported_configuration_requires_no_provider_setup():
    assert module().require_selection_configuration(IntegrationRequirements(VALUES)) == CHOICES


@pytest.mark.parametrize('edit', [
    {'LLM_STUDIO_TEST_SELECTED_MODEL':'gpt-6-sol'},
    {'LLM_STUDIO_TEST_SELECTED_EFFORT':'medium'},
    {'LLM_STUDIO_TEST_SELECTED_EFFORT':'unknown'},
    {'LLM_STUDIO_TEST_SELECTED_EFFORT':None},
])
def test_incomplete_choices_are_explicit_preconditions(edit):
    with pytest.raises(pytest.skip.Exception):
        module().require_selection_configuration(IntegrationRequirements(VALUES | edit))


@pytest.mark.parametrize('allowed',['gpt-6-sol','gpt-5.6-luna'])
def test_both_exact_models_must_be_allowed(allowed):
    with pytest.raises(ProviderAccessError,match='permitted'):
        module().require_selection_configuration(IntegrationRequirements(VALUES | {'LLM_ALLOWED_MODELS':allowed}))


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
            AvailableSelection('gpt-6-sol',family='gpt-sol',version='6',features=('reasoning_effort',),reasoning_efforts=('medium',)),
            AvailableSelection('gpt-5.6-luna',family='gpt-luna',version='5.6',features=('reasoning_effort',),reasoning_efforts=('off',)))),
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
    observed=selected.selection_catalog.__wrapped__(CHOICES,provider)
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


@pytest.mark.parametrize('variation', ['missing','duplicate','unknown_efforts','unsupported_effort'])
def test_catalog_preparation_requires_exact_advertised_selection(variation):
    from dataclasses import replace
    from api_agent.providers.llm import AvailableSelection
    selected=module()
    sol=AvailableSelection('gpt-6-sol',family='gpt-sol',version='6',features=('reasoning_effort',),reasoning_efforts=('medium',))
    luna=AvailableSelection('gpt-5.6-luna',family='gpt-luna',version='5.6',features=('reasoning_effort',),reasoning_efforts=('off',))
    rows={'missing':(sol,), 'duplicate':(sol,luna,luna),
        'family':(sol,replace(luna,family=None)), 'version':(sol,replace(luna,version=None)),
        'unknown_efforts':(sol,replace(luna,features=('reasoning_effort',),reasoning_efforts=None)),
        'unsupported_effort':(sol,replace(luna,features=('reasoning_effort',),reasoning_efforts=('low',)))}[variation]
    with pytest.raises(ValueError,match='Required'):
        selected.selection_catalog.__wrapped__(CHOICES,SimpleNamespace(describe=lambda:rows))


def test_non_gpt_catalog_supplies_family_version_and_controls():
    from api_agent.providers.llm import AvailableSelection
    selected = module()
    choices = ('vendor.alpha', 'vendor.beta', 'off', 'high')
    rows = (AvailableSelection('vendor.alpha', family='Alpha', version='2026', features=('reasoning_effort',),reasoning_efforts=('off',)),
        AvailableSelection('vendor.beta', family='Beta', version='2', features=('reasoning_effort',),reasoning_efforts=('high',)))
    observed = selected.selection_catalog.__wrapped__(choices, SimpleNamespace(describe=lambda:rows))
    assert observed.choices == choices
    assert observed.rows == rows


def test_catalog_accepts_distinct_models_with_same_family_and_unknown_versions():
    from api_agent.providers.llm import AvailableSelection
    rows = tuple(AvailableSelection(model, family='same', version=None, features=('reasoning_effort',),reasoning_efforts=('medium','off')) for model in CHOICES[:2])
    observed = module().selection_catalog.__wrapped__(CHOICES, SimpleNamespace(describe=lambda:rows))
    assert observed.rows == rows


@pytest.mark.parametrize('version, duplicate, label', [('2.7',False,'2.7'), (None,False,'Unknown version — vendor.beta'), ('2.7',True,'2.7 — vendor.beta')])
def test_prompt_uses_catalog_labels_and_actual_wire_selection(monkeypatch, version, duplicate, label):
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import AvailableSelection
    selected = module()
    choices = ('vendor.alpha','vendor.beta','off','high')
    wire = {'model':'vendor.beta','reasoning_effort':'high',
        'messages':[{'role':'user','content':'Respond with exactly: Maybe'}]}
    page = Mock()
    page.evaluate = AsyncMock(return_value={'default':'vendor.alpha','reasoning_default':'off'})
    controls = {}
    def locator(selector):
        if selector not in controls:
            control = Mock()
            control.click = AsyncMock()
            control.select_option = AsyncMock()
            control.get_attribute = AsyncMock(return_value='beta-family')
            control.filter.return_value = control
            controls[selector] = control
        return controls[selector]
    page.locator = locator
    outgoing = SimpleNamespace(post_data_json=wire)
    class Outgoing:
        async def __aenter__(self):
            future = asyncio.get_running_loop().create_future()
            future.set_result(outgoing)
            return SimpleNamespace(value=future)
        async def __aexit__(self, *args): pass
    def expect_request(predicate):
        assert predicate(SimpleNamespace(method='POST',url='https://local/app/agent/chat/stream'))
        assert not predicate(SimpleNamespace(method='GET',url='https://local/app/agent/chat/stream'))
        assert not predicate(SimpleNamespace(method='POST',url='https://local/other'))
        return Outgoing()
    page.expect_request = expect_request
    expectations = []
    def expect(control):
        result = SimpleNamespace(to_have_attribute=AsyncMock(),to_have_text=AsyncMock(),to_have_count=AsyncMock())
        expectations.append((control,result))
        return result
    import playwright.async_api
    monkeypatch.setattr(playwright.async_api,'expect',expect)
    monkeypatch.setattr(selected,'browser_prompt',AsyncMock(return_value='Maybe'))
    observed = SimpleNamespace(choices=choices,rows=(None,
        AvailableSelection('vendor.beta',family='Beta public',version=version)))
    observed.catalog = [observed.rows[1]]
    if duplicate:
        observed.catalog.append(AvailableSelection('vendor.gamma',family='Beta public',version=version))
    result = asyncio.run(selected.selection_prompt(SimpleNamespace(session=SimpleNamespace(page=page)), observed))
    assert result == wire
    controls['#model'].select_option.assert_awaited_once_with('beta-family')
    controls['#version'].select_option.assert_awaited_once_with('vendor.beta')
    controls['#reasoning'].select_option.assert_awaited_once_with('high')
    assert controls['#model option'].filter.call_args.kwargs['has_text'].fullmatch('Beta public')
    checked = [result for control,result in expectations if control is controls['#version option:checked']]
    checked[0].to_have_text.assert_awaited_once_with(label)


def test_final_evidence_checks_observed_provider_request(monkeypatch, capsys):
    import asyncio
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    selected = module()
    async def prompt(prepared, observed):
        return {'model':'vendor.beta','reasoning_effort':'high'}
    monkeypatch.setattr(selected, 'selection_prompt', prompt)
    result = LLMResult(Message('assistant',(TextContent('Maybe'),)), usage=Usage(1,2))
    observed = SimpleNamespace(choices=('vendor.alpha','vendor.beta','off','high'),
        rows=(SimpleNamespace(family='Alpha'),SimpleNamespace(version='2.7',family='Beta')),
        calls=[(SimpleNamespace(selected_llm='vendor.beta', payload=SimpleNamespace(
            generation=SimpleNamespace(reasoning_effort='high'))),result)])
    loop = asyncio.new_event_loop()
    try:
        selected.test_studio_selection(None,loop,SimpleNamespace(observed=observed))
    finally:
        loop.close()
    assert '"requested_version": "2.7"' in capsys.readouterr().out


def test_catalog_requires_family_for_ui_control():
    from api_agent.providers.llm import AvailableSelection
    rows = tuple(AvailableSelection(model, family=None, features=('reasoning_effort',),reasoning_efforts=('medium','off')) for model in CHOICES[:2])
    with pytest.raises(AssertionError, match='advertised model family'):
        module().selection_catalog.__wrapped__(CHOICES, SimpleNamespace(describe=lambda:rows))
