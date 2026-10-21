"""Bounded, deterministic rendering of admitted workflow projections."""

from __future__ import annotations

from dataclasses import dataclass
import base64
from bisect import bisect_right
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Mapping
from types import MappingProxyType

from .viewer_repository import RepositoryIdentity, CONFIG_PATH, read_repository_identity
from urllib.parse import quote, unquote, urlsplit
import html
import json
import math
import os
import posixpath
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import tomllib
from xml.etree import ElementTree as ET


PLANTUML_VERSION = "1.2026.9"
PLANTUML_SHA256 = "835b2f2eb1d756da3f03bffdbd0baf252b4b858547a741ac8e972958a09c7f91"
RENDER_OPTIONS = ("-Playout=smetana", "-tsvg")
_SVG_FONT_STACK = "DejaVu Sans, Liberation Sans, Arial, sans-serif"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_MAX_INPUT = 250_000
_MAX_OUTPUT = 4_000_000
_MAX_JAR = 30_000_000
_MAX_RECORDS = 1_000
_MAX_LABEL = 256
_MAX_REFERENCE = 65_536
_ALLOWED_TAGS = frozenset("svg g a rect ellipse circle path line polygon polyline text tspan defs linearGradient stop title desc clipPath".split())
_ALLOWED_ATTRS = frozenset("width height viewBox version xmlns xmlns:xlink id class style fill fill-opacity stroke stroke-width stroke-dasharray stroke-linecap stroke-linejoin x y x1 x2 y1 y2 cx cy rx ry r d points transform font-family font-size font-weight text-anchor dominant-baseline opacity offset gradientUnits target href preserveAspectRatio role aria-label contentStyleType data-diagram-type data-entity-1 data-entity-2 data-link-type data-qualified-name data-source-line lengthAdjust textLength zoomAndPan type actuate show title".split())
_STYLE_COLORS = frozenset({"none", "transparent", "currentcolor"})
_SVG_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_SVG_SCALARS = frozenset("x y x1 x2 y1 y2 cx cy rx ry width height stroke-width font-size textLength".split())


@dataclass(frozen=True)
class ViewFile:
    path: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class ViewSource:
    logical_path: str
    content_kind: str
    sha256: str
    length: int
    served: bool


@dataclass(frozen=True)
class ViewRoute:
    path: str
    content_type: str
    sha256: str
    length: int


@dataclass(frozen=True)
class ViewBundle:
    entry_path: str
    files: tuple[ViewFile, ...]
    graph_id: str = ""
    definition_id: str = ""
    projection_sha256: str = ""
    sources: tuple[ViewSource, ...] = ()
    routes: tuple[ViewRoute, ...] = ()


class RenderError(ValueError):
    """A view could not be rendered or verified."""


def verify_bundle_proof(bundle: ViewBundle, projection: object, *, repository_sources: tuple[ViewSource, ...] = ()) -> None:
    """Bind host routes to one admitted identity and captured source inventory."""
    if (bundle.graph_id, bundle.definition_id, bundle.projection_sha256) != (
            projection.graph_id, projection.definition_id, projection.projection_sha256):
        raise RenderError("workflow view identity differs from admitted projection")
    if bundle.entry_path != _route(projection.entry_path):
        raise RenderError("workflow view entry differs from admitted projection")
    paths = [file.path for file in bundle.files]
    if len(paths) != len(set(paths)) or bundle.entry_path not in paths:
        raise RenderError("duplicate or absent workflow view route")
    expected_routes = tuple(ViewRoute(f.path, f.content_type, sha256(f.body).hexdigest(), len(f.body))
                            for f in bundle.files)
    if bundle.routes != expected_routes:
        raise RenderError("workflow view route digest mismatch")
    owners = {PurePosixPath(d.logical_path).parts[0] for d in projection.documents}
    asset_types = {"viewer.js": "text/javascript; charset=utf-8",
                   "viewer.css": "text/css; charset=utf-8",
                   "marked.js": "text/javascript; charset=utf-8",
                   "dompurify.js": "text/javascript; charset=utf-8",
                   "marked-LICENSE.txt": "text/plain; charset=utf-8",
                   "dompurify-LICENSE.txt": "text/plain; charset=utf-8",
                   "PlantUML-NOTICE.txt": "text/plain; charset=utf-8",
                   "VIEWER-ASSET-NOTICE.txt": "text/plain; charset=utf-8",
                   "heading-id.js": "text/javascript; charset=utf-8",
                   "svg-links.js": "text/javascript; charset=utf-8",
                   "heading-id-LICENSE.txt": "text/plain; charset=utf-8",
                   "github-slugger-LICENSE.txt": "text/plain; charset=utf-8"}
    allowed = {(f"r/{owner}/.private_resources/viewer/{name}", kind)
               for owner in owners for name, kind in asset_types.items()}
    for document in projection.documents:
        allowed.update(((_route(document.logical_path), "text/html; charset=utf-8"),
                        (_svg_route(document.logical_path), "image/svg+xml"),
                        (_companion_route(document.logical_path), "text/markdown; charset=utf-8")))
    allowed.update((route, 'text/html; charset=utf-8') for route in _reference_pages(projection))
    if {(file.path, file.content_type) for file in bundle.files} != allowed or len(bundle.files) != len(allowed):
        raise RenderError("workflow view route inventory mismatch")
    reference_pages = _reference_pages(projection)
    if any(file.body != reference_pages[file.path] for file in bundle.files
           if file.path in reference_pages):
        raise RenderError('workflow reference page differs from captured source')
    selected_paths = {d.logical_path for d in projection.documents}
    expected_sources = [ViewSource(d.logical_path, "declaration", sha256(d.raw_bytes).hexdigest(),
                                   len(d.raw_bytes), True) for d in projection.documents]
    for resource in projection.resources:
        if resource.logical_path not in selected_paths:
            expected_sources.append(ViewSource(resource.logical_path, resource.kind, resource.sha256,
                                               len(resource.content_bytes), False))
    if bundle.sources != tuple(expected_sources) + repository_sources:
        raise RenderError("workflow view source inventory mismatch")


