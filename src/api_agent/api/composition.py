"""Compose selected-pack identity and session owners without opening listeners."""
from api_agent.auth.extensions import load_identity_adapter
from api_agent.auth.session import SessionStore
from .application import Runtime


def compose_runtime(context, values, *, callback_origin):
    """Caller supplies runtime-resolved values and its approved HTTPS origin.

    Provider transport and public authentication handlers are composed in later
    steps; this foundation never initiates authentication or claims it succeeded.
    """
    adapter = load_identity_adapter(context, values)
    sessions = SessionStore(context.pack_id, callback_origin)
    return Runtime(context.pack_id, identity_adapter=adapter, sessions=sessions)
