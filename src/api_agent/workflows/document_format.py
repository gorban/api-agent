"""Exact inert browser carrier for admitted workflow Markdown."""

from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
import html
import posixpath
import re
from urllib.parse import quote, unquote


_HEAD_V1 = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           b'<meta name="workflow-view" content="v1">'
           b'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; img-src \'self\'; connect-src \'none\'">'
           b'<link rel="stylesheet" href="{assets}viewer.css">'
           b'<script defer src="{assets}marked.js"></script>'
           b'<script defer src="{assets}dompurify.js"></script>'
           b'<script defer src="{assets}viewer.js"></script>'
           b'</head><body>')
_ROOT_FALLBACK_V1 = (b'\n\n\n\n\n\n'
                  b'<!--\n  Open this workflow with Python 3.13 or newer.\n'
                  b'./launch.py --view-flow\n-->\n\n\n\n\n\n'
                  b'<section id="workflow-fallback"><h2>Viewing this file</h2>'
                  b'<p>Python 3.13 or newer is required.</p>'
                  b'<pre>./launch.py --view-flow</pre></section>')
_CARRIER_V1 = b'<plaintext>\n'
_HINT = b'<!-- -*- mode: markdown -*- -->\n'
_HEAD = (
    b'<!-- -*- mode: markdown -*- -->\n'
    b'\n'
    b'<meta charset="utf-8">\n'
    b'<meta name="workflow-view" content="v2">\n'
    b'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\' \'unsafe-hashes\' \'sha256-oh7Gx0FkcA1mNom2ETU41Zi2LVeZci7ocogSJroEPis=\' \'sha256-tG8slQ0asE6MlAE2csEakfk6pO1QfspgSsr25hj415E=\'; img-src \'self\'; frame-src \'self\'; connect-src \'none\'">\n'
    b'\n'
    b'[workflow-hidden-text]: <html style="font-size:0">\n'
    b'[workflow-styles]: <link rel="stylesheet" href="{assets}viewer.css">\n'
    b'[workflow-markdown]: <script defer src="{assets}marked.js"> "</script>"\n'
    b'[workflow-headings]: <script defer src="{assets}heading-id.js"> "</script>"\n'
    b'[workflow-sanitize]: <script defer src="{assets}dompurify.js"> "</script>"\n'
    b'[workflow-viewer]: <script defer src="{assets}viewer.js"> "</script>"\n'
    b'[workflow-modal-styles]: <link rel="stylesheet" href="{assets}flow-modal.css">\n'
    b'[workflow-modal-script]: <script defer src="{assets}flow-modal.js"> "</script>"\n'
    b'\n'
)
_ROOT_FALLBACK = (
    b'<!--\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'  Open this file in a browser to view the workflow.\n'
    b'  For managed viewing, follow Getting started in README.md, then run the launcher.\n'
    b'./launch.sh --view-flow\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'-->\n'
    b'\n'
    b'[workflow-help]: <section id="workflow-fallback" style="font:16px/1.6 system-ui,sans-serif"> "\n'
    b'    <h2>Viewing this file</h2><p>Follow Getting started in README.md, then run from the repository root:</p>\n'
    b'    <pre>./launch.sh --view-flow</pre></section>"\n'
)
_ROOT_INSTRUCTIONS, _ROOT_HELP_BODY = _ROOT_FALLBACK.split(b'[workflow-help]', 1)
_ROOT_HELP = b'[workflow-help]' + _ROOT_HELP_BODY
_ROOT_HEAD = _HEAD.replace(_HINT, _HINT + _ROOT_INSTRUCTIONS, 1)
_HEAD_PRE_MODAL = (_HEAD.replace(b"img-src 'self'; frame-src 'self'; connect-src 'none'",
                                b"img-src 'self'; connect-src 'none'")
                   .replace(b'[workflow-modal-styles]: <link rel="stylesheet" href="{assets}flow-modal.css">\n', b'')
                   .replace(b'[workflow-modal-script]: <script defer src="{assets}flow-modal.js"> "</script>"\n', b''))
