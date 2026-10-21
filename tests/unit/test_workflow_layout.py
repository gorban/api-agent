"""Declaration errors are rejected before nodes or inference can execute."""
from pathlib import Path
import shutil
import json
import re
import pytest
from api_agent.workflows.layout import load_layout, LayoutError, _json

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def declaration(tmp_path):
    pack=tmp_path/'pack'
    shutil.copytree(ROOT/'demo/contrived',pack)
    shutil.copytree(ROOT/'demo/cfg',pack/'cfg')
    path=pack/'layout.md'
    path.write_text(path.read_text().replace('../../src/api_agent/workflows/contrived.py','functions.py').replace('../cfg/config.toml','cfg/config.toml'))
    (pack/'functions.py').write_text('# Registered implementation link fixture\n')
    return path


def parse(path): return load_layout(path,base_root=ROOT,pack_root=path.parent)


@pytest.mark.parametrize('old,new,expected',[
 ('# LangGraph Layout v1','# LangGraph Layout v9','heading'),
 ('## Components','## Unknown','heading'),
 ('## Notes','## Flow','heading'),
 ('# LangGraph Layout v1','# LangGraph Layout v1\n\nunowned prose','declared section'),
 ('## Notes','### Notes','heading'),
 ('## Timeouts','## Notes','heading'),
 ('    1.  [node first_draft]','     1.  [node first_draft]','four-space'),
 ('    1.  [node first_draft]','\t1.  [node first_draft]','ordered linked'),
 ('    1.  [node first_draft]','    1. [node first_draft]','four-column'),
 ('    2.  [node first_format]','    3.  [node first_format]','sequential'),
 ('2.  [component second]','100. [component second]','1..99'),
 ('    1.  [node first_draft]','        1.  [node first_draft]','four-space'),
 ('[node first_draft]','[llm first_draft]','step label'),
 ('[node first_draft]','[node FirstDraft]','step label'),
 ('[node second_draft]','[node first_draft]','duplicate step'),
 ('[component first]','[node first]','only components'),
 ('[component first](components.md)','not a link','inline file link'),
 ('| ID | Registry | Input | Output |','| ID | Wrong | Input | Output |','columns'),
 ('| second | 2 | second_guard |','| first | 2 | second_guard |','exactly once'),
 ('| first | 2 | first_guard |','| first | 0 | first_guard |','positive integer'),
 ('| first | 2 | first_guard |','| first | 2 | second_guard |','directly owned'),
 ('| first | 2 | first_guard |','| first | 2 | first_format |','final directly'),
 ('| first_draft | produce |','| first_draft | invalid-id |','registry ID'),
 ('| first_draft | user_input |','| missing | user_input |','input binding'),
 ('| first_draft | user_input |','| first_draft | undeclared |','input binding'),
 ('| first_draft | user_input | START | user_input | - |','| first_draft | user_input | second_draft | original_input | - |','earlier node'),
 ('| first_format | draft | first_draft | * | - |','| first_format | draft | first_draft | missing | - |','output field'),
 ('| first_draft | previous | PREVIOUS | * | null |','| first_draft | previous | PREVIOUS | * | invalid |','Expecting value'),
 ('| first_draft | previous | PREVIOUS | * | null |','| first_draft | previous | PREVIOUS | * | NaN |','nonfinite'),
 ('| first_draft | user_input | START | user_input | - |','| first_draft | previous | START | user_input | - |','input binding'),
 ('| first | second | otherwise |','| first | missing | otherwise |','siblings'),
 ('| first | second | otherwise |','| second | first | otherwise |','must advance'),
 ('| first | second | otherwise |','| first | first_format | otherwise |','siblings'),
 ('| first | second | otherwise |','| first | second | first_draft["route"] == "skip" |','duplicate'),
 ('| first | second | otherwise |','| first | second | bad-condition |','condition'),
 ('| first | second | otherwise |','| first | second | always |','conditional edges require otherwise'),
 ('| second_draft | [Producer]','| missing | [Producer]','prompt node'),
 ('[Producer](prompts/produce.md) | always','[Producer](prompts/produce.md) | bad-condition','condition'),
 ('(prompts/produce.md)','(https://example.invalid/prompt.md)','relative local'),
 ('(prompts/produce.md)','(prompts/produce.md#heading)','relative local'),
 ('(prompts/produce.md)','(prompts/produce.md?query)','relative local'),
 ('(prompts/produce.md)','(/tmp/prompt.md)','invalid local'),
 ('(prompts/produce.md)','(missing.md)','missing'),
 ('| Registry |','| **Registry** |','identifier'),
])
def test_single_declaration_mistake_has_located_diagnostic(declaration,old,new,expected):
    text=declaration.read_text()
    assert old in text
    declaration.write_text(text.replace(old,new,1))
    with pytest.raises(LayoutError,match=expected) as caught:
        parse(declaration)
    assert str(declaration)+':' in str(caught.value)


