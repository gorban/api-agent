"""A compiled binding plan carries the declared Application and pack-supplied prompt slots."""
import json
from pathlib import Path, PurePosixPath

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from test_binding_application import FACTORY, SECTION
from test_workflow_definition import make_graph

from api_agent.runtime.local_tools import selected_application
from api_agent.workflows import artifact
from api_agent.workflows.artifact import ArtifactRejected, CompiledDefinition, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.definition import load_definition


def build(tmp_path, *, application=True):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = make_graph(root)
    files = {'aq_binding/__init__.py': b'', 'aq_binding/native.py': (root / 'native.py').read_bytes()}
    if application:
        (root / 'application.py').write_text(FACTORY)
        entry.write_text(entry.read_text().replace('## Flow', SECTION))
        files['aq_binding/application.py'] = FACTORY.encode()
    wheel = build_wheel(tmp_path, 'aq-binding', files)
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)


@pytest.fixture
def built(tmp_path):
    return build(tmp_path)


def test_plan_without_application_keeps_the_original_sections(tmp_path):
    built = build(tmp_path, application=False)

    assert 'application_binding' not in json.loads(built.plan)
    assert load_built(built).application_binding is None


def test_compiled_application_round_trips_and_is_selected(built):
    definition = load_built(built)

    assert isinstance(definition, CompiledDefinition)
    assert definition.application_binding == ('create_application', PurePosixPath('/artifact/base/application.py'))
    application = selected_application(definition, config={'KEY': 1})
    assert application.definition is definition
    assert application.config == {'KEY': 1}


def test_compiled_application_source_is_a_hashed_binding_not_embedded(built):
    value = json.loads(built.plan)

    assert 'base/application.py' in {row['source'] for row in value['bindings']}
    assert 'def create_application' not in built.plan.decode()


@pytest.mark.parametrize('encoded,message', [
    (['create_application', {'$p': 'base/native.py'}], 'does not define declared binding create_application'),
    (['bad-name', {'$p': 'base/application.py'}], 'invalid application binding'),
    (['create_application', {'$p': 'base/missing.py'}], 'invalid application binding'),
    ('create_application', 'invalid application binding'),
    (['create_other', {'$p': 'base/application.py'}], 'does not define declared binding create_other'),
])
def test_invalid_application_plan_rows_are_rejected(built, encoded, message):
    plan = edited(built.plan, lambda value: value.update(application_binding=encoded))

    with pytest.raises(ArtifactRejected, match=message):
        load_built(built, plan)


def test_application_binding_is_not_valid_beside_typed_state(built):
    plan = edited(built.plan, lambda value: value.update(state={}))

    with pytest.raises(ArtifactRejected, match='missing or unknown sections'):
        load_built(built, plan)


def test_unknown_plan_sections_are_still_rejected(built):
    plan = edited(built.plan, lambda value: value.update(surprise=1))

    with pytest.raises(ArtifactRejected, match='missing or unknown sections'):
        load_built(built, plan)
