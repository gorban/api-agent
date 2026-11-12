"""Chromium installs as its own prerequisite step, before the bounded attempt that launches it."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.runtime import frontend, managed_browser, view_flow
from api_agent.runtime.local_run import LocalRunOwner
from api_agent.testing import allure_viewer
from api_agent.testing.requirements import IntegrationRequirementsPlan


def headed(preparations):
    return [options['headed'] for options in preparations]


@pytest.fixture
def local_start(tmp_path, monkeypatch):
    """Start a default local run whose prerequisites succeed and whose bounded preparation stops it."""
    from api_agent.runtime import local_run

    async def frontend_ready():
        return None

    def bounded_preparation(*args, **kwargs):
        raise OSError('stop at the bounded preparation')
    monkeypatch.setattr(frontend, 'prepare_source_frontend', frontend_ready)
    monkeypatch.setattr(view_flow, 'prepare_view_tools', lambda context: None)
    monkeypatch.setattr(local_run, 'component_timeout', bounded_preparation)
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)

    def start(values, **options):
        owner = LocalRunOwner(context, lambda: SimpleNamespace(errors=(), missing=(), values=values), **options)
        with pytest.raises(OSError, match='stop at the bounded preparation'):
            asyncio.run(owner.start())
    return start


@pytest.mark.parametrize('values', [{'RUN_UI': True}, {'DEBUG': True}])
def test_default_local_run_prepares_the_browser_before_its_bounded_preparation(
        local_start, chromium_preparations, values):
    local_start(values)

    assert headed(chromium_preparations) == [True]


def test_local_run_without_a_browser_to_open_prepares_none(local_start, chromium_preparations):
    local_start({})

    assert chromium_preparations == []


def test_local_run_with_its_own_browser_factory_leaves_browser_readiness_to_it(local_start, chromium_preparations):
    local_start({'RUN_UI': True}, browser_factory=lambda *args, **kwargs: None)

    assert chromium_preparations == []


def test_allure_viewer_installs_the_managed_browser_once_before_any_operation(chromium_preparations):
    viewer = allure_viewer.ManagedAllureViewer()

    async def scenario():
        await viewer.prepare_browser()
        await viewer.prepare_browser()
    asyncio.run(scenario())

    assert headed(chromium_preparations) == [True]


def test_allure_viewer_with_its_own_browser_factory_leaves_browser_readiness_to_it(chromium_preparations):
    viewer = allure_viewer.ManagedAllureViewer(browser_factory=lambda: None)

    asyncio.run(viewer.prepare_browser())

    assert chromium_preparations == []


def test_allure_history_preparation_prepares_the_viewer_browser(tmp_path):
    from api_agent.runtime.allure_history import AllureHistoryOwner
    prepared = []

    class Viewer:
        async def prepare_browser(self):
            prepared.append(True)
    owner = AllureHistoryOwner()
    owner.session = SimpleNamespace(result=lambda: SimpleNamespace(values={}))
    owner.allure_viewer = Viewer()

    asyncio.run(owner.prepare_allure_reporting())

    assert prepared == [True]


@pytest.mark.parametrize('ui, owned_ui, required', [
    (False, False, False), (True, False, True), (False, True, True), (True, True, True)])
def test_integration_plan_installs_the_browser_only_for_ui_sessions(chromium_preparations, ui, owned_ui, required):
    plan = IntegrationRequirementsPlan(False, requires_ui_session=ui, requires_owned_ui_session=owned_ui)

    plan.prepare_browser()

    assert plan.requires_browser is required
    assert headed(chromium_preparations) == ([False, True] if required else [])


def test_integration_plan_forwards_cancellation_to_the_browser_install(chromium_preparations):
    from threading import Event
    cancellation = Event()
    plan = IntegrationRequirementsPlan(False, requires_ui_session=True)

    plan.prepare_browser(cancellation=cancellation)
    cancellation.set()

    assert chromium_preparations[0]['cancelled']() is True
