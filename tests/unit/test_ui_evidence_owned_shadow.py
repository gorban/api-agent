"""An explicit authored chrome contract audits open shadow content before pixels leave."""
import pytest
from playwright.async_api import async_playwright
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import capture_studio_viewport


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_content', [False, True])
async def test_owned_navbar_keeps_authored_shadow_chrome_visible(allow_content):
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0"><modus-navbar data-test-id="navbar" style="display:block;height:56px"></modus-navbar>
              <main id="panel"><div id="private" style="height:40px">Private answer</div></main>
              <script>document.querySelector('modus-navbar').attachShadow({mode:'open'}).innerHTML =
                '<nav style="height:56px;background:rgb(0,0,255);color:white">Home <button>Help</button></nav>';</script></body>'''))
            await page.goto('http://localhost:9000/')
            image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(allow_content)),
                'http://localhost:9000', owned_shadow_selector='modus-navbar[data-test-id="navbar"]',
                content_mask_selector='#private', content_mask_anchor='#panel')
            assert image is not None
            import base64
            color = await page.evaluate('''async png => {
              const img = new Image(); img.src='data:image/png;base64,'+png; await img.decode();
              const canvas=document.createElement('canvas'); canvas.width=600;canvas.height=300;
              const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);
              return [...ctx.getImageData(500,30,1,1).data];
            }''', base64.b64encode(image).decode())
            assert color == [0, 0, 255, 255]
            assert await page.locator('modus-navbar').evaluate('''node =>
              [...node.shadowRoot.querySelectorAll('*')].every(child =>
                [...child.attributes].every(attr => !attr.name.startsWith('data-evidence-')))''')
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['split_slot_secret', 'closed_nested', 'duplicate_hosts'])
async def test_owned_shadow_rejects_unsafe_composed_content(variant):
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><modus-navbar data-test-id="navbar" style="display:block;height:56px"><span>secret</span></modus-navbar>
              <main id="panel"><div id="private">Private</div></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.evaluate('''variant => {
              const host=document.querySelector('modus-navbar');
              const root=host.attachShadow({mode:'open'});
              root.innerHTML='<span>shadow-</span><slot></slot><div id="nested"></div>';
              if(variant==='closed_nested') root.querySelector('#nested').attachShadow({mode:'closed'}).innerHTML='<p>Hidden</p>';
              if(variant==='duplicate_hosts') document.body.append(host.cloneNode());
            }''', variant)
            boundary = EmissionBoundary(ContentPolicy(True))
            boundary.register_secret('shadow-secret')
            reasons = []
            assert await capture_studio_viewport(page, boundary, 'http://localhost:9000',
                owned_shadow_selector='modus-navbar[data-test-id="navbar"]',
                unavailable=reasons.append) is None
            assert reasons == [{'split_slot_secret': 'registered_secret', 'closed_nested': 'closed_shadow',
                'duplicate_hosts': 'shadow_audit'}[variant]]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('selector', ['', 1, 'x' * 257])
async def test_invalid_owned_shadow_contract_rejects_without_browser(selector):
    reasons = []
    assert await capture_studio_viewport(None, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', owned_shadow_selector=selector, unavailable=reasons.append) is None
    assert reasons == ['shadow_contract']


@pytest.mark.asyncio
async def test_expanded_shadow_audit_is_bounded():
    from types import SimpleNamespace
    from api_agent.ui.evidence import _closed_author_shadow
    async def send(method, params):
        if method == 'DOM.getDocument':
            return {'root': {'nodeId': 1}}
        if method == 'DOM.querySelectorAll':
            return {'nodeIds': [2]}
        return {'node': {'children': [{}] * 50000}}
    with pytest.raises(ValueError, match='node budget'):
        await _closed_author_shadow(SimpleNamespace(send=send), 'modus-navbar')


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['missing_card', 'shadow_only', 'host_clip', 'slotted_mask'])
async def test_private_content_contract_covers_admitted_shadow_and_slots(variant):
    import base64
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0;background:white"><nav-owned id="owned" style="display:block;height:100px;overflow:auto"></nav-owned>
              <main id="panel"><textarea class="private" style="height:20px">Draft</textarea>
              <button id="send" style="background:red;height:40px;width:100px">Send</button></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#owned').evaluate('''(host, variant) => {
              const root=host.attachShadow({mode:'open'});
              if(variant==='missing_card') root.innerHTML='<article class="card"><div class="private" style="height:20px">First</div></article><article class="card">Unmarked confidential second card</article>';
              if(variant==='shadow_only') {
                root.innerHTML='<div class="private" style="height:40px">Shadow private</div>';
                document.querySelector('textarea').remove();
              }
              if(variant==='host_clip') root.innerHTML='<div class="private" style="height:600px;background:blue">Tall private</div>';
              if(variant==='slotted_mask') {
                root.innerHTML='<div class="private" style="height:40px"><slot name="private"></slot></div>';
                host.innerHTML='<span slot="private">Slotted confidential content</span>';
              }
            }''', variant)
            boundary = EmissionBoundary(ContentPolicy(False))
            if variant == 'slotted_mask':
                boundary.register_secret('Slotted confidential content')
            reasons = []
            image = await capture_studio_viewport(page, boundary, 'http://localhost:9000',
                owned_shadow_selector='nav-owned#owned', content_mask_selector='.private',
                content_mask_anchor='#panel', content_mask_requirements=(('.card', '.private'),),
                unavailable=reasons.append)
            if variant == 'missing_card':
                assert image is None and reasons == ['content_mask_missing']
            else:
                assert image is not None, reasons
                colors = await page.evaluate('''async png => {
                  const image=new Image();image.src='data:image/png;base64,'+png;await image.decode();
                  const c=document.createElement('canvas');c.width=600;c.height=300;
                  const ctx=c.getContext('2d');ctx.drawImage(image,0,0);
                  const send=document.querySelector('#send').getBoundingClientRect();
                  return {private:[...ctx.getImageData(20,20,1,1).data],
                    send:[...ctx.getImageData(send.x+send.width-8,send.y+send.height/2,1,1).data]};
                }''', base64.b64encode(image).decode())
                assert colors['private'] == [0, 0, 0, 255]
                assert colors['send'] == [255, 0, 0, 255]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_content', [False, True])
async def test_owned_chrome_admits_only_bound_assets_and_named_components(allow_content):
    import base64
    from api_agent.ui.evidence import OwnedChromeContract
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0"><nav-owned id="owned" style="display:block;height:80px"></nav-owned>
              <main id="panel"><div id="private" style="height:40px">Private</div></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#owned').evaluate('''host => {
              const source='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><path fill="lime" d="M0 0h40v40H0z"/></svg>');
              host.logoOptions={primary:{url:source}};
              const root=host.attachShadow({mode:'open'});
              root.innerHTML='<nav style="height:80px;background:blue"><img alt="Canonical" style="position:absolute;left:10px;top:10px;width:40px;height:40px">'+
                '<known-tooltip style="position:absolute;left:60px;top:10px;width:40px;height:40px"></known-tooltip>'+
                '<unknown-widget style="position:absolute;left:110px;top:10px;width:40px;height:40px"></unknown-widget>'+
                '<img alt="Unknown" style="position:absolute;left:160px;top:10px;width:40px;height:40px"></nav>';
              root.querySelectorAll('img').forEach(image => image.src=source);
              root.querySelector('known-tooltip').attachShadow({mode:'open'}).innerHTML=
                '<button style="padding:0;border:0"><svg width="40" height="40"><path fill="lime" d="M0 0h40v40H0z"/></svg></button>';
              root.querySelector('unknown-widget').attachShadow({mode:'open'}).innerHTML='<div style="height:40px;background:red">Unknown</div>';
            }''')
            await page.wait_for_function("[...document.querySelector('#owned').shadowRoot.querySelectorAll('img')].every(image => image.complete && image.naturalWidth)")
            image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(allow_content)),
                'http://localhost:9000', owned_chrome=OwnedChromeContract('nav-owned#owned',
                    shadow_selectors=('known-tooltip',), svg_selector='button svg',
                    image_selector='img[alt="Canonical"]', image_source_properties=('logoOptions.primary.url',)),
                content_mask_selector='#private', content_mask_anchor='#panel')
            assert image is not None
            colors = await page.evaluate('''async png => {
              const image=new Image();image.src='data:image/png;base64,'+png;await image.decode();
              const c=document.createElement('canvas');c.width=600;c.height=300;
              const ctx=c.getContext('2d');ctx.drawImage(image,0,0);
              return [30,80,130,180].map(x=>[...ctx.getImageData(x,30,1,1).data]);
            }''', base64.b64encode(image).decode())
            assert colors == [[0,255,0,255], [0,255,0,255], [0,0,0,255], [0,0,0,255]]
        finally:
            await browser.close()


@pytest.mark.parametrize('changes', [
    {'selector': ''}, {'selector': 1}, {'selector': 'x' * 257},
    {'shadow_selectors': []}, {'shadow_selectors': ('x',) * 17},
    {'shadow_selectors': (1,)}, {'shadow_selectors': ('',)}, {'shadow_selectors': ('x' * 257,)},
    {'svg_selector': ''}, {'svg_selector': 1}, {'svg_selector': 'x' * 257},
    {'image_selector': ''}, {'image_selector': 1}, {'image_selector': 'x' * 257},
    {'image_sources': []}, {'image_sources': ('x',) * 9},
    {'image_sources': (1,)}, {'image_sources': ('',)}, {'image_sources': ('x' * 65537,)},
    {'image_source_properties': []}, {'image_source_properties': ('x',) * 9},
    {'image_source_properties': (1,)}, {'image_source_properties': ('',)},
    {'image_source_properties': ('x' * 257,)},
])
def test_invalid_owned_chrome_fields_reject(changes):
    import asyncio
    from api_agent.ui.evidence import OwnedChromeContract
    contract = OwnedChromeContract(**{'selector': 'nav-owned#owned', **changes})
    reasons = []
    assert asyncio.run(capture_studio_viewport(None, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', owned_chrome=contract, unavailable=reasons.append)) is None
    assert reasons == ['shadow_contract']


def test_owned_chrome_rejects_wrong_contract_or_conflicting_shadow_selector():
    import asyncio
    from api_agent.ui.evidence import OwnedChromeContract
    for contract, selector in ((object(), None), (OwnedChromeContract('nav-owned#owned'), '#other')):
        assert asyncio.run(capture_studio_viewport(None, EmissionBoundary(ContentPolicy(True)),
            'http://localhost:9000', owned_chrome=contract, owned_shadow_selector=selector)) is None