def _route(logical_path: str) -> str:
    parts = PurePosixPath(logical_path).parts
    if len(parts) < 2 or parts[0] not in {"base", "pack"} or any(p in {".", ".."} for p in parts):
        raise RenderError("invalid projection path")
    if any(any(ord(c) < 32 or c in "\\?#" for c in part) for part in parts):
        raise RenderError("invalid projection path")
    return "r/" + quote(logical_path, safe="/-._~")


def _svg_route(logical_path: str) -> str:
    parts = PurePosixPath(logical_path).parts
    if not logical_path.endswith(".md.html"):
        raise RenderError("invalid declaration path")
    raw = parts[0] + "/.private_resources/diagrams/" + "/".join(parts[1:])[:-8] + ".svg"
    return "r/" + quote(raw, safe="/-._~")


def _companion_route(logical_path: str) -> str:
    return _svg_route(logical_path)[:-4] + ".md"


def _reference_route(reference: object) -> str:
    """A generated route whose identity includes the selected target and field."""
    identity = (reference.kind, reference.target_path, reference.target_sha256,
                reference.attributes)
    digest = sha256(repr(identity).encode()).hexdigest()[:24]
    owner = reference.owner_path.split('/', 1)[0]
    return f'r/{owner}/.private_resources/diagrams/contracts/{digest}.md.html'


def _reference_pages(projection: object) -> dict[str, bytes]:
    """Render only bounded, frozen schema and configuration declarations."""
    resources = {item.logical_path: item for item in projection.resources}
    pages: dict[str, bytes] = {}
    for reference in projection.references:
        attrs = dict(reference.attributes)
        is_schema = reference.kind == 'functions' and attrs.get('field') in ('Input', 'Output')
        is_timeout = reference.kind in ('timeout', 'timeouts') and bool(attrs.get('key') or attrs.get('Key'))
        is_pass_limit = reference.kind == 'pass_limit' and bool(attrs.get('key'))
        if not (is_schema or is_timeout or is_pass_limit):
            continue
        resource = resources.get(reference.target_path)
        if resource is None or resource.sha256 != reference.target_sha256:
            raise RenderError('reference differs from captured resource')
        if len(resource.content_bytes) > _MAX_REFERENCE:
            raise RenderError('reference exceeds display limit')
        try:
            if is_schema:
                if not reference.target_path.endswith('.json'):
                    continue
                value = json.loads(resource.content_bytes)
                body = json.dumps(value, indent=2, ensure_ascii=False)
                title = attrs['field'] + ' schema'
            else:
                if not reference.target_path.endswith('.toml'):
                    continue
                source = tomllib.loads(resource.content_bytes.decode('utf-8'))
                key = attrs.get('key') or attrs['Key']
                field = source.get('fields', {}).get(key)
                if not isinstance(field, dict):
                    raise RenderError('referenced configuration field is absent')
                body = '\n'.join(f'{name} = {json.dumps(value, ensure_ascii=False)}'
                                 for name, value in sorted(field.items()))
                title = ('Pass limit ' if is_pass_limit else 'Timeout ') + key
        except (UnicodeError, ValueError, TypeError) as exc:
            raise RenderError('invalid captured reference') from exc
        if len(body.encode()) > _MAX_REFERENCE:
            raise RenderError('reference display exceeds limit')
        route = _reference_route(reference)
        escaped = html.escape(body, quote=True)
        heading = html.escape(title, quote=True)
        page = (f'<!doctype html><html lang="en"><meta charset="utf-8">'
                        f'<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;">'
                        f'<title>{heading}</title><h1>{heading}</h1><pre>{escaped}</pre></html>').encode()
        pages[route] = (b'<!-- api-agent-reference-v1 sha256:' + sha256(page).hexdigest().encode() +
                        b' -->\n' + page)
    return pages


def _label(value: object) -> str:
    text = str(value)
    if len(text) > _MAX_LABEL or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise RenderError("invalid diagram label")
    # Numeric references keep PlantUML's Creole and link syntax inert while
    # rendering the admitted text exactly, including quotes and backslashes.
    return ''.join(char if char.isascii() and (char.isalnum() or char == ' ')
                   else f'&#{ord(char)};' for char in text)


def _markdown_id(value: object) -> str:
    text = str(value)
    if re.fullmatch(r'[a-z][a-z0-9_]{0,63}', text) is None:
        raise RenderError('invalid diagram navigation ID')
    return text


