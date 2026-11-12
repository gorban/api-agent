"""One-time codes are scrubbed while their sign-in runs, then released."""
import asyncio
from unittest.mock import Mock

import pytest

from api_agent.auth import credentials as helpers
from api_agent.auth.bindings import bind_role_credentials
from api_agent.providers.contracts import ContractError
from api_agent.providers.sessions import ScopedLLMCredentials, ScopedRoleCredentials, SignInCredentials, signing_in
from api_agent.providers.shared_setup import RevocableCredentials
from api_agent.telemetry.privacy import EmissionBoundary


class SignInFailed(Exception):
    pass


@pytest.fixture
def boundary():
    return EmissionBoundary()


@pytest.fixture
def sign_in(boundary):
    return SignInCredentials(boundary)


def bound_reader(boundary, monkeypatch, codes):
    """A real password-manager binding whose helper returns each code in turn."""
    config = {'UNATTENDED_AUTH': {'password_manager': {'manager': 'keeper', 'cli_path': '/synthetic-cli'},
        'roles': {'LLM User': [{'vault': 'Synthetic vault', 'user': 'Saved item'}]}}}
    monkeypatch.setattr(helpers, 'get_totp', Mock(side_effect=iter(codes)))
    monkeypatch.setattr(helpers, 'get_password', Mock(return_value='synthetic-password'))
    return bind_role_credentials(config, ('LLM User',), 0, registry=None, emissions=boundary, pending_tasks=set())


def test_the_registry_stays_bounded_across_many_sign_ins(boundary, sign_in):
    asyncio.run(boundary.aregister_secret('synthetic-password'))
    baseline = set(boundary._secrets)

    async def sign_in_once(code):
        with sign_in():
            await sign_in.register(code)
            assert boundary._text(code) == '[REDACTED]'

    async def many():
        for number in range(50):
            await sign_in_once('code-%06d' % number)
            assert boundary._secrets == baseline

    asyncio.run(many())
    assert boundary._text('code-000049') == 'code-000049'


def test_a_code_is_redacted_in_output_emitted_during_its_sign_in(boundary, sign_in):
    async def scenario():
        with sign_in():
            await sign_in.register('123456')
            assert boundary._text('page said 123456') == 'page said [REDACTED]'

    asyncio.run(scenario())


def test_a_code_is_redacted_in_an_error_raised_mid_sign_in(boundary, sign_in):
    emitted = []

    async def scenario():
        with sign_in():
            try:
                await sign_in.register('123456')
                raise SignInFailed('rejected code 123456')
            except SignInFailed as error:
                emitted.append(boundary.exception(error))

    asyncio.run(scenario())
    assert '123456' not in repr(emitted)
    assert boundary._text('123456') == '123456'


def test_a_failed_sign_in_still_releases_its_codes(boundary, sign_in):
    async def scenario():
        with pytest.raises(SignInFailed):
            with sign_in():
                await sign_in.register('123456')
                raise SignInFailed()

    asyncio.run(scenario())
    assert not boundary._secrets


def test_a_later_code_in_the_same_sign_in_keeps_the_earlier_one_scrubbed(boundary, sign_in):
    async def scenario():
        with sign_in():
            await sign_in.register('111111')
            await sign_in.register('222222')
            assert boundary._text('111111 222222') == '[REDACTED] [REDACTED]'

    asyncio.run(scenario())
    assert not boundary._secrets


def test_overlapping_sign_ins_release_their_codes_when_the_last_one_ends(boundary, sign_in):
    async def scenario():
        with sign_in():
            await sign_in.register('111111')
            with sign_in():
                await sign_in.register('222222')
            assert boundary._text('111111 222222') == '[REDACTED] [REDACTED]'
            await sign_in.register('333333')
        assert not boundary._secrets

    asyncio.run(scenario())


def test_a_code_outside_any_sign_in_is_refused(boundary, sign_in):
    with pytest.raises(ContractError, match='during a sign-in'):
        asyncio.run(sign_in.register('123456'))
    assert not boundary._secrets


def test_the_binding_scrubs_a_code_only_for_its_sign_in(boundary, monkeypatch):
    reader = bound_reader(boundary, monkeypatch, ['111111', '222222'])
    baseline = set(boundary._secrets)

    async def scenario():
        with reader.sign_in():
            assert await reader('LLM User', 0, 'totp') == '111111'
            assert boundary._text('111111') == '[REDACTED]'
        assert boundary._secrets == baseline
        with pytest.raises(ContractError, match='during a sign-in'):
            await reader('LLM User', 0, 'totp')

    asyncio.run(scenario())


def test_the_binding_keeps_passwords_registered_after_the_sign_in(boundary, monkeypatch):
    reader = bound_reader(boundary, monkeypatch, [])

    async def scenario():
        with reader.sign_in():
            await reader('LLM User', 0, 'password')

    asyncio.run(scenario())
    assert boundary._text('synthetic-password') == '[REDACTED]'


def test_the_binding_stays_bounded_across_many_sign_ins(boundary, monkeypatch):
    reader = bound_reader(boundary, monkeypatch, ['%06d' % number for number in range(30)])
    asyncio.run(boundary.aregister_secret('synthetic-password'))
    baseline = set(boundary._secrets)

    async def scenario():
        for _ in range(30):
            with reader.sign_in():
                await reader('LLM User', 0, 'totp')
            assert boundary._secrets == baseline

    asyncio.run(scenario())


def test_wrappers_open_the_scope_of_the_reader_they_wrap(boundary, monkeypatch):
    reader = bound_reader(boundary, monkeypatch, ['111111', '222222'])
    wrapped = ScopedLLMCredentials('LLM User', reader)

    async def scenario():
        with wrapped.sign_in():
            await wrapped(0, 'totp')
            assert boundary._text('111111') == '[REDACTED]'
        revocable = RevocableCredentials('operation', wrapped)
        with signing_in(revocable):
            await revocable(0, 'totp')
            assert boundary._text('222222') == '[REDACTED]'
        await revocable.aclose()

    asyncio.run(scenario())
    assert not boundary._secrets


def test_a_reader_without_a_scope_needs_none(boundary):
    async def retrieve(role, index, kind):
        return 'synthetic'

    for reader in (retrieve, ScopedRoleCredentials(['LLM User'], 0, retrieve)):
        with signing_in(reader):
            pass
    assert not boundary._secrets
