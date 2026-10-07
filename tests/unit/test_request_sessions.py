import asyncio
import pytest
from api_agent.auth.session import SessionStore, SessionError

def test_request_sessions_do_not_own_provider_token_state():
    now = [0]
    store = SessionStore('demo', 'https://agent.example', clock=lambda: now[0], session_ttl=3, max_sessions=1)
    opened = store.open(None, 'first', create=True)
    assert store.open(opened.cookie_value, 'first').handle is opened.handle
    with pytest.raises(SessionError, match='owner_mismatch'):
        store.open(opened.cookie_value, 'second')
    with pytest.raises(SessionError, match='capacity'):
        store.open(None, 'second', create=True)
    other = SessionStore('other', 'https://agent.example')
    with pytest.raises(SessionError, match='invalid_session'):
        other.open(opened.cookie_value, 'first')
    now[0] = 4
    with pytest.raises(SessionError, match='invalid_session'):
        store.open(opened.cookie_value, 'first')
    asyncio.run(store.aclose())
    with pytest.raises(SessionError, match='store_closed'):
        store.open(None, None, create=True)
    assert not hasattr(store, 'begin')


def test_config_session_bootstraps_provider_fields_and_cli_precedence(tmp_path):
    from api_agent.config.session import ConfigSession
    location = factory(tmp_path, "raise AssertionError('configuration must not import factory')\n")
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/config.toml').write_text('version = 1\n[fields.PROVIDER_LIMIT]\ntype = "int"\ndefault = 7\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False,
                              dotenv_path=tmp_path / '.env', anchors={'owner_root': tmp_path})
    session = ConfigSession(context, load_shared_schema(),
        argv=['--llm-provider', location, '--provider-limit', '9', '--tui-mode', 'unit'],
        environ={'LLM_PROVIDER': '/missing.py:create'})
    assert 'PROVIDER_LIMIT' in session.schema.fields
    result = session.result()
    result.raise_for_errors()
    assert result.values['PROVIDER_LIMIT'] == 9
