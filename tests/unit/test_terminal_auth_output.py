"""Authentication owns the terminal without losing ordinary failure evidence."""
import io
import os
from types import SimpleNamespace

import pytest

from api_agent.auth.terminal_interaction import TerminalInteraction
from api_agent.auth.terminal_output import TerminalOutput


@pytest.fixture
def terminal():
    reader, writer = os.pipe()
    output = io.StringIO()
    presentation = TerminalOutput(output, output)
    with os.fdopen(reader) as stream:
        interaction = TerminalInteraction(stream, output, presentation=presentation)
        yield interaction, presentation, output, writer
    os.close(writer)
    presentation.close()


def test_manager_prompt_stays_last_until_conversation_closes(terminal):
    interaction, presentation, output, writer = terminal
    presentation.write('Before login')
    channel = interaction.open('Password-manager unlock')
    channel.write('Password: ')

    presentation.write('Test progress: 1/2 completed')
    presentation.write('Useful failure detail', error=True)
    assert output.getvalue() == 'Before login\nAuthentication: Password-manager unlock\nPassword: '
    os.write(writer, b'synthetic-secret\n')
    assert channel.read(.1).value == 'synthetic-secret'
    assert output.getvalue().endswith('Password: ')
    channel.close()
    channel.close()

    assert output.getvalue() == ('Before login\nAuthentication: Password-manager unlock\nPassword: \n'
        'Test progress: 1/2 completed\nUseful failure detail\n')
    assert presentation.active == 0 and presentation.pending is None


@pytest.mark.parametrize('label', ['Keeper', '1Password', 'Custom password manager'])
def test_terminal_shows_the_same_selected_title_as_the_interactive_screen(terminal, label):
    interaction, _, output, _ = terminal
    channel = interaction.open(label)
    try:
        channel.write('Enter master password for user@example.test\nPassword: ')
        channel.write('')
        assert channel.label == label
        assert output.getvalue() == (f'Authentication: {label}\n'
            'Enter master password for user@example.test\nPassword: ')
    finally:
        channel.close()


def test_terminal_context_title_cannot_inject_control_characters(terminal):
    interaction, _, output, _ = terminal
    channel = interaction.open('Example\n\r\x1b\x07 manager')
    try:
        channel.write('Password: ')
        assert output.getvalue() == 'Authentication: Example manager\nPassword: '
    finally:
        channel.close()


def test_no_stdin_help_warns_that_owned_authentication_can_still_prompt():
    from api_agent.config import load_shared_schema
    from api_agent.runtime.bootstrap import generated_help
    text = generated_help(load_shared_schema())
    line = next(line for line in text.splitlines() if line.strip().startswith('--no-std-in:'))
    assert 'without menus' in line and 'owned authentication may still prompt' in line
    assert 'Worktree' in line and 'DEBUG safety confirmations' in line


@pytest.mark.asyncio
async def test_native_provider_url_and_code_use_the_same_terminal_exclusion(terminal):
    from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
    interaction, presentation, output, writer = terminal
    login = LoginInteraction(SimpleNamespace(interaction=interaction, interactive=True))
    try:
        await login.open()
        await login.write('Open https://example.test/sign-in\nAuthorization code: ')
        presentation.write('Concurrent test completion')
        assert output.getvalue().endswith('Authorization code: ')
        os.write(writer, b'c3ludGhldGlj\n')
        assert await login.read() == 'c3ludGhldGlj'
        assert 'Concurrent test completion' not in output.getvalue()
    finally:
        await login.aclose()

    assert output.getvalue().endswith('Authorization code: \nConcurrent test completion\n')
    assert 'c3ludGhldGlj' not in output.getvalue()