def _projection_source(projection: object, document: object, *, repositories: Mapping[str, RepositoryIdentity] | None = None) -> tuple[bytes, frozenset[str]]:
    all_steps = {step.id: step for step in projection.steps}
    if document.logical_path == projection.entry_path:
        owner = "GRAPH"
        steps = [step for step in projection.steps if step.parent is None]
        node_detail = False
    else:
        matches = [step for step in projection.steps
                   if step.declaration_path == document.logical_path
                   and (not hasattr(document, 'stable_id') or step.id == document.stable_id)]
        if len(matches) != 1:
            raise RenderError("unadmitted diagram document scope")
        selected = matches[0]
        owner = selected.id
        node_detail = selected.kind == "node"
        steps = [selected] if node_detail else [step for step in projection.steps if step.parent == owner]
    visible = {step.id for step in steps}
    edges = [] if node_detail else [edge for edge in projection.edges
        if edge.owner == owner or visible.intersection((edge.source, edge.target))]
    if len(steps) + len(edges) > _MAX_RECORDS:
        raise RenderError("diagram exceeds record limit")
    aliases = {step.id: "n" + str(index) for index, step in enumerate(steps)}
    admitted = {_route(item.logical_path) for item in projection.documents}
    links: set[str] = set()
    lines = ["@startuml", "top to bottom direction", "skinparam shadowing false",
             "skinparam defaultFontName sans-serif", "skinparam svgLinkTarget _top"]
    children = {}
    for step in projection.steps:
        children.setdefault(step.parent, []).append(step)

    def step_label(step):
        if step.kind != 'component':
            return _label(step.id)
        count = 0
        for candidate in projection.steps:
            if candidate.kind != 'node':
                continue
            cursor = candidate.parent
            while cursor is not None and cursor != step.id:
                cursor = all_steps[cursor].parent
            count += cursor == step.id
        if count == 1:
            return _label(step.id)
        return _label(f'{step.id} · {count} registered nodes')

    def step_href(step):
        target = _route(step.declaration_path)
        if target not in admitted:
            raise RenderError("unadmitted diagram link")
        href = posixpath.relpath(target, posixpath.dirname(_svg_route(document.logical_path)))
        if repositories:
            root_owner, relative = step.declaration_path.split("/", 1)
            if root_owner not in repositories:
                raise RenderError("missing workflow repository identity")
            href = repositories[root_owner].link(relative)
        fragment = getattr(step, 'declaration_fragment', '')
        if fragment:
            href += '#' + quote(fragment, safe='-._~')
        links.add(href)
        return href

    reference_pages = _reference_pages(projection)
    controllers = {item.component_id: item for item in getattr(projection, 'controllers', ())}

    def reference_href(reference):
        target = _reference_route(reference)
        # Both callers select only pages in this frozen, local map.
        href = posixpath.relpath(target, posixpath.dirname(_svg_route(document.logical_path)))
        if repositories:
            root_owner, relative = target.removeprefix('r/').split('/', 1)
            if root_owner not in repositories:
                raise RenderError('missing workflow repository identity')
            href = repositories[root_owner].link(relative)
        links.add(href)
        return href

    reference_index = 0
    for step in steps:
        schema_refs = [reference for reference in projection.references
                       if reference.owner_id == step.id and reference.kind == 'functions'
                       and _reference_route(reference) in reference_pages]
        schema_refs.sort(key=lambda item: ('Input', 'Output').index(dict(item.attributes)['field']))
        lines.append(f'rectangle "{step_label(step)}" as {aliases[step.id]} [[{step_href(step)}]]')
        prior_schema_alias = None
        for reference in schema_refs:
            field = dict(reference.attributes)['field']
            name = PurePosixPath(reference.target_path).name
            alias = f'ref{reference_index}'
            reference_index += 1
            lines.append(f'rectangle "{_label(field)}: {_label(name)}" as {alias} [[{reference_href(reference)}]]')
            lines.append(f'{aliases[step.id]} .. {alias}')
            if prior_schema_alias is not None:
                lines.append(f'{prior_schema_alias} -[hidden]down-> {alias}')
            prior_schema_alias = alias
    scope_ids = set(visible)
    cursor = owner if owner != 'GRAPH' else None
    while cursor in all_steps:
        scope_ids.add(cursor)
        cursor = all_steps[cursor].parent
    relevant_timeouts = [ref for ref in projection.references if ref.kind in ('timeout', 'timeouts')
                         and _reference_route(ref) in reference_pages
                         and (ref.owner_id in scope_ids or ref.owner_id is None)]
    for reference in relevant_timeouts:
        attrs = dict(reference.attributes)
        key = attrs.get('key') or attrs['Key']
        alias = f'timeout{reference_index}'
        reference_index += 1
        lines.append(f'rectangle "Timeout: {_label(key)}" as {alias} [[{reference_href(reference)}]]')
        if reference.owner_id in aliases:
            lines.append(f'{aliases[reference.owner_id]} .. {alias}')

    if owner in controllers and controllers[owner].max_passes_key:
        pass_refs = [ref for ref in projection.references
                     if ref.kind == 'pass_limit' and ref.owner_id == owner
                     and _reference_route(ref) in reference_pages]
        if (len(pass_refs) != 1 or
                dict(pass_refs[0].attributes).get('key') != controllers[owner].max_passes_key):
            raise RenderError('component pass limit differs from captured configuration')
        alias = f'passlimit{reference_index}'
        reference_index += 1
        label = _label(controllers[owner].max_passes_key)
        lines.append(f'rectangle "Configuration: {label}" as {alias} [[{reference_href(pass_refs[0])}]]')
        if controllers[owner].guard in aliases:
            lines.append(f'{aliases[controllers[owner].guard]} .. {alias}')

    boundaries = {}
    specials = set()

    def endpoint(identifier, edge_owner):
        if identifier in visible:
            return aliases[identifier], False
        if identifier in ("START", "END") and edge_owner == owner:
            if identifier not in specials:
                lines.append(f'circle "{identifier}" as {identifier.lower()}')
                specials.add(identifier)
            return identifier.lower(), False
        step = all_steps.get(identifier)
        key = (edge_owner if step is None else step.parent or "GRAPH", identifier)
        if key not in boundaries:
            alias = "boundary" + str(len(boundaries))
            label = "Boundary: " + (step_label(step) if step is not None else _label(identifier))
            label += " · scope " + _label(key[0])
            link = f" [[{step_href(step)}]]" if step is not None else ""
            lines.append(f'rectangle "{label}" as {alias}{link}')
            boundaries[key] = alias
        return boundaries[key], True

    active_controller = controllers.get(owner)
    for edge in edges:
        if any(identifier not in all_steps and identifier not in ("START", "END")
               for identifier in (edge.source, edge.target)):
            continue
        source, source_boundary = endpoint(edge.source, edge.owner)
        target, target_boundary = endpoint(edge.target, edge.owner)
        boundary = edge.owner != owner or source_boundary or target_boundary
        from .predicates import edge_label
        label = _label(edge_label(edge.condition or 'route'))
        if (active_controller is not None and active_controller.accepted
                and edge.owner == owner and edge.source == active_controller.guard
                and edge.target == 'END'):
            label = _label('.accepted')
        if boundary:
            label = "boundary (" + _label(edge.owner) + "): " + label
        arrow = "..>" if boundary else "-->"
        lines.append(f'{source} {arrow} {target} : {label}')
    if active_controller is not None and active_controller.accepted:
        from .predicates import component_decision
        guard_alias = aliases.get(active_controller.guard)
        first_child = children.get(owner, [None])[0]
        if guard_alias is None or first_child is None:
            raise RenderError('component controller differs from selected steps')
        if component_decision(active_controller.__dict__, True, 1) != 'accepted':
            raise RenderError('component acceptance metadata differs from runtime')
        if active_controller.retry:
            if component_decision(active_controller.__dict__, False, 1) != 'retry':
                raise RenderError('component retry metadata differs from runtime')
            limit = active_controller.max_passes_key or str(active_controller.max_passes)
            retry_label = _label(f'not .accepted and attempt < {limit}')
            lines.append(f'{guard_alias} ..> {aliases[first_child.id]} : {retry_label}')
        if active_controller.exhaustion:
            if component_decision(active_controller.__dict__, False,
                                  active_controller.max_passes) != 'exhausted':
                raise RenderError('component exhaustion metadata differs from runtime')
            lines.append('circle "EXHAUSTED" as exhausted')
            limit = active_controller.max_passes_key or str(active_controller.max_passes)
            exhaustion_label = _label(f'not .accepted and attempt == {limit}')
            lines.append(f'{guard_alias} ..> exhausted : {exhaustion_label}')
    if len(steps) + len(edges) + len(boundaries) > _MAX_RECORDS:
        raise RenderError("diagram exceeds record limit")
    lines.append("@enduml")
    data = ("\n".join(lines) + "\n").encode()
    if len(data) > _MAX_INPUT:
        raise RenderError("diagram exceeds input limit")
    return data, frozenset(links)


