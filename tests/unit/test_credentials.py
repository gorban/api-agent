from pathlib import Path
import sys
import time

import pytest

from api_agent.auth.credentials import CredentialError, get_password, get_totp, resolve_cli_path


def fake_cli(tmp_path, body):
    path = tmp_path / "cli with spaces"
    path.write_text(f"#!{sys.executable}\nimport sys, time, json\n{body}\n")
    path.chmod(0o700)
    return str(path)


def source(path, manager="keeper"):
    return dict(manager=manager, cli_path=path, vault="vault with spaces", user="user ' name")


@pytest.mark.parametrize("manager,kind,args", [
    ("keeper", "password", ["find-password", "vault with spaces/user ' name"]),
    ("keeper", "totp", ["clipboard-copy", "vault with spaces/user ' name", "--totp", "--output", "stdout", "-t"]),
    ("1password", "password", ["item", "get", "user ' name", "--vault", "vault with spaces", "--fields", "password", "--reveal"]),
    ("1password", "totp", ["item", "get", "user ' name", "--vault", "vault with spaces", "--otp"]),
])
def test_exact_argv_and_closed_stdin(tmp_path, manager, kind, args):
    path = fake_cli(tmp_path, f"assert sys.argv[1:] == {args!r}\nassert sys.stdin.read() == ''\nprint('example-value')")
    operation = get_password if kind == "password" else get_totp
    assert operation(source(path, manager)) == "example-value"
    assert resolve_cli_path(path) == path


def test_csv_preserves_comma_quotes_newline(tmp_path):
    path = fake_cli(tmp_path, "sys.stdout.write('\"a,b\"\"c\\nnext\"\\n')")
    assert get_password(source(path, "1password")) == 'a,b"c\nnext'


@pytest.mark.parametrize("output", ["a,b\n", "a\nb\n", '"unclosed', "", '""\n'])
def test_invalid_csv(tmp_path, output):
    path = fake_cli(tmp_path, f"sys.stdout.write({output!r})")
    with pytest.raises(CredentialError):
        get_password(source(path, "1password"))


@pytest.mark.parametrize("kind", [get_password, get_totp])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_prompt_fails_promptly_without_newline(tmp_path, kind, stream):
    path = fake_cli(tmp_path, f"sys.{stream}.write('Enter master password: ')\nsys.{stream}.flush()\ntime.sleep(10)")
    start = time.monotonic()
    with pytest.raises(CredentialError, match="Log into the CLI first") as error:
        kind(source(path), timeout=2)
    assert "Enter master password:" in str(error.value)
    assert error.value.exit_code != 0
    assert time.monotonic() - start < (2.5 if stream == "stdout" else 1.5)


def test_split_prompt(tmp_path):
    path = fake_cli(tmp_path, "sys.stderr.write('Enter master ')\nsys.stderr.flush()\ntime.sleep(.05)\nsys.stderr.write('password:')\nsys.stderr.flush()\ntime.sleep(10)")
    with pytest.raises(CredentialError, match="requires authentication"):
        get_totp(source(path), timeout=1)


@pytest.mark.parametrize("kind", [get_password, get_totp])
def test_timeout(tmp_path, kind):
    path = fake_cli(tmp_path, "time.sleep(10)")
    start = time.monotonic()
    with pytest.raises(CredentialError, match="timed out"):
        kind(source(path), timeout=.1)
    assert time.monotonic() - start < 1


def test_nonzero_captures_both_streams(tmp_path):
    path = fake_cli(tmp_path, "print('failure stdout')\nprint('failure stderr', file=sys.stderr)\nsys.exit(7)")
    with pytest.raises(CredentialError, match="exit code 7") as error:
        get_password(source(path))
    assert "failure stdout" in str(error.value)
    assert "failure stderr" in str(error.value)


def test_concurrent_large_streams(tmp_path, capsys):
    path = fake_cli(tmp_path, "sys.stderr.write('e' * 100000)\nsys.stderr.flush()\nprint('example-value')")
    assert get_password(source(path)) == "example-value"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("field,value", [("manager", "bad"), ("cli_path", ""), ("vault", " "), ("user", None)])
def test_invalid_descriptor(tmp_path, field, value):
    profile = source("unused")
    profile[field] = value
    with pytest.raises(CredentialError):
        get_password(profile)


def test_configured_path_never_falls_back(tmp_path):
    with pytest.raises(CredentialError, match="Configured credential CLI"):
        resolve_cli_path(str(tmp_path / "keeper"))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(tmp_path, timeout):
    path = fake_cli(tmp_path, "print('example-value')")
    with pytest.raises(CredentialError, match="timeout must be positive"):
        get_password(source(path), timeout=timeout)


@pytest.mark.parametrize("password", ["password:supersecret", "not signed in", "Enter master password: "])
def test_prompt_like_successful_credentials_remain_private(tmp_path, password, capsys):
    path = fake_cli(tmp_path, f"sys.stdout.write({password!r})")
    assert get_password(source(path)) == password
    assert capsys.readouterr() == ("", "")


def test_timeout_kills_owned_descendant_holding_pipes(tmp_path):
    import os
    if os.name != 'posix':
        pytest.skip('Process group regression requires POSIX')
    marker = tmp_path / 'child.pid'
    body = f"import subprocess\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\nopen({str(marker)!r}, 'w').write(str(child.pid))\ntime.sleep(30)"
    path = fake_cli(tmp_path, body)
    with pytest.raises(CredentialError, match='timed out'):
        get_password(source(path), timeout=.3)
    child_pid = int(marker.read_text())
    # A dead child can briefly remain as a zombie until the container init reaps it.
    stat = Path(f'/proc/{child_pid}/stat')
    def running():
        try:
            return stat.read_text().split()[2] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False
    deadline = time.monotonic() + .5
    while running() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not running()
