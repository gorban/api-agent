"""Tables inside a node or component declaration may leave out the owner column."""
import pytest

from api_agent.workflows.artifact_compile import _encoder
from api_agent.workflows.definition import load_definition
from api_agent.workflows.identity import declaration_identity, logical_resolver, workflow_identity
from api_agent.workflows.layout import LayoutError

TIMEOUT_SOURCE = '[settings](cfg.toml)'


def table(columns, rows):
    return ('| ' + ' | '.join(columns) + ' |\n| ' + ' | '.join('---' for _ in columns) + ' |\n'
            + ''.join('| ' + ' | '.join(row) + ' |\n' for row in rows))


def cells(columns, values, omit=()):
    """Header and rows keeping every column not named in ``omit``."""
    keep = [i for i, name in enumerate(columns) if name not in omit]
    return [columns[i] for i in keep], [[row[i] for i in keep] for row in values]


def scoped(columns, row, omit):
    header, rows = cells(columns, [row], omit)
    return table(header, rows)


FUNCTIONS = ('ID', 'Registry', 'Input', 'Output')
BINDINGS = ('Node', 'Input', 'From', 'Field', 'Default')
PROMPTS = ('Node', 'File', 'When')
TIMEOUTS = ('Owner', 'Source', 'Key')
COMPONENTS = ('ID', 'Max passes', 'Guard')


def graph(root, *, functions=(), bindings=(), prompts=(), node_timeouts=(), group_timeouts=(),
          components=()):
    (root / 'native.py').write_text('def inspect(inputs, context):\n    return {"seen": True}\n')
    (root / 'input.json').write_text('{"type":"object","properties":{"text":{"type":"string"}}}')
    (root / 'output.json').write_text('{"type":"object","properties":{"seen":{"type":"boolean"}}}')
    (root / 'cfg.toml').write_text('version = 1\n' + ''.join(
        f'[fields.{key}]\ntype = "float"\ndefault = 1.0\nvalidation = "positive"\n'
        for key in ('GRAPH_LIMIT', 'GROUP_LIMIT', 'NODE_LIMIT')))
    entry = root / 'flow.md.html'
    entry.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| scoped_graph |\n\n'
                     '## Flow\n\n1.  [component group](group.md.html)\n\n'
                     '## Timeouts\n\n' + table(TIMEOUTS, [['GRAPH', TIMEOUT_SOURCE, 'GRAPH_LIMIT']]))
    function_row = ['inspect', 'inspect', '[input](input.json)', '[output](output.json)']
    (root / 'group.md.html').write_text(
        '# Component: group\n\n## Flow\n\n1.  [node inspect](#node-inspect)\n\n'
        '## Components\n\n' + (components or scoped(COMPONENTS, ['group', '1', '-'], ())) + '\n'
        '## Timeouts\n\n' + (group_timeouts or scoped(TIMEOUTS, ['group', TIMEOUT_SOURCE, 'GROUP_LIMIT'], ())) + '\n'
        '## Node: inspect\n\n'
        '### Native\n\n' + table(('Registry', 'Kind', 'Source'),
                                 [['inspect', 'deterministic', '[source](native.py)']]) + '\n'
        '### Functions\n\n' + (functions or scoped(FUNCTIONS, function_row, ())) + '\n'
        '### Bindings\n\n' + (bindings or scoped(BINDINGS, ['inspect', 'text', 'START', 'text', '-'], ())) + '\n'
        '### Prompts\n\n' + (prompts or scoped(PROMPTS, ['inspect', '@hello', 'always'], ())) + '\n'
        '### Timeouts\n\n' + (node_timeouts or scoped(TIMEOUTS, ['inspect', TIMEOUT_SOURCE, 'NODE_LIMIT'], ())) + '\n'
        '### Prompt: hello\n\n```text\nHello.\n```\n')
    return entry


def short_graph(root, functions_omit=('ID', 'Registry')):
    function_row = ['inspect', 'inspect', '[input](input.json)', '[output](output.json)']
    return graph(
        root,
        functions=scoped(FUNCTIONS, function_row, functions_omit),
        bindings=scoped(BINDINGS, ['inspect', 'text', 'START', 'text', '-'], ('Node',)),
        prompts=scoped(PROMPTS, ['inspect', '@hello', 'always'], ('Node',)),
        node_timeouts=scoped(TIMEOUTS, ['inspect', TIMEOUT_SOURCE, 'NODE_LIMIT'], ('Owner',)),
        group_timeouts=scoped(TIMEOUTS, ['group', TIMEOUT_SOURCE, 'GROUP_LIMIT'], ('Owner',)),
        components=scoped(COMPONENTS, ['group', '1', '-'], ('ID',)))