_ROOT_HEAD_PRE_MODAL = _HEAD_PRE_MODAL.replace(_HINT, _HINT + _ROOT_INSTRUCTIONS, 1)
_CARRIER = (
    b'[workflow-source]: <plaintext id="workflow-source" hidden>\n'
    b'\n'
)
_NEW_START = b'\n<!-- workflow-generated-diagram-v2:start -->\n'
_NEW_END = b'\n<!-- workflow-generated-diagram-v2:end -->\n'


def _view_command(flow_path: str | None) -> str:
    from .layout import LayoutError
    command = './launch.sh --view-flow'
    if flow_path is None or flow_path == 'flow.md.html':
        return command
    path = PurePosixPath(flow_path)
    if (not flow_path or path.is_absolute() or path.as_posix() != flow_path
            or flow_path.startswith('-') or '<' in flow_path or '>' in flow_path
            or any(part in ('', '.', '..') for part in flow_path.split('/'))
            or any(ord(char) < 32 or ord(char) == 127 for char in flow_path)):
        raise LayoutError('invalid workflow viewing path')
    quoted = ''.join('\\' + char if char in '\\"$`' else char for char in flow_path)
    return command + ' "' + quoted + '"'


def _normalize_view_command(raw: bytes, *, flow_path: str | None = None) -> bytes:
    """Admit only a safely quoted path in the otherwise exact root envelope."""
    from .layout import LayoutError
    prefix, separator, body = raw.partition(_CARRIER)
    if not separator or not raw.startswith(_HINT):
        return raw
    # Keep already-published v2 envelopes readable; only normalize the
    # instructional header, never the executable workflow body.
    prefix = prefix.replace(b'  For managed viewing, use Python 3.13 or newer.\n',
                            b'  For managed viewing, follow Getting started in README.md, then run the launcher.\n')
    prefix = prefix.replace(b'  For managed viewing, install mise and run the launcher.\n',
                            b'  For managed viewing, follow Getting started in README.md, then run the launcher.\n')
    prefix = prefix.replace(b'<h2>Viewing this file</h2><p>Use Python 3.13 or newer from the repository root:</p>',
                            b'<h2>Viewing this file</h2><p>Follow Getting started in README.md, then run from the repository root:</p>')
    prefix = prefix.replace(b'<h2>Viewing this file</h2><p>Install mise, then run from the repository root:</p>',
                            b'<h2>Viewing this file</h2><p>Follow Getting started in README.md, then run from the repository root:</p>')
    prefix = prefix.replace(b'./launch.py --view-flow', b'./launch.sh --view-flow')
    match = re.search(rb'^\./launch\.sh --view-flow ("(?:\\.|[^"\\\r\n])*")$', prefix, re.M)
    if match is None:
        return prefix + separator + body
    try:
        command = match[0].decode('utf-8')
        path = re.sub(r'\\([\\"$`])', r'\1', match[1][1:-1].decode('utf-8'))
        if _view_command(path) != command:
            raise ValueError()
        if flow_path is not None and path != flow_path:
            raise LayoutError('workflow viewing command does not match declaration location')
    except (UnicodeError, ValueError) as error:
        raise LayoutError('invalid workflow viewing command') from error
    base = b'./launch.sh --view-flow'
    prefix = prefix.replace(b'<pre>' + html.escape(command, quote=False).encode() + b'</pre>',
                            b'<pre>' + base + b'</pre>')
    prefix = prefix.replace(match[0], base, 1)
    return prefix + separator + body


def _saved_generated(generated):
    image, _, links = generated.partition(b'\n\n')
    target = image.removeprefix(b'![Workflow diagram](').removesuffix(b')')
    return (_NEW_START + b'[' + image + b'](' + target + b')\n\n'
            b'<details open>\n<summary>Diagram links</summary>\n\n' + links
            + b'\n\n</details>' + _NEW_END)