def _navigation(projection: object, document: object) -> bytes:
    source = _route(document.logical_path)
    svg = _svg_route(document.logical_path)
    relative_svg = posixpath.relpath(svg, posixpath.dirname(source))
    rows = [f"![Workflow diagram]({relative_svg})", f"[Open standalone diagram]({relative_svg})"]
    owner = PurePosixPath(document.logical_path).parts[0]
    for step in projection.steps:
        if step.declaration_path == document.logical_path and getattr(step, 'declaration_fragment', ''):
            rows.append(f"- [{_markdown_id(step.id)}](#{quote(step.declaration_fragment, safe='-._~')})")
    for target in projection.documents:
        if target.logical_path == document.logical_path:
            continue
        if PurePosixPath(target.logical_path).parts[0] != owner:
            continue
        relative = posixpath.relpath(_route(target.logical_path), posixpath.dirname(source))
        rows.append(f"- [{_markdown_id(target.stable_id)}]({relative})")
    body = "\n\n".join(rows[:2])
    if len(rows) > 2:
        body += "\n\n" + "\n".join(rows[2:])
    return body.encode()


def _hosted_declaration(projection: object, document: object) -> bytes:
    """Rewrite only selected Flow links identified by admitted ID, owner and line."""
    from .document_format import extract_document

    extracted = extract_document(document.raw_bytes, path=Path(document.logical_path))
    lines = document.executable_bytes.splitlines(keepends=True)
    for step in projection.steps:
        if step.owner_path != document.logical_path:
            continue
        try:
            offset = extracted.line_map.index(step.flow_line)
        except ValueError as exc:
            raise RenderError("selected Flow line absent from admitted declaration") from exc
        original = lines[offset]
        match = re.fullmatch(rb'(\s*\d{1,2}\.\s+\[([^\]]+)\]\()([^\r\n)]*)(\)\s*\r?\n?)', original)
        if match is None or step.id.encode() not in match[2].split():
            raise RenderError("selected Flow link differs from admitted stable ID")
        target = _route(step.declaration_path)
        href = posixpath.relpath(target, posixpath.dirname(_route(document.logical_path)))
        fragment = getattr(step, 'declaration_fragment', '')
        if fragment:
            href += '#' + quote(fragment, safe='-._~')
        lines[offset] = match[1] + href.encode() + match[4]
    return _annotate_hosted_resources(projection, document, lines)


