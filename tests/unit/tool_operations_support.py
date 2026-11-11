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
          implementation='tool/tool.toml', graph_timeout=None):
    """Write one graph whose `reader` and `writer` nodes share one tool instance."""
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
            + '\n## Tools\n\n' + table([(port, operations)]))
    components = ('\n## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n| work | 1 | writer |\n'
                  if grammar == 'older' else '')
    (root / 'component.md.html').write_text(
        '# Component: work\n\n## Flow\n\n1.  [node reader](reader.md.html)\n'
        '2.  [node writer](writer.md.html)\n' + components)
    typed = ('\n## State\n\n| Registry | Source |\n| --- | --- |\n| State | [source](native.py) |\n'
             '\n## Edges\n\n| From | To | Router | Source |\n| --- | --- | --- | --- |\n'
             '| reader | writer | - | - |\n| writer | END | - | - |\n| work | END | - | - |\n'
             if grammar == 'typed' else '')
    timeouts = ''
    if graph_timeout is not None:
        (root / 'timeouts.toml').write_text(
            'version = 1\n\n[fields.GRAPH_TIMEOUT]\ntype = "float"\n'
            f'default = {graph_timeout}\nvalidation = "positive"\nacknowledge_default = false\n'
            'help = "Whole workflow deadline in seconds."\n')
        timeouts = ('\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n'
                    '| GRAPH | [configuration](timeouts.toml) | GRAPH_TIMEOUT |\n')
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
