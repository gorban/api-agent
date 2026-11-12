"""Static generated-plan freshness checks."""
import runpy
import sys
import shutil
from types import SimpleNamespace

import pytest

from artifact_acceptance_support import QUALIFICATION, build_wheel
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.workflows import artifact_plans
from api_agent.workflows.artifact_plans import (
    check_plans,
    compile_selected_plan,
    main,
    regenerate_plans,
)


def _pack(tmp_path):
    root = tmp_path / 'repo'
    pack = root / 'pack'
    shutil.copytree(QUALIFICATION, pack)
    (pack / 'build').mkdir()
    native = pack / 'src/aq_bindings'
    wheel = build_wheel(pack / 'build', 'aq-bindings', {
        'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
        'aq_bindings/native.py': (native / 'native.py').read_bytes(),
    })
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text(
        'version=1\nid="compiled"\nconfig="config.toml"\n'
        '[workflow]\nversion = 1\nroot="pack"\nentry="flow.md.html"\n'
        '[workflow.artifact]\n'
        'plan=".private_resources/plans/artifact_qualification.json"\n'
        'graph_id="artifact_qualification"\n'
        f'wheels=["build/{wheel.name}"]\n')
    return root, pack


def test_regenerated_plan_passes_the_static_freshness_check(tmp_path):
    root, pack = _pack(tmp_path)

    assert regenerate_plans(root) == (pack / '.private_resources/plans/artifact_qualification.json',)
    check_plans(root)


def test_compilation_is_byte_identical_across_runs_and_checkout_locations(tmp_path):
    root, pack = _pack(tmp_path)
    relocated = tmp_path / 'relocated-checkout'
    shutil.copytree(root, relocated)

    first_context = load_launch_context(root, pack)
    second_context = load_launch_context(relocated, relocated / 'pack')
    first = compile_selected_plan(first_context)
    second = compile_selected_plan(second_context)

    assert compile_selected_plan(first_context) == first
    assert compile_selected_plan(second_context) == second
    assert first == second


def test_compilation_requires_a_selected_workflow_entry():
    with pytest.raises(ValueError, match='selected workflow entry'):
        compile_selected_plan(SimpleNamespace(workflow_entry=None))


def test_compilation_requires_declared_source_wheels(tmp_path):
    context = SimpleNamespace(workflow_entry=tmp_path / 'flow.md.html', workflow_artifact_wheels=())

    with pytest.raises(ValueError, match='declared source wheels'):
        compile_selected_plan(context)


def test_compilation_rejects_missing_wheel_before_invoking_compiler(tmp_path, monkeypatch):
    context = SimpleNamespace(
        workflow_entry=tmp_path / 'flow.md.html',
        workflow_artifact_wheels=(tmp_path / 'missing.whl',),
        base_root=tmp_path, pack_root=tmp_path, workflow_root='base', workflow_replacements=(),
    )
    monkeypatch.setattr(artifact_plans, 'compile_plan',
                        lambda *_args, **_kwargs: pytest.fail('compiler was invoked'))

    with pytest.raises(ValueError, match='declared source wheel is missing'):
        compile_selected_plan(context)


def test_freshness_operations_require_declared_wheels_to_exist(tmp_path):
    root, pack = _pack(tmp_path)
    regenerate_plans(root)
    declared, = load_launch_context(root, pack).workflow_artifact_wheels
    declared.unlink()

    with pytest.raises(ValueError, match='declared source wheel is missing'):
        compile_selected_plan(load_launch_context(root, pack))
    with pytest.raises(ValueError, match='declared source wheel is missing'):
        check_plans(root)
    with pytest.raises(ValueError, match='declared source wheel is missing'):
        regenerate_plans(root)


def test_freshness_check_detects_a_stale_plan(tmp_path):
    root, pack = _pack(tmp_path)
    plan, = regenerate_plans(root)
    plan.write_bytes(plan.read_bytes().replace(b'"graph_id":"artifact_qualification"', b'"graph_id":"stale"'))

    with pytest.raises(ValueError, match='compiled plan is stale; regenerate'):
        check_plans(root)


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
def test_freshness_check_names_a_line_ending_added_after_compilation(tmp_path, ending):
    root, pack = _pack(tmp_path)
    plan, = regenerate_plans(root)
    plan.write_bytes(plan.read_bytes() + ending)

    with pytest.raises(ValueError, match=(
            'compiled plan ends with a line ending the compiler does not write; exclude compiled plans '
            'from end-of-file and line-ending conversion, then regenerate')):
        check_plans(root)


