"""Explicit integration consumer for the selected UI provider; no LLM setup."""
from api_agent.ui.session import PreparedUISession


async def prepare_ui_session(config, services, *, browser_owner, user_index=None,
                             credentials=None, runtime_owner=None, **options):
    """Return a ready owner that the fixture retains through DEBUG inspection.

    Supply scoped credentials or a synchronous credentials_for(roles, index)
    binder that validates configured profiles before launch without fetching secrets. Runtime dependencies/TLS belong to the supplied
    operation owner; provider launch uses those exact injected services.
    """
    owner = PreparedUISession(config, services, browser_owner=browser_owner,
        user_index=user_index, credentials=credentials, runtime_owner=runtime_owner, **options)
    await owner.start()
    return owner
