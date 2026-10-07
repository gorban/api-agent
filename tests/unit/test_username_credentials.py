"""Usernames are fetched identities, not stored entry labels."""
import pytest

from api_agent.auth import credentials
from test_credentials import fake_cli, source


@pytest.mark.parametrize("manager,args", [
    ("keeper", ["clipboard-copy", "vault with spaces/user ' name", "--field", "login", "--output", "stdout"]),
    ("1password", ["item", "get", "user ' name", "--vault", "vault with spaces", "--fields", "username"]),
])
def test_username_exact_command_and_distinct_identity(tmp_path, manager, args, capsys):
    cli = fake_cli(tmp_path, f"assert sys.argv[1:] == {args!r}\nassert sys.stdin.read() == ''\nprint('actual-login@example.invalid')")
    descriptor = source(cli, manager)
    assert credentials.get_username(descriptor) == "actual-login@example.invalid"
    assert descriptor["user"] == "user ' name"
    assert capsys.readouterr() == ("", "")


def test_username_csv_preserves_quotes_and_comma(tmp_path):
    cli = fake_cli(tmp_path, "sys.stdout.write('\"last,first\"\"alias\"\\n')")
    assert credentials.get_username(source(cli, "1password")) == 'last,first"alias'


@pytest.mark.parametrize("manager,output", [("keeper", "\n"), ("keeper", " \n"),
    ("1password", '""\n'), ("1password", 'a,b\n'), ("1password", '"unclosed')])
def test_username_invalid_result_is_safely_rejected(tmp_path, manager, output):
    cli = fake_cli(tmp_path, f"sys.stdout.write({output!r})")
    with pytest.raises(credentials.CredentialError, match="Username retrieval failed"):
        credentials.get_username(source(cli, manager))


def test_username_raw_whitespace_content_is_preserved(tmp_path):
    cli = fake_cli(tmp_path, "sys.stdout.write(' login-with-space \\n')")
    assert credentials.get_username(source(cli)) == ' login-with-space '


@pytest.mark.parametrize("body,timeout", [
    ("print('private-identity'); print('private-identity', file=sys.stderr); sys.exit(7)", 1),
    ("sys.stderr.write('Enter master password: private-identity'); sys.stderr.flush(); time.sleep(10)", 1),
    ("time.sleep(10)", .05),
])
def test_username_failure_does_not_disclose_cli_identity(tmp_path, body, timeout):
    cli = fake_cli(tmp_path, body)
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.get_username(source(cli), timeout=timeout)
    assert caught.value.exit_code != 0
    assert "private-identity" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_unknown_kind_is_rejected_before_cli_resolution(monkeypatch):
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda value: pytest.fail('resolved CLI'))
    with pytest.raises(credentials.CredentialError, match='Unsupported credential kind'):
        credentials._retrieve({}, 'other', 1)


def test_rejects_non_string_credential_kind_before_cli_resolution(monkeypatch):
    from api_agent.auth import credentials
    class Pretend:
        def __hash__(self): return hash('username')
        def __eq__(self, other): return True
    class Unsafe(str):
        pass
    def unexpected(*args): raise AssertionError('resolved CLI for invalid kind')
    monkeypatch.setattr(credentials, 'resolve_cli_path', unexpected)
    for value in ([], Pretend(), Unsafe('username')):
        with pytest.raises(credentials.CredentialError, match='Unsupported credential kind'):
            credentials._retrieve({}, value, .1)
