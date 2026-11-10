"""Build the session-state workflow fixture in a temporary directory."""
import asyncio
from pathlib import Path
import shutil

from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'session_workflow'
FLOW = 'chat.md.html'


def session_entry(root, *edits, fixture=FIXTURE, flow=FLOW):
    """Copy a fixture and apply ``(old, new)`` replacements to its root declaration."""
    shutil.copytree(fixture, root, dirs_exist_ok=True)
    entry = root / flow
    text = entry.read_text()
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    entry.write_text(text)
    return entry


def load_session(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def session_workflow(entry, **kwargs):
    definition = load_session(entry)
    source = next(step.source for step in definition.layout.steps if step.kind == 'node')
    namespace = load_native_catalog(definition, source)
    registry = {row['Registry']: NodeFunction(namespace[row['Registry']])
                for row in definition.layout.tables['Functions']}
    return Workflow(definition, registry, **kwargs)


def turn(workflow, message, thread='chat'):
    return asyncio.run(workflow.arun({'message': message}, thread_id=thread))