def test_runtime_pack_prompt_token_and_uri_escaped_file(declaration,monkeypatch,tmp_path):
    prompt=declaration.parent/'prompts/prompt with space.md'
    prompt.write_text('Pack fragment')
    declaration.write_text(declaration.read_text().replace('[Producer](prompts/produce.md)',r'[\<resource_pack\> fragment](\<resource_pack\>/prompts/prompt%20with%20space.md)'))
    monkeypatch.chdir(tmp_path)
    assert all(row['File']==prompt for row in parse(declaration).tables['Prompts'])


@pytest.mark.parametrize('value',['false','{"type":"unknown"}','{"type":"object","required":"bad"}','{"type":"object","$ref":"https://example.invalid/schema"}'])
def test_invalid_schema_before_inference(declaration,value):
    (declaration.parent/'schemas/draft.json').write_text(value)
    with pytest.raises(LayoutError): parse(declaration)


@pytest.mark.parametrize('value',['{"a":1,"a":2}','{"a":NaN}'])
def test_duplicate_and_nonfinite_schema_json_rejected(tmp_path,value):
    path=tmp_path/'schema.json';path.write_text(value)
    with pytest.raises(ValueError): _json(path)


def test_missing_required_binding_rejected(declaration):
    line='| first_draft | user_input | START | user_input | - |\n'
    declaration.write_text(declaration.read_text().replace(line,''))
    with pytest.raises(LayoutError,match='required inputs'): parse(declaration)


def test_link_escape_and_nonreadable_prompt_rejected(declaration,tmp_path):
    outside=tmp_path/'secret.md';outside.write_text('not part of the artifact')
    (declaration.parent/'prompts/produce.md').unlink()
    (declaration.parent/'prompts/produce.md').symlink_to(outside)
    with pytest.raises(LayoutError,match='outside'): parse(declaration)


def test_missing_and_outside_layout_have_located_errors(tmp_path):
    with pytest.raises(LayoutError,match='outside'):
        load_layout(tmp_path/'missing.md',base_root=ROOT,pack_root=ROOT/'demo')
    with pytest.raises(LayoutError,match='missing.md:1'):
        load_layout(tmp_path/'missing.md',base_root=ROOT,pack_root=tmp_path)


def test_notes_cannot_change_execution(declaration):
    before=parse(declaration)
    declaration.write_text(declaration.read_text()+'\n1. any prose or ``` arbitrary code in Notes\n')
    assert parse(declaration).steps==before.steps