def admit(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def budgets(definition):
    return workflow_identity(definition, timeouts={'GRAPH': 1, 'group': 1, 'inspect': 1},
                             component_passes={'group': 1})


@pytest.fixture
def explicit(tmp_path):
    return admit(graph(tmp_path))


@pytest.mark.parametrize('omit', [('ID', 'Registry'), ('ID',), ('Registry',), ()])
def test_omitted_owner_columns_normalize_to_the_explicit_rows(tmp_path, explicit, omit):
    shortened = admit(short_graph(tmp_path, omit))

    assert shortened.layout.tables == explicit.layout.tables
    assert declaration_identity(shortened) == declaration_identity(explicit)
    assert budgets(shortened) == budgets(explicit)
    encode = lambda definition: _encoder(logical_resolver(definition))(definition.layout.tables)
    assert encode(shortened) == encode(explicit)
    assert [row['Registry'] for row in shortened.layout.tables['Functions']] == ['inspect']


def test_declaration_bytes_still_differ_between_the_two_forms(tmp_path, explicit):
    shortened = admit(short_graph(tmp_path))

    assert shortened.definition_id != explicit.definition_id


@pytest.mark.parametrize('name,columns,row', [
    ('Functions', FUNCTIONS, ['other', 'inspect', '[input](input.json)', '[output](output.json)']),
    ('Bindings', BINDINGS, ['other', 'text', 'START', 'text', '-']),
    ('Prompts', PROMPTS, ['other', '@hello', 'always']),
    ('Timeouts', TIMEOUTS, ['other', TIMEOUT_SOURCE, 'NODE_LIMIT']),
])
def test_node_table_owner_column_must_name_the_enclosing_node(tmp_path, name, columns, row):
    entry = graph(tmp_path, **{{'Functions': 'functions', 'Bindings': 'bindings', 'Prompts': 'prompts',
                                'Timeouts': 'node_timeouts'}[name]: table(columns, [row])})

    with pytest.raises(LayoutError, match=r"group\.md\.html:\d+: \w+ must name the enclosing node 'inspect'"):
        admit(entry)


@pytest.mark.parametrize('name,columns,row,keyword', [
    ('Timeouts', TIMEOUTS, ['inspect', TIMEOUT_SOURCE, 'GROUP_LIMIT'], 'group_timeouts'),
    ('Components', COMPONENTS, ['other', '1', '-'], 'components'),
])
def test_component_table_owner_column_must_name_the_enclosing_component(tmp_path, name, columns, row, keyword):
    entry = graph(tmp_path, **{keyword: table(columns, [row])})

    with pytest.raises(LayoutError, match=r"group\.md\.html:\d+: \w+ must name the enclosing component 'group'"):
        admit(entry)


@pytest.mark.parametrize('section,columns', [
    ('Timeouts', ('Source', 'Key')),
    ('Bindings', ('Input', 'From', 'Field', 'Default')),
])
def test_root_tables_still_require_the_owner_column(tmp_path, section, columns):
    entry = graph(tmp_path)
    row = {'Timeouts': [TIMEOUT_SOURCE, 'GRAPH_LIMIT'], 'Bindings': ['text', 'START', 'text', '-']}[section]
    entry.write_text(entry.read_text().split('## Timeouts')[0] + f'## {section}\n\n' + table(columns, [row]))

    with pytest.raises(LayoutError, match='wrong table column count'):
        admit(entry)


@pytest.mark.parametrize('section,columns,row', [
    ('Bindings', ('Input', 'From', 'Field', 'Default'), ['text', 'START', 'text', '-']),
    ('Prompts', ('File', 'When'), ['@hello', 'always']),
])
def test_component_documents_still_require_their_descendant_owner_column(tmp_path, section, columns, row):
    entry = graph(tmp_path)
    component = tmp_path / 'group.md.html'
    component.write_text(component.read_text().replace(
        '## Node: inspect', f'## {section}\n\n' + table(columns, [row]) + '\n## Node: inspect'))

    with pytest.raises(LayoutError, match='wrong table column count'):
        admit(entry)


def test_a_node_document_cannot_omit_the_components_id_column(tmp_path):
    entry = graph(tmp_path)
    component = tmp_path / 'group.md.html'
    component.write_text(component.read_text().replace(
        '### Native', '### Components\n\n' + table(('Max passes', 'Guard'), [['1', '-']]) + '\n### Native'))

    with pytest.raises(LayoutError, match='wrong table column count'):
        admit(entry)


def test_a_defaulted_registry_must_still_match_the_native_registry(tmp_path):
    entry = short_graph(tmp_path)
    component = tmp_path / 'group.md.html'
    component.write_text(component.read_text().replace('| inspect | deterministic |', '| other | deterministic |'))

    with pytest.raises(LayoutError, match='Native registry and Functions must match'):
        admit(entry)


def test_response_and_summary_function_layouts_may_omit_the_owner_columns(tmp_path):
    columns = (*FUNCTIONS, 'Response schema', 'Output mode', 'Fallback', 'Display summary')
    row = ['inspect', 'inspect', '[input](input.json)', '[output](output.json)', '-', '-', '-', '-']
    entry = graph(tmp_path, functions=scoped(columns, row, ('ID', 'Registry')))

    definition = admit(entry)

    assert definition.layout.tables['Functions'][0]['ID'] == 'inspect'
    assert definition.layout.tables['Functions'][0]['Display summary'] == '-'


def test_node_documents_in_their_own_files_use_the_same_implied_owner(tmp_path):
    entry = short_graph(tmp_path)
    component = tmp_path / 'group.md.html'
    text = component.read_text()
    start = text.index('## Node: inspect')
    component.write_text(text[:start].replace('(#node-inspect)', '(inspect.md.html)'))
    (tmp_path / 'inspect.md.html').write_text('# Node: inspect\n\n' + text[start:].split('\n', 2)[2].replace('### ', '## '))

    definition = admit(entry)

    assert {row['Node'] for row in definition.layout.tables['Bindings']} == {'inspect'}
    assert {row['_owner_id'] for row in definition.layout.tables['Timeouts'] if row['Owner'] == 'inspect'} == {'inspect'}
