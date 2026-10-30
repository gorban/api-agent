"""Root-page viewing commands identify the displayed graph without changing it."""
from pathlib import Path

import pytest

from api_agent.workflows.document_format import extract_document, validate_presentation_location, wrap_document
from api_agent.workflows.definition import LayoutError


@pytest.mark.parametrize('relative', [
    'demo/contrived/flow.md.html', 'nested space/flow.md.html', 'nested/a$b`c"d.md.html',
    'nested/a&b.md.html',
])
def test_root_page_command_round_trips_without_changing_executable_bytes(tmp_path, relative):
    source = b'# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n'
    path = tmp_path / relative
    prefix = '../' * (len(Path(relative).parts) - 1) + '.private_resources/viewer/'
    wrapped = wrap_document(source, root=True, asset_prefix=prefix, flow_path=relative)
    assert b'./launch.sh --view-flow "' in wrapped
    assert extract_document(wrapped, path=path).executable_bytes == source
    validate_presentation_location(wrapped, path=path, root=tmp_path)


def test_repository_root_command_omits_optional_path():
    wrapped = wrap_document(b'# Workflow v2\n', root=True,
                            asset_prefix='.private_resources/viewer/', flow_path='flow.md.html')
    assert b'./launch.sh --view-flow\n' in wrapped
    assert b'./launch.sh --view-flow "' not in wrapped


def test_previous_python_launcher_envelope_remains_readable(tmp_path):
    source = b'# Workflow v2\n'
    wrapped = wrap_document(source, root=True, asset_prefix='../.private_resources/viewer/',
                            flow_path='nested/flow.md.html')
    legacy = wrapped.replace(b'./launch.sh --view-flow', b'./launch.py --view-flow')
    legacy = legacy.replace(b'For managed viewing, follow Getting started in README.md, then run the launcher.',
                            b'For managed viewing, use Python 3.13 or newer.')
    legacy = legacy.replace(b'Follow Getting started in README.md, then run from the repository root:',
                            b'Use Python 3.13 or newer from the repository root:')
    path = tmp_path / 'nested/flow.md.html'
    assert extract_document(legacy, path=path).executable_bytes == source
    validate_presentation_location(legacy, path=path, root=tmp_path)


def test_previous_mise_envelope_remains_readable(tmp_path):
    source = b'# Workflow v2\n'
    wrapped = wrap_document(source, root=True, asset_prefix='.private_resources/viewer/',
                            flow_path='flow.md.html')
    legacy = wrapped.replace(b'For managed viewing, follow Getting started in README.md, then run the launcher.',
                             b'For managed viewing, install mise and run the launcher.')
    legacy = legacy.replace(b'Follow Getting started in README.md, then run from the repository root:',
                            b'Install mise, then run from the repository root:')
    path = tmp_path / 'flow.md.html'
    assert extract_document(legacy, path=path).executable_bytes == source
    validate_presentation_location(legacy, path=path, root=tmp_path)


@pytest.mark.parametrize('relative', ['../flow.md.html', '/flow.md.html', 'a//flow.md.html',
                                      'a/./flow.md.html', 'a/flow\n.md.html', '-a/flow.md.html',
                                      'a/-->flow.md.html'])
def test_generated_viewing_command_rejects_unsafe_paths(relative):
    with pytest.raises(LayoutError, match='viewing path'):
        wrap_document(b'# Workflow v2\n', root=True,
                      asset_prefix='.private_resources/viewer/', flow_path=relative)


def test_root_page_rejects_mismatched_help_and_editor_commands(tmp_path):
    wrapped = wrap_document(b'# Workflow v2\n', root=True,
                            asset_prefix='.private_resources/viewer/', flow_path='nested/flow.md.html')
    changed = wrapped.replace(b'<pre>./launch.sh --view-flow "nested/flow.md.html"</pre>',
                              b'<pre>./launch.sh --view-flow "other/flow.md.html"</pre>')
    with pytest.raises(LayoutError, match='envelope'):
        extract_document(changed, path=tmp_path / 'flow.md.html')


def test_root_page_rejects_coordinated_wrong_viewing_path(tmp_path):
    wrapped = wrap_document(b'# Workflow v2\n', root=True,
                            asset_prefix='../.private_resources/viewer/', flow_path='nested/flow.md.html')
    changed = wrapped.replace(b'nested/flow.md.html', b'other/flow.md.html')
    with pytest.raises(LayoutError, match='viewing command'):
        validate_presentation_location(changed, path=tmp_path / 'nested/flow.md.html', root=tmp_path)


def test_root_page_binds_viewing_path_to_recorded_owner_prefix(tmp_path):
    from api_agent.workflows.viewer_repository import RepositoryIdentity, repository_config_bytes
    config = tmp_path / 'cfg/workflow-view.toml'
    config.parent.mkdir()
    config.write_bytes(repository_config_bytes(RepositoryIdentity('//github.com/example/agent', 'demo')))
    wrapped = wrap_document(b'# Workflow v2\n', root=True,
                            asset_prefix='../.private_resources/viewer/', flow_path='demo/nested/flow.md.html')
    validate_presentation_location(wrapped, path=tmp_path / 'nested/flow.md.html', root=tmp_path)


def test_generated_root_command_requires_canonical_omitted_argument(tmp_path):
    wrapped = wrap_document(b'# Workflow v2\n', root=True,
                            asset_prefix='.private_resources/viewer/', flow_path='nested/flow.md.html')
    changed = wrapped.replace(b'nested/flow.md.html', b'flow.md.html')
    with pytest.raises(LayoutError, match='viewing command'):
        extract_document(changed, path=tmp_path / 'flow.md.html')