_ASSETS = re.compile(rb'(?:\.\./)*\.private_resources/viewer/')
_GENERATED_START = b'\n\n## Generated Diagram v1\n'
_GENERATED_END = b'\n<!-- workflow-generated-diagram-v1:end -->\n'
_HEADING = b'## Generated Diagram v1'
_END_MARKER = b'<!-- workflow-generated-diagram-v1:end -->'
_ID = rb'[a-z][a-z0-9_]{0,63}'
_SEGMENT = rb'(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+'
_RELATIVE = rb'(?:' + _SEGMENT + rb'/)*' + _SEGMENT
_NAV_TARGET = rb'(?:' + _RELATIVE + rb'\.md\.html|#node-' + _ID + rb')'
_NAV_ITEM = rb'- \[' + _ID + rb'\]\(' + _NAV_TARGET + rb'\)'
_GENERATED_BODY = re.compile(
    rb'!\[Workflow diagram\]\((' + _RELATIVE + rb'\.svg)\)\n\n'
    rb'\[Open standalone diagram\]\(\1\)'
    rb'(?:\n\n' + _NAV_ITEM + rb'(?:\n' + _NAV_ITEM + rb')*)?')


@dataclass(frozen=True)
class ExtractedDocument:
    executable_bytes: bytes
    line_map: tuple[int, ...]
    presentation: bool
    generated: bytes | None


def _lines(data, offset):
    return tuple(range(offset + 1, offset + len(data.splitlines()) + 1))


def _is_root_document(data: bytes) -> bool:
    lines = data.splitlines()
    return bool(lines) and lines[0] in (b'# Workflow v2', b'# LangGraph flow', b'# Workflow v3')


def extract_document(raw: bytes, *, path: Path) -> ExtractedDocument:
    """Remove only exact browser envelopes and finite generated navigation."""
    from .layout import LayoutError

    if not isinstance(raw, bytes):
        raise TypeError('workflow document must be bytes')
    raw = _normalize_view_command(raw)
    path = Path(path)
    presented = raw.startswith((b'<!doctype html>', _HINT))
    offset = 0
    data = raw
    if presented:
        version2 = raw.startswith(_HINT)
        head = _HEAD if version2 else _HEAD_V1
        carrier = _CARRIER if version2 else _CARRIER_V1
        fallback = _ROOT_FALLBACK if version2 else _ROOT_FALLBACK_V1
        asset_match = re.search(rb'<link rel="stylesheet" href="([^" ]+)viewer.css">', raw)
        if asset_match is None or _ASSETS.fullmatch(asset_match[1]) is None:
            raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
        formatted = head.replace(b'{assets}', asset_match[1])
        prefixes = [formatted + fallback + carrier, formatted + carrier]
        if version2:
            prefixes.insert(0, _ROOT_HEAD.replace(b'{assets}', asset_match[1]) + _ROOT_HELP + carrier)
            old = _HEAD_PRE_MODAL.replace(b'{assets}', asset_match[1])
            prefixes.extend((old + fallback + carrier, old + carrier))
            prefixes.append(_ROOT_HEAD_PRE_MODAL.replace(b'{assets}', asset_match[1]) + _ROOT_HELP + carrier)
        prefix = next((candidate for candidate in prefixes if raw.startswith(candidate)), None)
        if prefix is None:
            raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
        data = raw[len(prefix):]
        offset = prefix.count(b'\n')
    elif raw.startswith((b'<!DOCTYPE', b'<html', b'<script', b'<!doctype', b'<!-- -*-')):
        raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
    generated = None
    # Identify the reserved heading through Markdown tokens, so literal marker text
    # in a fenced prompt remains part of the executable snapshot.
    from .layout import _MD
    try:
        decoded = data.decode('utf-8')
    except UnicodeError as error:
        raise LayoutError(f'{path}:1: invalid UTF-8') from error
    tokens = _MD.parse(decoded)
    physical_lines = list(_lines(data, offset))
    lines = data.splitlines(keepends=True)
    fenced = {line for token in tokens if token.type in ('fence', 'code_block')
              and token.map is not None for line in range(*token.map)}
    markers = [index for index, line in enumerate(lines)
               if index not in fenced and line.rstrip(b'\r\n').startswith(
                   b'<!-- workflow-generated-diagram-v2:')]
    if markers:
        first_heading = next((token.map[0] for token in tokens
                              if token.type == 'heading_open' and token.map is not None), None)
        start_line = markers[0]
        end_lines = [index for index in markers
                     if lines[index] == _NEW_END.lstrip(b'\n')]
        if (first_heading != 0 or start_line != 2 or len(markers) != 2 or len(end_lines) != 1
                or lines[1] != b'\n'):
            raise LayoutError(f'{path}:{offset + start_line + 1}: malformed generated diagram section')
        end_line = end_lines[0]
        block = b''.join(lines[1:end_line + 1])
        match = re.fullmatch(
            re.escape(_NEW_START) + rb'\[(!\[Workflow diagram\]\((' + _RELATIVE
            + rb'\.svg)\))\]\(\2\)\n\n<details open>\n'
            rb'<summary>Diagram links</summary>\n\n(.*?)\n\n</details>'
            + re.escape(_NEW_END), block, re.S)
        if match is None:
            raise LayoutError(f'{path}:{offset + start_line + 1}: unsafe generated diagram section')
        generated = match[1] + b'\n\n' + match[3]
        if _GENERATED_BODY.fullmatch(generated) is None:
            raise LayoutError(f'{path}:{offset + start_line + 1}: unsafe generated diagram section')
        data = lines[0] + b''.join(lines[end_line + 1:])
        physical_lines = physical_lines[:1] + physical_lines[end_line + 1:]
        decoded = data.decode('utf-8')
        tokens = _MD.parse(decoded)
    headings = [token.map[0] for token in tokens if token.type == 'heading_open'
                and token.tag == 'h2' and token.map is not None
                and decoded.splitlines()[token.map[0]] == _HEADING.decode()]
    if headings:
        if generated is not None:
            raise LayoutError(f'{path}:1: duplicate generated diagram section')
        if len(headings) != 1 or not data.endswith(_GENERATED_END):
            raise LayoutError(f'{path}:{offset + headings[0] + 1}: malformed generated diagram section')
        lines = data.splitlines(keepends=True)
        marker = sum(map(len, lines[:headings[0]]))
        if marker < 2 or data[marker - 2:marker] != b'\n\n':
            raise LayoutError(f'{path}:{offset + headings[0] + 1}: malformed generated diagram section')
        generated = data[marker + len(_HEADING) + 1:-len(_GENERATED_END)]
        if _GENERATED_BODY.fullmatch(generated) is None:
            raise LayoutError(f'{path}:{offset + headings[0] + 1}: unsafe generated diagram section')
        data = data[:marker - 2]
        physical_lines = physical_lines[:len(data.splitlines())]
    return ExtractedDocument(data, tuple(physical_lines), presented, generated)


