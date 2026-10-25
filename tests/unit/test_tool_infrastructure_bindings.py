"""Admitted outputs and activations remain operation scoped."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration
from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.runtime.infrastructure_contract import Activation, EndpointOutput
from api_agent.tooling.models import SelectedTool, ToolPlan
from api_agent.tooling.services import bind_tool_outputs, selected_contributions, tool_operation_values


def tool(identity='demo/primary', **kwargs):
    from pathlib import Path
    return SelectedTool(identity, 'verified-read@1', Path('/unused/factory.py'), 'create_tool', b'', {}, **kwargs)


def plan(tools, **kwargs):
    schema = ConfigSchema({'LOCAL': FieldDeclaration('LOCAL', 'bool', default=False)})
    return ToolPlan(tuple(tools), schema, {}, (), 'descriptor', 'operation', **kwargs)


def config(*, explicit=False, value=False):
    return ConfigResult({'LOCAL': value}, {'LOCAL': ResolvedValue(value, 'cli' if explicit else 'default', explicit)}, (), (), ())


def test_declared_activation_copies_operation_values_without_changing_user_config():
    selected = plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http', 'activation': {'LOCAL': True}}})])
    saved = config()

    effective = tool_operation_values(selected, saved)

    assert saved.values == {'LOCAL': False}
    assert effective.values == {'LOCAL': True}
    assert effective.fields['LOCAL'].source == 'operation:tools'
    assert effective.tool_plan.config.values == {'LOCAL': True}


def test_explicit_disabled_local_activation_rejects_before_acquisition():
    selected = plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http', 'activation': {'LOCAL': True}}})])

    with pytest.raises(ValueError, match='Explicit infrastructure activation'):
        tool_operation_values(selected, config(explicit=True))


def test_disabled_transitive_dependency_is_not_silently_enabled():
    primary = SimpleNamespace(id='demo/api', activation=None, dependencies=('demo/backing',))
    backing = SimpleNamespace(id='demo/backing', activation=Activation('LOCAL', True), dependencies=())
    selected = plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http'}})],
                    infrastructure_contributions=(primary, backing), config=config())

    with pytest.raises(ValueError, match='infrastructure is disabled'):
        selected_contributions(selected)


def test_shared_secondary_borrows_primary_effective_output_and_preserves_configured_values():
    original = {'ENDPOINT': ''}
    primary = tool(local_values=original, configured_values=original,
                   pending={'ENDPOINT': {'output': 'demo/api/http', 'path': '/graphql'}})
    secondary = tool('demo/secondary', local_values={}, configured_values={})
    selected = plan([secondary, primary], shared_primary={'demo/secondary': 'demo/primary'})
    infrastructure = SimpleNamespace(endpoints={'demo/api': (EndpointOutput('http', 'http://localhost:8090', 'http://api:8090'),)})

    bound = bind_tool_outputs(selected, infrastructure)

    assert all(item.local_values['ENDPOINT'] == 'http://localhost:8090/graphql' for item in bound.tools)
    assert all(not item.pending for item in bound.tools)
    assert primary.configured_values == {'ENDPOINT': ''}
    assert primary.local_values == {'ENDPOINT': ''}
    assert selected.tools[0].local_values == {}


def test_missing_ready_output_rejects_before_tool_factory_configuration():
    selected = plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http'}})])

    with pytest.raises(ValueError, match='output is not ready'):
        bind_tool_outputs(selected, SimpleNamespace(endpoints={}))


@pytest.mark.parametrize('fields,value,code', [
    ({}, True, 'declared scalar'),
    ({'LOCAL': FieldDeclaration('LOCAL', 'jsonobject')}, {}, 'declared scalar'),
    ({'LOCAL': FieldDeclaration('LOCAL', 'bool', sensitive=True)}, True, 'declared field type'),
    ({'LOCAL': FieldDeclaration('LOCAL', 'int')}, True, 'declared field type'),
])
def test_invalid_activation_declarations_reject_before_effective_configuration(fields, value, code):
    selected = replace(plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http', 'activation': {'LOCAL': value}}})]),
                       schema=ConfigSchema(fields))

    with pytest.raises(ValueError, match=code):
        tool_operation_values(selected, config())


def test_conflicting_selected_activation_values_reject_without_changing_saved_values():
    selected = plan([tool(pending={'ENDPOINT': {'output': 'demo/api/http', 'activation': {'LOCAL': True}}}),
        tool('demo/secondary', pending={'ENDPOINT': {'output': 'demo/other/http', 'activation': {'LOCAL': False}}})])
    saved = config()

    with pytest.raises(ValueError, match='activations conflict'):
        tool_operation_values(selected, saved)

    assert saved.values == {'LOCAL': False}


def test_required_output_roots_deduplicate_shared_dependencies_and_reject_missing_service():
    api = SimpleNamespace(id='demo/api', activation=None, dependencies=('demo/backing',))
    backing = SimpleNamespace(id='demo/backing', activation=None, dependencies=())
    selected = plan([tool(prerequisite_roots=('demo/api', 'demo/backing'))],
                    infrastructure_contributions=(api, backing))

    assert [service.id for service in selected_contributions(selected)] == ['demo/api', 'demo/backing']
    with pytest.raises(ValueError, match='prerequisite is unavailable'):
        selected_contributions(replace(selected, infrastructure_contributions=(api,)))


def test_output_binding_rejects_unsupported_scope_and_cyclic_sharing():
    selected = plan([tool(), tool('demo/secondary')],
                    shared_primary={'demo/primary': 'demo/secondary', 'demo/secondary': 'demo/primary'})

    with pytest.raises(ValueError, match='address scope'):
        bind_tool_outputs(selected, SimpleNamespace(endpoints={}), address='unknown')
    with pytest.raises(ValueError, match='Cyclic shared'):
        bind_tool_outputs(selected, SimpleNamespace(endpoints={}))


def test_empty_infrastructure_declaration_is_captured_as_admission_evidence(tmp_path):
    from api_agent.tooling.services import capture_prerequisites
    declaration = tmp_path / 'infrastructure.toml'
    declaration.write_text('version=1\nowner="sample"\nservices=[]\n')
    selected = tool(infrastructure={'owner': 'sample', 'owner_root': tmp_path, 'declarations': (declaration,)})

    contributions, assets = capture_prerequisites((selected,), {'base': tmp_path})

    assert contributions == ()
    assert assets[declaration] == declaration.read_bytes()


@pytest.mark.parametrize('mode', ['missing-owner', 'escaped-root', 'conflicting-root'])
def test_tool_infrastructure_requires_one_admitted_owner_root(tmp_path, mode):
    from api_agent.tooling.services import capture_prerequisites
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    if mode == 'missing-owner':
        selected = (tool(prerequisite_roots=('sample/api',)),)
        roots, code = {'base': tmp_path}, 'no declared owner'
    else:
        metadata = lambda root: {'owner': 'sample', 'owner_root': root, 'declarations': ()}
        selected = (tool(infrastructure=metadata(first)),)
        roots, code = {'base': second}, 'outside admitted roots'
        if mode == 'conflicting-root':
            selected += (tool('demo/secondary', infrastructure=metadata(second)),)
            roots, code = {'base': tmp_path}, 'conflicting roots'

    with pytest.raises(ValueError, match=code):
        capture_prerequisites(selected, roots)


def test_captured_dependency_manifests_feed_existing_preparation_and_groups(tmp_path):
    from api_agent.tooling.admission import capture_tool_factory
    from api_agent.tooling.services import selected_tool_manifests
    factory = tmp_path / 'factory.py'
    factory.write_text('def create_tool(config, services): pass\n')
    (tmp_path / 'provider-dependencies.toml').write_text('version=1\ndependencies=["example-package==1.0"]\n'
        '[groups]\noptional=["optional-package>=2"]\n')
    (tmp_path / 'LICENSE').write_text('Synthetic fixture ownership\n')
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    selected = plan([tool(admission=admission), tool('demo/secondary', admission=admission)])

    manifests = selected_tool_manifests(selected)

    assert len(manifests) == 1
    assert manifests[0].requirements() == ('example-package==1.0',)
    assert manifests[0].requirements(('optional',)) == ('example-package==1.0', 'optional-package>=2')


@pytest.mark.parametrize('body,code', [
    ('version=2\n', 'dependency manifest'),
    ('version=1\ndependencies="invalid"\n', 'dependency manifest'),
    ('version=1\n[groups]\noptional="invalid"\n', 'dependency group'),
    ('version=1\ndependencies=["https://example.test/archive.whl"]\n', 'PEP 508'),
])
def test_invalid_captured_dependency_manifest_rejects_before_preparation(tmp_path, body, code):
    from api_agent.tooling.admission import capture_tool_factory
    from api_agent.tooling.services import selected_tool_manifests
    factory = tmp_path / 'factory.py'
    factory.write_text('def create_tool(config, services): pass\n')
    (tmp_path / 'provider-dependencies.toml').write_text(body)
    selected = plan([tool(admission=capture_tool_factory(factory, 'create_tool', tmp_path))])

    with pytest.raises(ValueError, match=code):
        selected_tool_manifests(selected)


def test_tool_preflight_rejects_duplicate_instances_and_missing_service_registry(tmp_path):
    from api_agent.tooling.admission import capture_tool_factory
    from api_agent.tooling.services import tool_configurations
    factory = tmp_path / 'factory.py'
    factory.write_text('def create_tool(config, services): pass\n')
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    selected = tool(admission=admission, local_values={'OPTIONS': ('a', 'b')})

    with pytest.raises(ValueError, match='identities or graph bindings conflict'):
        tool_configurations(plan([selected, selected]), {})
    with pytest.raises(ValueError, match='service registry is unavailable'):
        tool_configurations(plan([replace(selected, service_bindings={'store': 'sample/store'},
            service_capabilities={'store': 'records@1'})]), {})
    assert tool_configurations(plan([selected]), {})[0][1] == {'OPTIONS': ['a', 'b']}


def test_prepare_projection_requires_an_executable_admission():
    from api_agent.tooling.services import prepare_tool_plan
    with pytest.raises(ValueError, match='executable admission is missing'):
        prepare_tool_plan(plan([tool()]))


def test_shared_admission_receipt_does_not_hide_changed_captured_source(tmp_path):
    from api_agent.tooling.admission import capture_tool_factory, ToolAdmissionError
    from api_agent.tooling.services import prepare_tool_plan
    factory = tmp_path / 'factory.py'
    factory.write_text('def create_tool(config, services): return None\n')
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    first = tool(identity='sample/one', admission=admission)
    changed = replace(admission, sources={'factory.py': b'def create_tool(config, services): return 2\n'})
    second = tool(identity='sample/two', admission=changed)

    with pytest.raises(ToolAdmissionError, match='admission identity changed'):
        prepare_tool_plan(plan([first, second]))


@pytest.mark.parametrize('mode,code', [('duplicate', 'Duplicate tool infrastructure identity'),
                                     ('changed', 'changed during admission')])
def test_independently_captured_infrastructure_cannot_overwrite_identity_or_asset_bytes(tmp_path, monkeypatch, mode, code):
    from hashlib import sha256
    from api_agent.runtime import infrastructure_contract
    from api_agent.runtime.infrastructure_contract import CapturedAsset
    from api_agent.tooling.services import capture_prerequisites
    declaration = tmp_path / 'infrastructure.toml'
    body = b'version=1\nowner="sample"\nservices=[]\n'
    declaration.write_bytes(body)
    captured = CapturedAsset('infrastructure.toml', body, sha256(body).hexdigest())
    first = CapturedAsset('shared.json', b'first', sha256(b'first').hexdigest())
    second_body = b'first' if mode == 'duplicate' else b'replaced during capture'
    second = CapturedAsset('shared.json', second_body, sha256(second_body).hexdigest())
    services = [SimpleNamespace(id='sample/api', declaration_source=captured, compose_source=first, assets=()),
                SimpleNamespace(id='sample/api' if mode == 'duplicate' else 'sample/other',
                                declaration_source=captured, compose_source=second, assets=())]
    monkeypatch.setattr(infrastructure_contract, 'load_infrastructure_contributions', lambda context: tuple(services))
    selected = tool(infrastructure={'owner': 'sample', 'owner_root': tmp_path, 'declarations': (declaration,)})

    with pytest.raises(ValueError, match=code):
        capture_prerequisites((selected,), {'base': tmp_path})
