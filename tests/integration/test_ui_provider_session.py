"""Selected-provider launch and original rendered session, without inference."""
import pytest


pytestmark = pytest.mark.feature('UI sessions')

@pytest.mark.requires_ui_session
def test_ui_provider_session(ui_provider_session):
    """Shared readiness proof; provider-specific interactions stay with providers."""
    assert ui_provider_session.session is not None
    assert ui_provider_session.session.browser.is_connected()
    assert not ui_provider_session.session.page.is_closed()
