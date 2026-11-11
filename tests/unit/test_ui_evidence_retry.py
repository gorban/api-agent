"""Turn screenshots retry transient instability and report why they finally failed."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from api_agent.providers.progress import component_timeout, progress_sink
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui import evidence
from api_agent.ui.evidence import OwnedFrameContract, capture_studio_viewport

_ORIGIN = 'http://localhost:9000'
_OWNED_FRAME = OwnedFrameContract('iframe[data-testid="chat-message-html"]',
    '/assets/agent-html-runtime/renderer.html', 'allow-scripts')
_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='


async def _pixels(page, png, box):
    """Whether a PNG region is all black, and whether it contains any red."""
    return await page.evaluate('''async ({png, box}) => {
      const image = new Image(); image.src = 'data:image/png;base64,' + png;
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
      const data = context.getImageData(box.x, box.y, box.width, box.height).data;
      let black = true, red = false;
      for (let index = 0; index < data.length; index += 4) {
        const [r, g, b] = data.slice(index, index + 3);
        black &&= r === 0 && g === 0 && b === 0;
        red ||= r > 200 && g < 80 && b < 80;
      }
      return {black, red};
    }''', {'png': base64.b64encode(png).decode(), 'box': box})


async def _owned_frame_page(browser, markup):
    page = await browser.new_page(viewport={'width': 600, 'height': 400})
    await page.route(_ORIGIN + '/da2/Home', lambda route: route.fulfill(
        content_type='text/html', body=f'''<body style="margin:0;background:white">
        <iframe data-testid="chat-message-html" sandbox="allow-scripts"
          src="/assets/agent-html-runtime/renderer.html" style="width:300px;height:80px;border:0"></iframe>
        {markup}<button id="send">Send</button></body>'''))
    await page.route(_ORIGIN + _OWNED_FRAME.path, lambda route: route.fulfill(
        content_type='text/html', body='<body><div>Rendered answer</div></body>'))
    await page.goto(_ORIGIN + '/da2/Home')
    await page.frame_locator(_OWNED_FRAME.selector).locator('div').wait_for()
    return page


async def _capture(page, reasons, **options):
    return await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), _ORIGIN,
        path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME, unavailable=reasons.append,
        **options)


@pytest.mark.asyncio
async def test_paused_capture_isolates_surfaces_without_moving_layout_on_every_turn():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            # Collapsed margins move as soon as size containment applies.
            page = await _owned_frame_page(browser, f'<div id="art" style="background-image:url({_PIXEL})">'
                '<p style="margin:20px 0;color:red;font:bold 30px Arial">Private art</p></div>')
            send = await page.locator('#send').bounding_box()
            art = await page.locator('#art').bounding_box()
            for _turn in range(2):
                reasons = []
                image = await _capture(page, reasons)
                assert image is not None, reasons
                pixels = await _pixels(page, image, {key: int(art[key]) + (1 if key in 'xy' else -2)
                                                     for key in ('x', 'y', 'width', 'height')})
                assert pixels == {'black': True, 'red': False}
                assert await page.locator('#art').get_attribute('style') == f'background-image:url({_PIXEL})'
                assert await page.locator('#send').bounding_box() == send
                assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_paused_capture_withholds_top_layer_opened_inside_an_isolated_surface():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await _owned_frame_page(browser, f'<div id="art" style="background-image:url({_PIXEL})">'
                '<div id="pop" popover style="background:red">Confidential escape</div></div>')
            await page.locator('#pop').evaluate('node => node.showPopover()')
            reasons = []
            assert await _capture(page, reasons) is None
            assert reasons == ['content_top_layer']
        finally:
            await browser.close()


async def _exit_popover(page):
    await page.locator('#pop').evaluate('node => node.showPopover()')
    await page.wait_for_timeout(50)
    await page.locator('#pop').evaluate('node => node.hidePopover()')
    assert await page.locator('#pop').evaluate("node => !node.matches(':popover-open')")


async def _open_picker(page):
    await page.locator('#pick').click()
    assert await page.locator('#pick').evaluate("node => node.matches(':open')")


_TOP_LAYER_SURFACES = {
    # A closing popover stays in the top layer for its exit transition, and
    # captures freeze transitions, but it no longer matches :popover-open.
    'exiting-popover': (f'''<span id="host" style="background-image:url({_PIXEL})"><span id="pop"
        popover style="transition:overlay 60s allow-discrete,display 60s allow-discrete;
        background:rgb(255,0,0);width:200px;height:100px;border:0">Confidential escape</span></span>''',
        _exit_popover),
    # A customizable select paints its open picker in the top layer.
    'select-picker': (f'''<style>select, ::picker(select) {{ appearance: base-select; }}</style>
        <div id="host" style="background-image:url({_PIXEL})"><select id="pick">
        <option style="background:rgb(255,0,0);height:100px">Confidential option</option></select></div>''',
        _open_picker),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('paused', [True, False], ids=['paused', 'live'])
@pytest.mark.parametrize('surface', sorted(_TOP_LAYER_SURFACES))
async def test_top_layer_without_an_open_state_is_withheld(surface, paused):
    from playwright.async_api import async_playwright
    markup, open_surface = _TOP_LAYER_SURFACES[surface]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            if paused:
                page = await _owned_frame_page(browser, markup)
            else:
                page = await browser.new_page()
                await page.route(_ORIGIN + '/', lambda route: route.fulfill(
                    content_type='text/html', body='<body style="margin:0">' + markup + '</body>'))
                await page.goto(_ORIGIN + '/')
            await open_surface(page)
            reasons = []
            if paused:
                assert await _capture(page, reasons) is None
                assert reasons == ['content_top_layer']
            else:
                # Live size containment on the host already ends top-layer painting.
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), _ORIGIN,
                    unavailable=reasons.append)
                assert image is not None, reasons
                assert not (await _pixels(page, image, {'x': 0, 'y': 0, 'width': 1280, 'height': 720}))['red']
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['content-root', 'opaque'])
async def test_live_isolated_root_in_the_top_layer_is_refused_before_containment(surface):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            style = 'background:rgb(255,0,0)' + (
                '' if surface == 'content-root' else f';background-image:url({_PIXEL})')
            # Containment on these crashed the renderer and stalled capture cleanup.
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=f'''
              <body style="margin:0"><main id="panel"><section id="private">Confidential answer</section>
              <div id="top" popover style="{style}">Confidential top</div>
              <button id="send">Send</button></main></body>'''))
            await page.goto(_ORIGIN + '/')
            await page.locator('#top').evaluate('node => node.showPopover()')
            reasons = []
            assert await asyncio.wait_for(capture_studio_viewport(page,
                EmissionBoundary(ContentPolicy(False)), _ORIGIN,
                content_mask_selector='#private' + (', #top' if surface == 'content-root' else ''),
                content_mask_anchor='#panel', unavailable=reasons.append), 20) is None
            assert reasons == ['content_top_layer']
            assert await page.locator('#top').get_attribute('style') == style
        finally:
            await browser.close()


_SHADOW_HOSTS = {
    'block': '<div id="host" style="width:50px;height:20px"></div>',
    'inline-custom': '<x-card id="host">card</x-card>',
    'span': '<span id="host">card</span>',
    'table-row': '<div id="host" style="display:table-row">card</div>',
    'inline-in-isolated-block': f'<div style="background-image:url({_PIXEL})"><x-card id="host">card</x-card></div>',
}


@pytest.mark.asyncio
@pytest.mark.parametrize('paused', [True, False], ids=['paused', 'live'])
@pytest.mark.parametrize('host', sorted(_SHADOW_HOSTS))
async def test_top_layer_inside_unowned_shadow_root_is_withheld(host, paused):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            # content-visibility does not suppress inline or table hosts, so the
            # audit must reach unowned shadow internals instead of trusting CSS.
            markup = _SHADOW_HOSTS[host] + """<script>
              const root = document.getElementById('host').attachShadow({mode:'open'});
              root.innerHTML = '<div id="pop" popover style="background:rgb(255,0,0);width:200px;height:100px;border:0">Confidential escape</div>';
              root.getElementById('pop').showPopover();
            </script>"""
            if paused:
                page = await _owned_frame_page(browser, markup)
                reasons = []
                assert await _capture(page, reasons) is None
            else:
                page = await browser.new_page()
                await page.route(_ORIGIN + '/', lambda route: route.fulfill(
                    content_type='text/html', body='<body style="margin:0">' + markup + '</body>'))
                await page.goto(_ORIGIN + '/')
                reasons = []
                assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), _ORIGIN,
                    unavailable=reasons.append) is None
            assert reasons == ['content_top_layer']
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_change_during_screenshot_recovers_on_a_restored_second_attempt(monkeypatch):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(evidence, 'CAPTURE_RETRY_DELAY_SECONDS', 0)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route(_ORIGIN + '/', lambda route: route.fulfill(content_type='text/html', body='''
              <style id="paint"></style><header style="height:40px;background:blue">Navigation</header>'''))
            await page.goto(_ORIGIN + '/')
            original, calls = page.screenshot, []
            async def changing_once(**kwargs):
                calls.append(True)
                if len(calls) == 1:
                    await page.evaluate("document.querySelector('#paint').sheet.insertRule('header{background:red!important}')")
                return await original(**kwargs)
            monkeypatch.setattr(page, 'screenshot', changing_once)
            reasons, stages = [], []
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), _ORIGIN,
                unavailable=reasons.append, stage=lambda name, _elapsed: stages.append(name)) is not None
            assert reasons == [] and len(calls) == 2
            assert stages.count('retry') == 1 and stages.count('cleanup') == 2
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_repeated_layout_failure_reports_the_moved_element_once(monkeypatch):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(evidence, 'CAPTURE_RETRY_DELAY_SECONDS', 0)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0"><main id="panel"><section id="private" data-testid="private">
                <p style="margin:20px 0">Confidential answer</p>
              </section><button id="send">Send</button></main></body>'''))
            await page.goto(_ORIGIN + '/')
            reasons, details, stages = [], [], []
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                _ORIGIN, content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append, failure_detail=details.append,
                stage=lambda name, _elapsed: stages.append(name)) is None
            # The same element moving the same way twice is deterministic: stop early.
            assert reasons == ['content_layout']
            assert stages.count('cleanup') == 2 and stages.count('retry') == 1
            [detail] = details
            assert detail['reason'] == 'content_layout' and detail['attempts'] == 2
            shift = detail['layout_shift']
            assert (shift['tag'], shift['testid'], shift['kind']) == ('section', 'private', 'content')
            assert shift['shifted'] >= 1 and shift['before'] != shift['after']
            assert set(shift['before']) == {'x', 'y', 'width', 'height'}
            assert await page.locator('#private').get_attribute('style') is None
        finally:
            await browser.close()


def _scripted_attempts(monkeypatch, outcomes, *, seconds=()):
    calls, clock = [], [0.0]
    async def once(page, boundary, origin, *, unavailable, **options):
        calls.append(options)
        clock[0] += seconds[len(calls) - 1] if seconds else 0
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, list):
            # Several refusals recorded by one attempt.
            for refusal in outcome:
                unavailable(*refusal)
            return None
        if isinstance(outcome, tuple) and len(outcome) == 2:
            # A refusal recorded by an attempt that is then interrupted.
            unavailable(*outcome[0])
            raise outcome[1]
        reason, shift, image = outcome
        if reason is not None:
            unavailable(reason, shift)
        return image
    monkeypatch.setattr(evidence, '_capture_viewport_once', once)
    monkeypatch.setattr(evidence, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(evidence, 'CAPTURE_RETRY_DELAY_SECONDS', 0)
    return calls


def _run(**options):
    reasons, details = [], []
    result = asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
        unavailable=reasons.append, failure_detail=details.append, **options))
    return result, reasons, details


def test_transient_failure_redoes_the_whole_capture_and_reports_nothing(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [('content_layout', None, None), (None, None, b'png')])
    assert _run(path='/x') == (b'png', [], [])
    assert len(calls) == 2 and calls[0] == calls[1] == {'path': '/x'}


def test_deterministic_refusal_is_reported_without_retrying(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [('registered_secret', None, None)])
    assert _run() == (None, ['registered_secret'], [{'reason': 'registered_secret', 'attempts': 1,
                                                      'layout_shift': None, 'interrupted': False}])
    assert len(calls) == 1


def test_changing_layout_shifts_use_every_attempt(monkeypatch):
    first, second = (evidence._layout_shift(dict(_SHIFT, shifted=count),
                                            EmissionBoundary(ContentPolicy(True))) for count in (1, 2))
    calls = _scripted_attempts(monkeypatch, [('content_layout', first, None),
        ('content_layout', second, None), ('content_layout', second, None)])
    _, reasons, details = _run()
    assert len(calls) == 3 and reasons == ['content_layout'] and details[0]['attempts'] == 3


def test_retry_that_would_outlast_the_window_is_skipped(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [('paint_changed', None, None)] * 3, seconds=(9, 3, 9))
    _, reasons, details = _run(retry_window_seconds=20)
    # After 9s, a 9s retry fits 20s. After 12s the slowest attempt (9s), not the
    # last one (3s), projects 21s, so the third attempt is skipped.
    assert len(calls) == 2 and reasons == ['paint_changed'] and details[0]['attempts'] == 2


def test_caller_deadline_clips_the_retry_window(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [('paint_changed', None, None)] * 3, seconds=(3, 3, 3))
    reasons, components = [], []
    async def capture():
        with progress_sink(lambda progress: components.append((progress.name, progress.timeout))):
            async with component_timeout('Caller page budget', 5):
                return await capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
                    unavailable=reasons.append, retry_window_seconds=20)
    assert asyncio.run(capture()) is None
    # A 3s retry after 3s fits the 20s window, but not the caller's 5s deadline.
    assert len(calls) == 1 and reasons == ['paint_changed']
    timeouts = [timeout for name, timeout in components if name == 'UI viewport capture']
    assert timeouts and all(4 < timeout <= 5 for timeout in timeouts)


def test_attempt_with_any_deterministic_refusal_is_not_retried(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [
        [('document_changed', None), ('registered_secret', None), ('paint_changed', None)],
        (None, None, b'png')])
    result, reasons, details = _run()
    assert result is None and len(calls) == 1 and reasons == ['registered_secret']
    assert details[0]['reason'] == 'registered_secret' and details[0]['attempts'] == 1


def test_refused_attempt_never_returns_its_pixels(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [('registered_secret', None, b'png')])
    assert _run() == (None, ['registered_secret'], [{'reason': 'registered_secret', 'attempts': 1,
                                                      'layout_shift': None, 'interrupted': False}])
    assert len(calls) == 1


def test_cancellation_after_a_refusal_still_reports_it(monkeypatch):
    _scripted_attempts(monkeypatch, [('frame_changed', None, None), asyncio.CancelledError()])
    reasons, details = [], []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
            unavailable=reasons.append, failure_detail=details.append))
    assert reasons == ['frame_changed']
    assert details == [{'reason': 'frame_changed', 'attempts': 1, 'layout_shift': None, 'interrupted': True}]


def test_interrupted_attempt_reports_its_own_refusal_despite_a_failing_callback(monkeypatch):
    _scripted_attempts(monkeypatch, [('paint_changed', None, None),
        (('pause_lost', None), asyncio.CancelledError())])
    reasons = []
    def failing(detail):
        raise RuntimeError('diagnostic sink failed')
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
            unavailable=reasons.append, failure_detail=failing))
    assert reasons == ['pause_lost']


def test_refusal_inside_an_interrupted_first_attempt_counts_that_attempt(monkeypatch):
    _scripted_attempts(monkeypatch, [(('frame_changed', None), asyncio.CancelledError())])
    details = []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
            failure_detail=details.append))
    assert details == [{'reason': 'frame_changed', 'attempts': 1, 'layout_shift': None, 'interrupted': True}]


def test_error_before_any_refusal_reports_nothing(monkeypatch):
    _scripted_attempts(monkeypatch, [RuntimeError('capture failed')])
    reasons, details = [], []
    with pytest.raises(RuntimeError, match='capture failed'):
        asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
            unavailable=reasons.append, failure_detail=details.append))
    assert reasons == [] and details == []


def test_silent_refusal_without_callbacks_is_not_retried(monkeypatch):
    calls = _scripted_attempts(monkeypatch, [(None, None, None)])
    assert asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN)) is None
    assert len(calls) == 1


@pytest.mark.parametrize('window', [-1, 0, float('inf'), float('nan'), '5', True])
def test_invalid_retry_window_is_rejected(window):
    with pytest.raises(ValueError, match='retry window'):
        asyncio.run(capture_studio_viewport(SimpleNamespace(), object(), _ORIGIN,
                                            retry_window_seconds=window))


_BOX = {'x': 0, 'y': 1.5, 'width': 10, 'height': 20}
_SHIFT = {'tag': 'section', 'testid': 'chat-card', 'kind': 'opaque', 'shifted': 2,
          'before': _BOX, 'after': {**_BOX, 'y': 21.5}}


@pytest.mark.parametrize('value, expected', [
    (_SHIFT, {**_SHIFT, 'before': {'x': 0., 'y': 1.5, 'width': 10., 'height': 20.},
              'after': {'x': 0., 'y': 21.5, 'width': 10., 'height': 20.}}),
    ({**_SHIFT, 'testid': 'has space'}, 'no-testid'),
    ({**_SHIFT, 'testid': None}, 'no-testid'),
    ({**_SHIFT, 'testid': ''}, 'no-testid'),
    ({**_SHIFT, 'testid': 'x' * 65}, 'no-testid'),
    ({**_SHIFT, 'testid': '秘密'}, 'no-testid'),
    (None, None),
    ({**_SHIFT, 'tag': ''}, None),
    ({**_SHIFT, 'tag': 7}, None),
    ({**_SHIFT, 'tag': 'x' * 33}, None),
    ({**_SHIFT, 'tag': 'café-card'}, None),
    ({**_SHIFT, 'kind': 'text'}, None),
    ({**_SHIFT, 'shifted': 0}, None),
    ({**_SHIFT, 'shifted': True}, None),
    ({**_SHIFT, 'before': None}, None),
    ({**_SHIFT, 'before': {'x': 0}}, None),
    ({**_SHIFT, 'after': {**_BOX, 'x': float('nan')}}, None),
    ({**_SHIFT, 'after': {**_BOX, 'x': '1'}}, None),
])
def test_layout_shift_admits_only_bounded_structure(value, expected):
    result = evidence._layout_shift(value, EmissionBoundary(ContentPolicy(True)))
    if expected == 'no-testid':
        assert result is not None and result['testid'] is None and result['tag'] == 'section'
    else:
        assert result == expected


@pytest.mark.parametrize('secret', ['chat-card', 'card'])
def test_layout_shift_drops_a_test_id_the_credential_boundary_redacts(secret):
    boundary = EmissionBoundary(ContentPolicy(True))
    boundary.register_secret(secret)
    shift = evidence._layout_shift(_SHIFT, boundary)
    assert shift['testid'] is None and shift['tag'] == 'section'
