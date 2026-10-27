"""Schema-driven conversation shared by independent GraphQL and REST selections."""
from api_agent.providers.rich_inference import object_json
from api_agent.workflows.tool_conversation import run_tool_conversation


ALIASES = {'primary_read': 'primary', 'secondary_read': 'secondary'}
PROMPT = ('Read hello independently with primary_read and secondary_read. '
          'Use each selected tool exactly once with arguments matching its declared schema. '
          'The values are unknown until read. Return only a JSON object with primary_read '
          'and secondary_read keys containing their respective exact hello strings. '
          'Emit raw JSON only: no Markdown, code fences, or surrounding commentary.')


async def converse(inputs, context):
    services = context.services
    journal = services['tool_journal']
    expected = services['expected_values']

    def verify_final(final, results):
        journal['final_matches'] = False
        if (set(results) != set(ALIASES)
                or any(len(records) != 1 for records in results.values())
                or any(not call['result_matches'] for call in journal['calls'])):
            raise ValueError('Each named tool must independently return its target value')
        if object_json(final) != expected:
            raise ValueError('Final answer must attribute each exact value to its named tool')
        journal['final_matches'] = True

    tools = services['tools']
    gates = {alias: (lambda invocation, port=port: tools.gate(
        scope_kind='node', scope=context.node_id, port=port, invocation_id=invocation))
        for alias, port in ALIASES.items()}
    await run_tool_conversation(prompt=PROMPT, gates=gates,
        complete=services['complete'], selection=services['selection'],
        generation=services.get('generation'), required_tools=tuple(ALIASES),
        verify_final=verify_final, journal=journal,
        result_proof=lambda alias, status, data: status == 'success'
            and data == {'hello': expected[alias]})
    journal['completed'] = True
    return {'verified': True}
