"""Build-time compilation: wheels, binding selection and plan encoding."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
import zipfile

import pytest

from artifact_acceptance_support import QUALIFICATION, ROOT, build_wheel
from artifact_unit_support import (build_rich_v3, copy_tree, isolated_imports, load_built, qualification_wheel,  # noqa: F401
                                   rich_built, v2_built, v3_built)

from api_agent.workflows import artifact_compile
from api_agent.workflows.artifact import canonical_json, plan_digest
from api_agent.workflows.artifact_compile import (ArtifactCompileError, compile_definition, compile_plan,
                                                  load_source_definition, read_wheel)
from api_agent.workflows.model import Replacement, Resource
from api_agent.workflows.stateful import load_stateful_definition

NATIVE = (QUALIFICATION / 'src/aq_bindings/native.py').read_bytes()


def parsed_v3(root=QUALIFICATION):
    return load_stateful_definition(root / 'flow.md.html', base_root=root, pack_root=root)


def wheel_files(**extra):
    return {'aq_bindings/__init__.py': b'', 'aq_bindings/native.py': NATIVE, **extra}


def test_wheel_is_read_with_its_identity_and_file_hashes(tmp_path):
    wheel = build_wheel(tmp_path, 'aq-bindings', wheel_files())

    info = read_wheel(wheel)

    assert (info.name, info.version, info.dist_info) == ('aq-bindings', '1.0', 'aq_bindings-1.0.dist-info')
    assert info.files['aq_bindings/native.py'] == sha256(NATIVE).hexdigest()
    assert 'aq_bindings-1.0.dist-info/RECORD' not in info.files
    assert set(info.files) == {'aq_bindings/__init__.py', 'aq_bindings/native.py',
                               'aq_bindings-1.0.dist-info/METADATA', 'aq_bindings-1.0.dist-info/WHEEL'}


def rewrite(wheel, mutate):
    """Rewrite ``wheel`` after ``mutate(members)`` where members maps names to bytes."""
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return wheel


@pytest.mark.parametrize('name,mutate,match', [
    ('tampered_member', lambda m: m.__setitem__('aq_bindings/native.py', b'x = 1\n'), 'not a verifiable source wheel'),
    ('unlisted_member', lambda m: m.__setitem__('aq_bindings/extra.py', b''), 'not a verifiable source wheel'),
    ('no_record', lambda m: m.pop('aq_bindings-1.0.dist-info/RECORD'), 'not a verifiable source wheel'),
    ('no_metadata', lambda m: m.pop('aq_bindings-1.0.dist-info/METADATA'), 'not a verifiable source wheel'),
    ('second_dist_info', lambda m: m.__setitem__('other-1.0.dist-info/METADATA', b''), 'exactly one dist-info'),
    ('no_dist_info', lambda m: [m.pop(k) for k in [k for k in m if '.dist-info' in k]], 'exactly one dist-info'),
])
def test_wheels_that_do_not_verify_against_their_record_are_rejected(tmp_path, name, mutate, match):
    wheel = rewrite(build_wheel(tmp_path, 'aq-bindings', wheel_files()), mutate)

    with pytest.raises(ArtifactCompileError, match=match):
        read_wheel(wheel)


def test_unreadable_duplicate_and_oversized_wheels_are_rejected(tmp_path, monkeypatch):
    garbage = tmp_path / 'garbage.whl'
    garbage.write_bytes(b'not a zip')
    duplicate = tmp_path / 'duplicate.whl'
    with zipfile.ZipFile(duplicate, 'w') as archive, pytest.warns(UserWarning, match='Duplicate name'):
        archive.writestr('a.py', b'1')
        archive.writestr('a.py', b'2')
    good = build_wheel(tmp_path, 'aq-bindings', wheel_files())

    with pytest.raises(ArtifactCompileError, match='not a readable wheel'):
        read_wheel(garbage)
    with pytest.raises(ArtifactCompileError, match='not a readable wheel'):
        read_wheel(tmp_path / 'missing.whl')
    with pytest.raises(ArtifactCompileError, match='duplicate wheel member'):
        read_wheel(duplicate)
    monkeypatch.setattr(artifact_compile, 'MAX_WHEEL_BYTES', 10)
    with pytest.raises(ArtifactCompileError, match='wheel exceeds'):
        read_wheel(good)


def test_every_native_source_needs_exactly_one_wheel_file_with_its_bytes(tmp_path):
    other = build_wheel(tmp_path, 'aq-other', {'aq_other/__init__.py': b'', 'aq_other/mod.py': b'X = 1\n'})
    with pytest.raises(ArtifactCompileError, match='found 0'):
        compile_definition(parsed_v3(), [other])
    first = build_wheel(tmp_path, 'aq-first', wheel_files())
    second = build_wheel(tmp_path, 'aq-second', wheel_files())
    with pytest.raises(ArtifactCompileError, match='found 2'):
        compile_definition(parsed_v3(), [first, second])


def test_identical_bytes_at_several_wheel_paths_resolve_by_the_logical_path_suffix(tmp_path):
    wheel = build_wheel(tmp_path, 'aq-bindings', wheel_files(**{'aq_copy/native.py': NATIVE}))

    plan = json.loads(compile_definition(parsed_v3(), [wheel]))

    assert [(row['module'], row['file']) for row in plan['bindings']] == [
        ('aq_bindings.native', 'aq_bindings/native.py')]


def test_only_python_files_with_importable_names_can_supply_a_binding(tmp_path):
    wheel = build_wheel(tmp_path, 'aq-bindings', {'aq-bindings/native.py': NATIVE})

    with pytest.raises(ArtifactCompileError, match='found 0'):
        compile_definition(parsed_v3(), [wheel])


def test_declared_dependency_modules_must_match_their_wheel_modules(tmp_path):
    root = copy_tree(QUALIFICATION, tmp_path / 'tree')
    (root / 'src/aq_bindings/helper.py').write_bytes(b'HELPER = 1\n')
    flow = root / 'flow.md.html'
    flow.write_text(flow.read_text() + '\n## Native Dependencies\n\n| Module | Source |\n| --- | --- |\n'
                    '| aq_bindings.helper | [Python](src/aq_bindings/helper.py) |\n')
    misplaced = build_wheel(tmp_path, 'aq-bindings', wheel_files(**{'aq_elsewhere/helper.py': b'HELPER = 1\n'}))

    with pytest.raises(ArtifactCompileError, match='is not the declared dependency aq_bindings.helper'):
        compile_definition(parsed_v3(root), [misplaced])


def test_helper_native_sources_cannot_be_compiled(tmp_path):
    root = copy_tree(QUALIFICATION, tmp_path / 'tree')
    (root / 'src/aq_bindings/helper.py').write_bytes(b'HELPER = 1\n')
    flow = root / 'flow.md.html'
    flow.write_text(flow.read_text() + '\n## Native Sources\n\n| Source |\n| --- |\n'
                    '| [Python](src/aq_bindings/helper.py) |\n')

    with pytest.raises(ArtifactCompileError, match='helper native sources cannot be compiled'):
        compile_definition(parsed_v3(root), [qualification_wheel(tmp_path)])


def test_resources_need_a_supported_kind_and_an_admitted_key(tmp_path):
    definition = parsed_v3()
    wheel = qualification_wheel(tmp_path)
    digest = sha256(b'z').hexdigest()

    with pytest.raises(ArtifactCompileError, match='unsupported resource kind mystery'):
        compile_definition(replace(definition, resources=(*definition.resources,
                                                          Resource('mystery', 'base/zzz', b'z', digest))), [wheel])
    with pytest.raises(ArtifactCompileError, match='or no admitted key'):
        compile_definition(replace(definition, resources=(*definition.resources,
                                                          Resource('data', 'base/zzz', b'z', digest))), [wheel])


def test_plan_values_must_be_json_representable():
    encode = artifact_compile._encoder(lambda path: 'logical')

    assert encode({'a': (1, 2.5, None, True, 'x')}) == {'$m': [['a', [1, 2.5, None, True, 'x']]]}
    for value in (float('inf'), {1: 'x'}, {'a': {2}}, object()):
        with pytest.raises(ArtifactCompileError):
            encode(value)


def test_identity_free_mappings_compile_in_canonical_order(tmp_path):
    definition = parsed_v3()
    wheel = qualification_wheel(tmp_path)
    declarations = dict(definition.selected_declarations)
    reversed_declarations = dict(reversed(list(declarations.items())))

    first = compile_definition(
        replace(definition, selected_declarations=MappingProxyType(declarations)), [wheel])
    second = compile_definition(
        replace(definition, selected_declarations=MappingProxyType(reversed_declarations)), [wheel])

    assert first == second


def test_source_definition_loader_selects_the_engine_family_by_heading():
    from api_agent.workflows.definition import WorkflowDefinition
    from api_agent.workflows.stateful import StatefulDefinition

    v3 = load_source_definition(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION, pack_root=QUALIFICATION)
    v2 = load_source_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')

    assert isinstance(v3, StatefulDefinition) and isinstance(v2, WorkflowDefinition)


def test_replacements_are_compiled_with_their_resolved_selection(tmp_path):
    root = copy_tree(QUALIFICATION, tmp_path / 'tree')
    (root / 'alt.md.html').write_text((root / 'intake.md.html').read_text())
    selected = (Replacement('intake', Path('alt.md.html'), 'base'),)
    wheel = qualification_wheel(tmp_path)

    plan = compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel],
                        replacements=selected)

    parsed = load_stateful_definition(root / 'flow.md.html', base_root=root, pack_root=root, replacements=selected)
    assert json.loads(plan)['replacements'] == [['intake', 'base', 'alt.md.html']]
    assert json.loads(plan)['definition_id'] == parsed.definition_id
    assert plan != compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel])


def test_compilation_is_independent_of_the_checkout_location(tmp_path):
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    first = compile_plan(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION, pack_root=QUALIFICATION,
                         wheels=[qualification_wheel(tmp_path / 'one')])
    moved = copy_tree(QUALIFICATION, tmp_path / 'elsewhere' / 'checkout')
    second = compile_plan(moved / 'flow.md.html', base_root=moved, pack_root=moved,
                          wheels=[qualification_wheel(tmp_path / 'two')])

    assert first == second and plan_digest(first) == plan_digest(second)
    assert str(tmp_path).encode() not in first and str(QUALIFICATION).encode() not in first


def test_plan_never_embeds_declaration_or_native_source_bytes(rich_built, v2_built):
    for built in (rich_built, v2_built):
        value = json.loads(built.plan)
        text = built.plan.decode()
        for item in value['resources']:
            if item['kind'] in ('declaration', 'native_source', 'native_dependency'):
                assert item['content'] is None
        assert 'def ' not in text and '# Workflow' not in text and '## Native' not in text


def test_rich_plan_is_deterministic_for_equal_inputs(tmp_path_factory):
    one = build_rich_v3(tmp_path_factory.mktemp('one'))
    two = build_rich_v3(tmp_path_factory.mktemp('two'))

    assert one.plan == two.plan
    assert canonical_json(json.loads(one.plan)) == one.plan
