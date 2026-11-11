"""Source and compiled workflows agree on abandoned outputs and nullable appends."""
import asyncio
import json

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, load_built
from api_agent.workflows import LayoutError
from api_agent.workflows.predicates import PredicateError
from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from expiry_support import NATIVE, expiring
from loop_support import build
from session_workflow_support import session_entry, session_workflow, turn
from test_workflow_expiry import group


def compiled(entry, tmp_path, package, files):
    wheel = build_wheel(tmp_path, package.replace('_', '-'), files)
    plan = compile_plan(entry, base_root=entry.parent, pack_root=entry.parent, wheels=[wheel])
    return load_built(Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path))


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
@pytest.mark.parametrize('use_abandoned', [False, True])
def test_expiry_routes_only_using_surviving_results(tmp_path, artifact, use_abandoned):
    root = tmp_path / 'tree'
    root.mkdir()
    condition = ('group.expired and prepare["ok"]' if use_abandoned else
                 'group.expired and (group.attempt >= 1 or prepare["ok"])')
    entry = group(root, ('group', 'timed_out', condition), ('group', 'finish', 'otherwise'))
    if artifact == 'source':
        workflow, native, _ = build(entry)
    else:
        loaded = compiled(entry, tmp_path, 'aq_expiry',
                          {'aq_expiry/__init__.py': b'', 'aq_expiry/native.py': NATIVE.encode()})
        workflow, native = loaded.workflow(), vars(loaded.modules['aq_expiry.native'])
    native['SCRIPT']['work'] = ['hang']
    if use_abandoned:
        with pytest.raises(TimeoutError):
            asyncio.run(workflow.arun({'accept_at': 1}))
        assert native['CALLS'] == ['prepare', 'work']
    else:
        values = asyncio.run(workflow.arun({'accept_at': 1}))
        assert native['CALLS'] == ['prepare', 'work', 'timed_out']
        assert 'prepare' not in values


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
def test_nullable_append_is_a_no_write_and_preserves_prior_values(tmp_path, artifact):
    root = tmp_path / 'tree'
    entry = session_entry(root)
    schema_path = root / 'schemas' / 'reply_output.json'
    schema = json.loads(schema_path.read_text())
    schema['properties']['messages']['type'] = ['array', 'null']
    schema_path.write_text(json.dumps(schema))
    nodes = root / 'src' / 'aq_session' / 'nodes.py'
    nodes.write_text(nodes.read_text().replace("'messages': [{'role': 'user', 'text': message}, {'role': 'assistant', 'text': text}]", "'messages': None if message == 'none' else [{'role': 'user', 'text': message}, {'role': 'assistant', 'text': text}]"))
    if artifact == 'source':
        workflow = session_workflow(entry)
    else:
        package = root / 'src' / 'aq_session'
        loaded = compiled(entry, tmp_path, 'aq_session', {f'aq_session/{name}': (package / name).read_bytes() for name in ('__init__.py', 'nodes.py')})
        workflow = loaded.workflow()
    turn(workflow, 'none', thread='null-first')
    assert 'history' not in asyncio.run(workflow.session('null-first'))
    turn(workflow, 'first')
    turn(workflow, 'none')
    assert turn(workflow, 'last')['reply']['seen']['history'] == 2


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
@pytest.mark.parametrize('owner', ['group', 'work'])
def test_expiry_dispatch_skips_only_abandoned_candidate(tmp_path, artifact, owner):
    root = tmp_path / 'tree'
    root.mkdir()
    rows = ((owner, 'timed_out', f'{owner}.expired and prepare["ok"]'),
            (owner, 'timed_out', f'{owner}.expired and {owner}.attempt >= 1'),
            (owner, 'finish', 'otherwise'))
    if owner == 'group':
        entry = group(root, *rows)
    else:
        # A completed work visit is abandoned when its next visit expires.
        rows = (('work', 'timed_out', 'work.expired and work["n"] == 1'),
                ('work', 'timed_out', 'work.expired and work.attempt >= 2'),
                ('work', 'review', 'otherwise'),
                ('review', 'work', 'work.attempt < NODE_TIMEOUT_PASSES'),
                ('review', 'finish', 'otherwise'))
        entry = expiring(root, *rows, flow=[('node', name) for name in ('work', 'review', 'finish', 'timed_out')])
    if artifact == 'source':
        workflow, native, _ = build(entry)
    else:
        loaded = compiled(entry, tmp_path, 'aq_expiry',
                          {'aq_expiry/__init__.py': b'', 'aq_expiry/native.py': NATIVE.encode()})
        workflow, native = loaded.workflow(), vars(loaded.modules['aq_expiry.native'])
    native['SCRIPT']['work'] = ['hang'] if owner == 'group' else ['ok', 'hang']
    values = asyncio.run(workflow.arun({'accept_at': 1}))
    assert native['CALLS'][-1] == 'timed_out'
    assert ('prepare' if owner == 'group' else 'work') not in values


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
def test_expired_component_preserves_prior_completed_result(tmp_path, artifact):
    root = tmp_path / 'tree'
    root.mkdir()
    rows = (('group', 'timed_out', 'group.expired and group["n"] == 1'),
            ('group', 'review', 'otherwise'),
            ('review', 'group', 'group.attempt < NODE_TIMEOUT_PASSES'),
            ('review', 'finish', 'otherwise'))
    entry = expiring(root, *rows, flow=[('component', 'group'), *[('node', x) for x in ('review', 'finish', 'timed_out')]],
                     owners=(('group', 'GROUP_TIMEOUT'),),
                     components={'group': (['prepare', 'work'], '')})
    if artifact == 'source':
        workflow, native, _ = build(entry)
    else:
        loaded = compiled(entry, tmp_path, 'aq_expiry',
                          {'aq_expiry/__init__.py': b'', 'aq_expiry/native.py': NATIVE.encode()})
        workflow, native = loaded.workflow(), vars(loaded.modules['aq_expiry.native'])
    native['SCRIPT']['work'] = ['ok', 'hang']
    values = asyncio.run(workflow.arun({'accept_at': 1}))
    assert native['CALLS'] == ['prepare', 'work', 'review', 'prepare', 'work', 'timed_out']
    assert 'work' not in values


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
def test_append_rejects_array_union_with_non_null_scalar(tmp_path, artifact):
    root = tmp_path / 'tree'
    entry = session_entry(root)
    schema_path = root / 'schemas' / 'reply_output.json'
    if artifact == 'source':
        schema = json.loads(schema_path.read_text())
        schema['properties']['messages']['type'] = ['array', 'string']
        schema_path.write_text(json.dumps(schema))
        with pytest.raises(LayoutError, match='append session source'):
            session_workflow(entry)
    else:
        package = root / 'src' / 'aq_session'
        wheel = build_wheel(tmp_path, 'aq-session', {f'aq_session/{name}': (package / name).read_bytes() for name in ('__init__.py', 'nodes.py')})
        plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
        built = Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)
        def invalid(value):
            tables = dict(value['layout']['tables']['$m'])
            reply = next(row for row in tables['Functions'] if dict(row['$m'])['ID'] == 'reply')
            schema = dict(reply['$m'])['Output schema']
            properties = dict(schema['$m'])['properties']
            messages = dict(properties['$m'])['messages']
            for pair in messages['$m']:
                if pair[0] == 'type':
                    pair[1] = ['array', 'string']
        with pytest.raises(ArtifactRejected, match='append session source'):
            load_built(built, edited(plan, invalid))