def test_freshness_check_detects_a_missing_plan(tmp_path):
    root, _ = _pack(tmp_path)

    with pytest.raises(ValueError, match='compiled plan is missing'):
        check_plans(root)


def test_freshness_check_requires_the_canonical_plan_location(tmp_path):
    root, pack = _pack(tmp_path)
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().replace(
        '.private_resources/plans/artifact_qualification.json',
        '.private_resources/plans/incorrect.json'))

    with pytest.raises(ValueError, match='compiled plans belong'):
        check_plans(root)


def test_freshness_check_reports_a_plan_that_cannot_be_recompiled(tmp_path):
    root, pack = _pack(tmp_path)
    plan, = regenerate_plans(root)
    (pack / 'flow.md.html').write_text('# Workflow v4\n')

    with pytest.raises(ValueError, match='cannot compile selected graph'):
        check_plans(root)

    assert plan.is_file()


def test_freshness_check_reports_an_invalid_resource_pack(tmp_path):
    root = tmp_path / 'repo'
    pack = root / 'pack'
    pack.mkdir(parents=True)
    (pack / 'pack.toml').write_text('version=0\n')

    with pytest.raises(ValueError, match='invalid resource pack'):
        check_plans(root)


def test_freshness_check_rejects_an_orphaned_compiled_plan(tmp_path):
    root, pack = _pack(tmp_path)
    plan, = regenerate_plans(root)
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().split('[workflow.artifact]')[0])

    with pytest.raises(ValueError, match='delete it or restore its declaration'):
        check_plans(root)

    assert plan.is_file()


def test_regeneration_removes_an_orphaned_compiled_plan(tmp_path):
    root, pack = _pack(tmp_path)
    plan, = regenerate_plans(root)
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().split('[workflow.artifact]')[0])

    assert regenerate_plans(root) == ()
    assert not plan.exists()


def test_check_command_returns_success_for_a_regenerated_plan(tmp_path):
    root, _ = _pack(tmp_path)
    regenerate_plans(root)

    assert main(['check', '--root', str(root)]) == 0


def test_check_command_reports_freshness_failure(tmp_path, capsys):
    root, _ = _pack(tmp_path)

    with pytest.raises(SystemExit) as error:
        main(['check', '--root', str(root)])

    assert error.value.code == 1
    assert 'Compiled workflow plan check failed' in capsys.readouterr().err


def test_regenerate_command_writes_the_selected_plan(tmp_path):
    root, pack = _pack(tmp_path)

    assert main(['regenerate', '--root', str(root)]) == 0
    assert (pack / '.private_resources/plans/artifact_qualification.json').is_file()


def test_module_entrypoint_runs_the_check_command(tmp_path, monkeypatch):
    root, _ = _pack(tmp_path)
    regenerate_plans(root)
    monkeypatch.setattr('sys.argv', ['artifact_plans', 'check', '--root', str(root)])
    monkeypatch.delitem(sys.modules, 'api_agent.workflows.artifact_plans')

    with pytest.raises(SystemExit) as error:
        runpy.run_module('api_agent.workflows.artifact_plans', run_name='__main__')

    assert error.value.code == 0


def test_plan_directories_may_hold_files_that_are_not_generated_plans(tmp_path):
    root, pack = _pack(tmp_path)
    regenerate_plans(root)
    plans = pack / '.private_resources/plans'
    readme = plans / 'README.txt'
    readme.write_text('Generated by artifact_plans; do not edit.\n')
    nested = plans / 'notes' / 'history.json'
    nested.parent.mkdir()
    nested.write_text('{}')

    regenerate_plans(root)
    check_plans(root)

    assert readme.read_text() == 'Generated by artifact_plans; do not edit.\n'
    assert nested.read_text() == '{}'


def test_stale_qualification_plan_fails_the_check_until_regenerated(tmp_path, monkeypatch):
    plan = tmp_path / 'qualification/simple_chat.json'
    plan.parent.mkdir()
    plan.write_bytes(b'stale')
    monkeypatch.setattr(artifact_plans, '_qualification_plan', lambda root: b'fresh')
    with pytest.raises(ValueError, match='qualification/simple_chat.json: compiled plan is stale'):
        check_plans(tmp_path)
    regenerate_plans(tmp_path)
    assert plan.read_bytes() == b'fresh'
    check_plans(tmp_path)


def test_roots_without_a_qualification_plan_never_compile_one(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_plans, '_qualification_plan', lambda root: pytest.fail('compiled'))
    check_plans(tmp_path)
    regenerate_plans(tmp_path)
    assert not (tmp_path / 'qualification').exists()
