"""Controlled full recording, IPC, worker and original-browser retention proof."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.recorded import run_recorded_integration
from api_agent.ui import provider as included


FACTORY = '''
import json, os
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
class Provider:
    version = 1
    capabilities = ProviderCapabilities('llm', ('complete', 'describe'))
    def __init__(self, config): self.config = config
    async def asetup(self, **kwargs):
        class Auth:
            async def __call__(self): return object()
            async def aclose(self): pass
        return Auth()
    async def adescribe(self, *, auth): return (AvailableSelection('sample'),)
    async def acomplete(self, request, *, auth):
        with open(self.config['SYNTHETIC_CALLS'], 'a') as stream:
            stream.write(json.dumps({'pid':os.getpid(),'request_id':request.request_id})+'\\n')
        return LLMResult(Message('assistant',(TextContent('Ok'),)),Usage(2,3))
    async def aclose(self): pass
def create(config, services): return Provider(config)
'''

CONFTEST = '''
import json
from pathlib import Path
from uuid import uuid4
import pytest
from api_agent.testing import ui_runtime
original = ui_runtime.managed_browser
def headless(*, inspection, headless):
    return original(inspection=inspection, headless=True)
ui_runtime.managed_browser = headless
@pytest.fixture(autouse=True)
def save_original_page(ui_provider_session, integration_config):
    path = Path(integration_config.values['SYNTHETIC_PAGES']) / (str(uuid4())+'.json')
    path.write_text(json.dumps({'origin':ui_provider_session.session.page.url}))
'''


def frozen_files(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.iterdir() if path.is_file()}


def send(client, binding):
    return client.post(binding['apiOrigin'] + '/app/agent/chat',
        headers={'Origin': binding['origin'], **binding['apiHeaders']},
        json={'session_id': str(uuid4()), 'model': 'sample',
            'messages': [{'role': 'user', 'content': 'synthetic manual'}]})


@pytest.mark.parametrize('jobs,publication_failure', [(0, False), (2, False), (2, True)])
def test_finalized_recording_controls_original_workers(tmp_path, monkeypatch, jobs, publication_failure):
    source = Path(__file__).resolve().parents[2]
    monkeypatch.setenv('PYTHONPATH', str(source / 'src'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    (base / 'conftest.py').write_text(CONFTEST)
    helper = (source / 'tests/integration/test_studio_request.py').read_text()
    helper = helper.replace('def test_studio_request(ui_provider_session, ui_provider_loop):',
        "@pytest.mark.parametrize('slot', [0, 1])\ndef test_studio_request(ui_provider_session, ui_provider_loop, slot):")
    (suite / 'test_request.py').write_text(helper)
    factory = tmp_path / 'factory.py'
    factory.write_text(FACTORY)
    calls = tmp_path / 'calls.jsonl'
    calls.touch()
    pages = tmp_path / 'pages'
    pages.mkdir()
    config = {'LLM_PROVIDER': str(factory) + ':create', 'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
        'UI_LISTENER_URL': 'http://127.0.0.1:9443', 'LLM_DEFAULT_MODEL': 'sample',
        'DEBUG': True, 'NO_STD_IN': True, 'TUI_MODE': 'integration',
        'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': str(tmp_path / 'allure'),
        'TEST_PARALLEL_JOBS': jobs, 'SYNTHETIC_CALLS': str(calls), 'SYNTHETIC_PAGES': str(pages)}
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: ())
    selected = [test for test in collect_tests(context, 'integration', config=config) if test.repo == 'base']
    assert len(selected) == 2
    bindings, frozen = [], {}
    client = httpx.Client(trust_env=False, timeout=10)
    def persist(directory, manifest):
        assert len(calls.read_text().splitlines()) == 2
        for path in pages.glob('*.json'):
            origin = json.loads(path.read_text())['origin'].rstrip('/')
            binding = client.get(origin + '/config.json').json() | {'origin': origin}
            assert send(client, binding).status_code == 503
            bindings.append(binding)
        assert len(bindings) == 2
        frozen.update(frozen_files(directory))
        if publication_failure:
            raise OSError('controlled publication failure')
    result = None
    try:
        result = run_recorded_integration(context, selected, config, accepted=True,
            storage=SimpleNamespace(persist=persist))
        assert result.persisted is not publication_failure
        assert result.exit_code == int(publication_failure), result.execution.output
        assert all(test.status == 'passed' for test in result.execution.tests), result.execution.output
        assert len(result.execution.inspections) >= 1
        for binding in bindings:
            response = send(client, binding)
            assert response.status_code == (503 if publication_failure else 200), response.text
        assert len(calls.read_text().splitlines()) == (2 if publication_failure else 4)
        assert frozen_files(result.directory) == frozen
        assert result.usage['prompt_count'] == 2
        for inspection in result.execution.inspections:
            assert inspection.end_inspection(timeout=10)
            assert inspection.closed
        for binding in bindings:
            with pytest.raises(httpx.TransportError):
                client.get(binding['origin'] + '/config.json')
    finally:
        if result is not None:
            for inspection in result.execution.inspections:
                if not inspection.closed: inspection.abort()
        client.close()
