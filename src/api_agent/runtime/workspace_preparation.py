"""Prepare the workspaces that selected providers declare, before any worker starts."""
from functools import partial
import os
from pathlib import Path
import threading


def _text(value):
    value = getattr(value, 'value', value)
    return value if isinstance(value, str) and value.strip() else None


def _workspace_path(value):
    value = getattr(value, 'value', value)
    try:
        value = os.fspath(value)
    except TypeError:
        return None
    return value if isinstance(value, str) and value.strip() and '\0' not in value else None


def prepare_selected_workspaces(context, values, kinds, *, cancellation=None):
    """Install each selected provider's declared Node workspace once, by canonical path.

    Provider factories are never imported; only their inert manifests are read.
    Credential values are looked up by declared field name and go to the installer
    alone. Nothing happens for kinds without a declaration.
    """
    if not kinds:
        return ()
    from api_agent.providers.distribution import selected_manifests
    from api_agent.runtime.node_workspace import ensure_node_workspace
    manifests = selected_manifests(values, owner_root=getattr(context, 'owner_root', context.base_root),
                                   active_kinds=kinds)
    prepared = []
    for manifest in manifests:
        for workspace in manifest.node_workspaces:
            selected = _workspace_path(values.get(workspace.path_field))
            if selected is None:
                raise ValueError(f'{workspace.path_field} must select the workspace to prepare.')
            root = Path(selected).expanduser().resolve()
            if root in prepared:
                continue
            ensure_node_workspace(root, ready=workspace.ready, environment=workspace.environment,
                credentials={name: _text(values.get(name)) for name in workspace.credential_fields},
                cancellation=cancellation)
            prepared.append(root)
    return tuple(prepared)


async def aprepare_selected_workspaces(context, values, kinds):
    from api_agent.runtime.tasks import offload
    cancellation = threading.Event()
    await offload(partial(prepare_selected_workspaces, context, values, kinds, cancellation=cancellation),
                  cancellation=cancellation)
