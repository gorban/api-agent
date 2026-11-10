"""A Session is saved only when the turn's final outcome is passed, inside the graph budget."""
import asyncio

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeCause, OutcomeState
from api_agent.workflows import WorkflowError

from session_workflow_support import session_entry, session_workflow, turn

HISTORY = '| history | reply.messages | append |'
TIMEOUTS = ('\n## Timeouts\n\n| Owner | Source | Key |\n| --- | --- | --- |\n'
            '| GRAPH | [config](budget.toml) | GRAPH_BUDGET |\n')


def outcome(state, cause=None):
    return {'execution_outcome': lambda: ExecutionOutcome(state, cause)}


def second_turn_history(workflow, services):
    asyncio.run(workflow.arun({'message': 'one'}, thread_id='chat', services=services))
    return turn(workflow, 'two')['reply']['seen']['history']


@pytest.mark.parametrize(('state', 'cause'), [
    (OutcomeState.PARTIAL, OutcomeCause.TOOL),
    (OutcomeState.FAILURE, OutcomeCause.EXECUTION),
    (OutcomeState.CANCELLED, None),
    (OutcomeState.UNKNOWN, None),
])
def test_turn_with_a_non_passed_outcome_does_not_save_its_session(tmp_path, state, cause):
    workflow = session_workflow(session_entry(tmp_path))

    assert second_turn_history(workflow, outcome(state, cause)) == 0


def test_turn_with_a_success_outcome_saves_its_session(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))

    assert second_turn_history(workflow, outcome(OutcomeState.SUCCESS)) == 2


def budgeted(tmp_path, seconds, **kwargs):
    (tmp_path / 'budget.toml').write_text(
        f'version = 1\n[fields.GRAPH_BUDGET]\ntype = "float"\ndefault = {seconds}\n')
    entry = session_entry(tmp_path)
    entry.write_text(entry.read_text() + TIMEOUTS)
    return session_workflow(entry, **kwargs)


class SlowSaver(InMemorySaver):
    """Delays every checkpoint write while ``slow`` is set, standing in for a stalled store."""

    slow = False

    async def aput(self, *args, **kwargs):
        if self.slow:
            await asyncio.sleep(5)
        return await super().aput(*args, **kwargs)


def test_a_save_that_outlasts_the_graph_budget_times_out_and_saves_nothing(tmp_path):
    saver = SlowSaver()
    workflow = budgeted(tmp_path, 0.5, checkpointer=saver)
    saver.slow = True

    with pytest.raises(TimeoutError):
        turn(workflow, 'one')
    saver.slow = False

    assert turn(workflow, 'two')['reply']['seen']['history'] == 0


def recorded(monkeypatch, tmp_path):
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents', lambda *args, **kwargs: None)


@pytest.mark.parametrize('target', ['api_agent.workflows.journal.WorkflowJournal.finalize',
                                    'api_agent.workflows.evidence.publish'])
def test_a_journal_failure_after_the_save_puts_the_previous_session_back(monkeypatch, tmp_path, target):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')
    recorded(monkeypatch, tmp_path)

    def broken(*args, **kwargs):
        raise OSError('evidence store unavailable')
    monkeypatch.setattr(target, broken)
    with pytest.raises(OSError, match='evidence store unavailable'):
        turn(workflow, 'two')
    monkeypatch.undo()

    assert turn(workflow, 'three')['reply']['seen']['lines'] == ['one#2']


def test_the_session_is_saved_before_the_journal_is_published(monkeypatch, tmp_path):
    saver = InMemorySaver()
    workflow = session_workflow(session_entry(tmp_path), checkpointer=saver)
    recorded(monkeypatch, tmp_path)
    saved_at_publish = []

    def publish(payload):
        saved_at_publish.extend(item.checkpoint['channel_values'].get('session', {}).get('topic')
                                for item in saver.list(None))
    monkeypatch.setattr('api_agent.workflows.evidence.publish', publish)

    turn(workflow, 'one')

    assert 'one' in saved_at_publish


def test_a_failed_turn_still_leaves_the_session_untouched(tmp_path):
    workflow = session_workflow(session_entry(tmp_path))
    turn(workflow, 'one')

    with pytest.raises(WorkflowError, match='draft failed'):
        turn(workflow, 'boom')

    assert turn(workflow, 'two')['reply']['seen']['history'] == 2