def test_large_deferred_diagnostics_spill_without_truncation_or_stream_reordering():
    stdout, stderr = io.StringIO(), io.StringIO()
    presentation = TerminalOutput(stdout, stderr)
    presentation.hold()
    presentation.write('x' * 70000)
    presentation.write('error evidence', error=True, end='')
    spool = presentation.pending
    assert spool._rolled and stdout.getvalue() == stderr.getvalue() == ''

    presentation.release()

    assert stdout.getvalue() == 'x' * 70000 + '\n'
    assert stderr.getvalue() == '\nerror evidence'
    assert spool.closed and presentation.pending is None
    presentation.write('immediate output', end='')
    assert stdout.getvalue().endswith('\nimmediate output')


def test_output_waits_for_every_open_channel(terminal):
    interaction, presentation, output, _ = terminal
    first = interaction.open('First')
    second = interaction.open('Second')
    presentation.write('Waiting output')
    first.close()
    assert output.getvalue() == ''
    second.close()
    assert output.getvalue() == '\nWaiting output\n'


def test_overlapping_authentication_conversations_are_serialized(terminal):
    interaction, presentation, output, writer = terminal
    first = interaction.open('Password manager')
    second = interaction.open('Provider sign-in')
    first.write('Password: ')
    second.write('Open https://example.test/sign-in\nAuthorization code: ')
    presentation.write('Background progress')
    os.write(writer, b'first-answer\n')
    assert second.read(0) is None
    assert output.getvalue() == 'Authentication: Password manager\nPassword: '
    assert first.read(.1).value == 'first-answer'

    first.close()

    assert output.getvalue() == ('Authentication: Password manager\nPassword: \n'
        'Authentication: Provider sign-in\nOpen https://example.test/sign-in\nAuthorization code: ')
    os.write(writer, b'second-answer\n')
    assert second.read(.1).value == 'second-answer'
    second.close()
    assert output.getvalue().endswith('Authorization code: \nBackground progress\n')


@pytest.mark.parametrize('close_first', ['active', 'queued'])
def test_overlapping_channels_keep_echo_masked_and_restore_original_settings(close_first):
    import termios
    master, slave = os.openpty()
    try:
        original = termios.tcgetattr(slave)
        with os.fdopen(os.dup(slave)) as stream:
            interaction = TerminalInteraction(stream, io.StringIO())
            active = interaction.open('Password manager')
            queued = interaction.open('Provider')
            first, last = (active, queued) if close_first == 'active' else (queued, active)
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
            first.close()
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
            last.close()
            assert termios.tcgetattr(slave) == original
    finally:
        os.close(master)
        os.close(slave)


def test_queued_prompt_limit_fails_without_displaying_its_transcript(terminal):
    from api_agent.auth.credentials import CredentialRecoveryError
    interaction, _, output, _ = terminal
    first = interaction.open('First')
    queued = interaction.open('Queued')
    try:
        queued.write('x' * 65536)
        with pytest.raises(CredentialRecoveryError, match='display limit'):
            queued.write('overflow')
        assert output.getvalue() == ''
    finally:
        queued.close()
        first.close()


def test_exceptional_operation_exit_retains_output_until_owned_channel_closes():
    output = io.StringIO()
    presentation = TerminalOutput(output, output)
    presentation.hold()
    presentation.write('Original failure')
    spool = presentation.pending

    presentation.close()
    presentation.close()

    assert output.getvalue() == '' and not spool.closed
    presentation.release()
    assert output.getvalue() == '\nOriginal failure\n'
    assert spool.closed


@pytest.mark.parametrize('failure', ['write', 'flush'])
@pytest.mark.parametrize('queued', [False, True])
def test_spool_is_closed_even_when_release_separator_delivery_fails(failure, queued):
    class BrokenStream(io.StringIO):
        def flush(self):
            raise OSError('Terminal flush failed')
    output = io.StringIO()
    presentation = TerminalOutput(output, output)
    presentation.hold()
    if queued:
        presentation.write('Retained in report')
    spool = presentation.pending
    if failure == 'write':
        output.close()
        error = ValueError
    else:
        presentation.stderr = BrokenStream()
        error = OSError
    with pytest.raises(error):
        presentation.release()
    assert (spool is None or spool.closed) and presentation.pending is None
