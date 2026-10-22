"""A selected pack resource renders through the pinned viewer toolchain."""

import os
from pathlib import Path

from api_agent.runtime.resource_pack import load_launch_context
from api_agent.runtime.view_flow import _prepare_view

from test_workflow_viewer_render_toolchain import _qa_prerequisites


def test_pack_resource_uses_pinned_renderer_and_keeps_owner_source(tmp_path, monkeypatch):
    jar, java = _qa_prerequisites()
    monkeypatch.setenv('API_AGENT_PLANTUML_JAR', str(jar))
    monkeypatch.setenv('PATH', str(Path(java).parent) + ':' + os.environ['PATH'])
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    (base / 'children').mkdir(parents=True)
    pack.mkdir()
    (base / 'flow.md.html').write_text(
        '# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n'
        '## Flow\n\n1.  [node work](children/work.md.html)\n')
    (base / 'children/work.md.html').write_text(
        '# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
        '| work | deterministic | [Python](../native.py) |\n\n'
        '## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
        '| work | work | [input](../schema.json) | [output](../schema.json) |\n\n'
        '## Prompts\n\n| Node | File | When |\n| --- | --- | --- |\n'
        r'| work | [Pack prompt](\<resource_pack\>/prompt.md) | always |' + '\n')
    (base / 'native.py').write_text('def work(inputs, context):\n    return {}\n')
    (base / 'schema.json').write_text('{"type":"object"}')
    (pack / 'prompt.md').write_text('Synthetic selected prompt.\n')
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text('version=1\nid="synthetic-pack"\nconfig="config.toml"\n')
    source_before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}

    _, projection, canonical, bundle, _ = _prepare_view(load_launch_context(base, pack))

    assert not canonical
    assert any(resource.logical_path == 'pack/prompt.md' for resource in projection.resources)
    assert any(file.path == 'r/base/.private_resources/diagrams/flow.svg'
               and b'<svg' in file.body for file in bundle.files)
    assert {path: path.read_bytes() for path in base.rglob('*') if path.is_file()} == source_before
