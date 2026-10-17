"""Harmless included-frontend interaction through the selected provider owner."""
import pytest


@pytest.mark.requires_ui_session
def test_studio_theme(ui_provider_session, ui_provider_loop):
    page = ui_provider_session.session.page
    ui_provider_loop.run_until_complete(_change_theme(page))


async def _change_theme(page):
    from playwright.async_api import expect
    await expect(page.locator('#prompt')).to_be_visible()
    await page.get_by_role('button', name='Settings', exact=True).click()
    await page.get_by_label('Appearance').select_option('dark')
    await expect(page.get_by_label('Appearance')).to_have_value('dark')