def canonical_executable_bytes(raw: bytes, *, path: Path) -> bytes:
    return extract_document(raw, path=path).executable_bytes


def validate_presentation_location(raw: bytes, *, path: Path, root: Path) -> None:
    """Bind a presented document's static links to its selected owning root."""
    from .layout import LayoutError

    path, root = Path(path), Path(root)
    relative = path.relative_to(root).as_posix()
    if re.search(rb'^\./launch\.(?:py|sh) --view-flow "', raw, re.M):
        from .viewer_repository import read_repository_identity
        recorded = read_repository_identity(root)
        prefix = recorded[0].path_prefix if recorded is not None else ''
        expected_path = '/'.join(part for part in (prefix, relative) if part)
        raw = _normalize_view_command(raw, flow_path=expected_path)
    else:
        raw = _normalize_view_command(raw)
    extracted = extract_document(raw, path=path)
    if not extracted.presentation and extracted.generated is None:
        return
    if extracted.presentation:
        depth = len(Path(relative).parts) - 1
        assets = (b'../' * depth) + b'.private_resources/viewer/'
        is_root = _is_root_document(extracted.executable_bytes)
        version2 = raw.startswith(_HINT)
        head, fallback, carrier = ((_HEAD, _ROOT_FALLBACK, _CARRIER) if version2 else
                                    (_HEAD_V1, _ROOT_FALLBACK_V1, _CARRIER_V1))
        expected = [head.replace(b'{assets}', assets) + (fallback if is_root else b'') + carrier]
        if version2 and is_root:
            expected.append(_ROOT_HEAD.replace(b'{assets}', assets) + _ROOT_HELP + carrier)
        if version2:
            expected.append(_HEAD_PRE_MODAL.replace(b'{assets}', assets)
                            + (fallback if is_root else b'') + carrier)
            if is_root:
                expected.append(_ROOT_HEAD_PRE_MODAL.replace(b'{assets}', assets) + _ROOT_HELP + carrier)
        if not any(raw.startswith(prefix) for prefix in expected):
            raise LayoutError(f'{path}:1: viewer asset prefix does not match declaration owner')
    if extracted.generated is not None:
        svg = posixpath.relpath('.private_resources/diagrams/' + relative.removesuffix('.md.html') + '.svg',
                                posixpath.dirname(relative) or '.')
        svg_uri = quote(svg, safe='/-._~')
        expected = f'![Workflow diagram]({svg_uri})\n\n[Open standalone diagram]({svg_uri})'.encode()
        if not extracted.generated.startswith(expected):
            raise LayoutError(f'{path}:1: generated diagram path does not match declaration owner')
        local_links = re.findall(rb'- \[(' + _ID + rb')\]\(#node-(' + _ID + rb')\)',
                                 extracted.generated[len(expected):])
        if local_links:
            from .layout import _MD
            text = extracted.executable_bytes.decode('utf-8')
            lines = text.splitlines()
            headings = {lines[token.map[0]].removeprefix('## Node: ')
                        for token in _MD.parse(text)
                        if token.type == 'heading_open' and token.tag == 'h2'
                        and token.map is not None and lines[token.map[0]].startswith('## Node: ')}
            if any(label != identifier or identifier.decode() not in headings
                   for label, identifier in local_links):
                raise LayoutError(f'{path}:1: generated node navigation has no local declaration')
        for target in re.findall(rb'\]\((' + _RELATIVE + rb'\.md\.html)\)',
                                 extracted.generated[len(expected):]):
            encoded = target.decode('ascii')
            try:
                decoded = unquote(encoded, errors='strict')
            except UnicodeError as error:
                raise LayoutError(f'{path}:1: invalid generated navigation encoding') from error
            if (quote(decoded, safe='/-._~') != encoded or re.search(r'%(?:2f|5c)', encoded, re.I)
                    or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                    or '\\' in decoded or '?' in decoded or '#' in decoded):
                raise LayoutError(f'{path}:1: invalid generated navigation encoding')
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(relative), decoded))
            if normalized == '..' or normalized.startswith('../'):
                raise LayoutError(f'{path}:1: generated navigation escapes declaration owner')