@pytest.mark.parametrize('artifact', ['source', 'compiled'])
def test_expiry_does_not_hide_missing_unrelated_result(tmp_path, artifact):
    root = tmp_path / 'tree'
    root.mkdir()
    source = NATIVE.replace("CALLS.append('prepare')\n    return {'ok': True}",
                            "CALLS.append('prepare')\n    return {}")
    entry = expiring(root, ('group', 'timed_out', 'group.expired and prepare["ok"]'),
                     ('group', 'finish', 'otherwise'),
                     flow=[('node', 'prepare'), ('component', 'group'), ('node', 'finish'), ('node', 'timed_out')],
                     owners=(('group', 'GROUP_TIMEOUT'),), components={'group': (['work'], '')})
    (root / 'native.py').write_text(source)
    if artifact == 'source':
        workflow, native, _ = build(entry)
    else:
        loaded = compiled(entry, tmp_path, 'aq_expiry',
                          {'aq_expiry/__init__.py': b'', 'aq_expiry/native.py': source.encode()})
        workflow, native = loaded.workflow(), vars(loaded.modules['aq_expiry.native'])
    native['SCRIPT']['work'] = ['hang']
    with pytest.raises(PredicateError, match='prepare.ok was not produced'):
        asyncio.run(workflow.arun({'accept_at': 1}))