def _annotate_hosted_resources(projection: object, document: object,
                               lines: list[bytes]) -> bytes:
    """Describe captured, unserved resources without creating broken host links."""
    selected = {item.logical_path for item in projection.documents}
    resources = {item.logical_path: item for item in projection.resources
                 if item.logical_path not in selected}
    folder = posixpath.dirname(document.logical_path)
    data = b"".join(lines)
    fence: tuple[bytes, int] | None = None
    protected: list[tuple[int, int]] = []
    offset = 0
    for line in lines:
        marker = re.match(rb"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = (run[:1], len(run))
            elif run[:1] == fence[0] and len(run) >= fence[1]:
                fence = None
        if marker or fence is not None or line.startswith((b"    ", b"\t")):
            protected.append((offset, offset + len(line)))
        offset += len(line)
    for comment in re.finditer(rb"<!--[\s\S]*?-->", data):
        protected.append(comment.span())
    mask = bytearray(len(data))
    for start, end in protected:
        mask[start:end] = b"\1" * (end - start)
    def is_protected(position: int) -> bool:
        return bool(mask[position])
    ticks = list(re.finditer(rb"`+", data))
    by_length: dict[int, list[int]] = {}
    for position, run in enumerate(ticks):
        by_length.setdefault(len(run[0]), []).append(position)
    index = 0
    while index < len(ticks):
        opening = ticks[index]
        if is_protected(opening.start()):
            index += 1
            continue
        candidates = by_length[len(opening[0])]
        next_position = bisect_right(candidates, index)
        while next_position < len(candidates) and is_protected(ticks[candidates[next_position]].start()):
            next_position += 1
        if next_position == len(candidates):
            index += 1  # An unmatched delimiter is literal Markdown.
            continue
        closing_index = candidates[next_position]
        end = ticks[closing_index].end()
        mask[opening.start():end] = b"\1" * (end - opening.start())
        index = closing_index + 1
    def replace_link(match: re.Match[bytes]) -> bytes:
        if is_protected(match.start()) or (match.start() and data[match.start() - 1:match.start()] == b"\\"):
            return match[0]
        href = match[2]
        try:
            uri = href.decode("utf-8")
            parsed = urlsplit(uri)
            if parsed.scheme or parsed.netloc or parsed.query or not parsed.path:
                return match[0]
            path = unquote(parsed.path, errors="strict")
            if "\\" in path or any(ord(char) < 32 for char in path):
                return match[0]
            logical = posixpath.normpath(posixpath.join(folder, path))
            if not logical.startswith(document.logical_path.split("/", 1)[0] + "/"):
                return match[0]
            resource = resources.get(logical)
        except (UnicodeDecodeError, ValueError):
            return match[0]
        if resource is None:
            return match[0]
        label = match[1]
        detail = (f" (captured {resource.kind}: {logical}; SHA-256 "
                  f"{resource.sha256}; unavailable in hosted view)").encode()
        return label + detail
    return re.sub(rb"(?<!!)\[([^\]\r\n]+)\]\(([^)\r\n]+)\)", replace_link, data)


def _companion(projection: object, document: object, *, hosted_selected: bool = False) -> bytes:
    route = _companion_route(document.logical_path)
    folder = posixpath.dirname(route)
    svg = posixpath.relpath(_svg_route(document.logical_path), folder)
    declaration = posixpath.relpath(_route(document.logical_path), folder)
    lines = [f"# Workflow diagram: {_markdown_id(document.stable_id)}", "",
             f"![Workflow diagram]({svg})", "",
             f"[Open standalone diagram]({svg})", "",
             f"[Source declaration]({declaration})"]
    owner = PurePosixPath(document.logical_path).parts[0]
    peers = [item for item in projection.documents
             if item.logical_path != document.logical_path and
             (hosted_selected or PurePosixPath(item.logical_path).parts[0] == owner)]
    if peers:
        lines.extend(("", "## Navigation", ""))
        for item in peers:
            target = posixpath.relpath(_companion_route(item.logical_path), folder)
            lines.append(f"- [{_markdown_id(item.stable_id)}]({target})")
    body = ("\n".join(lines) + "\n").encode()
    marker = b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode() + b" -->\n"
    return marker + body


def _asset_root() -> Path:
    module = Path(__file__).resolve()
    package = module.parents[1]
    installed = package / ".private_resources/viewer"
    if (installed / "viewer.js").is_file():
        return installed
    source_root = module.parents[3]
    source = source_root / ".private_resources/viewer"
    dist_markers = tuple(package.parent.glob("api_agent-*.dist-info"))
    if (package.parent.name == "src" and not dist_markers and
            (source_root / "pyproject.toml").is_file() and
            module == source_root / "src/api_agent/workflows/viewer_render.py" and
            (source / "viewer.js").is_file()):
        return source
    raise RenderError("workflow viewer assets missing")


def default_renderer() -> "PlantUMLRenderer":
    path = os.environ.get("API_AGENT_PLANTUML_JAR")
    if path:
        jar = Path(path)
    else:
        jar = Path.home() / ".cache/api-agent/plantuml/plantuml-mit-1.2026.9.jar"
    if not jar.is_file():
        raise RenderError("PlantUML renderer missing; run the workflow viewer preparation command")
    return PlantUMLRenderer(jar)


def _read_regular_jar(path: Path) -> bytes:
    """Read bounded bytes from the opened regular inode, never a FIFO or link."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RenderError("renderer artifact is not an accessible regular file") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_JAR:
            raise RenderError("renderer artifact must be a bounded regular file")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(_MAX_JAR + 1)
        if len(data) > _MAX_JAR or len(data) != info.st_size:
            raise RenderError("renderer artifact changed or exceeds size limit")
        return data


def _drain(process: subprocess.Popen[bytes], *, timeout: float) -> tuple[bytes, bytes]:
    """Drain both pipes with one wall deadline and a finite combined byte cap."""
    deadline = time.monotonic() + timeout
    buffers: dict[object, bytearray] = {}
    selector = selectors.DefaultSelector()
    for pipe in (process.stdout, process.stderr):
        assert pipe is not None
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)
        buffers[pipe] = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.fileobj].extend(chunk)
                if sum(map(len, buffers.values())) > 65536:
                    raise RenderError("renderer diagnostics exceeded limit")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                break
            time.sleep(min(0.01, remaining))
        # Keep the exited leader unreaped until its entire process group has
        # been stopped, including descendants that closed our pipes early.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException:
        if process.returncode is None:
            try:
                # WNOWAIT keeps an exited leader's PID reserved while its
                # descendants may still hold the renderer pipes open.
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        process.wait()
        raise
    finally:
        selector.close()
        for pipe in buffers:
            pipe.close()
    if process.returncode:
        raise RenderError("renderer failed")
    return bytes(buffers[process.stdout]), bytes(buffers[process.stderr])


def validate_svg(data: bytes, *, allowed_links: frozenset[str] = frozenset()) -> bytes:
    if len(data) > _MAX_OUTPUT or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise RenderError("unsafe SVG")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RenderError("invalid SVG") from exc
    if root.tag != "{" + _SVG_NS + "}svg":
        raise RenderError("invalid SVG root")
    def finite_dimension(value: str) -> float:
        if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?(?:px)?", value):
            raise RenderError("invalid SVG dimension")
        number = float(value.removesuffix("px"))
        if not math.isfinite(number) or number <= 0 or number > 16384:
            raise RenderError("SVG dimension exceeds limit")
        return number

    def safe_style(value: str) -> bool:
        for declaration in value.split(";"):
            if not declaration.strip():
                continue
            key, separator, raw = declaration.partition(":")
            if not separator:
                return False
            key, raw = key.strip().lower(), raw.strip()
            if key in {"width", "height", "stroke-width", "stroke-miterlimit", "font-size"}:
                try:
                    finite_dimension(raw)
                except RenderError:
                    return False
            elif key in {"fill", "stroke", "background"}:
                if not (re.fullmatch(r"#[0-9a-fA-F]{3,8}", raw) or raw.lower() in _STYLE_COLORS):
                    return False
            elif key in {"fill-opacity", "stroke-opacity", "opacity"}:
                if not re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", raw):
                    return False
            elif key == "stroke-linejoin":
                if raw not in {"miter", "round", "bevel"}:
                    return False
            elif key == "stroke-linecap":
                if raw not in {"butt", "round", "square"}:
                    return False
            elif key == "stroke-dasharray":
                if raw != "7,7":
                    return False
            elif key == "font-weight":
                if raw not in {"normal", "bold"}:
                    return False
            elif key == "text-anchor":
                if raw not in {"start", "middle", "end"}:
                    return False
            else:
                return False
        return True

    def bounded_numbers(value: str, allowed: str = "") -> list[float]:
        numbers = _SVG_NUMBER.findall(value)
        residue = _SVG_NUMBER.sub("", value)
        if (not numbers or any(char not in allowed for char in residue) or
                any(not math.isfinite(float(number)) or abs(float(number)) > 16384
                    for number in numbers)):
            raise RenderError("invalid SVG geometry")
        return [float(number) for number in numbers]

    def safe_transform(value: str) -> None:
        parts = re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^()]*)\)", value)
        residue = re.sub(r"(?:matrix|translate|scale|rotate|skewX|skewY)\s*\([^()]*\)", "", value)
        if not parts or residue.strip(" ,\t\r\n"):
            raise RenderError("invalid SVG transform")
        for operation, arguments in parts:
            numbers = bounded_numbers(arguments, " ,\t\r\n")
            if operation in {"scale", "matrix"} and any(abs(number) > 100 for number in numbers):
                raise RenderError("SVG transform exceeds limit")

    width = finite_dimension(root.attrib.get("width", ""))
    height = finite_dimension(root.attrib.get("height", ""))
    if width * height > 50_000_000:
        raise RenderError("SVG area exceeds limit")
    viewbox = root.attrib.get("viewBox", "").split()
    if len(viewbox) != 4 or any(not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", part)
                                for part in viewbox):
        raise RenderError("invalid SVG viewBox")
    box = [float(part) for part in viewbox]
    if not all(math.isfinite(value) for value in box) or any(abs(value) > 16384 for value in box) or box[2] <= 0 or box[3] <= 0 or box[2] * box[3] > 50_000_000:
        raise RenderError("SVG viewBox exceeds limit")
    root_style = root.attrib.get("style", "")
    for key, value in re.findall(r"(?:^|;)\s*(width|height)\s*:\s*([^;]+)", root_style, flags=re.I):
        key = key.lower()
        if finite_dimension(value.strip()) != (width if key == "width" else height):
            raise RenderError("SVG CSS dimensions disagree")
    ids: set[str] = set()
    refs: list[str] = []
    for element in root.iter():
        tag = element.tag.removeprefix("{" + _SVG_NS + "}")
        if tag not in _ALLOWED_TAGS:
            raise RenderError("unsafe SVG element")
        for key, value in element.attrib.items():
            attr = key.removeprefix("{" + _XLINK_NS + "}").removeprefix("{" + _SVG_NS + "}")
            if attr not in _ALLOWED_ATTRS or attr.startswith("on"):
                raise RenderError("unsafe SVG attribute")
            if attr == "id":
                ids.add(value)
            elif attr in {"width", "height"} and value.endswith("px"):
                bounded_numbers(value[:-2])
            elif attr in _SVG_SCALARS:
                bounded_numbers(value)
            elif attr in {"d", "points", "stroke-dasharray"}:
                bounded_numbers(value, " ,\t\r\nMmLlHhVvCcSsQqTtAaZz" if attr == "d" else " ,\t\r\n")
            elif attr == "transform":
                safe_transform(value)
            elif attr == "href":
                if value.startswith("#"):
                    refs.append(value[1:])
                elif value not in allowed_links:
                    raise RenderError("unadmitted SVG link")
            elif attr == "style" and not safe_style(value):
                raise RenderError("unsafe SVG style")
            elif "url(" in value.lower() or "javascript:" in value.lower() or "data:" in value.lower():
                raise RenderError("unsafe SVG resource")
    if any(ref not in ids for ref in refs):
        raise RenderError("broken SVG fragment")
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    # ElementTree drops PlantUML's encoded source PI and non-graphic comments.
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _with_font_fallbacks(graphic: bytes) -> bytes:
    """Keep generated labels readable with installed platform fonts."""
    root = ET.fromstring(graphic)
    for element in root.iter():
        if element.get("font-family") == "sans-serif":
            element.set("font-family", _SVG_FONT_STACK)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@dataclass(frozen=True)
class PlantUMLRenderer:
    jar: Path
    java: str = "java"
    timeout: float = 20.0

    def render(self, source: bytes, *, allowed_links: frozenset[str] = frozenset()) -> bytes:
        from api_agent.providers.progress import report_timeout_sync

        if len(source) > _MAX_INPUT or self.timeout <= 0 or self.timeout > 120:
            raise RenderError("invalid renderer limits")
        with report_timeout_sync("Workflow diagram rendering", self.timeout) as budget:
            jar_bytes = _read_regular_jar(self.jar)
            if sha256(jar_bytes).hexdigest() != PLANTUML_SHA256:
                raise RenderError("PlantUML artifact digest mismatch")
            with tempfile.TemporaryDirectory(prefix="api-agent-diagram-") as directory:
                puml = Path(directory) / "diagram.puml"
                puml.write_bytes(source)
                jar = Path(directory) / "renderer.jar"
                jar.write_bytes(jar_bytes)
                args = [self.java, "-Xmx256m", "-DPLANTUML_SECURITY_PROFILE=SANDBOX", "-jar", str(jar), *RENDER_OPTIONS, str(puml)]
                try:
                    process = subprocess.Popen(args, cwd=directory, env={"PATH": os.environ.get("PATH", ""), "HOME": directory, "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                except FileNotFoundError as exc:
                    raise RenderError("Java 17 or newer is required for workflow view generation") from exc
                try:
                    _drain(process, timeout=budget.remaining or 0.001)
                except subprocess.TimeoutExpired as exc:
                    raise RenderError("renderer timed out") from exc
                output = Path(directory) / "diagram.svg"
                if not output.is_file() or output.stat().st_size > _MAX_OUTPUT:
                    raise RenderError("renderer output missing or oversized")
                safe_graphic = validate_svg(output.read_bytes(), allowed_links=allowed_links)
                return _with_font_fallbacks(safe_graphic)


def _physical_file(route: str, roots: Mapping[str, Path]) -> Path:
    if re.search(r"%(?:2f|5c)", route, re.I):
        raise RenderError("encoded separator in viewer route")
    decoded = unquote(route, errors="strict")
    if quote(decoded, safe="/-._~") != route:
        raise RenderError("noncanonical viewer route")
    parts = PurePosixPath(decoded).parts
    if len(parts) < 3 or parts[0] != "r" or parts[1] not in roots or any(p in {".", ".."} for p in parts):
        raise RenderError("invalid viewer route")
    base = Path(roots[parts[1]]).resolve()
    result = base.joinpath(*parts[2:])
    if any(parent.is_symlink() for parent in (result, *result.parents) if parent != base and parent.is_relative_to(base)):
        raise RenderError("viewer asset symlink rejected")
    if not result.resolve().is_relative_to(base):
        raise RenderError("viewer route escapes owner")
    return result


def check_view_bundle(bundle: ViewBundle, *, roots: Mapping[str, Path],
                      check_orphans: bool = True) -> None:
    """Compare owned presentation bytes with the admitted markup projection."""
    expected: dict[str, set[Path]] = {}
    for file in bundle.files:
        path = _physical_file(file.path, roots)
        expected.setdefault(PurePosixPath(file.path).parts[1], set()).add(path)
        if _read_view_file(path) != file.body:
            raise RenderError(f"missing or stale workflow view: {file.path}; regenerate workflow views")
    for owner, paths in expected.items() if check_orphans else ():
        diagram_dir = Path(roots[owner]).resolve() / ".private_resources/diagrams"
        if diagram_dir.exists():
            for pattern in ("*.svg", "*.md", "contracts/*.md.html"):
                for artifact in diagram_dir.rglob(pattern):
                    if artifact not in paths:
                        raise RenderError("orphaned workflow diagram; regenerate workflow views")


def _read_view_file(path: Path) -> bytes | None:
    """Read a bounded regular view inode without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RenderError("workflow view file is not accessible") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            raise RenderError("workflow view file must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise RenderError("workflow view file changed during read")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _saved_graphic(path: Path, *, metadata: dict[str, object],
                   allowed_links: frozenset[str], script: bytes = b"") -> bytes:
    data = _read_view_file(path)
    if data is None or len(data) > _MAX_OUTPUT + (4 * _MAX_INPUT // 3) + 4096:
        raise RenderError("missing or stale workflow diagram; regenerate workflow views")
    match = re.fullmatch(rb"(<\?xml [^\n]+\?>\n)<!-- api-agent-view-v1 (\{[^\n]+\}) -->\n([\s\S]+)", data)
    if match is None:
        raise RenderError("invalid workflow diagram provenance")
    try:
        actual = json.loads(match[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise RenderError("invalid workflow diagram provenance") from exc
    graphic = match[1] + match[3]
    if (actual.get("graphic_sha256") != sha256(graphic).hexdigest() or
            actual != {**metadata, "graphic_sha256": sha256(graphic).hexdigest()}):
        raise RenderError("stale workflow diagram provenance; regenerate workflow views")
    raw_graphic = _strip_navigation(graphic, script)
    canonical_raw = validate_svg(raw_graphic, allowed_links=allowed_links)
    if canonical_raw != raw_graphic and not (script and canonical_raw.endswith(b" />") and raw_graphic == canonical_raw[:-3] + b"></svg>"):
        raise RenderError("noncanonical workflow diagram graphic")
    return data


def build_view_bundle(projection: object, *, roots: Mapping[str, Path],
                      renderer: PlantUMLRenderer | None = None, verify: bool = True,
                      hosted_selected: bool | None = None, check_orphans: bool = False,
                      repositories: Mapping[str, RepositoryIdentity] | None = None,
                      repository_sources: tuple[ViewSource, ...] = ()) -> ViewBundle:
    from api_agent.providers.progress import report_timeout_sync

    with report_timeout_sync("Workflow view bundle", 120) as budget:
        selected = not verify if hosted_selected is None else hosted_selected
        if selected and verify:
            raise RenderError("hosted selected views cannot use source-owned static verification")
        return _build_view_bundle(projection, roots=roots, renderer=renderer,
                                  verify=verify, hosted_selected=selected, budget=budget,
                                  check_orphans=check_orphans,
                                  repositories=repositories, repository_sources=repository_sources)


def _build_view_bundle(projection: object, *, roots: Mapping[str, Path],
                       renderer: PlantUMLRenderer | None, verify: bool,
                       hosted_selected: bool, budget: object, check_orphans: bool = False,
                       repositories: Mapping[str, RepositoryIdentity] | None = None,
                       repository_sources: tuple[ViewSource, ...] = ()) -> ViewBundle:
    """Generate a finite immutable bundle from one admitted projection."""
    if projection.version != 1 or len(projection.documents) > _MAX_RECORDS:
        raise RenderError("unsupported projection")
    from .document_format import wrap_document

    if not verify:
        renderer = renderer or default_renderer()
    files: list[ViewFile] = []
    semantic = projection.projection_sha256
    asset_root = _asset_root()
    owners = {PurePosixPath(d.logical_path).parts[0] for d in projection.documents}
    reference_pages = _reference_pages(projection)
    if repositories:
        repositories = {owner: identity for owner, identity in repositories.items()
                        if owner in owners}
    asset_names = ("viewer.js", "viewer.css", "marked.js", "dompurify.js",
                   "marked-LICENSE.txt", "dompurify-LICENSE.txt", "PlantUML-NOTICE.txt",
                   "VIEWER-ASSET-NOTICE.txt", "heading-id.js", "svg-links.js",
                   "heading-id-LICENSE.txt", "github-slugger-LICENSE.txt")
    asset_bodies = {}
    for name in asset_names:
        body = _read_view_file(asset_root / name)
        if body is None:
            source = Path(__file__).resolve().parents[3] / ".private_resources/viewer"
            guidance = ("run npm ci from the repository root to prepare offline frontend assets"
                        if asset_root == source else
                        "reinstall api-agent from a complete distribution; installed viewing requires no npm")
            raise RenderError("workflow viewer dependencies missing; " + guidance)
        asset_bodies[name] = body
    asset_digest = sha256(b"".join(asset_bodies.values())).hexdigest()
    for owner in sorted(owners):
        for name in asset_names:
            body = asset_bodies[name]
            suffix = name.rsplit(".", 1)[-1]
            content_type = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
                            "txt": "text/plain; charset=utf-8"}[suffix]
            files.append(ViewFile(f"r/{owner}/.private_resources/viewer/{name}", content_type, body))
    for route, body in sorted(reference_pages.items()):
        files.append(ViewFile(route, 'text/html; charset=utf-8', body))
    for document in projection.documents:
        if budget.remaining is not None and budget.remaining <= 0:
            raise RenderError("workflow view generation timed out")
        source, links = _projection_source(projection, document, repositories=repositories)
        script_roots = ({owner: Path("/r") / owner for owner in roots} if hosted_selected else roots)
        script = _navigation_script(document.logical_path, script_roots, repositories)
        svg_path = _svg_route(document.logical_path)
        metadata = {"version": 1, "semantic": semantic, "document": document.sha256,
                    "puml": sha256(source).hexdigest(),
                    "puml_b64": base64.b64encode(source).decode("ascii"),
                    "plantuml": PLANTUML_VERSION,
                    "jar_sha256": PLANTUML_SHA256, "options": list(RENDER_OPTIONS),
                    "assets_sha256": asset_digest}
        if repositories:
            metadata["repositories"] = {owner: {"repository_url": identity.repository_url, "path_prefix": identity.path_prefix}
                                         for owner, identity in sorted(repositories.items())}
        if verify:
            svg = _saved_graphic(_physical_file(svg_path, roots), metadata=metadata, allowed_links=links, script=script)
        else:
            assert renderer is not None
            graphic = renderer.render(source, allowed_links=links)
            if not graphic.startswith(b"<?xml ") or b"?>\n" not in graphic:
                raise RenderError("renderer SVG has no XML declaration")
            graphic = validate_svg(graphic, allowed_links=links)
            if script:
                if graphic.endswith(b" />"):
                    graphic = graphic[:-3] + b"></svg>"
                graphic = graphic.removesuffix(b"</svg>") + script + b"</svg>"
            generated_metadata = {**metadata, "graphic_sha256": sha256(graphic).hexdigest()}
            marker = b"<!-- api-agent-view-v1 " + json.dumps(generated_metadata, sort_keys=True, separators=(",", ":")).encode() + b" -->\n"
            declaration, separator, body = graphic.partition(b"?>\n")
            svg = declaration + separator + marker + body
        files.append(ViewFile(svg_path, "image/svg+xml", svg))
        files.append(ViewFile(_companion_route(document.logical_path), "text/markdown; charset=utf-8",
                              _companion(projection, document, hosted_selected=hosted_selected)))
        depth = len(PurePosixPath(document.logical_path).parts) - 2
        prefix = "../" * depth + ".private_resources/viewer/"
        executable = (_hosted_declaration(projection, document) if hosted_selected
                      else document.executable_bytes)
        presented = wrap_document(executable, root=document.logical_path == projection.entry_path,
                                  asset_prefix=prefix, generated=_navigation(projection, document))
        files.append(ViewFile(_route(document.logical_path), "text/html; charset=utf-8", presented))
    entry = _route(projection.entry_path)
    sources = [ViewSource(d.logical_path, "declaration", sha256(d.raw_bytes).hexdigest(),
                          len(d.raw_bytes), True) for d in projection.documents]
    selected_paths = {d.logical_path for d in projection.documents}
    for resource in projection.resources:
        if resource.logical_path not in selected_paths:
            sources.append(ViewSource(resource.logical_path, resource.kind, resource.sha256,
                                      len(resource.content_bytes), False))
    routes = tuple(ViewRoute(f.path, f.content_type, sha256(f.body).hexdigest(), len(f.body))
                   for f in files)
    bundle = ViewBundle(entry, tuple(files), projection.graph_id, projection.definition_id,
                        projection.projection_sha256, tuple(sources) + repository_sources, routes)
    verify_bundle_proof(bundle, projection, repository_sources=repository_sources)
    if verify:
        check_view_bundle(bundle, roots=roots, check_orphans=check_orphans)
    return bundle


def _navigation_script(logical_path: str, roots: Mapping[str, Path],
                       repositories: Mapping[str, RepositoryIdentity] | None) -> bytes:
    if not repositories:
        return b""
    svg = _physical_file(_svg_route(logical_path), roots)
    mapping = {}
    identities = {}
    for owner, identity in sorted(repositories.items()):
        if owner not in roots:
            raise RenderError("unadmitted workflow repository owner")
        root = Path(roots[owner]).resolve()
        prefix = identity.link_prefix
        if prefix in identities and identities[prefix] != root:
            raise RenderError("ambiguous workflow repository mapping")
        if any(prefix != other and (prefix.startswith(other) or other.startswith(prefix)) for other in identities):
            raise RenderError("overlapping workflow repository mapping")
        identities[prefix] = root
        mapping[prefix] = quote(Path(os.path.relpath(root, svg.parent)).as_posix().rstrip("/"), safe="/-._~") + "/"
    asset = posixpath.relpath(f"r/{logical_path.split('/')[0]}/.private_resources/viewer/svg-links.js", posixpath.dirname(_svg_route(logical_path)))
    encoded = html.escape(json.dumps(mapping, sort_keys=True, separators=(",", ":")), quote=True)
    return f'<script xmlns:xlink="http://www.w3.org/1999/xlink" data-workflow-links="v1" data-workflow-roots="{encoded}" href="{asset}" xlink:href="{asset}"></script>'.encode()


def _strip_navigation(graphic: bytes, script: bytes) -> bytes:
    if not script:
        return graphic
    suffix = script + b"</svg>"
    if not graphic.endswith(suffix) or graphic.count(script) != 1:
        raise RenderError("invalid workflow SVG navigation envelope")
    return graphic[:-len(suffix)] + b"</svg>"


def capture_repository_sources(roots: Mapping[str, Path]) -> tuple[Mapping[str, RepositoryIdentity], tuple[ViewSource, ...]]:
    """Capture recorded owner identities and trusted, unserved source evidence."""
    identities = {}
    sources = []
    seen = set()
    captured = {}
    for owner, root in sorted(roots.items()):
        physical = Path(root).resolve() / CONFIG_PATH
        if physical not in captured:
            captured[physical] = read_repository_identity(root)
        recorded = captured[physical]
        if recorded is None:
            continue
        identity, data = recorded
        identities[owner] = identity
        physical = Path(root).resolve() / CONFIG_PATH
        if physical not in seen:
            sources.append(ViewSource(owner + "/" + CONFIG_PATH, "repository", sha256(data).hexdigest(), len(data), False))
            seen.add(physical)
    return MappingProxyType(identities), tuple(sources)
