"""Native node for the explicitly selected shared-target integration graph."""
from api_agent.providers.rich_inference import object_json
from api_agent.workflows.tool_conversation import run_tool_conversation


ALIASES = {'primary_read': 'primary', 'secondary_read': 'secondary'}
PROMPT = ('Use primary_read and secondary_read independently to read hello. '
          'Make exactly one read-only literal GraphQL query with each tool: { hello }. '
          'The hello value is unknown until read. Return only a JSON object with '
          'exactly primary_read and secondary_read as keys, each containing the '
          'exact hello string returned by that named tool. '
          'Emit raw JSON only: no Markdown, code fences, or surrounding commentary.')


async def converse(inputs, context):
    services = context.services
    journal = services['tool_journal']
    expected = services['expected_value']

    def verify_final(final, results):
        journal['final_matches'] = False
        if (set(results) != set(ALIASES)
                or any(len(records) != 1 for records in results.values())
                or any(not call['result_matches'] for call in journal['calls'])):
            raise ValueError('Both tools must independently return the controlled backend value')
        report = object_json(final)
        if report != dict.fromkeys(ALIASES, expected):
            raise ValueError('Final answer must attribute the exact value to both named tools')
        journal['final_matches'] = True

    gates = {alias: context.tools[port] for alias, port in ALIASES.items()}
    await run_tool_conversation(prompt=PROMPT, gates=gates,
        complete=services['complete'], selection=services['selection'],
        generation=services.get('generation'), required_tools=tuple(ALIASES),
        verify_final=verify_final, journal=journal,
        result_proof=lambda alias, status, data: status == 'success' and data == {'hello': expected})
    journal['completed'] = True
    return {'verified': True}
