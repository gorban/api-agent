"""Build two-node graphs, in both workflow grammars, bound to an operations@1 tool."""
from pathlib import Path
import shutil
from types import MappingProxyType

from api_agent.config.declarations import schema_from_dict
from api_agent.config.resolve import resolve_config
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.tooling.selection import compose_tools

FIXTURE = Path(__file__).parent / 'fixtures' / 'tool_operations'
GRAMMARS = ['older', 'typed']


def table(rows):
    return ('| Port | Instance | Operations |\n| --- | --- | --- |\n'
            + ''.join(f'| {port} | [instance](instance.toml) | {operations} |\n'
                      for port, operations in rows))


def build(root, grammar, *, reader='introspect, query', writer='mutate', interface='operations@1',
          implementation='tool/tool.toml', graph_timeout=None, timeouts=(), node_timeouts=None):
    """Write one graph whose `reader` and `writer` nodes share one tool instance.

    ``timeouts`` rows ``(owner, key, default)`` go to the root; ``node_timeouts`` maps a node to
    rows written in its own document. Every key is declared in one configuration file.
    """
    shutil.copytree(FIXTURE, root / 'tool')
    (root / 'instance.toml').write_text(
        f'version = 1\nid = "sample/records"\ninterface = "{interface}"\n'
        f'implementation = "{implementation}"\n')
    (root / 'native.py').write_text('raise AssertionError("workflow native imported")\n')
    (root / 'input.json').write_text('{"type":"object"}')
    (root / 'output.json').write_text('{"type":"object"}')
    for node, port, operations in (('reader', 'lookup', reader), ('writer', 'store', writer)):
        functions = (f'\n## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n'
                     f'| {node} | {node} | [input](input.json) | [output](output.json) |\n'
                     if grammar == 'older' else '')
        (root / f'{node}.md.html').write_text(
            f'# Node: {node}\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
            f'| {node} | deterministic | [source](native.py) |\n' + functions
            + '\n## Tools\n\n' + table([(port, operations)])
            + ('\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n' + ''.join(
                f'| {owner} | [configuration](timeouts.toml) | {key} |\n'
                for owner, key, _ in (node_timeouts or {}).get(node, ())) if node in (node_timeouts or {}) else ''))
    components = ('\n## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| work | 1 | writer |\n'
                  if grammar == 'older' else '')
    (root / 'component.md.html').write_text(
        '# Component: work\n\n## Flow\n\n1.  [node reader](reader.md.html)\n'
        '2.  [node writer](writer.md.html)\n' + components)
    typed = ('\n## State\n\n| Registry | Source |\n| --- | --- |\n| State | [source](native.py) |\n'
             '\n## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
             '| reader | writer | - | - |\n| writer | END | - | - |\n| work | END | - | - |\n'
             if grammar == 'typed' else '')
    rows = [('GRAPH', 'GRAPH_TIMEOUT', graph_timeout)] if graph_timeout is not None else []
    rows += list(timeouts)
    declared = {key: default for _, key, default in
                rows + [row for node in (node_timeouts or {}).values() for row in node]}
    timeouts = ''
    if declared:
        (root / 'timeouts.toml').write_text('version = 1\n' + ''.join(
            f'\n[fields.{key}]\ntype = "float"\ndefault = {default}\nvalidation = "positive"\n'
            'acknowledge_default = false\nhelp = "Declared budget in seconds."\n'
            for key, default in declared.items()))
    if rows:
        timeouts = ('\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n' + ''.join(
            f'| {owner} | [configuration](timeouts.toml) | {key} |\n' for owner, key, _ in rows))
    entry = root / 'flow.md.html'
    entry.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| sample |\n'
                     '\n## Flow\n\n1.  [component work](component.md.html)\n' + typed + timeouts)
    return entry


def compose(root, entry):
    context = LaunchContext(root, root, root, 'sample', 'sample', root / 'config.toml',
        MappingProxyType({}), MappingProxyType({}), workflow_entry=entry)
    base = schema_from_dict({'version': 1, 'fields': {}})
    plan = compose_tools(context, base, environ={})
    return plan.bind_values(resolve_config(plan.schema, environ={}))
