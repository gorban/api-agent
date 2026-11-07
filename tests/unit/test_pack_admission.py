"""Ordered admission and handoff reject changed declarations before effects."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys

import pytest

from api_agent.runtime.pack_admission import composition_identity, composition_report, verify_composition
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from test_pack_composition import BASE, pack


def test_report_captures_order_and_owners_without_configuration_values(tmp_path):
    first = pack(tmp_path, 'first', config='version=1\n[fields.SECRET]\ntype="str"\ndefault="sensitive-value"\n')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second], tmp_path)
    report = composition_report(context)
    assert report['version'] == 1
    assert report['launcher_root'] == str(tmp_path)
    assert [row['id'] for row in report['contributions']] == ['first', 'second', 'runtime-project']
    assert report['contributions'][0]['owner'] == str(first)
    assert 'sensitive-value' not in str(report)
    assert len(report['contributions'][0]['declarations'][0]['sha256']) == 64
    with pytest.raises(FrozenInstanceError):
        context.pack_contributions[0].pack_id = 'changed'
    verify_composition(context, composition_identity(context))


@pytest.mark.parametrize('name', ['pack.toml', 'config.toml', 'provider.toml', 'pyproject.toml'])
def test_changed_declaration_is_rejected(tmp_path, name):
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('version=1\n')
    (first / 'pyproject.toml').write_text('[project]\nname="example-first"\n')
    context = load_launch_context(BASE, [first, second])
    with (first / name).open('a') as stream:
        stream.write('# changed\n')
    with pytest.raises(ResourcePackError, match='changed'):
        verify_composition(context)


def test_new_dependency_manifest_is_rejected(tmp_path):
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    (first / 'pyproject.toml').write_text('[project]\nname="example-first"\n')
    with pytest.raises(ResourcePackError, match='appeared'):
        verify_composition(context)


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'oversized'])
def test_manifest_admission_requires_bounded_regular_file(tmp_path, kind):
    from api_agent.runtime.pack_admission import MAX_DECLARATION_BYTES
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    path = first / 'pack.toml'
    body = path.read_bytes()
    path.unlink()
    if kind == 'symlink':
        target = first / 'actual.toml'
        target.write_bytes(body)
        path.symlink_to(target)
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(body + b'#' * MAX_DECLARATION_BYTES)
    with pytest.raises(ResourcePackError, match='bounded regular'):
        load_launch_context(BASE, [first, second])


def test_changed_order_or_launcher_identity_is_rejected(tmp_path):
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second], first)
    expected = composition_identity(context)
    for changed in (load_launch_context(BASE, [second, first], first),
                    load_launch_context(BASE, [first, second], second)):
        with pytest.raises(ResourcePackError, match='handoff'):
            verify_composition(changed, expected)


def test_relaunch_checks_admission_before_import_or_effects(tmp_path, monkeypatch):
    from api_agent.runtime.dependencies import _relaunch
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    expected = composition_identity(context)
    calls = []
    monkeypatch.setattr(subprocess, 'call', lambda command, **kwargs: calls.append(command) or 0)
    assert _relaunch(context, Path(sys.executable), ['--help']) == 0
    handoff = calls[0][2]
    assert f'_composition_identity={expected!r}' in handoff
    (first / 'pack.toml').write_text('invalid')
    with pytest.raises(ResourcePackError, match='changed'):
        _relaunch(context, Path(sys.executable), ['--help'])
    assert len(calls) == 1


def test_child_handoff_rejects_changed_selection_before_hook_assurance(tmp_path, monkeypatch, capsys):
    from api_agent.runtime.bootstrap import main
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    expected = composition_identity(context)
    (first / 'pack.toml').write_text((first / 'pack.toml').read_text() + '# changed\n')
    def forbidden(*args, **kwargs):
        pytest.fail('hook assurance must follow handoff verification')
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', forbidden)
    assert main(['--help'], base_root=BASE, resource_pack_dir=[first, second],
                _composition_identity=expected) == 2
    assert 'handoff' in capsys.readouterr().err


def test_changed_owner_alias_is_rejected(tmp_path):
    first = pack(tmp_path, 'first', manifest='owner="owner"\n')
    (first / 'owner').mkdir()
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    (first / 'owner').rename(first / 'actual-owner')
    (first / 'owner').symlink_to(first / 'actual-owner', target_is_directory=True)
    with pytest.raises(ResourcePackError, match='ownership'):
        verify_composition(context)


@pytest.mark.parametrize('name', ['config.toml', 'provider.toml'])
def test_declaration_symlink_alias_is_rejected_even_when_resolved_inside_pack(tmp_path, name):
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('version=1\n')
    (first / name).rename(first / 'actual.toml')
    (first / name).symlink_to(first / 'actual.toml')
    with pytest.raises(ResourcePackError, match='bounded regular'):
        load_launch_context(BASE, [first, second])


def test_preparation_rejects_late_mutation_before_parser_install(tmp_path, monkeypatch):
    from api_agent.runtime.dependencies import ensure_environment
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    (first / 'config.toml').write_text('version=1\n# changed\n')
    def forbidden():
        pytest.fail('dependency preparation must follow verification')
    monkeypatch.setattr('api_agent.runtime.dependencies._requirement_types', forbidden)
    with pytest.raises(ResourcePackError, match='changed'):
        ensure_environment(context, [])
    assert not (second / '.venv').exists()


def test_actual_interpreter_handoff_rejects_changed_manifest(tmp_path, monkeypatch):
    from api_agent.runtime.dependencies import _relaunch
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    results = []
    def mutate_and_execute(command, **kwargs):
        with (first / 'pack.toml').open('a') as stream:
            stream.write('# changed during handoff\n')
        result = subprocess.run(command, capture_output=True, text=True, **kwargs)
        results.append(result)
        return result.returncode
    monkeypatch.setattr(subprocess, 'call', mutate_and_execute)
    assert _relaunch(context, Path(sys.executable), ['--help']) == 2
    assert 'handoff does not match' in results[0].stderr


def test_configuration_inventory_is_bounded(tmp_path):
    from api_agent.runtime.pack_admission import MAX_DECLARATIONS
    entries = ','.join(f'"provider{i}.toml"' for i in range(MAX_DECLARATIONS))
    first = pack(tmp_path, 'first', manifest=f'[providers]\nconfigs=[{entries}]\n')
    second = pack(tmp_path, 'second')
    for i in range(MAX_DECLARATIONS):
        (first / f'provider{i}.toml').write_text('version=1\n')
    with pytest.raises(ResourcePackError, match='too many'):
        load_launch_context(BASE, [first, second])


def test_replaced_physical_pack_root_is_rejected(tmp_path):
    import shutil
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    first.rename(tmp_path / 'original')
    shutil.copytree(tmp_path / 'original', first)
    with pytest.raises(ResourcePackError, match='ownership'):
        verify_composition(context)


def test_manifest_parser_uses_captured_bytes_during_replaced_read(tmp_path, monkeypatch):
    import api_agent.runtime.resource_pack as resource_pack
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    original = resource_pack.load_launch_context
    body = (first / 'pack.toml').read_bytes()
    def replace_during_parse(base, directory=None, launcher=None, **kwargs):
        if directory == first:
            (first / 'pack.toml').write_bytes(body.replace(b'id="first"', b'id="unadmitted"'))
            try:
                return original(base, directory, launcher, **kwargs)
            finally:
                (first / 'pack.toml').write_bytes(body)
        return original(base, directory, launcher, **kwargs)
    monkeypatch.setattr(resource_pack, 'load_launch_context', replace_during_parse)
    context = original(BASE, [first, second])
    assert context.pack_contributions[0].pack_id == 'first'


def test_schema_parser_consumes_captured_bytes_after_verification_race(tmp_path, monkeypatch):
    import api_agent.runtime.pack_admission as admission
    from api_agent.config import load_shared_schema
    from api_agent.runtime.pack_composition import load_context_schema
    first = pack(tmp_path, 'first', config='version=1\n[fields.CAPTURED]\ntype="str"\ndefault="admitted"\n')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    original = admission.verify_composition
    def verify_then_replace(selected, expected=None):
        original(selected, expected)
        (first / 'config.toml').write_text('version=1\n[fields.CAPTURED]\ntype="str"\ndefault="unadmitted"\n')
    monkeypatch.setattr(admission, 'verify_composition', verify_then_replace)
    schema = load_context_schema(context, load_shared_schema(BASE))
    assert schema.fields['CAPTURED'].default == 'admitted'


def test_auxiliary_parser_consumes_captured_bytes_after_source_changes(tmp_path):
    from api_agent.config import ConfigSchema
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.runtime.pack_admission import configuration_inputs
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('version=1\n[fields.CAPTURED]\ntype="str"\ndefault="admitted"\n')
    context = load_launch_context(BASE, [first, second])
    (first / 'provider.toml').write_text('version=1\n[fields.CAPTURED]\ntype="str"\ndefault="unadmitted"\n')
    schema = bootstrap_schema(ConfigSchema({}), (), {}, {}, configuration_paths=context.provider_config_paths,
                              **configuration_inputs(context))
    assert schema.fields['CAPTURED'].default == 'admitted'
    assert configuration_inputs(load_launch_context(BASE, second)) == {}


def test_dependency_parser_consumes_captured_bytes_after_verification_race(tmp_path, monkeypatch):
    import api_agent.runtime.pack_admission as admission
    from api_agent.runtime.dependencies import requirements
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    (first / 'pyproject.toml').write_text('[project]\nname="example-first"\ndependencies=["admitted-example>=1"]\n')
    context = load_launch_context(BASE, [first, second])
    original = admission.verify_composition
    def verify_then_replace(selected, expected=None):
        original(selected, expected)
        (first / 'pyproject.toml').write_text('[project]\nname="example-first"\ndependencies=["unadmitted-example>=1"]\n')
        (second / 'pyproject.toml').write_text('[project]\ndependencies=["new-unadmitted-example>=1"]\n')
    monkeypatch.setattr(admission, 'verify_composition', verify_then_replace)
    selected = requirements(context)
    assert 'admitted-example>=1' in selected
    assert all('unadmitted' not in value for value in selected)


def test_disappeared_pack_root_rejects_with_contract_error(tmp_path):
    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    first.rename(tmp_path / 'original')
    with pytest.raises(ResourcePackError, match='unavailable'):
        verify_composition(context)


def project_composition(tmp_path):
    base = tmp_path / 'runtime'
    base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["runtime-admitted>=1"]\n')
    library = tmp_path / 'library'
    library.mkdir()
    (library / 'pyproject.toml').write_text('[project]\ndependencies=["library-admitted>=1"]\n')
    (library / 'flow.md.html').write_text('# Workflow v3\n')
    first = pack(tmp_path, 'first', manifest='''[workflow]
version=2
root="base"
entry="flow.md.html"
[workflow.library]
source="../library"
distribution="example-library"
resource="example_library"
''')
    second = pack(tmp_path, 'second')
    return base, library, first, second


@pytest.mark.parametrize('owner', ['runtime', 'library'])
def test_project_declaration_changes_rejected_in_child_handoff(tmp_path, monkeypatch, capsys, owner):
    from api_agent.runtime.bootstrap import main
    base, library, first, second = project_composition(tmp_path)
    context = load_launch_context(base, [first, second])
    expected = composition_identity(context)
    changed = base if owner == 'runtime' else library
    with (changed / 'pyproject.toml').open('a') as stream:
        stream.write('# changed before child\n')
    def forbidden(*args):
        pytest.fail('child effects must follow project identity verification')
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', forbidden)
    assert main(['--help'], base_root=base, resource_pack_dir=[first, second],
                _composition_identity=expected) == 2
    assert 'handoff' in capsys.readouterr().err


def test_project_dependency_parsing_uses_admitted_runtime_and_library_bytes(tmp_path, monkeypatch):
    import api_agent.runtime.pack_admission as admission
    from api_agent.runtime.dependencies import requirements
    base, library, first, second = project_composition(tmp_path)
    context = load_launch_context(base, [first, second])
    original = admission.verify_composition
    def verify_then_replace(selected, expected=None):
        original(selected, expected)
        for root in (base, library):
            (root / 'pyproject.toml').write_text('[project]\ndependencies=["unadmitted>=1"]\n')
    monkeypatch.setattr(admission, 'verify_composition', verify_then_replace)
    assert requirements(context) == ('runtime-admitted>=1', 'library-admitted>=1')


def test_installed_workflow_project_requires_inventory_ownership(tmp_path):
    from api_agent.runtime.pack_admission import admit_project
    (tmp_path / 'pyproject.toml').write_text('[project]\n')
    with pytest.raises(ResourcePackError, match='not owned'):
        admit_project(tmp_path, 'workflow-project', frozenset({'flow.md.html'}))
    admitted = admit_project(tmp_path, 'workflow-project', frozenset({'pyproject.toml'}))
    assert admitted.declarations[0].relative_path == 'pyproject.toml'
    (tmp_path / 'pyproject.toml').unlink()
    absent = admit_project(tmp_path, 'workflow-project')
    assert absent.absent_declarations == ('pyproject.toml',)


@pytest.mark.parametrize('owner', ['runtime', 'first', 'library'])
@pytest.mark.parametrize('has_project', [True, False])
def test_dependency_alias_swap_preserves_admitted_owner_bytes_and_absence(tmp_path, monkeypatch, owner, has_project):
    import api_agent.runtime.pack_admission as admission
    from api_agent.runtime.dependencies import requirements
    base, library, first, second = project_composition(tmp_path)
    target = {'runtime': base, 'first': first, 'library': library}[owner]
    if has_project:
        (target / 'pyproject.toml').write_text('[project]\ndependencies=["target-admitted>=1"]\n')
    else:
        (target / 'pyproject.toml').unlink(missing_ok=True)
    context = load_launch_context(base, [first, second])
    expected_requirements = requirements(context)
    expected_identity = composition_identity(context)
    alternate = tmp_path / 'unadmitted'
    alternate.mkdir()
    (alternate / 'pyproject.toml').write_text('[project]\ndependencies=["unadmitted>=1"]\n')
    saved = tmp_path / 'saved'
    original = admission.verify_composition
    def verify_then_swap(selected, expected=None):
        original(selected, expected)
        target.rename(saved)
        target.symlink_to(alternate, target_is_directory=True)
    monkeypatch.setattr(admission, 'verify_composition', verify_then_swap)
    try:
        consumed = requirements(context)
    finally:
        target.unlink()
        saved.rename(target)
    assert consumed == expected_requirements
    original(context, expected_identity)


def test_missing_admitted_dependency_owner_rejects_without_live_fallback(tmp_path):
    from dataclasses import replace
    from api_agent.runtime.dependencies import requirements
    base, library, first, second = project_composition(tmp_path)
    context = load_launch_context(base, [first, second])
    incomplete = replace(context, project_contributions=context.project_contributions[1:])
    with pytest.raises(ResourcePackError, match='Dependency owner is absent'):
        requirements(incomplete)


@pytest.mark.parametrize('field,spec,expected_suffix', [
    ('EXAMPLE_PROVIDER', 'type="str"\ndefault="provider.py:factory"', 'provider.py:factory'),
    ('EXAMPLE_PROVIDER', 'type="str"\nconditional_defaults=[{when={DEBUG=true},value="provider.py:factory"}]', 'provider.py:factory'),
    ('ASSET_PATH', 'type="path"\nanchor="pack_root"\ndefault="assets"', 'assets'),
    ('ASSET_PATH', 'type="path"\nanchor="owner_root"\nconditional_defaults=[{when={DEBUG=true},value="assets"}]', 'assets'),
])
def test_schema_alias_swap_preserves_admitted_default_and_source_ownership(tmp_path, monkeypatch, field, spec, expected_suffix):
    import api_agent.runtime.pack_admission as admission
    from api_agent.config import load_shared_schema
    from api_agent.config.source_reads import capture_reads
    from api_agent.runtime.pack_composition import load_context_schema
    first = pack(tmp_path, 'first', config=f'version=1\n[fields.{field}]\n{spec}\n')
    second = pack(tmp_path, 'second')
    context = load_launch_context(BASE, [first, second])
    expected_identity = composition_identity(context)
    alternate = tmp_path / 'unadmitted'
    alternate.mkdir()
    saved = tmp_path / 'saved'
    shared = load_shared_schema(BASE)
    original = admission.verify_composition
    def verify_then_swap(selected, expected=None):
        original(selected, expected)
        first.rename(saved)
        first.symlink_to(alternate, target_is_directory=True)
    monkeypatch.setattr(admission, 'verify_composition', verify_then_swap)
    try:
        with capture_reads() as reads:
            schema = load_context_schema(context, shared)
    finally:
        first.unlink()
        saved.rename(first)
    declaration = schema.fields[field]
    actual = (declaration.conditional_defaults[0]['value'] if declaration.conditional_defaults else declaration.default)
    assert actual == str(first / expected_suffix)
    assert first / 'config.toml' in reads.files
    assert all(not path.is_relative_to(alternate) for path in reads.files)
    original(context, expected_identity)


def test_auxiliary_alias_swap_preserves_admitted_source_provenance(tmp_path):
    from api_agent.config import ConfigSchema
    from api_agent.config.source_reads import capture_reads
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.runtime.pack_admission import configuration_inputs
    first = pack(tmp_path, 'first', manifest='[providers]\nconfigs=["provider.toml"]\n')
    second = pack(tmp_path, 'second')
    (first / 'provider.toml').write_text('version=1\n[fields.CAPTURED]\ntype="str"\ndefault="admitted"\n')
    context = load_launch_context(BASE, [first, second])
    expected_identity = composition_identity(context)
    inputs = configuration_inputs(context)
    alternate = tmp_path / 'unadmitted'
    alternate.mkdir()
    first.rename(tmp_path / 'saved')
    first.symlink_to(alternate, target_is_directory=True)
    try:
        with capture_reads() as reads:
            schema = bootstrap_schema(ConfigSchema({}), (), {}, {}, configuration_paths=context.provider_config_paths, **inputs)
    finally:
        first.unlink()
        (tmp_path / 'saved').rename(first)
    assert schema.fields['CAPTURED'].contribution_origin == first / 'provider.toml'
    assert first / 'provider.toml' in reads.files
    verify_composition(context, expected_identity)


@pytest.mark.parametrize('change', ['add', 'change', 'delete', 'symlink'])
def test_pack_profile_presence_and_content_are_admitted(tmp_path, change):
    first, second = pack(tmp_path, 'first'), pack(tmp_path, 'second')
    profile = first / 'test-profiles.toml'
    if change != 'add':
        profile.write_text('version=1\n')
    context = load_launch_context(BASE, [first, second])
    contribution = context.pack_contributions[0]
    if change == 'add':
        assert 'test-profiles.toml' in contribution.absent_declarations
        profile.write_text('version=1\n')
    else:
        assert next(row.body for row in contribution.declarations if row.relative_path == 'test-profiles.toml') == b'version=1\n'
        if change == 'change':
            profile.write_text('version=2\n')
        else:
            profile.unlink()
            if change == 'symlink':
                profile.symlink_to(first / 'config.toml')
    with pytest.raises(ResourcePackError):
        verify_composition(context)


def test_pack_profile_symlink_is_rejected_during_admission(tmp_path):
    first, second = pack(tmp_path, 'first'), pack(tmp_path, 'second')
    (first / 'test-profiles.toml').symlink_to(first / 'config.toml')
    with pytest.raises(ResourcePackError, match='bounded regular'):
        load_launch_context(BASE, [first, second])
