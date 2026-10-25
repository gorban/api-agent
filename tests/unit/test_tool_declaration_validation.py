"""Malformed declarations reject before configuration or executable acquisition."""
from pathlib import Path

import pytest

from api_agent.tooling import declarations
from api_agent.tooling.declarations import Capture, instance_declaration, implementation_declaration
from api_agent.tooling.models import OperationSelection, ToolSelectionError, operation_selection


@pytest.mark.parametrize('changes,message', [
    ({'version': True}, 'version 1'),
    ({'unexpected': True}, 'version 1'),
    ({'id': 'unqualified'}, 'qualified identity'),
    ({'interface': 'read'}, 'versioned interface'),
    ({'implementation': ''}, 'default implementation'),
    ({'selector': 'lowercase'}, 'configuration field name'),
    ({'expose_selector': 'yes'}, 'presentation'),
    ({'bindings': {'ENDPOINT': 'lowercase'}}, 'configuration field names'),
    ({'bindings': {'ENDPOINT': 1}}, 'string mapping'),
    ({'defaults': {'UNKNOWN': 'value'}}, 'declared local bindings'),
    ({'services': []}, 'string mapping'),
    ({'prerequisites': ['']}, 'bounded string array'),
    ({'local': []}, 'must be a table'),
    ({'local': {'slot': 'ENDPOINT'}}, 'Invalid local'),
    ({'local': {'slot': 'ENDPOINT', 'output': 'api', 'path': '/', 'declaration': 'api.toml',
                'activation': {'START': []}}}, 'typed controller'),
    ({'infrastructure': []}, 'must be a table'),
    ({'infrastructure': {'owner': 'sample', 'root': '.', 'declarations': [False]}}, 'ownership'),
])
def test_invalid_instance_metadata_rejects(changes, message):
    declaration = {'version': 1, 'id': 'sample/primary', 'interface': 'verified-read@1',
                   'implementation': 'read.toml', 'bindings': {'ENDPOINT': 'API_ENDPOINT'}}
    declaration.update(changes)

    with pytest.raises(ToolSelectionError, match=message):
        instance_declaration(declaration)


@pytest.mark.parametrize('changes,message', [
    ({'interface': 'read'}, 'versioned interface'),
    ({'factory': None}, 'factory target'),
    ({'factory': 'factory.py:other'}, 'name create_tool'),
    ({'inputs': []}, 'inputs must be a table'),
])
def test_invalid_implementation_metadata_rejects(changes, message):
    declaration = {'version': 1, 'interface': 'verified-read@1', 'factory': 'factory.py:create_tool'}
    declaration.update(changes)

    with pytest.raises(ToolSelectionError, match=message):
        implementation_declaration(declaration)


def test_capture_requires_an_owner_and_a_canonical_bounded_path(tmp_path):
    capture = Capture({'base': tmp_path, 'pack': tmp_path})

    with pytest.raises(ToolSelectionError, match='escapes'):
        capture.owner(tmp_path.parent / 'outside')
    with pytest.raises(ToolSelectionError, match='bounded file path'):
        capture.path(tmp_path / 'instance.toml', 'bad\\path')
    with pytest.raises(ToolSelectionError, match='distribution-owned'):
        capture.package_path('package::sample/file.toml')
    with pytest.raises(ToolSelectionError, match='canonical distribution path'):
        capture.package_path('package:sample:sample/../file.toml')


def test_capture_enforces_distinct_file_budget_and_stable_repeated_bytes(tmp_path, monkeypatch):
    capture = Capture({'base': tmp_path, 'pack': tmp_path})
    monkeypatch.setattr(declarations, 'MAX_CAPTURE_FILES', 1)
    path = tmp_path / 'one.toml'
    capture.add(path, b'one')

    assert capture.add(path, b'one') == b'one'
    with pytest.raises(ToolSelectionError, match='changed during admission'):
        capture.add(path, b'two')
    with pytest.raises(ToolSelectionError, match='capture budget'):
        capture.add(tmp_path / 'two.toml', b'two')


def test_capture_enforces_installed_library_inventory(tmp_path):
    capture = Capture({'base': tmp_path, 'pack': tmp_path}, library_files=frozenset({'owned.toml'}))

    assert capture.add(tmp_path / 'owned.toml', b'owned') == b'owned'
    with pytest.raises(ToolSelectionError, match='not owned by installed workflow'):
        capture.add(tmp_path / 'extra.toml', b'extra')


def test_capture_rejects_invalid_toml_and_rechecks_disk_bytes(tmp_path):
    source = tmp_path / 'one.toml'
    source.write_bytes(b'\xff')
    capture = Capture({'base': tmp_path, 'pack': tmp_path})
    with pytest.raises(ToolSelectionError, match='Invalid selected tool TOML'):
        capture.toml(source)
    source.write_bytes(b'version=1')
    fresh = Capture({'base': tmp_path, 'pack': tmp_path})
    fresh.read(source)
    source.write_bytes(b'version=2')

    with pytest.raises(ToolSelectionError, match='changed during admission'):
        fresh.verify()


@pytest.mark.parametrize('kwargs,message', [
    ({'mode': 'invented'}, 'Unknown tool operation'),
    ({'mode': 'tests', 'test_ids': ('',)}, 'nonempty identities'),
    ({'mode': 'tests', 'test_ids': ('case', 'case')}, 'Duplicate selected'),
    ({'mode': 'tests', 'test_ids': ('case',), 'graphless': ('other',)}, 'Graphless admission'),
    ({'test_ids': ('case',)}, 'Application selection'),
])
def test_invalid_operation_selection_rejects(kwargs, message):
    with pytest.raises(ToolSelectionError, match=message):
        OperationSelection(**kwargs)


def test_explicit_mode_selection_is_normalized():
    assert operation_selection('run-local') == OperationSelection()
    assert operation_selection('collection') == OperationSelection('collection')
