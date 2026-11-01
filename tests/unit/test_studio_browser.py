"""Studio submission waits for rendered responses and owned turn evidence."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.testing.studio_browser import browser_prompt

CONVERSATION = '11111111-1111-4111-8111-111111111111'
TURN = '22222222-2222-4222-8222-222222222222'


class Locator:
    def __init__(self, text='', value='', state='settled'):
        self.text = text
        self.value = value
        self.state = state
        self.last = self
        self.fill = AsyncMock()
        self.click = AsyncMock()
        self.inner_text = AsyncMock(side_effect=lambda: self.text)
        self.text_content = AsyncMock(side_effect=lambda: self.text)
        self.is_hidden = AsyncMock(return_value=True)


class Expectation:
    def __init__(self, locator):
        self.locator = locator

    async def to_have_attribute(self, name, value, *, timeout):
        assert name == 'data-catalog-state'
        assert timeout == 15000
        assert self.locator.state == value

    async def to_have_value(self, pattern):
        assert pattern.fullmatch(self.locator.value)

    async def to_be_enabled(self):
        assert self.locator is not None

    async def to_have_text(self, pattern, *, timeout):
        assert timeout == 120000
        assert pattern.fullmatch(self.locator.text)


class Submission:
    def __init__(self, request):
        self.request = request

    async def __aenter__(self):
        self.value = self.resolve()
        return self

    async def resolve(self):
        return self.request

    async def __aexit__(self, *args):
        return False


class Page:
    def __init__(self):
        self.nodes = {
            '#model-status': Locator(), '#model': Locator(value='synthetic-model'),
            '#send': Locator(), '#prompt': Locator(), '#status': Locator('Response interrupted'),
            '.assistant .metadata-grid dd': Locator('Complete'),
            '.assistant .message-body': Locator('Ok'), '#stop': Locator(),
        }
        self.listeners = {}
        self.predicate = None
        self.request = SimpleNamespace(method='POST', url='https://example.test/app/agent/chat',
            post_data_json={'conversation_id': CONVERSATION, 'session_id': TURN})

    def locator(self, selector):
        return self.nodes[selector]

    def on(self, event, listener):
        self.listeners[event] = listener

    def remove_listener(self, event, listener):
        assert self.listeners.pop(event) is listener

    def expect_request(self, predicate):
        self.predicate = predicate
        assert predicate(self.request)
        return Submission(self.request)


@pytest.fixture
def studio(monkeypatch):
    import playwright.async_api
    monkeypatch.setattr(playwright.async_api, 'expect', Expectation)
    page = Page()
    capture = AsyncMock()
    evidence = SimpleNamespace(ui_turn_lock=asyncio.Lock(), emissions=None, capture=capture)
    session = SimpleNamespace(session=SimpleNamespace(page=page), services={'ui_turn_evidence': evidence})
    return SimpleNamespace(page=page, capture=capture, session=session)


@pytest.mark.asyncio
async def test_submission_binds_request_and_awaits_response_and_capture(studio):
    response_entered, response_release = asyncio.Event(), asyncio.Event()
    capture_entered, capture_release = asyncio.Event(), asyncio.Event()

    async def response():
        response_entered.set()
        await response_release.wait()
        return 'Rendered answer'

    async def capture(phase, *, owner):
        assert phase == 'success'
        assert owner == {'kind': 'turn', 'conversation_id': CONVERSATION, 'turn_id': TURN}
        capture_entered.set()
        await capture_release.wait()

    studio.page.nodes['.assistant .message-body'].inner_text.side_effect = response
    studio.capture.side_effect = capture
    task = asyncio.create_task(browser_prompt(studio.session, prompt='A synthetic question'))
    await asyncio.wait_for(response_entered.wait(), 1)
    assert not task.done()
    studio.capture.assert_not_awaited()
    response_release.set()
    await asyncio.wait_for(capture_entered.wait(), 1)
    assert not task.done()
    assert studio.page.listeners == {}
    capture_release.set()
    assert await asyncio.wait_for(task, 1) == 'Rendered answer'
    studio.page.nodes['#prompt'].fill.assert_awaited_once_with('A synthetic question')
    studio.page.nodes['#send'].click.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path,accepted', [
    ('POST', '/app/agent/chat', True), ('POST', '/app/agent/chat/stream?x=1', True),
    ('GET', '/app/agent/chat', False), ('POST', '/unrelated', False),
])
async def test_submission_matches_only_chat_post_requests(studio, method, path, accepted):
    await browser_prompt(studio.session)
    assert studio.page.predicate(SimpleNamespace(method=method, url='https://example.test' + path)) is accepted


@pytest.mark.asyncio
async def test_catalog_timeout_reports_diagnostic_and_removes_listener(studio):
    studio.page.nodes['#model-status'].state = 'loading'
    studio.page.nodes['#model-status'].text = 'Catalog unavailable'
    with pytest.raises(AssertionError, match='Studio catalog did not settle: Catalog unavailable') as caught:
        await browser_prompt(studio.session)
    assert isinstance(caught.value.__cause__, AssertionError)
    assert studio.page.listeners == {}
    studio.page.nodes['#send'].click.assert_not_awaited()
    studio.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_model_reports_catalog_diagnostic(studio):
    studio.page.nodes['#model'].value = ''
    studio.page.nodes['#model-status'].text = 'No models offered'
    with pytest.raises(AssertionError, match='Studio model choices are unavailable: No models offered'):
        await browser_prompt(studio.session)
    assert studio.page.listeners == {}
    studio.page.nodes['#send'].click.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_diagnostic_prevents_submission(studio):
    studio.page.nodes['#model-status'].text = 'Selection rejected'
    with pytest.raises(AssertionError, match='Studio model selection is unavailable: Selection rejected'):
        await browser_prompt(studio.session)
    assert studio.page.listeners == {}
    studio.page.nodes['#send'].click.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_response_reports_status(studio):
    studio.page.nodes['.assistant .metadata-grid dd'].text = 'Incomplete'
    with pytest.raises(AssertionError, match='Response interrupted'):
        await browser_prompt(studio.session)
    studio.capture.assert_awaited_once_with('failure', owner={
        'kind': 'turn', 'conversation_id': CONVERSATION, 'turn_id': TURN})
    assert studio.page.listeners == {}


@pytest.mark.asyncio
async def test_empty_response_is_rejected(studio):
    studio.page.nodes['.assistant .message-body'].text = '  \n '
    with pytest.raises(AssertionError, match='empty browser response'):
        await browser_prompt(studio.session)
    assert studio.page.listeners == {}


@pytest.mark.asyncio
async def test_page_errors_are_rejected(studio):
    async def click():
        studio.page.listeners['pageerror'](RuntimeError('Rendering failed'))
    studio.page.nodes['#send'].click.side_effect = click
    with pytest.raises(AssertionError, match='Rendering failed'):
        await browser_prompt(studio.session)
    assert studio.page.listeners == {}


@pytest.mark.asyncio
async def test_visible_stop_means_response_is_not_finished(studio):
    studio.page.nodes['#stop'].is_hidden.return_value = False
    with pytest.raises(AssertionError):
        await browser_prompt(studio.session)
    assert studio.page.listeners == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('Input failed'), asyncio.CancelledError()])
async def test_exception_and_cancellation_remove_listener(studio, error):
    studio.page.nodes['#prompt'].fill.side_effect = error
    with pytest.raises(type(error)) as caught:
        await browser_prompt(studio.session)
    assert caught.value is error
    assert studio.page.listeners == {}
    studio.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_readiness_blocks_input_and_submission(studio, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def ready(expectation):
        assert expectation.locator is studio.page.nodes['#send']
        entered.set()
        await release.wait()

    monkeypatch.setattr(Expectation, 'to_be_enabled', ready)
    task = asyncio.create_task(browser_prompt(studio.session))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
        studio.page.nodes['#prompt'].fill.assert_not_awaited()
        studio.page.nodes['#send'].click.assert_not_awaited()
        studio.capture.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
    studio.page.nodes['#prompt'].fill.assert_awaited_once_with('Answer with exactly: Ok')
    studio.page.nodes['#send'].click.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_completion_readiness_blocks_answer_and_capture(studio, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(expectation, pattern, *, timeout):
        assert expectation.locator is studio.page.nodes['.assistant .metadata-grid dd']
        assert pattern.fullmatch('Complete')
        assert pattern.fullmatch('Incomplete')
        assert timeout == 120000
        entered.set()
        await release.wait()

    monkeypatch.setattr(Expectation, 'to_have_text', complete)
    task = asyncio.create_task(browser_prompt(studio.session))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
        studio.page.nodes['.assistant .metadata-grid dd'].text_content.assert_not_awaited()
        studio.page.nodes['.assistant .message-body'].inner_text.assert_not_awaited()
        studio.capture.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
    studio.page.nodes['.assistant .message-body'].inner_text.assert_awaited_once_with()
    studio.capture.assert_awaited_once()
