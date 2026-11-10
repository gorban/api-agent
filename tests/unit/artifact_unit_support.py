"""Shared fixtures for the compiled-artifact unit tests (in-process loader coverage)."""
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import pytest

from artifact_acceptance_support import QUALIFICATION, ROOT, build_wheel, install

from api_agent.workflows import artifact
from api_agent.workflows.artifact import _definition_id, canonical_json, compiled_plan_identity, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.model import Replacement, Resource

BINDING_PREFIXES = ('aq_', 'tb_')


@dataclass(frozen=True)
class Built:
    plan: bytes
    digest: str
    site: Path
    wheel: Path
    directory: Path


def qualification_wheel(directory):
    native = QUALIFICATION / 'src' / 'aq_bindings'
    return build_wheel(directory, 'aq-bindings', {'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
                                                  'aq_bindings/native.py': (native / 'native.py').read_bytes()})


def contrived_wheel(directory):
    return build_wheel(directory, 'aq-contrived', {'aq_contrived/__init__.py': b'',
                                                    'aq_contrived/functions.py':
                                                        (ROOT / 'demo/contrived/functions.py').read_bytes()})


def build_v3(directory):
    wheel = qualification_wheel(directory)
    plan = compile_plan(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION, pack_root=QUALIFICATION,
                        wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory)


def build_v2(directory):
    wheel = contrived_wheel(directory)
    plan = compile_plan(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo',
                        wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory)


@pytest.fixture(scope='module')
def v3_built(tmp_path_factory):
    return build_v3(tmp_path_factory.mktemp('v3'))


@pytest.fixture(scope='module')
def v2_built(tmp_path_factory):
    return build_v2(tmp_path_factory.mktemp('v2'))


