"""Exact inert browser carrier for admitted workflow Markdown."""

from dataclasses import dataclass
from pathlib import Path
import posixpath
import re
from urllib.parse import quote, unquote


_HEAD = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           b'<meta name="workflow-view" content="v1">'
           b'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; img-src \'self\'; connect-src \'none\'">'
           b'<link rel="stylesheet" href="{assets}viewer.css">'
           b'<script defer src="{assets}marked.js"></script>'
           b'<script defer src="{assets}dompurify.js"></script>'
           b'<script defer src="{assets}viewer.js"></script>'
           b'</head><body>')
_ROOT_FALLBACK = (b'\n\n\n\n\n\n'
                  b'<!--\n  Open this workflow with Python 3.13 or newer.\n'
                  b'./launch.py --view-flow\n-->\n\n\n\n\n\n'
                  b'<section id="workflow-fallback"><h2>Viewing this file</h2>'
                  b'<p>Python 3.13 or newer is required.</p>'
                  b'<pre>./launch.py --view-flow</pre></section>')
_CARRIER = b'<plaintext>\n'
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
    return bool(lines) and lines[0] in (b'# Workflow v2', b'# Workflow v3')


def extract_document(raw: bytes, *, path: Path) -> ExtractedDocument:
    """Remove only the exact browser envelope and exact terminal generated block."""
    from .layout import LayoutError

    if not isinstance(raw, bytes):
        raise TypeError('workflow document must be bytes')
    path = Path(path)
    presented = raw.startswith(b'<!doctype html>')
    offset = 0
    data = raw
    if presented:
        match = re.match(rb'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                         rb'<meta name="workflow-view" content="v1">'
                         rb'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\'; img-src \'self\'; connect-src \'none\'">'
                         rb'<link rel="stylesheet" href="((?:\.\./)*\.private_resources/viewer/)viewer\.css">'
                         rb'<script defer src="\1marked\.js"></script>'
                         rb'<script defer src="\1dompurify\.js"></script>'
                         rb'<script defer src="\1viewer\.js"></script>'
                         rb'</head><body>', raw)
        if match is None or _ASSETS.fullmatch(match[1]) is None:
            raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
        suffix = raw[match.end():]
        carrier = (_ROOT_FALLBACK + _CARRIER if suffix.startswith(_ROOT_FALLBACK)
                   else _CARRIER)
        if not suffix.startswith(carrier):
            raise LayoutError(f'{path}:1: invalid workflow presentation envelope')
        data = suffix[len(carrier):]
        offset = (raw[:match.end()] + carrier).count(b'\n')
    elif raw.startswith((b'<!DOCTYPE', b'<html', b'<script', b'<!doctype')):
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
    headings = [token.map[0] for token in tokens if token.type == 'heading_open'
                and token.tag == 'h2' and token.map is not None
                and decoded.splitlines()[token.map[0]] == _HEADING.decode()]
    if headings:
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
    return ExtractedDocument(data, _lines(data, offset), presented, generated)


def canonical_executable_bytes(raw: bytes, *, path: Path) -> bytes:
    return extract_document(raw, path=path).executable_bytes


def validate_presentation_location(raw: bytes, *, path: Path, root: Path) -> None:
    """Bind a presented document's static links to its selected owning root."""
    from .layout import LayoutError

    path, root = Path(path), Path(root)
    relative = path.relative_to(root).as_posix()
    extracted = extract_document(raw, path=path)
    if not extracted.presentation:
        return
    depth = len(Path(relative).parts) - 1
    assets = (b'../' * depth) + b'.private_resources/viewer/'
    is_root = _is_root_document(extracted.executable_bytes)
    expected_prefix = (_HEAD.replace(b'{assets}', assets)
                       + (_ROOT_FALLBACK if is_root else b'') + _CARRIER)
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
    """Present admitted Markdown as inert plaintext; the generator owns the suffix."""
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
    suffix = (_GENERATED_START + generated + _GENERATED_END) if generated else b''
    return (_HEAD.replace(b'{assets}', prefix) + (_ROOT_FALLBACK if root else b'')
            + _CARRIER + executable_bytes + suffix)
