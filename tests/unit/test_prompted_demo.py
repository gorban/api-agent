"""Unit proof that each demo resource pack fills the prompted demo's declared slots."""
import asyncio
from pathlib import Path


from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.workflows.definition import load_definition
from api_agent.workflows.execution import Workflow
from api_agent.workflows.native import load_native_bindings

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / 'demo/prompted'


def system_prompts(pack, questions):
    definition = load_definition(DEMO / 'flow.md.html', base_root=ROOT, pack_root=DEMO / 'packs' / pack,
                                 entry_root='base')
    requests = []

    async def llm(request):
        requests.append(request)
        return LLMResult(Message('assistant', (TextContent(f'answer {len(requests)}'),)), Usage(1, 1))

    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                        services={'llm': llm, 'selection': 'demo/model'})
    answers = [asyncio.run(workflow.arun({'question': question, **({} if index else {'turns': 0})},
                                         thread_id=pack))['answer']
               for index, question in enumerate(questions)]
    assert answers == [f'answer {index}' for index in range(1, len(questions) + 1)]
    assert [request.payload.messages[1].content[0].text for request in requests] == questions
    return [request.payload.messages[0].content[0].text for request in requests]


def test_prompted_demo_declares_the_graph_timeout_chat_composition_requires():
    from api_agent.workflows.configuration import declared_timeouts
    definition = load_definition(DEMO / 'flow.md.html', base_root=ROOT, pack_root=DEMO / 'packs' / 'fleet',
                                 entry_root='base')

    assert declared_timeouts(definition)['GRAPH'] == 600.0


def test_each_pack_fills_the_declared_prompt_slots():
    fleet = system_prompts('fleet', ['Which units are due?', 'And next week?'])
    construction = system_prompts('construction', ['Which projects are late?'])

    framing = ("You are a fleet equipment management assistant.\nDomain rules:\n"
               "- Report equipment by unit number.\n- Prefer the most recent inspection.\n"
               "Answer the user's question in one short paragraph.\n")
    assert fleet == [framing, framing + '\n\nThis is a follow-up turn; keep continuity with your earlier answer.\n']
    assert construction == [
        "You are a construction project management assistant.\nDomain rules:\n- Follow the user request.\n"
        "Answer the user's question in one short paragraph.\n"]