@pytest.fixture(autouse=True)
def isolated_imports(monkeypatch):
    """Each test imports its normal bound modules afresh."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(BINDING_PREFIXES):
            del sys.modules[name]


def edited(plan, change):
    """A canonical plan with ``change(value)`` applied and identities recomputed."""
    value = json.loads(plan)
    change(value)
    value['definition_id'] = _definition_id(
        value['workflow_version'], value['graph_id'],
        [Resource(row['kind'], row['path'], None, row['sha256'], row['size'])
         for row in value['resources']],
        [Replacement(target, Path(entry), root) for target, root, entry in value['replacements']],
        [(name, path['$p']) for name, path in sorted(dict(value.get('prompt_slots', {'$m': []})['$m']).items())])
    value['compiled_plan_identity'] = compiled_plan_identity(value)
    return canonical_json(value)


SAME = object()


def load_built(built, plan=SAME):
    """Load a plan after making its ordinary Python wheel site importable."""
    for name in tuple(sys.modules):
        if name.startswith(BINDING_PREFIXES):
            sys.modules.pop(name)
    sys.path.insert(0, str(built.site))
    try:
        return artifact.load_artifact(built.plan if plan is SAME else plan)
    finally:
        sys.path.remove(str(built.site))


RICH_FLOW_EXTRA = '''
## Resources

| ID | Kind | Source |
| --- | --- | --- |
| notes | data | [text](notes.txt) |

## Native Dependencies

| Module | Source |
| --- | --- |
| aq_bindings.helper | [Python](src/aq_bindings/helper.py) |

## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](limits.toml) | GRAPH_LIMIT |

## Tools

| Port | Instance |
| --- | --- |
| search | [instance](instance.toml) |
'''


def copy_tree(source, target):
    import shutil
    shutil.copytree(source, target)
    return target


def build_rich_v3(directory, *, flow_extra=RICH_FLOW_EXTRA, helper=b'HELPER = "helper"\n', review_extra=''):
    """A v3 graph with a declared data resource, native dependency, timeout and tool port."""
    root = copy_tree(QUALIFICATION, directory / 'tree')
    (root / 'notes.txt').write_bytes(b'notes\n')
    (root / 'instance.toml').write_bytes(b'id = "sample/primary"\n')
    (root / 'limits.toml').write_bytes(b'version = 1\n[fields.GRAPH_LIMIT]\ntype = "float"\ndefault = 5.5\n'
                                       + (b'[fields.REVIEW_LIMIT]\ntype = "float"\ndefault = 2.5\n' if review_extra else b''))
    review = root / 'review.md.html'
    review.write_text(review.read_text() + review_extra)
    (root / 'src/aq_bindings/helper.py').write_bytes(helper)
    flow = root / 'flow.md.html'
    flow.write_text(flow.read_text() + flow_extra)
    native = root / 'src/aq_bindings'
    wheel = build_wheel(directory, 'aq-bindings', {
        'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
        'aq_bindings/helper.py': helper, 'aq_bindings/native.py': (native / 'native.py').read_bytes()})
    plan = compile_plan(flow, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory)


def build_tool_v3(directory):
    """A tool-bound graph whose descriptor and native module resolve from its pinned wheel."""
    root = copy_tree(QUALIFICATION, directory / 'tree')
    (root / 'instance.toml').write_text(
        'version=1\nid="sample/compiled"\ninterface="verified-read@1"\n'
        'implementation="package:aq-bindings:aq_bindings/tool_impl.toml"\n')
    native = root / 'src/aq_bindings'
    (native / 'native.py').write_text('''from typing import TypedDict

class State(TypedDict):
    messages: list
    mode: str
    attempts: int
    feedback: str
    trail: list
    outcome: str

async def intake(state, context):
    prepared = await context.tools['compiled'].prepare({'value': 'hello'})
    context.services['grant'] = await context.tools['compiled'].verify(
        prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    return {'trail': [*state['trail'], 'intake'], 'attempts': 0, 'feedback': '', 'outcome': ''}

async def work(state, context):
    trail = [*state['trail'], 'work:' + str(state['attempts'])]
    if state['attempts'] == 0:
        return {'trail': trail, 'attempts': 1, 'feedback': 'continue'}
    return {'trail': trail, 'attempts': 2, 'feedback': ''}

def route_work(state, context):
    return 'work' if state['feedback'] else 'finish'

async def finish(state, context):
    result = await context.tools['compiled'].execute(context.services['grant'])
    return {'trail': [*state['trail'], 'finish'], 'outcome': result.data['executed']}
''')
    for node, operations in [('intake', 'prepare, verify'), ('finish', 'execute')]:
        path = root / f'{node}.md.html'
        path.write_text(path.read_text() + '\n## Tools\n\n| Port | Instance | Operations |\n'
                        f'| --- | --- | --- |\n| compiled | [instance](instance.toml) | {operations} |\n')
    implementation = b'version=1\ninterface="verified-read@1"\nfactory="factory.py:create_tool"\n'
    factory = b'''from hashlib import sha256
import xxhash
from api_agent.providers.tools import ToolResult, ToolSpec

class Owner:
    version = 1
    async def describe(self):
        return ToolSpec("compiled", "Compiled fixture", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}, {"type": "object"}, sha256(b"compiled-tool").hexdigest())
    async def validate(self, prepared): pass
    async def invoke(self, prepared):
        xxhash.xxh3_64_hexdigest(prepared.arguments["value"].encode())
        return ToolResult("success", {"executed": prepared.arguments["value"]})
    async def aclose(self): pass
def create_tool(config, services): return Owner()
'''
    wheel = build_wheel(directory, 'aq-bindings', {
        'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
        'aq_bindings/native.py': (native / 'native.py').read_bytes(),
        'aq_bindings/tool_impl.toml': implementation,
        'aq_bindings/factory.py': factory,
    })
    plan = compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory)


INLINE_NODE = '''### Native

| Registry | Kind | Source |
| --- | --- | --- |
| inspect | deterministic | [source](native.py) |

### Functions

| ID | Registry | Input | Output |
| --- | --- | --- | --- |
| inspect | inspect | [input](input.json) | [output](output.json) |

### Prompts

| Node | File | When |
| --- | --- | --- |
| inspect | @greeting | always |

### Prompt: greeting

```text
Hello.
```
'''


def build_inline_v2(directory):
    """A v2 graph whose only prompt is declared inline in its component declaration."""
    root = directory / 'tree'
    root.mkdir(parents=True)
    native = b'def inspect(inputs, context):\n    return {"seen": True}\n'
    (root / 'native.py').write_bytes(native)
    (root / 'input.json').write_text('{"type":"object"}')
    (root / 'output.json').write_text('{"type":"object","properties":{"seen":{"type":"boolean"}}}')
    (root / 'flow.md.html').write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| inline_graph |\n\n'
                                       '## Flow\n\n1.  [component group](group.md.html)\n')
    (root / 'group.md.html').write_text(
        '# Component: group\n\n## Flow\n\n1.  [node inspect](#node-inspect)\n\n'
        '## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| group | 1 | - |\n\n'
        '## Node: inspect\n\n' + INLINE_NODE)
    wheel = build_wheel(directory, 'aq-inline', {'aq_inline/__init__.py': b'', 'aq_inline/native.py': native})
    plan = compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory)


@pytest.fixture(scope='module')
def rich_built(tmp_path_factory):
    return build_rich_v3(tmp_path_factory.mktemp('rich'))


@pytest.fixture(scope='module')
def inline_built(tmp_path_factory):
    return build_inline_v2(tmp_path_factory.mktemp('inline'))


@pytest.fixture
def tool_built(tmp_path):
    return build_tool_v3(tmp_path)
