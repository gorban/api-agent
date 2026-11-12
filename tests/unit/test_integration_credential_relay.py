"""The suite launcher owns one credential relay for every wave's processes."""
from types import SimpleNamespace

import pytest

from api_agent.testing import credential_relay, integration
from api_agent.testing.collection import collect_tests

AUTH = {'password_manager': {'manager': '1password', 'cli_path': 'op'},
        'roles': {'Partner': [{'vault': 'Employee', 'user': 'builder'}]}}


@pytest.fixture(autouse=True)
def operation():
    integration._RUNNER_OPERATION.credential_relay = None
    yield integration._RUNNER_OPERATION
    integration._RUNNER_OPERATION.credential_relay = None


class Relay:
    instances = []
    def __init__(self, sources):
        self.sources, self.events = sources, []
        Relay.instances.append(self)
    def start(self):
        self.events.append('start')
    def warm(self):
        self.events.append('warm')


@pytest.mark.parametrize('config', [{}, {'UNATTENDED_AUTH': 'invalid'}])
def test_no_configured_account_starts_no_relay(operation, monkeypatch, tmp_path, config):
    monkeypatch.setattr(credential_relay, 'CredentialRelay', Relay)
    integration._start_credential_relay(SimpleNamespace(owner_root=tmp_path), config)
    assert operation.credential_relay is None


def test_configured_accounts_start_and_warm_one_relay(operation, monkeypatch, tmp_path):
    Relay.instances.clear()
    monkeypatch.setattr(credential_relay, 'CredentialRelay', Relay)
    integration._start_credential_relay(SimpleNamespace(owner_root=tmp_path), {'UNATTENDED_AUTH': AUTH})
    assert operation.credential_relay is Relay.instances[0]
    assert Relay.instances[0].events == ['start', 'warm']
    assert [source.user for source in Relay.instances[0].sources] == ['builder']


def test_children_receive_the_relay_and_the_operation_closes_it(tmp_path, monkeypatch):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_sample.py').write_text('def test_sample():\n    pass\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: (suite,))
    tests = collect_tests(context, 'integration')
    relay = SimpleNamespace(descriptor='relay-descriptor', closed=False)
    relay.close = lambda: setattr(relay, 'closed', True)
    environments = []
    def popen(args, **kwargs):
        environments.append(kwargs['env'])
        raise OSError('synthetic launch failure')
    def before_run():
        integration._RUNNER_OPERATION.credential_relay = relay
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: tests)
    monkeypatch.setattr(integration, 'subprocess',
                        SimpleNamespace(**(vars(integration.subprocess) | {'Popen': popen})))
    integration.run_integration_tests(context, tests, accepted=True, config={}, before_run=before_run)
    assert environments[0][credential_relay.ENVIRONMENT] == 'relay-descriptor'
    assert relay.closed
