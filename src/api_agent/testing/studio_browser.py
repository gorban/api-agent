"""Browser actions against the included Studio prepared page."""
import re
from urllib.parse import urlsplit

from api_agent.providers.progress import component_timeout
from api_agent.testing.ui_turn import ui_turn


async def browser_prompt(prepared, *, prompt="Answer with exactly: Ok"):
    """Consume the selected provider's original prepared page for one request."""
    from playwright.async_api import expect
    async with ui_turn(prepared) as turn:
        page = prepared.session.page
        errors = []
        listener = lambda error: errors.append(str(error))
        page.on('pageerror', listener)
        try:
            async with component_timeout('Studio browser request', 150):
                try:
                    # Existing config and catalog fetches each allow five seconds.
                    await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled', timeout=15000)
                except AssertionError as error:
                    diagnostic = await page.locator('#model-status').inner_text()
                    raise AssertionError('Studio catalog did not settle: ' + diagnostic) from error
                try:
                    await expect(page.locator('#model')).to_have_value(re.compile(r'.+'))
                except AssertionError as error:
                    diagnostic = await page.locator('#model-status').inner_text()
                    raise AssertionError('Studio model choices are unavailable: ' + diagnostic) from error
                diagnostic = await page.locator('#model-status').inner_text()
                assert not diagnostic, 'Studio model selection is unavailable: ' + diagnostic
                await expect(page.locator('#send')).to_be_enabled()
                await page.locator('#prompt').fill(prompt)
                async with page.expect_request(lambda request: request.method == 'POST' and
                        urlsplit(request.url).path in ('/app/agent/chat', '/app/agent/chat/stream')) as submitted:
                    await page.locator('#send').click()
                request = await submitted.value
                payload = request.post_data_json
                turn.bind(conversation_id=payload['conversation_id'], turn_id=payload['session_id'])
                status = page.locator('.assistant .metadata-grid dd').last
                await expect(status).to_have_text(re.compile(r'Complete|Incomplete'), timeout=120000)
                assert await status.text_content() == 'Complete', await page.locator('#status').inner_text()
                await page.wait_for_function("""() => {
                    const row = [...document.querySelectorAll('.assistant')].at(-1);
                    return row && !row.classList.contains('draft') &&
                        !row.querySelector('.thinking-indicator') &&
                        row.querySelector('.message-body') &&
                        row.getAnimations({subtree:true}).every(animation =>
                            animation.playState === 'finished' || animation.playState === 'idle') &&
                        document.querySelector('#stop')?.hidden;
                }""", timeout=120000)
                answer = await page.locator('.assistant .message-body').last.inner_text()
                assert answer.strip(), 'Agent returned an empty browser response.'
                assert not errors, errors
                assert await page.locator('#stop').is_hidden()
                return answer
        finally:
            page.remove_listener('pageerror', listener)

