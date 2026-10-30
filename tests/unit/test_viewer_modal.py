"""Managed overlay host captures fixed assets and decorates only exact wrappers."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime import viewer_host, viewer_modal
from api_agent.workflows.document_format import (_CARRIER, _HEAD_PRE_MODAL, _ROOT_HEAD_PRE_MODAL,
                                                 _ROOT_HELP,
                                                 extract_document, validate_presentation_location,
                                                 wrap_document)
from api_agent.workflows.viewer_render import ViewFile


def _wrapper(root=True):
    source = b'# Workflow v2\n' if root else b'# Component\n'
    prefix = '.private_resources/viewer/' if root else '../.private_resources/viewer/'
    return wrap_document(source, root=root, asset_prefix=prefix)


@pytest.mark.parametrize('root', [True, False])
def test_old_v2_envelope_remains_exactly_readable_while_new_header_is_emitted(tmp_path, root):
    prefix = b'.private_resources/viewer/' if root else b'../.private_resources/viewer/'
    old_head = (_ROOT_HEAD_PRE_MODAL.replace(b'{assets}', prefix) + _ROOT_HELP if root else
                _HEAD_PRE_MODAL.replace(b'{assets}', prefix))
    old = old_head + _CARRIER + (b'# Workflow v2\n' if root else b'# Component\n')
    location = tmp_path / ('flow.md.html' if root else 'components/agent.md.html')
    assert extract_document(old, path=location).executable_bytes == (
        b'# Workflow v2\n' if root else b'# Component\n')
    validate_presentation_location(old, path=location, root=tmp_path)
    hosted, managed = viewer_modal.decorate_managed_wrapper(
        'r/base/flow.md.html' if root else 'r/base/components/agent.md.html',
        'text/html; charset=utf-8', old)
    assert managed and b"frame-src 'self'" in hosted.partition(_CARRIER)[0]
    assert hosted.partition(_CARRIER)[2] == old.partition(_CARRIER)[2]
    current = _wrapper(root)
    assert b"frame-src 'self'" in current and b'flow-modal.js' in current
    hybrid = old.replace(b"img-src 'self'; connect-src 'none'",
                         b"img-src 'self'; frame-src 'self'; connect-src 'none'")
    with pytest.raises(ValueError, match='invalid workflow presentation envelope'):
        extract_document(hybrid, path=location)


def test_packaged_modal_assets_are_finite_and_missing_assets_fail(tmp_path, monkeypatch):
    assets = tmp_path / '.private_resources/viewer'
    assets.mkdir(parents=True)
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: assets)
    (assets / 'flow-modal.js').write_bytes(b'/* modal */')
    (assets / 'flow-modal.css').write_bytes(b'/* style */')
    captured = viewer_modal.capture_modal_assets()
    assert captured == {
        viewer_modal.SCRIPT_ROUTE: ('text/javascript; charset=utf-8', b'/* modal */'),
        viewer_modal.STYLE_ROUTE: ('text/css; charset=utf-8', b'/* style */'),
    }
    (assets / 'flow-modal.js').write_bytes(b'x' * 128_001)
    with pytest.raises(ValueError, match='Invalid packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()
    (assets / 'flow-modal.js').unlink()
    with pytest.raises(ValueError, match='Missing packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()


def test_packaged_assets_reject_symlinks_and_fifo_without_blocking(tmp_path, monkeypatch):
    assets = tmp_path / '.private_resources/viewer'
    assets.mkdir(parents=True)
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: assets)
    outside = tmp_path / 'outside.js'
    outside.write_bytes(b'foreign')
    (assets / 'flow-modal.js').symlink_to(outside)
    (assets / 'flow-modal.css').write_bytes(b'css')
    with pytest.raises(ValueError, match='Missing packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()
    (assets / 'flow-modal.js').unlink()
    os.mkfifo(assets / 'flow-modal.js')
    with pytest.raises(ValueError, match='Invalid packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()
    (assets / 'flow-modal.js').unlink()
    assets.rename(tmp_path / '.private_resources/original_assets')
    assets.symlink_to(tmp_path / '.private_resources/original_assets', target_is_directory=True)
    with pytest.raises(ValueError, match='Missing packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()


def test_packaged_asset_truncation_during_capture_fails_closed(tmp_path, monkeypatch):
    assets = tmp_path / '.private_resources/viewer'
    assets.mkdir(parents=True)
    (assets / 'flow-modal.js').write_bytes(b'javascript')
    (assets / 'flow-modal.css').write_bytes(b'css')
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: assets)
    monkeypatch.setattr(viewer_modal.os, 'read', lambda fd, count: b'')
    with pytest.raises(ValueError, match='Invalid packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()


def test_packaged_asset_growth_during_capture_fails_closed(tmp_path, monkeypatch):
    assets = tmp_path / '.private_resources/viewer'
    assets.mkdir(parents=True)
    (assets / 'flow-modal.js').write_bytes(b'javascript')
    (assets / 'flow-modal.css').write_bytes(b'css')
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: assets)
    original = os.read
    monkeypatch.setattr(viewer_modal.os, 'read', lambda fd, count: b'x' if count == 1 else original(fd, count))
    with pytest.raises(ValueError, match='Invalid packaged managed viewer asset'):
        viewer_modal.capture_modal_assets()


def test_trusted_package_alias_is_allowed_but_relative_location_is_not(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    (outside / 'runtime' / '.private_resources/viewer').mkdir(parents=True)
    (outside / 'runtime' / '.private_resources/viewer' / 'flow-modal.js').write_bytes(b'foreign')
    (outside / 'runtime' / '.private_resources/viewer' / 'flow-modal.css').write_bytes(b'foreign')
    (tmp_path / 'linked').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: tmp_path / 'linked' / 'runtime' / '.private_resources/viewer')
    assert viewer_modal.capture_modal_assets()[viewer_modal.SCRIPT_ROUTE][1] == b'foreign'
    monkeypatch.setattr(viewer_modal, '_modal_asset_root', lambda: Path('relative/runtime/.private_resources/viewer'))
    with pytest.raises(ValueError, match='Invalid packaged managed viewer location'):
        viewer_modal.capture_modal_assets()


@pytest.mark.parametrize('root', [True, False])
def test_exact_wrapper_decoration_preserves_carrier_and_admitted_routes(root):
    original = _wrapper(root)
    paths = viewer_modal.admitted_svg_paths({
        'r/base/.private_resources/diagrams/z.svg': ('image/svg+xml', b'<svg/>'),
        'r/base/.private_resources/diagrams/a.svg': ('image/svg+xml', b'<svg/>'),
        'r/base/cfg/config.toml': ('text/plain; charset=utf-8', b'x'),
    })
    path = 'r/base/flow.md.html' if root else 'r/base/components/agent.md.html'
    enhanced, managed = viewer_modal.decorate_managed_wrapper(
        path, 'text/html; charset=utf-8', original)
    assert managed
    assert enhanced.partition(_CARRIER)[2] == original.partition(_CARRIER)[2]
    header = enhanced.partition(_CARRIER)[0]
    assert header.index(b'viewer.js') < header.index(b'workflow-managed-svg-routes')
    assert header.index(b'workflow-managed-svg-routes') < header.index(b'managed-modal.js')
    assert b"frame-src 'self'" in header and b"connect-src 'none'" in header
    assert b"'unsafe-inline'" not in header
    assert b'/.private_resources/managed-modal.js' in header
    assert b'/.private_resources/managed-modal.css' in header
    assert b'name="workflow-managed-svg-routes" content="v1"' in header
    script = viewer_modal.compose_modal_script(b'/* packaged */', paths)
    assert script.startswith(b'window.__apiAgentManagedSvgRoutes = ')
    encoded = script.split(b' = ', 1)[1].split(b';\n', 1)[0]
    assert json.loads(encoded.decode()) == [
        '/r/base/.private_resources/diagrams/a.svg',
        '/r/base/.private_resources/diagrams/z.svg',
    ]
    assert sha256(original).digest() != sha256(enhanced).digest()


def test_only_exact_managed_wrapper_is_enhanced():
    assert viewer_modal.decorate_managed_wrapper('r/base/flow.md.html', 'text/html; charset=utf-8',
                                                 b'<p>plain</p>') == (b'<p>plain</p>', False)
    with pytest.raises(ValueError, match='invalid route or type'):
        viewer_modal.decorate_managed_wrapper('r/base/flow.md.html', 'text/plain', _wrapper())
    damaged = _wrapper().replace(b'viewer.js', b'missing.js', 1)
    with pytest.raises(ValueError, match='Invalid managed workflow wrapper'):
        viewer_modal.decorate_managed_wrapper('r/base/flow.md.html', 'text/html; charset=utf-8',
                                              damaged)


def test_svg_manifest_finite_and_json_encoded():
    paths = {f'r/base/.private_resources/diagrams/{index}.svg': ('image/svg+xml', b'x')
             for index in range(129)}
    assert len(json.loads(viewer_modal.admitted_svg_paths(paths).decode())) == 129
    paths.update({f'r/base/.private_resources/diagrams/{index}.svg': ('image/svg+xml', b'x')
                  for index in range(129, 1_000)})
    assert len(json.loads(viewer_modal.admitted_svg_paths(paths).decode())) == 1_000
    paths['r/base/.private_resources/diagrams/1000.svg'] = ('image/svg+xml', b'x')
    with pytest.raises(ValueError, match='Too many managed SVG routes'):
        viewer_modal.admitted_svg_paths(paths)
    paths = {'r/base/.private_resources/diagrams/a%26b.svg': ('image/svg+xml', b'x')}
    manifest = viewer_modal.admitted_svg_paths(paths)
    assert b'&quot;' not in manifest
    assert json.loads(manifest.decode()) == ['/r/base/.private_resources/diagrams/a%26b.svg']


def test_manifest_byte_budget_rejects_before_script_composition(monkeypatch):
    paths = {'r/base/.private_resources/diagrams/a.svg': ('image/svg+xml', b'x')}
    encoded = viewer_modal.admitted_svg_paths(paths)
    monkeypatch.setattr(viewer_modal, '_MAX_MANIFEST', len(encoded) - 1)
    with pytest.raises(ValueError, match='manifest exceeds limit'):
        viewer_modal.admitted_svg_paths(paths)


async def _get(port, path):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(f'GET /{path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response.partition(b'\r\n\r\n')


def test_host_serves_fixed_assets_without_mutating_captured_graph_or_svg(monkeypatch):
    from api_agent.workflows import viewer_render
    monkeypatch.setattr(viewer_render, 'verify_bundle_proof', lambda *args, **kwargs: None)
    wrapper = _wrapper()
    graphic = b'<svg xmlns="http://www.w3.org/2000/svg" />'
    files = (
        ViewFile('r/base/flow.md.html', 'text/html; charset=utf-8', wrapper),
        ViewFile('r/base/.private_resources/diagrams/flow.svg', 'image/svg+xml', graphic),
        ViewFile('r/base/cfg/config.toml', 'text/plain; charset=utf-8', b'limit = 2\n'),
    )
    bundle = SimpleNamespace(entry_path=files[0].path, files=files)

    async def exercise():
        projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),), resources=())
        host = await viewer_host.start_viewer_host(bundle, projection=projection)
        try:
            assert host.entry_url.endswith('/r/base/flow.md.html')
            headers, _, body = await _get(host.port, files[0].path)
            assert b'frame-src \'self\'' in headers
            assert b'workflow-managed-svg-routes' in body
            assert body.endswith(b'# Workflow v2\n')
            for file in files[1:]:
                _, _, served = await _get(host.port, file.path)
                assert served == file.body
            for path, kind in ((viewer_modal.SCRIPT_ROUTE, b'text/javascript'),
                               (viewer_modal.STYLE_ROUTE, b'text/css')):
                headers, _, served = await _get(host.port, path)
                assert kind in headers and served == host.routes[path][1]
                if path == viewer_modal.SCRIPT_ROUTE:
                    assert served.startswith(b'window.__apiAgentManagedSvgRoutes = [')
                    assert b'/r/base/.private_resources/diagrams/flow.svg' in served
            headers, _, _ = await _get(host.port, '.private_resources/unknown.js')
            assert headers.startswith(b'HTTP/1.1 404')
        finally:
            await host.aclose()

    asyncio.run(exercise())
    assert files[0].body == wrapper and files[1].body == graphic


def test_host_rejects_fixed_route_collision_before_binding(monkeypatch):
    from api_agent.workflows import viewer_render
    monkeypatch.setattr(viewer_render, 'verify_bundle_proof', lambda *args, **kwargs: None)
    async def unexpected_bind(*args, **kwargs):
        raise AssertionError('listener must not bind')
    monkeypatch.setattr(viewer_host.asyncio, 'start_server', unexpected_bind)
    files = (ViewFile('r/base/flow.md.html', 'text/html; charset=utf-8', b'<p>plain</p>'),
             ViewFile(viewer_modal.SCRIPT_ROUTE, 'text/javascript; charset=utf-8', b'foreign'))
    bundle = SimpleNamespace(entry_path=files[0].path, files=files)
    with pytest.raises(ValueError, match='asset route collision'):
        asyncio.run(viewer_host.start_viewer_host(bundle, projection=object()))


def test_host_admits_129_svg_routes(monkeypatch):
    from api_agent.workflows import viewer_render
    monkeypatch.setattr(viewer_render, 'verify_bundle_proof', lambda *args, **kwargs: None)
    files = [ViewFile('r/base/flow.md.html', 'text/html; charset=utf-8', _wrapper())]
    files.extend(ViewFile(f'r/base/.private_resources/diagrams/{index}.svg', 'image/svg+xml',
                          b'<svg xmlns="http://www.w3.org/2000/svg" />') for index in range(129))
    bundle = SimpleNamespace(entry_path=files[0].path, files=tuple(files))
    async def exercise():
        projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),), resources=())
        host = await viewer_host.start_viewer_host(bundle, projection=projection)
        try:
            assert len(host.routes) >= 130
            assert b'/r/base/.private_resources/diagrams/128.svg' in host.routes[viewer_modal.SCRIPT_ROUTE][1]
        finally:
            await host.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('temporary', [False, True])
def test_host_serves_captured_native_text_for_saved_and_temporary_views(temporary):
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.view_flow import _prepare_view
    from api_agent.workflows.viewer_render import build_view_bundle, capture_repository_sources
    from test_workflow_viewer_discovery import TinyRenderer
    root = Path(__file__).resolve().parents[2]
    entry = root / 'demo/simple/flow.md.html'
    declaration = root / 'demo/simple/workflows/components/execute_tool.md.html'
    before = declaration.read_bytes()
    definition, projection, canonical, bundle, repository_sources = _prepare_view(load_launch_context(root), entry=entry)
    assert canonical is True
    if temporary:
        bundle = build_view_bundle(projection, roots=definition.roots, renderer=TinyRenderer(), verify=False,
                                   repositories=capture_repository_sources(definition.roots)[0],
                                   repository_sources=repository_sources)
    async def exercise():
        host = await viewer_host.start_viewer_host(bundle, projection=projection, repository_sources=repository_sources)
        try:
            headers, _, body = await _get(host.port, 'r/base/demo/simple/workflows/components/execute_tool.md.html')
            assert headers.startswith(b'HTTP/1.1 200')
            assert b'[Python](../../functions.py)' in body
            assert b'captured native_source:' not in body
            headers, _, source = await _get(host.port, 'r/base/demo/simple/functions.py')
            assert headers.startswith(b'HTTP/1.1 200')
            assert b'text/plain; charset=utf-8' in headers
            assert source == (root / 'demo/simple/functions.py').read_bytes()
            assert b'workflow-generated-diagram-v2:start' in body
            assert b'[input](../../schemas/empty.json)' in body
        finally:
            await host.aclose()
    asyncio.run(exercise())
    assert declaration.read_bytes() == before


def test_managed_page_must_belong_to_the_admitted_projection():
    projection = SimpleNamespace(documents=(), resources=())
    with pytest.raises(ValueError, match='outside the admitted projection'):
        viewer_modal.decorate_managed_wrapper('r/base/flow.md.html', 'text/html; charset=utf-8',
                                              _wrapper(), projection=projection)
