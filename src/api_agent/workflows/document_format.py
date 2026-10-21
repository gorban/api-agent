"""Exact inert browser carrier for admitted workflow Markdown."""

from dataclasses import dataclass
from pathlib import Path
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
    b'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\' \'unsafe-hashes\' \'sha256-oh7Gx0FkcA1mNom2ETU41Zi2LVeZci7ocogSJroEPis=\' \'sha256-tG8slQ0asE6MlAE2csEakfk6pO1QfspgSsr25hj415E=\'; img-src \'self\'; connect-src \'none\'">\n'
    b'\n'
    b'[workflow-hidden-text]: <html style="font-size:0">\n'
    b'[workflow-styles]: <link rel="stylesheet" href="{assets}viewer.css">\n'
    b'[workflow-markdown]: <script defer src="{assets}marked.js"> "</script>"\n'
    b'[workflow-headings]: <script defer src="{assets}heading-id.js"> "</script>"\n'
    b'[workflow-sanitize]: <script defer src="{assets}dompurify.js"> "</script>"\n'
    b'[workflow-viewer]: <script defer src="{assets}viewer.js"> "</script>"\n'
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
    b'  For managed viewing, use Python 3.13 or newer.\n'
    b'./launch.py --view-flow\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'\n'
    b'-->\n'
    b'\n'
    b'[workflow-help]: <section id="workflow-fallback" style="font:16px/1.6 system-ui,sans-serif"> "\n'
    b'    <h2>Viewing this file</h2><p>Use Python 3.13 or newer from the repository root:</p>\n'
    b'    <pre>./launch.py --view-flow</pre></section>"\n'
)
_CARRIER = (
    b'[workflow-source]: <plaintext id="workflow-source" hidden>\n'
    b'\n'
)
_NEW_START = b'\n<!-- workflow-generated-diagram-v2:start -->\n'
_NEW_END = b'\n<!-- workflow-generated-diagram-v2:end -->\n'


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
_GENERATED_BODY = re.compile(
    rb'!\[Workflow diagram\]\((' + _RELATIVE + rb'\.svg)\)\n\n'
    rb'\[Open standalone diagram\]\(\1\)'
    rb'(?:\n\n- \[' + _ID + rb'\]\(' + _RELATIVE + rb'\.md\.html\)'
    rb'(?:\n- \[' + _ID + rb'\]\(' + _RELATIVE + rb'\.md\.html\))*' rb')?')


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
        prefix = head.replace(b'{assets}', asset_match[1])
        if not raw.startswith(prefix):
            raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
        suffix = raw[len(prefix):]
        prefix += fallback + carrier if suffix.startswith(fallback) else carrier
        if not raw.startswith(prefix):
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
        expected_prefix = (head.replace(b'{assets}', assets)
                           + (fallback if is_root else b'') + carrier)
        if expected_prefix != raw[:len(expected_prefix)]:
            raise LayoutError(f'{path}:1: viewer asset prefix does not match declaration owner')
    if extracted.generated is not None:
        svg = posixpath.relpath('.private_resources/diagrams/' + relative.removesuffix('.md.html') + '.svg',
                                posixpath.dirname(relative) or '.')
        svg_uri = quote(svg, safe='/-._~')
        expected = f'![Workflow diagram]({svg_uri})\n\n[Open standalone diagram]({svg_uri})'.encode()
        if not extracted.generated.startswith(expected):
            raise LayoutError(f'{path}:1: generated diagram path does not match declaration owner')
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
                  generated: bytes = b'') -> bytes:
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
    return (_HEAD.replace(b'{assets}', prefix) + (_ROOT_FALLBACK if root else b'')
            + _CARRIER + executable_bytes)
