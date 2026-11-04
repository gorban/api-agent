"""Selected E2E logical tests retain native retries without unrelated tests."""
import json

import pytest

from api_agent.testing.allure_selection import confirmation_selection, marked_result, _SELECTION_LABEL
from api_agent.testing.allure_source import PreviewSnapshot, ViewerError
from api_agent.testing.allure_viewer import ManagedAllureViewer
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.recording import RunIdentity, RunRecorder
from test_allure_viewer import cli, port, Browser, async_test, generated_root


@pytest.fixture
def confirmation_snapshot(tmp_path):
    recorder = RunRecorder(tmp_path / 'input', RunIdentity.create('sample', 'e2e'))
    latest = {}
    for name, statuses in [('recovered', ['failed','passed','passed']),
                            ('first', ['passed']), ('excluded', ['failed','failed'])]:
        identity = TestIdentity(name, 'sample', 'test_case.py', 'test_case.py::test_' + name)
        for attempt, status in enumerate(statuses, 1):
            latest[name] = recorder.record_test(TestResult(identity, status), attempt=attempt)
    manifest = recorder.finalize(e2e_reruns=2)
    files = {name: (recorder.directory / name).read_bytes() for name in manifest['files']}
    return PreviewSnapshot(files, manifest, latest, False)


@pytest.mark.parametrize('multiple', [False, True])
@async_test
async def test_selected_e2e_report_keeps_retry_attempts_and_exact_primary_link(confirmation_snapshot, cli, port, multiple):
    snapshot = confirmation_snapshot
    recovered = snapshot.result_files['recovered']
    selected = (recovered, snapshot.result_files['first']) if multiple else recovered
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    try:
        location = await owner.show_preview(snapshot, selected)
        cases = [json.loads(path.read_bytes()) for path in (generated_root(owner, location) / 'data/test-cases').glob('*.json')]
        assert len(cases) == (4 if multiple else 3)
        expected_files = confirmation_selection(snapshot.manifest, snapshot.files,
            selected if multiple else (selected,))
        expected = {marked_result(name, snapshot.files[name])[1] for name in expected_files}
        observed = {label['value'] for case in cases for label in case['labels'] if label['name'] == _SELECTION_LABEL}
        assert observed == expected
        excluded_marker = marked_result(snapshot.result_files['excluded'], snapshot.files[snapshot.result_files['excluded']])[1]
        assert excluded_marker not in observed
        if multiple:
            assert '#' not in location.url
        else:
            primary = marked_result(recovered, snapshot.files[recovered])[1]
            case = next(case for case in cases if any(label.get('value') == primary for label in case['labels']))
            assert location.url.endswith('#testresult/' + case['uid'])
    finally:
        await owner.aclose()


def test_corrupt_confirmation_selection_is_rejected_before_viewer_resources(confirmation_snapshot, cli, port):
    async def scenario():
        snapshot = confirmation_snapshot
        snapshot.manifest['metadata']['e2e_outcomes'][0]['final_status'] = 'failed'
        # Rejected before the CLI/server/browser start despite valid artifact bytes.
        owner = ManagedAllureViewer(port, browser_factory=Browser)
        try:
            with pytest.raises(ViewerError, match='confirmation'):
                await owner.show_preview(snapshot, snapshot.result_files['recovered'])
            assert owner._server is None and owner._temporary is None and owner._browser is None
        finally:
            await owner.aclose()
    import asyncio
    asyncio.run(scenario())
