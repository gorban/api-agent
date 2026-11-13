"""A removed Chromium node requires a fresh audit, never a skipped audit."""

import pytest
from playwright.async_api import async_playwright

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import capture_studio_viewport


@pytest.mark.asyncio
@pytest.mark.parametrize('audit', [1, 2])
@pytest.mark.parametrize('case', ['removed', 'removed_then_closed', 'unknown_error'])
async def test_shadow_audit_retries_only_when_the_dom_proves_a_node_was_removed(monkeypatch, case, audit):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body><header>Controls</header><main id="panel">
                <div id="private">Private answer</div></main><div id="temporary"></div></body>
            '''))
            await page.goto('http://localhost:9000/')
            create = page.context.new_cdp_session
            observed = {'changed': False, 'stale_error': False, 'audits': 0}

            async def create_session(selected):
                session = await create(selected)
                send = session.send

                async def intercepted(method, params=None):
                    if method == 'DOM.describeNode' and case == 'unknown_error' and observed['audits'] >= audit:
                        raise RuntimeError('Controlled unrelated audit failure')
                    try:
                        result = await send(method, params)
                    except Exception:
                        if method == 'DOM.describeNode':
                            observed['stale_error'] = True
                        raise
                    if method == 'DOM.querySelectorAll' and params['selector'] == '*':
                        observed['audits'] += 1
                    if method == 'DOM.querySelectorAll' and params['selector'] == '*' and observed['audits'] == audit and not observed['changed']:
                        observed['changed'] = True
                        target = await send('DOM.querySelector', {
                            'nodeId': params['nodeId'], 'selector': '#temporary'})
                        await send('DOM.removeNode', {'nodeId': target['nodeId']})
                        if case == 'removed_then_closed':
                            await send('Runtime.evaluate', {'expression': '''(() => {
                                const host = document.createElement('div');
                                host.attachShadow({mode:'closed'}).innerHTML = '<p>Closed</p>';
                                document.body.append(host);
                            })()'''})
                    return result

                monkeypatch.setattr(session, 'send', intercepted)
                return session

            monkeypatch.setattr(page.context, 'new_cdp_session', create_session)
            reasons, stages = [], []
            image = await capture_studio_viewport(
                page, EmissionBoundary(ContentPolicy(False)), 'http://localhost:9000',
                content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append, stage=lambda name, elapsed: stages.append(name),
                retry_window_seconds=5,
            )
            if case == 'removed':
                assert observed['stale_error'] and 'retry' in stages
                assert image is not None and reasons == []
            elif case == 'removed_then_closed':
                assert observed['stale_error'] and 'retry' in stages
                assert image is None and reasons == ['closed_shadow']
            else:
                assert 'retry' not in stages
                assert image is None and reasons == ['shadow_audit']
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('packet,expected', [
    ({'nodeIds': []}, True),
    ({'nodeIds': [2, 3]}, True),
    ({'nodeIds': [7]}, False),
    ({'nodeIds': [True]}, False),
    ({'nodeIds': ['7']}, False),
    ({'nodeIds': [-1]}, False),
    ({'nodeIds': [1] * 10001}, False),
    ({'nodeIds': None}, False),
    ({}, False),
    (RuntimeError('Controlled protocol failure'), False),
])
async def test_removed_node_proof_requires_a_bounded_valid_same_document_inventory(packet, expected):
    from types import SimpleNamespace
    from api_agent.ui.evidence import _shadow_node_removed

    async def send(method, params):
        assert method == 'DOM.querySelectorAll'
        assert params == {'nodeId': 11, 'selector': '*'}
        if isinstance(packet, Exception):
            raise packet
        return packet

    assert await _shadow_node_removed(SimpleNamespace(send=send), 11, 7) is expected