@pytest.mark.parametrize('old,new,expected', [
    ('# LangGraph Layout v1', '# **LangGraph Layout v1**', 'plain cell'),
    ('## Notes', '## Notes\n\nnotes\n\n## Notes', 'heading'),
    ('## Timeouts', '## Timeouts\n\nnot a table', 'fixed-column table'),
    ('| first | 2 | first_guard |', '| first | 2 | first_guard | extra |', 'cells per row'),
    ('| first | 2 | first_guard |', '| first | 2 |', 'cells per row'),
    ('1.  [component first]', '- [component first]', 'nested ordered list'),
    ('## Components', 'flow prose\n\n## Components', 'only a nested ordered list'),
    ('| first_draft | attempt | ATTEMPT | value | - |', '| first_draft | attempt | ATTEMPT | wrong | - |', 'source field'),
    ('| first_draft | previous | PREVIOUS | * | null |', '| first_draft | previous | PREVIOUS | * | {"nested": NaN} |', 'nonfinite'),
    ('| first_draft | attempt | ATTEMPT | value | - |', '| first_draft | attempt | ATTEMPT | value | "wrong" |', 'not of type'),
    ('(prompts/produce.md)', r'(\<resource_pack\>/prompts/produce.md)', 'visible in the label'),
    ('(prompts/produce.md)', r'(prefix/\<resource_pack\>/prompts/produce.md)', 'leading root token'),
    ('(prompts/produce.md)', r'(\<unknown\>/prompts/produce.md)', 'unresolved variable'),
])
def test_additional_structural_rejections(declaration, old, new, expected):
    declaration.write_text(declaration.read_text().replace(old, new, 1))
    with pytest.raises(LayoutError, match=expected):
        parse(declaration)


def test_all_fixed_sections_are_required(declaration):
    declaration.write_text(declaration.read_text().replace('## Timeouts\n', ''))
    with pytest.raises(LayoutError, match='all fixed table sections'):
        parse(declaration)


def test_separate_flow_lists_are_rejected(declaration):
    declaration.write_text(declaration.read_text().replace('2.  [component second]', '2)  [component second]'))
    with pytest.raises(LayoutError, match='exactly one nested ordered list'):
        parse(declaration)


@pytest.mark.parametrize('owner,key', [('missing', 'LIMIT'), ('GRAPH', 'bad-key')])
def test_invalid_timeout_owner_and_key(declaration, owner, key):
    (declaration.parent / 'timeouts.toml').write_text('version=1')
    table = f'| Owner | Source | Key |\n| --- | --- | --- |\n| {owner} | [timeouts](timeouts.toml) | {key} |\n'
    declaration.write_text(re.sub(r'## Timeouts.*?(?=## Notes)', '## Timeouts\n\n' + table + '\n', declaration.read_text(), flags=re.S))
    with pytest.raises(LayoutError, match='timeout owner/key'):
        parse(declaration)


def test_duplicate_timeout_owner(declaration):
    (declaration.parent / 'timeouts.toml').write_text('version=1')
    row = '| GRAPH | [timeouts](timeouts.toml) | LIMIT |\n'
    declaration.write_text(re.sub(r'## Timeouts.*?(?=## Notes)', '## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n' + row * 2 + '\n', declaration.read_text(), flags=re.S))
    with pytest.raises(LayoutError, match='duplicate timeout'):
        parse(declaration)


def test_two_digit_numbering_supports_all_99_siblings(tmp_path):
    (tmp_path / 'functions.py').write_text('# local source fixture')
    (tmp_path / 'schema.json').write_text('{"type":"object"}')
    flow = '\n'.join(f'{number}.{" " * (3 - len(str(number)))}[node step_{number}](functions.py)' for number in range(1, 100))
    functions = '\n'.join(f'| step_{number} | invoke | [input](schema.json) | [output](schema.json) |' for number in range(1, 100))
    path = tmp_path / 'layout.md'
    path.write_text('# LangGraph Layout v1\n\n## Flow\n\n' + flow + '\n\n## Components\n\n## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n' + functions + '\n\n## Bindings\n\n## Edges\n\n## Prompts\n\n## Timeouts\n')
    layout = load_layout(path, base_root=tmp_path, pack_root=tmp_path)
    assert len(layout.steps) == 99 and layout.steps[-1].id == 'step_99'


def test_node_source_links_support_selected_pack_root(declaration):
    declaration.write_text(declaration.read_text().replace('(functions.py)', r'(\<resource_pack\>/functions.py)'))
    layout = parse(declaration)
    assert all(step.source == declaration.parent / 'functions.py' for step in layout.steps if step.kind == 'node')