def wrap_document(executable_bytes: bytes, *, root: bool, asset_prefix: str,
                  generated: bytes = b'', flow_path: str | None = None) -> bytes:
    """Present inert Markdown with optional finite navigation after its heading."""
    from .layout import LayoutError

    if not isinstance(executable_bytes, bytes) or not isinstance(generated, bytes):
        raise TypeError('workflow document and generated section must be bytes')
    prefix = asset_prefix.encode('ascii')
    if _ASSETS.fullmatch(prefix) is None:
        raise LayoutError('invalid viewer asset prefix')
    source = extract_document(executable_bytes, path=Path('flow.md.html'))
    if source.presentation or source.generated is not None:
        raise LayoutError('wrap_document requires executable Markdown')
    if root != _is_root_document(source.executable_bytes):
        raise LayoutError('workflow presentation root mismatch')
    if generated:
        heading, separator, remainder = executable_bytes.partition(b'\n')
        if not separator:
            raise LayoutError('generated diagram requires a complete heading line')
        executable_bytes = heading + separator + _saved_generated(generated) + remainder
    head = _ROOT_HEAD if root else _HEAD
    head = head.replace(b'{assets}', prefix)
    help_body = _ROOT_HELP if root else b''
    if root:
        command = _view_command(flow_path)
        head = head.replace(b'./launch.sh --view-flow', command.encode('utf-8'))
        help_body = help_body.replace(b'./launch.sh --view-flow', html.escape(command, quote=False).encode())
    return head + help_body + _CARRIER + executable_bytes
