"""Executable declarations retain dialect, link and per-run prompt boundaries."""
import asyncio
import json
from pathlib import Path
import runpy
import shutil

import pytest
from jsonschema import Draft7Validator, Draft202012Validator

from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.workflows import LayoutError, NodeFunction, Workflow, load_layout

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def declaration(tmp_path):
    pack = tmp_path / 'fixture'
    shutil.copytree(ROOT / 'demo/contrived', pack)
    shutil.copytree(ROOT / 'demo/cfg', pack / 'cfg')
    path = pack / 'layout.md'
    path.write_text(path.read_text().replace('../cfg/config.toml', 'cfg/config.toml'))
    return path


def compile_workflow(path, complete, **services):
    layout = load_layout(path, base_root=ROOT, pack_root=path.parent)
    catalog = runpy.run_path(str(path.parent / 'functions.py'))
    schema = next(row['Output schema'] for row in layout.tables['Functions'] if row['ID'] == 'first_draft')
    return Workflow(layout, {name: NodeFunction(catalog[name]) for name in ('produce', 'validate_format', 'guard')},
                    {'skip_second': lambda values: values['first_draft']['route'] == 'skip', **services.pop('conditions', {})},
                    services=dict(llm=complete, selection='synthetic', trial='test', output_schema=schema, **services))


@pytest.mark.parametrize('dialect', ['http://json-schema.org/draft-07/schema#', 'https://example.invalid/unknown-schema'])
def test_unsupported_declared_dialect_rejects_before_compiled_functions(declaration, dialect):
    schema_path = declaration.parent / 'schemas/draft.json'
    schema = json.loads(schema_path.read_text())
    schema.update({'$schema': dialect, 'dependencies': {'original_input': ['required_peer']}})
    schema_path.write_text(json.dumps(schema))
    calls = []
    with pytest.raises(LayoutError, match='only JSON Schema draft 2020-12'):
        graph = compile_workflow(declaration, lambda request: calls.append(request))
        asyncio.run(graph.arun({'user_input': 'original'}))
    assert calls == []
    value = dict(original_input='original', route='skip', ready=True, final_output='original')
    assert tuple(Draft7Validator(schema).iter_errors(value))
    assert not tuple(Draft202012Validator(schema).iter_errors(value))


def test_explicit_supported_dialect_executes_real_source_matched_graph(declaration):
    for path in (declaration.parent / 'schemas').glob('*.json'):
        schema = json.loads(path.read_text())
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        path.write_text(json.dumps(schema))
    calls = []
    async def complete(request):
        calls.append(request)
        return LLMResult(Message('assistant', (TextContent(json.dumps(dict(original_input='original', route='skip', ready=True, final_output='original'))),)))
    result = asyncio.run(compile_workflow(declaration, complete).arun({'user_input': 'original'}))
    assert result['first_draft']['original_input'] == 'original' and len(calls) == 1


@pytest.mark.parametrize('syntax', ['[Producer][chosen]', '[chosen][]', '[chosen]', '![Producer](prompts/produce.md)', '<file:///tmp/prompt.md>'])
def test_executable_prompt_links_require_inline_syntax_despite_notes_definitions(declaration, syntax):
    text = declaration.read_text().replace('[Producer](prompts/produce.md)', syntax)
    declaration.write_text(text + '\n[chosen]: prompts/produce.md\n')
    calls = []
    with pytest.raises(LayoutError, match='inline file link'):
        compile_workflow(declaration, lambda request: calls.append(request))
    assert calls == []


def test_notes_reference_definition_cannot_redirect_inline_prompt(declaration):
    declaration.write_text(declaration.read_text() + '\n[Producer]: missing.md\n\nNotes may mention [Producer] and ![an image](missing.md).\n')
    layout = load_layout(declaration, base_root=ROOT, pack_root=declaration.parent)
    assert all(row['File'] == declaration.parent / 'prompts/produce.md' for row in layout.tables['Prompts'])


