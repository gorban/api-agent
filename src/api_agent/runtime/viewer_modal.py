"""Fixed packaged presentation assets and bounded managed-wrapper decoration."""

import json
import os
from pathlib import Path
import stat

from api_agent.workflows.document_format import _CARRIER, _HINT, extract_document


SCRIPT_ROUTE = '.private_resources/managed-modal.js'
STYLE_ROUTE = '.private_resources/managed-modal.css'
_MAX_ASSET = 128_000
_MAX_SVGS = 1_000
_MAX_MANIFEST = _MAX_SVGS * (4096 + 4) + 2
_VIEWER_LINE = b'[workflow-viewer]: <script defer src="'
_PRE_MODAL_FRAME_META = b"img-src 'self'; connect-src 'none'"
_MODAL_HEAD = (
    b'[workflow-managed-style]: <link rel="stylesheet" href="/.private_resources/managed-modal.css">\n'
    b'[workflow-managed-script]: <script defer src="/.private_resources/managed-modal.js"> "</script>"\n'
)


def _modal_asset_root():
    from api_agent.workflows.viewer_render import _asset_root
    return _asset_root()


def capture_modal_assets():
    """Read exactly two packaged files, with a finite bound before binding."""
    asset_root = Path(str(_modal_asset_root()))
    package = asset_root.parents[1]
    if not package.is_absolute():
        raise ValueError('Invalid packaged managed viewer location')
    result = {}
    try:
        # The loader's package root is trusted authority; canonicalize its
        # installation alias before opening mutable children without following links.
        package = package.resolve(strict=True)
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in (*package.parts[1:], '.private_resources', 'viewer'):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            for filename, route, content_type in (
                    ('flow-modal.js', SCRIPT_ROUTE, 'text/javascript; charset=utf-8'),
                    ('flow-modal.css', STYLE_ROUTE, 'text/css; charset=utf-8')):
                fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_ASSET:
                        raise ValueError('Invalid packaged managed viewer asset')
                    chunks = []
                    remaining = info.st_size
                    while remaining:
                        chunk = os.read(fd, remaining)
                        if not chunk:
                            raise ValueError('Invalid packaged managed viewer asset')
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    if os.read(fd, 1):
                        raise ValueError('Invalid packaged managed viewer asset')
                    body = b''.join(chunks)
                finally:
                    os.close(fd)
                result[route] = (content_type, body)
        finally:
            os.close(directory)
    except OSError as error:
        raise ValueError('Missing packaged managed viewer asset') from error
    return result


def admitted_svg_paths(routes):
    """Freeze exact URL pathnames already admitted as SVG bundle routes."""
    paths = tuple(sorted('/' + path for path, (kind, _) in routes.items()
                         if path.startswith('r/') and kind == 'image/svg+xml'))
    if len(paths) > _MAX_SVGS:
        raise ValueError('Too many managed SVG routes')
    manifest = json.dumps(paths, separators=(',', ':'), ensure_ascii=True)
    encoded = manifest.encode('ascii')
    if len(encoded) > _MAX_MANIFEST:
        raise ValueError('Managed SVG route manifest exceeds limit')
    return encoded


def compose_modal_script(script, manifest):
    """Bind one finite route manifest to the fixed packaged script before listening."""
    return b'window.__apiAgentManagedSvgRoutes = ' + manifest + b';\n' + script


def decorate_managed_wrapper(path, content_type, body, *, projection=None):
    """Decorate captured pages and annotate resources unavailable from this host."""
    if not body.startswith(_HINT):
        return body, False
    if not (path.startswith('r/') and path.endswith('.md.html')
            and content_type == 'text/html; charset=utf-8'):
        raise ValueError('Managed wrapper has invalid route or type')
    try:
        extracted = extract_document(body, path=Path(path))
    except (ValueError, TypeError) as error:
        raise ValueError('Invalid managed workflow wrapper') from error
    header, marker, carrier = body.partition(_CARRIER)
    if projection is not None:
        from api_agent.workflows.viewer_render import _annotate_hosted_resources, _route
        from api_agent.workflows.document_format import _saved_generated
        document = next((item for item in projection.documents if _route(item.logical_path) == path), None)
        if document is None:
            raise ValueError('Managed wrapper is outside the admitted projection')
        carrier = _annotate_hosted_resources(projection, document,
                                             extracted.executable_bytes.splitlines(keepends=True))
        if extracted.generated is not None:
            heading, newline, rest = carrier.partition(b'\n')
            carrier = heading + newline + _saved_generated(extracted.generated) + rest
    line_start = header.index(_VIEWER_LINE)
    line_end = header.index(b'\n', line_start)
    meta = b'[workflow-managed-svg-routes]: <meta name="workflow-managed-svg-routes" content="v1">\n'
    header = header[:line_end + 1] + meta + _MODAL_HEAD + header[line_end + 1:]
    header = header.replace(_PRE_MODAL_FRAME_META,
                            b"img-src 'self'; frame-src 'self'; connect-src 'none'", 1)
    return header + marker + carrier, True
