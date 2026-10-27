"""Pure, scoped diagnostic identities over admitted workflow snapshots.

Version one covers declarations and declared budgets, not arbitrary native or
provider configuration. Exact admission and physical provenance are separate.
"""
from collections.abc import Mapping
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import tomllib

from .definition import InlinePromptRef, WorkflowDefinition
from .layout import MAX_COMPONENT_PASSES


def _digest(domain, value):
    data = json.dumps([domain, value], sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')
    return sha256(data).hexdigest()


def validate_identity(value):
    """Validate and copy the fixed, content-free diagnostic descriptor."""
    if (type(value) is not dict or set(value) != {
            'version', 'declaration_sha256', 'settings_sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or any(type(value[key]) is not str or
                   re.fullmatch('[0-9a-f]{64}', value[key]) is None
                   for key in ('declaration_sha256', 'settings_sha256'))):
        raise ValueError('Invalid workflow semantic identity')
    return dict(value)


class _Snapshot:
    def __init__(self, definition):
        self.definition = definition
        self.resources = {r.logical_path: r for r in definition.resources}

    def logical(self, path):
        if isinstance(path, InlinePromptRef):
            # Inline prompt IDs are globally unique within an admitted graph.
            candidates = [r.logical_path for r in self.definition.resources
                          if r.kind == 'prompt' and r.logical_path.startswith('inline/')
                          and r.logical_path.endswith('/' + path.id)]
        else:
            candidates = [key + '/' + path.relative_to(root).as_posix()
                          for key, root in self.definition.roots.items()
                          if path.is_relative_to(root)]
            candidates = [p for p in candidates if p in self.resources]
        if len(candidates) != 1:
            raise ValueError('Missing or ambiguous admitted workflow resource')
        return candidates[0]

    def value(self, value):
        if isinstance(value, (Path, InlinePromptRef)):
            return self.logical(value)
        if isinstance(value, Mapping):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.value(item) for item in value]
        return value

    def row(self, row):
        return self.value({key: item for key, item in row.items()
                           if key not in ('line', '_owner')})

    def declaration(self):
        d = self.definition
        v2 = isinstance(d, WorkflowDefinition)
        if not v2 and type(d).__name__ != 'StatefulDefinition':
            raise TypeError('Expected an admitted workflow definition')
        tables = d.layout.tables
        timeout_rows = tables['Timeouts'] if v2 else d.timeouts
        components = tables['Components'] if v2 else ()
        selected = {}
        for row in timeout_rows:
            selected.setdefault(self.logical(row['Source']), set()).add(row['Key'])
        for row in components:
            if row.get('max_passes_key') is not None:
                selected.setdefault(self.logical(row['max_passes_link']), set()).add(row['max_passes_key'])
        opaque = set() if v2 else {r.logical_path for r in d.declared_resources.values()}
        if v2:
            for row in tables['Functions']:
                for column in ('Input', 'Output', 'Response schema'):
                    if column in row and row[column] != '-':
                        opaque.add(self.logical(row[column]))
            opaque.update(self.logical(row['File']) for row in tables['Prompts'])
            opaque.update(self.logical(step.source) for step in d.layout.steps if step.kind == 'node')
        resources = []
        for r in d.resources:
            if r.kind == 'declaration' and r.logical_path not in opaque:
                continue
            if r.kind == 'configuration' and r.logical_path in selected and r.logical_path not in opaque:
                parsed = tomllib.loads(r.content_bytes.decode('utf-8'))
                resources.append([r.kind, r.logical_path, parsed.get('version'),
                                  {key: parsed['fields'][key] for key in sorted(selected[r.logical_path])}])
            else:
                resources.append([r.kind, r.logical_path, sha256(r.content_bytes).hexdigest()])
        result = dict(version=2 if v2 else 3, graph_id=d.graph_id,
                      entry_root=d.entry_root, entry=self.logical(d.entry),
                      steps=[dict(id=s.id, kind=s.kind, parent=s.parent,
                                  source=self.logical(s.source)) for s in d.layout.steps],
                      resources=resources,
                      declarations={key: self.logical(row[2]) for key, row in
                                    (d.selected_declarations or {}).items()},
                      fragments=dict(d.declaration_fragments or {}),
                      replacements=[[r.target_id, r.root_id, Path(r.entry).as_posix()] for r in d.replacements],
                      tools=[[t.scope, t.port, self.logical(t.instance), t.scope_kind]
                             for t in d.tool_usages])
        if v2:
            # Linked pass labels contain presentation syntax; the admitted key
            # and logical source carry the binding instead.
            normalized = {}
            for name in ('Components', 'Functions', 'Bindings', 'Edges', 'Prompts', 'Timeouts'):
                normalized[name] = []
                for row in tables[name]:
                    row = dict(row)
                    if name == 'Bindings' and 'default_value' in row:
                        row.pop('Default')
                    if name == 'Components' and row.get('max_passes_key') is not None:
                        row.pop('Max passes')
                    normalized[name].append(self.row(row))
            result['tables'] = normalized
        else:
            result.update(state=[d.schema_name, self.logical(d.schema_source)],
                          nodes=[self.row(row) for row in d.nodes],
                          edges=[self.row(row) for row in d.edges],
                          helpers=self.value(d.helper_sources),
                          dependencies=self.value(d.native_dependencies),
                          declared_resources={key: r.logical_path for key, r in d.declared_resources.items()},
                          timeouts=[self.row(row) for row in d.timeouts])
        return result


def declaration_identity(definition):
    """Hash the ordered executable declaration without refreshing any source."""
    return _digest('workflow-declaration-v1', _Snapshot(definition).declaration())


def workflow_identity(definition, *, timeouts, component_passes):
    """Bind the declaration to complete, already resolved declared budgets."""
    v2 = isinstance(definition, WorkflowDefinition)
    timeout_rows = definition.layout.tables['Timeouts'] if v2 else definition.timeouts
    components = definition.layout.tables['Components'] if v2 else ()
    if (not isinstance(timeouts, Mapping) or set(timeouts) != {r['Owner'] for r in timeout_rows}
            or not isinstance(component_passes, Mapping)
            or set(component_passes) != {r['ID'] for r in components}):
        raise ValueError('Workflow budget owner coverage differs from declaration')
    for value in timeouts.values():
        try:
            valid = value is None or (type(value) in (int, float)
                                     and math.isfinite(value) and value > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('Workflow timeout must be finite positive seconds or None')
    for row in components:
        value = component_passes[row['ID']]
        if (type(value) is not int or not 1 <= value <= MAX_COMPONENT_PASSES
                or (row['Guard'] == '-' and value != 1)
                or (row.get('max_passes_key') is None and value != int(row['Max passes']))):
            raise ValueError('Invalid workflow component pass budget')
    declaration = declaration_identity(definition)
    return dict(version=1, declaration_sha256=declaration,
                settings_sha256=_digest('workflow-settings-v1', dict(
                    declaration_sha256=declaration, timeouts=dict(timeouts),
                    component_passes=dict(component_passes))))