def test_retry_prompts_are_snapshotted_in_order_per_execution(declaration):
    first = declaration.parent / 'prompts/produce.md'
    first.write_text('first prefix')
    second = first.with_name('conditional.md')
    second.write_text('conditional prefix')
    declaration.write_text(declaration.read_text().replace('| first_draft | [Producer](prompts/produce.md) | always |',
        '| first_draft | [Producer](prompts/produce.md) | always |\n| first_draft | [Conditional](prompts/conditional.md) | include |'))
    requests = []
    async def complete(request):
        requests.append(request)
        first.write_text('next execution prefix')
        second.unlink(missing_ok=True)
        attempt = json.loads(request.payload.messages[-1].content[0].text)['attempt']
        return LLMResult(Message('assistant', (TextContent(json.dumps(dict(original_input='original', route='skip', ready=attempt == 2, final_output='original'))),)))
    graph = compile_workflow(declaration, complete, conditions={'include': lambda values: True})
    asyncio.run(graph.arun({'user_input': 'original'}))
    assert len(requests) == 2
    assert requests[0].payload.messages[:2] == requests[1].payload.messages[:2]
    assert requests[0].payload.messages[0].content[0].text == 'first prefix\n\nconditional prefix'
    assert requests[0].payload.messages[1].content[0].text == 'original'
    second.write_text('new conditional prefix')
    asyncio.run(graph.arun({'user_input': 'original'}))
    assert requests[2].payload.messages[0].content[0].text == 'next execution prefix\n\nnew conditional prefix'
    assert requests[2].payload.messages[:2] == requests[3].payload.messages[:2]


def test_missing_prompt_at_execution_start_has_no_node_effects_and_failed_scope(declaration):
    requests, events = [], []
    graph = compile_workflow(declaration, lambda request: requests.append(request), observe=events.append)
    (declaration.parent / 'prompts/produce.md').unlink()
    with pytest.raises(FileNotFoundError):
        asyncio.run(graph.arun({'user_input': 'original'}))
    assert requests == []
    assert [row['outcome'] for row in events if row['event'] == 'scope_end'] == ['failed']
    assert not any(row['event'] == 'node' for row in events)


def test_concurrent_executions_keep_independent_prompt_snapshots(declaration):
    path = declaration.parent / 'prompts/produce.md'
    path.write_text('first execution')
    captured = []
    async def exercise():
        first_started, release = asyncio.Event(), asyncio.Event()
        async def complete(request):
            original = request.payload.messages[1].content[0].text
            captured.append((original, request.payload.messages[0].content[0].text))
            attempt = json.loads(request.payload.messages[-1].content[0].text)['attempt']
            if original == 'first' and attempt == 1:
                first_started.set()
                await release.wait()
            return LLMResult(Message('assistant', (TextContent(json.dumps(dict(original_input=original, route='skip', ready=attempt == 2, final_output=original))),)))
        graph = compile_workflow(declaration, complete)
        first = asyncio.create_task(graph.arun({'user_input': 'first'}))
        await first_started.wait()
        path.write_text('second execution')
        await graph.arun({'user_input': 'second'})
        release.set()
        await first
    asyncio.run(exercise())
    assert [text for run, text in captured if run == 'first'] == ['first execution'] * 2
    assert [text for run, text in captured if run == 'second'] == ['second execution'] * 2


def test_shared_prompt_path_is_read_once_per_execution_before_node_effects(declaration, monkeypatch):
    requests = []
    async def complete(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent(json.dumps(dict(original_input='original', route='next_a', ready=True, final_output='original'))),)))
    graph = compile_workflow(declaration, complete)
    prompt = declaration.parent / 'prompts/produce.md'
    original_read = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path == prompt:
            reads.append(path)
            assert not requests
            if len(reads) > 1:
                raise AssertionError('Shared prompt was reread during execution')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    asyncio.run(graph.arun({'user_input': 'original'}))
    assert reads == [prompt] and len(requests) == 2
    assert requests[0].payload.messages[0] == requests[1].payload.messages[0]
