"""Whole wrapped links use explicit actions without unsolicited clipboard writes."""
import asyncio

import pytest
from textual.widgets import Static
from textual.widgets._toast import Toast

from api_agent.testing.allure_viewer import ViewerLocation
from api_agent.tui import allure
from api_agent.tui.clipboard import ClipboardError
from test_tui import fixture


REPORT = 'http://127.0.0.1:5252/runs/' + 'a' * 180 + '/index.html#testresult/' + 'b' * 16
CDP = 'http://127.0.0.1:45665'


def link_cells(widget, url):
    cells = []
    inset = widget.content_region.offset - widget.region.offset
    for y in range(widget.size.height):
        x = 0
        for segment in widget.render_line(y):
            if segment.style and segment.style.meta.get('@click', (None, ()))[1] == (url,):
                cells.append(((x + inset.x, y + inset.y), segment.style))
            x += len(segment.text)
    return cells


@pytest.fixture
def interactions(monkeypatch):
    calls = []
    async def copy(app, value):
        calls.append(('copy', value))
        return True
    monkeypatch.setattr(allure, 'copy_text', copy)
    monkeypatch.setattr('api_agent.tui.app.ApiAgentApp.copy_to_clipboard',
                        lambda app, value: calls.append(('terminal', value)))
    return calls


@pytest.mark.parametrize('segment', [0, -1], ids=['first-line', 'last-line'])
@pytest.mark.parametrize('width', [82, 140], ids=['narrow', 'wide'])
def test_whole_wrapped_toast_link_hovers_opens_and_copies(tmp_path, monkeypatch, interactions, segment, width):
    async def scenario():
        app = fixture(tmp_path)
        monkeypatch.setattr(app, 'open_url', lambda value: interactions.append(('open', value)))
        async with app.run_test(size=(width, 42), notifications=True) as pilot:
            await pilot.pause()
            original_screen = app.screen
            app.show_allure_location(ViewerLocation(REPORT, CDP))
            await pilot.pause()
            toast = app.screen.query_one(Toast)
            cells = link_cells(toast, REPORT)
            assert len({position[1] for position, _ in cells}) > 1
            assert len({style.link_id for _, style in cells}) == 1
            position, style = cells[segment]
            await pilot.hover(toast, offset=position)
            await pilot.pause()
            assert toast.highlight_link_id == style.link_id
            assert all(app.screen.get_style_at(toast.region.x + x, toast.region.y + y).bold
                       for (x, y), _ in cells)
            assert interactions == []
            await pilot.click(toast, offset=position)
            await pilot.pause()
            assert interactions == [('copy', REPORT), ('open', REPORT)]
            assert app.screen is original_screen and app.allure_viewer is None
    asyncio.run(scenario())


def test_cdp_toast_link_copies_base_without_opening_empty_root(tmp_path, monkeypatch, interactions):
    async def scenario():
        app = fixture(tmp_path)
        monkeypatch.setattr(app, 'open_url', lambda value: interactions.append(('open', value)))
        async with app.run_test(size=(82, 42), notifications=True) as pilot:
            await pilot.pause()
            app.show_allure_location(ViewerLocation(REPORT, CDP))
            await pilot.pause()
            toast = app.screen.query_one(Toast)
            assert REPORT + '\nBrowser CDP (click to copy; not a web page):' in str(toast.render())
            position, _ = link_cells(toast, CDP)[-1]
            assert interactions == []
            await pilot.click(toast, offset=position)
            await pilot.pause()
            assert interactions == [('copy', CDP)]
    asyncio.run(scenario())


def test_history_links_preserve_literal_errors_and_open_exact_archive(tmp_path, monkeypatch, interactions):
    async def scenario():
        app = fixture(tmp_path)
        monkeypatch.setattr(app, 'open_url', lambda value: interactions.append(('open', value)))
        archive = 'http://127.0.0.1:5252/archive.html?q=[bold]literal[/]'
        error = 'Browser unavailable: [@click=app.quit]literal[/]'
        async with app.run_test(size=(90, 42)) as pilot:
            await pilot.pause()
            await app.show('allure', 'History')
            await pilot.pause()
            app.show_allure_location(ViewerLocation(REPORT, CDP, browser_error=error, archive_url=archive))
            await pilot.pause()
            body = app.screen.query_one('#body', Static)
            assert REPORT + "\n\nBrowser CDP" in str(body.content)
            assert error in str(body.content) and archive in str(body.content)
            assert all('quit' not in str(span.style) for span in body.content.spans)
            assert link_cells(body, REPORT)
            position, _ = link_cells(body, archive)[-1]
            assert interactions == []
            await pilot.click(body, offset=position)
            await pilot.pause()
            assert interactions == [('copy', archive), ('open', archive)]
            assert app.screen.kind == 'allure'
    asyncio.run(scenario())


@pytest.fixture(params=[
    (False, False, False, 'terminal clipboard'),
    (True, True, False, 'Could not copy'),
    (True, False, True, 'Could not open'),
    (True, True, True, 'Could not open'),
], ids=['terminal-fallback', 'clipboard-failure', 'opener-failure', 'both-fail'])
def failing_interactions(monkeypatch, request):
    native, copy_failure, open_failure, expected = request.param
    calls = []
    async def copy(app, value):
        calls.append(('copy', value))
        if copy_failure:
            raise ClipboardError('unavailable')
        return native
    def open_url(value):
        calls.append(('open', value))
        if open_failure:
            raise RuntimeError('unavailable')
    monkeypatch.setattr(allure, 'copy_text', copy)
    return calls, open_url, expected, copy_failure or open_failure


def test_clipboard_and_opener_fail_independently(tmp_path, monkeypatch, failing_interactions):
    async def scenario():
        app = fixture(tmp_path)
        calls, opener, expected, failure = failing_interactions
        notices = []
        monkeypatch.setattr(app, 'open_url', opener)
        monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(str(message)))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.action_allure_open_url(REPORT)
            assert calls == [('copy', REPORT), ('open', REPORT)]
            assert any(expected in notice for notice in notices)
            assert (REPORT in notices[-1]) == failure
    asyncio.run(scenario())


@pytest.mark.parametrize('native', [True, False], ids=['native-copy', 'terminal-copy'])
def test_driver_false_open_result_still_explains_manual_fallback(tmp_path, monkeypatch, native):
    async def scenario():
        app = fixture(tmp_path)
        copied, opened, notices = [], [], []
        async def copy(app, value):
            copied.append(value)
            return native
        monkeypatch.setattr(allure, 'copy_text', copy)
        monkeypatch.setattr('webbrowser.open', lambda value: opened.append(value) or False)
        monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(str(message)))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.action_allure_open_url(REPORT)
            assert opened == copied == [REPORT]
            assert any('If no browser opens, paste the URL into a browser.' in notice for notice in notices)
            assert not any('browser opened' in notice.lower() for notice in notices)
    asyncio.run(scenario())
