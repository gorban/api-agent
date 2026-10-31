"""HTTPS preparation contracts with simulated mkcert and OpenSSL only."""
from pathlib import Path
import subprocess
import pytest

from api_agent.runtime.https import HTTPSPreparationError, prepare_local_https


def certificate_text(names):
    return ('Certificate:\n    Data:\n        X509v3 extensions:\n'
            '            X509v3 Subject Alternative Name:\n                ' + names + '\n'
            '    Signature Algorithm: sha256WithRSAEncryption\n')


class FakeRunner:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.valid = True
        self.fail = None
        (root / 'rootCA.pem').write_text('public CA')

    def __call__(self, argv, *, timeout, interactive):
        self.calls.append((argv, timeout, interactive))
        if self.fail == argv[:2]:
            return subprocess.CompletedProcess(argv, 2, '', 'secret output')
        if argv[:2] == ['mkcert', '-CAROOT']:
            return subprocess.CompletedProcess(argv, 0, str(self.root) + '\n', '')
        if argv[:2] == ['mkcert', '-cert-file']:
            Path(argv[2]).write_text('cert')
            Path(argv[4]).write_text('key')
        if argv[:2] == ['openssl', 'x509']:
            if '-checkend' in argv and not self.valid:
                return subprocess.CompletedProcess(argv, 1, '', '')
            if '-text' in argv:
                names = 'DNS:localhost, DNS:localhost, IP Address:127.0.0.1, IP Address:::1'
                return subprocess.CompletedProcess(argv, 0, certificate_text(names), '')
            if '-pubkey' in argv:
                return subprocess.CompletedProcess(argv, 0, 'PUBLIC\n', '')
        if argv[:2] == ['openssl', 'pkey']:
            return subprocess.CompletedProcess(argv, 0, 'PUBLIC\n', '')
        return subprocess.CompletedProcess(argv, 0, '', '')


def test_generate_then_reuse_exact_commands(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    cert_dir = tmp_path / '.certs'
    result = prepare_local_https(cert_dir, runner=runner, interactive=False, timeout=3)
    assert result.material.ca_file == root / 'rootCA.pem'
    assert result.material.cert_file.read_text() == 'cert'
    assert result.material.key_file.stat().st_mode & 0o077 == 0
    assert runner.calls[0] == (['mkcert', '-install'], 3, False)
    assert runner.calls[1] == (['mkcert', '-CAROOT'], 3, False)
    generated = [call for call, _, _ in runner.calls if call[:2] == ['mkcert', '-cert-file']]
    assert len(generated) == 1
    assert generated[0][-3:] == ['localhost', '127.0.0.1', '::1']
    assert 'Verify host browser trust separately' in result.diagnostics[1]
    runner.calls.clear()
    again = prepare_local_https(cert_dir, runner=runner)
    assert again.material == result.material
    assert not any(call[:2] == ['mkcert', '-cert-file'] for call, _, _ in runner.calls)


def test_invalid_existing_regenerates(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'
    prepare_local_https(directory, runner=runner)
    runner.valid = False
    with pytest.raises(HTTPSPreparationError, match='Generated certificate failed validation'):
        prepare_local_https(directory, runner=runner)
    assert (directory / 'localhost.pem').read_text() == 'cert'


def test_trust_failure_is_actionable_and_redacted(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    runner.fail = ['mkcert', '-install']
    with pytest.raises(HTTPSPreparationError, match='Run mkcert -install interactively') as error:
        prepare_local_https(tmp_path / '.certs', runner=runner, interactive=False)
    assert 'secret output' not in str(error.value)
    assert len(runner.calls) == 1


def test_symlink_key_rejected_before_trust(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'; directory.mkdir()
    (directory / 'localhost-key.pem').symlink_to(root / 'rootCA.pem')
    with pytest.raises(HTTPSPreparationError, match='symlink'):
        prepare_local_https(directory, runner=runner)
    assert runner.calls == []


def test_missing_ca_and_bad_names(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    (root / 'rootCA.pem').unlink()
    with pytest.raises(HTTPSPreparationError, match='rootCA.pem is missing'):
        prepare_local_https(tmp_path / '.certs', runner=runner)
    with pytest.raises(ValueError, match='invalid certificate'):
        prepare_local_https(tmp_path / '.certs', hostname='../../oops', runner=runner)


def test_timeout_reports_bounded_failure(tmp_path):
    def timed_out(argv, *, timeout, interactive):
        raise subprocess.TimeoutExpired(argv, timeout)
    with pytest.raises(HTTPSPreparationError, match='timed out after 2s'):
        prepare_local_https(tmp_path / '.certs', runner=timed_out, timeout=2)

@pytest.mark.parametrize('failure', [FileNotFoundError(), PermissionError(), subprocess.CalledProcessError(1, 'mkcert')])
def test_startup_and_cancellation_failures(tmp_path, failure):
    def broken(argv, *, timeout, interactive):
        raise failure
    with pytest.raises(HTTPSPreparationError):
        prepare_local_https(tmp_path / '.certs', runner=broken)


def test_cancellation_propagates(tmp_path):
    def cancelled(argv, *, timeout, interactive):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        prepare_local_https(tmp_path / '.certs', runner=cancelled)


def test_missing_generated_files(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    original = runner.__call__
    def no_outputs(argv, *, timeout, interactive):
        if argv[:2] == ['mkcert', '-cert-file']:
            return subprocess.CompletedProcess(argv, 0, '', '')
        return original(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='did not create'):
        prepare_local_https(tmp_path / '.certs', runner=no_outputs)


def test_ca_lookup_empty(tmp_path):
    def empty(argv, *, timeout, interactive):
        return subprocess.CompletedProcess(argv, 0, '', '')
    with pytest.raises(HTTPSPreparationError, match='no directory'):
        prepare_local_https(tmp_path / '.certs', runner=empty)


def test_invalid_timeout_and_directory(tmp_path):
    with pytest.raises(ValueError, match='positive'):
        prepare_local_https(tmp_path, timeout=0)
    file = tmp_path / 'file'; file.touch()
    with pytest.raises(HTTPSPreparationError, match='not a directory'):
        prepare_local_https(file, runner=lambda *a, **k: None)

@pytest.mark.parametrize('invalid', ['sans', 'key', 'ca', 'expiry'])
def test_existing_validation_causes_regeneration(tmp_path, invalid):
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    cert_dir = tmp_path / '.certs'
    prepare_local_https(cert_dir, runner=base)
    class InvalidExisting:
        generated = False
        calls = []
        def __call__(self, argv, *, timeout, interactive):
            self.calls.append(argv)
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated:
                if invalid == 'expiry' and argv[:2] == ['openssl', 'x509'] and '-checkend' in argv:
                    return subprocess.CompletedProcess(argv, 1, '', '')
                if invalid == 'sans' and '-text' in argv:
                    return subprocess.CompletedProcess(argv, 0, 'DNS:unrelated.example', '')
                if invalid == 'key' and argv[:2] == ['openssl', 'pkey']:
                    return subprocess.CompletedProcess(argv, 0, 'OTHER', '')
                if invalid == 'ca' and argv[:2] == ['openssl', 'verify']:
                    return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    runner = InvalidExisting()
    prepare_local_https(cert_dir, runner=runner)
    assert runner.generated


def test_symlink_directory_preserved_and_nonregular_key_rejected(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    real = tmp_path / 'actual'; real.mkdir()
    link = tmp_path / '.certs'; link.symlink_to(real, target_is_directory=True)
    runner = FakeRunner(root)
    prepare_local_https(link, runner=runner)
    assert link.is_symlink()
    key = real / 'localhost-key.pem'
    key.unlink(); key.mkdir()
    with pytest.raises(HTTPSPreparationError, match='not a regular file'):
        prepare_local_https(link, runner=runner)


def test_default_runner_no_stdin_and_tool_missing(monkeypatch, tmp_path):
    from api_agent.runtime import https
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
    monkeypatch.setattr(https.shutil, 'which', lambda name: '/fake/' + name)
    monkeypatch.setattr(https.subprocess, 'run', fake_run)
    with pytest.raises(HTTPSPreparationError, match='timed out'):
        prepare_local_https(tmp_path / '.certs', interactive=False, timeout=2)
    assert calls[0][1]['stdin'] == subprocess.DEVNULL
    assert calls[0][1]['timeout'] == 2
    monkeypatch.setattr(https.shutil, 'which', lambda name: None)
    with pytest.raises(HTTPSPreparationError, match='mkcert is required'):
        prepare_local_https(tmp_path / 'other')


def test_default_runner_interactive_and_nonzero(monkeypatch, tmp_path):
    from api_agent.runtime import https
    calls = []
    monkeypatch.setattr(https.shutil, 'which', lambda name: '/fake/' + name)
    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 2, '', 'private output')
    monkeypatch.setattr(https.subprocess, 'run', fake_run)
    with pytest.raises(HTTPSPreparationError) as error:
        prepare_local_https(tmp_path / '.certs')
    assert calls[0]['stdin'] is None
    assert 'private output' not in str(error.value)


def test_dot_hostname_rejected(tmp_path):
    with pytest.raises(ValueError, match='invalid certificate hostname'):
        prepare_local_https(tmp_path / '.certs', hostname='.', runner=lambda *a, **k: None)


def test_expanded_ipv6_san_reuses_and_checks_renewal_window(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    prepare_local_https(directory, runner=base)
    calls = []
    def expanded(argv, *, timeout, interactive):
        calls.append(argv)
        if '-text' in argv:
            return subprocess.CompletedProcess(argv, 0,
                certificate_text('DNS:localhost, DNS:localhost, IP Address:127.0.0.1, IP Address:0:0:0:0:0:0:0:1'), '')
        return base(argv, timeout=timeout, interactive=interactive)
    result = prepare_local_https(directory, runner=expanded, renewal_days=3)
    assert result.diagnostics[0] == 'Local certificate reused.'
    assert any(call[-2:] == ['-checkend', str(3 * 86400)] for call in calls)
    assert not any(call[:2] == ['mkcert', '-cert-file'] for call in calls)
    with pytest.raises(ValueError, match='renewal_days'):
        prepare_local_https(directory, runner=base, renewal_days=0)


def test_reused_key_permissions_and_owner_rejected(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=runner)
    result.material.key_file.chmod(0o644)
    runner.calls.clear()
    with pytest.raises(HTTPSPreparationError, match='mode 0600'):
        prepare_local_https(directory, runner=runner)
    assert runner.calls == []
    result.material.key_file.chmod(0o600)
    monkeypatch.setattr(https.os, 'geteuid', lambda: result.material.key_file.stat().st_uid + 1)
    with pytest.raises(HTTPSPreparationError, match='owned by another user'):
        https._safe_key(result.material.key_file)


def test_failed_pair_install_restores_old_pair(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=base)
    result.material.cert_file.write_text('old cert')
    result.material.key_file.write_text('old key')
    old_replace = https.os.replace
    def break_key(src, dst):
        if str(src).endswith('/key.pem') and Path(dst) == result.material.key_file:
            raise OSError('private error')
        return old_replace(src, dst)
    monkeypatch.setattr(https.os, 'replace', break_key)
    class Expired:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='Could not install') as error:
        prepare_local_https(directory, runner=Expired())
    assert 'private error' not in str(error.value)
    assert result.material.cert_file.read_text() == 'old cert'
    assert result.material.key_file.read_text() == 'old key'


def test_two_callers_serialize_on_symlinked_directory(tmp_path):
    import threading
    root = tmp_path / 'ca'; root.mkdir()
    actual = tmp_path / 'actual'; actual.mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(actual, target_is_directory=True)
    base = FakeRunner(root)
    entered = threading.Event()
    release = threading.Event()
    generation_count = []
    def slow(argv, *, timeout, interactive):
        if argv[:2] == ['mkcert', '-cert-file']:
            generation_count.append(1)
            entered.set()
            assert release.wait(3)
        return base(argv, timeout=timeout, interactive=interactive)
    results = []
    def prepare(path):
        try:
            results.append(prepare_local_https(path, runner=slow, timeout=3))
        except Exception as exc:
            results.append(exc)
    first = threading.Thread(target=prepare, args=(alias,))
    second = threading.Thread(target=prepare, args=(actual,))
    first.start()
    assert entered.wait(3)
    second.start()
    release.set()
    first.join(3); second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2 and all(not isinstance(item, Exception) for item in results)
    assert len(generation_count) == 1
    assert alias.is_symlink()


def test_interactive_trust_output_is_visible(monkeypatch, tmp_path):
    from api_agent.runtime import https
    calls = []
    monkeypatch.setattr(https.shutil, 'which', lambda name: '/fake/' + name)
    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 2, None, None)
    monkeypatch.setattr(https.subprocess, 'run', fake_run)
    with pytest.raises(HTTPSPreparationError):
        prepare_local_https(tmp_path / '.certs', interactive=True)
    assert calls[0]['capture_output'] is False
    assert calls[0]['stdin'] is None


def test_dns_san_cannot_satisfy_ip_address(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    def wrong_type(argv, *, timeout, interactive):
        if '-text' in argv:
            return subprocess.CompletedProcess(argv, 0,
                certificate_text('DNS:localhost, DNS:localhost, DNS:127.0.0.1, DNS:::1'), '')
        return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='Generated certificate failed validation'):
        prepare_local_https(tmp_path / '.certs', runner=wrong_type)


def test_lock_wait_is_bounded(tmp_path):
    import fcntl
    root = tmp_path / 'ca'; root.mkdir()
    directory = tmp_path / '.certs'; directory.mkdir()
    lock = directory / '.https.lock'
    lock.touch(mode=0o600)
    with lock.open('r+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with pytest.raises(HTTPSPreparationError, match='Timed out waiting'):
            prepare_local_https(directory, runner=FakeRunner(root), timeout=0.01)


def test_installed_validation_failure_rolls_back(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=base)
    result.material.cert_file.write_text('prior certificate')
    result.material.key_file.write_text('prior key')
    class BadInstalled:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if self.generated and '-in' in argv and str(result.material.cert_file) in argv and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='Could not install'):
        prepare_local_https(directory, runner=BadInstalled())
    assert result.material.cert_file.read_text() == 'prior certificate'
    assert result.material.key_file.read_text() == 'prior key'


def test_unsafe_lock_file_rejected_before_commands(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'; directory.mkdir()
    (directory / '.https.lock').symlink_to(root / 'rootCA.pem')
    with pytest.raises(HTTPSPreparationError, match='symlink'):
        prepare_local_https(directory, runner=runner)
    assert runner.calls == []


def test_cancellation_during_replacement_restores_pair(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=base)
    result.material.cert_file.write_text('prior cert')
    result.material.key_file.write_text('prior key')
    old_replace = https.os.replace
    def interrupt_key(src, dst):
        if str(src).endswith('/key.pem') and Path(dst) == result.material.key_file:
            raise KeyboardInterrupt()
        return old_replace(src, dst)
    monkeypatch.setattr(https.os, 'replace', interrupt_key)
    class Expired:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(KeyboardInterrupt):
        prepare_local_https(directory, runner=Expired())
    assert result.material.cert_file.read_text() == 'prior cert'
    assert result.material.key_file.read_text() == 'prior key'


@pytest.mark.parametrize('failed_backup', ['cert', 'key'])
def test_backup_failure_keeps_untouched_original_pair(tmp_path, monkeypatch, failed_backup):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=base)
    result.material.cert_file.write_text('original cert')
    result.material.key_file.write_text('original key')
    old_replace = https.os.replace
    failed_source = result.material.cert_file if failed_backup == 'cert' else result.material.key_file
    def fail_backup(src, dst):
        if Path(src) == failed_source and Path(dst).name.startswith('old-'):
            raise OSError('injected backup failure')
        return old_replace(src, dst)
    monkeypatch.setattr(https.os, 'replace', fail_backup)
    class Expired:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='Could not install'):
        prepare_local_https(directory, runner=Expired())
    assert result.material.cert_file.read_text() == 'original cert'
    assert result.material.key_file.read_text() == 'original key'


@pytest.mark.parametrize('failed_restore', ['old-cert.pem', 'old-key.pem'])
def test_restore_failure_preserves_private_backup(tmp_path, monkeypatch, failed_restore):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    base = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=base)
    result.material.cert_file.write_text('recoverable old cert')
    result.material.key_file.write_text('recoverable old key')
    old_replace = https.os.replace
    def fail_install_and_restore(src, dst):
        source, target = Path(src), Path(dst)
        if source.name == 'key.pem' and target == result.material.key_file:
            raise OSError('injected install failure')
        if source.name == failed_restore and target in (result.material.cert_file, result.material.key_file):
            raise OSError('injected restore failure')
        return old_replace(src, dst)
    monkeypatch.setattr(https.os, 'replace', fail_install_and_restore)
    class Expired:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return base(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='recover backup files from ') as error:
        prepare_local_https(directory, runner=Expired())
    recovery = Path(str(error.value).split('recover backup files from ', 1)[1])
    assert recovery.is_dir()
    assert recovery.stat().st_mode & 0o077 == 0
    expected = 'recoverable old cert' if failed_restore == 'old-cert.pem' else 'recoverable old key'
    assert (recovery / failed_restore).read_text() == expected
    if failed_restore == 'old-key.pem':
        assert (recovery / failed_restore).stat().st_mode & 0o077 == 0
        assert result.material.cert_file.read_text() == 'recoverable old cert'
        assert not result.material.key_file.exists()
    else:
        assert result.material.key_file.read_text() == 'recoverable old key'
        assert not result.material.cert_file.exists()
    assert 'recoverable old cert' not in str(error.value)


def test_lock_with_broad_permissions_is_rejected_before_commands(tmp_path):
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'; directory.mkdir()
    lock = directory / '.https.lock'
    lock.touch(mode=0o600)
    lock.chmod(0o644)
    with pytest.raises(HTTPSPreparationError, match='Unsafe HTTPS lock file permissions'):
        prepare_local_https(directory, runner=runner)
    assert runner.calls == []


def test_lock_open_error_is_redacted_before_commands(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    original_open = https.os.open
    def denied(path, flags, mode=0o777):
        if Path(path).name == '.https.lock':
            raise PermissionError('private filesystem detail')
        return original_open(path, flags, mode)
    monkeypatch.setattr(https.os, 'open', denied)
    with pytest.raises(HTTPSPreparationError, match='Could not acquire local HTTPS preparation lock') as error:
        prepare_local_https(tmp_path / '.certs', runner=runner)
    assert 'private filesystem detail' not in str(error.value)
    assert runner.calls == []


def test_renewal_window_must_include_one_second(tmp_path):
    with pytest.raises(ValueError, match='renewal_days is too small'):
        prepare_local_https(tmp_path / '.certs', renewal_days=1 / 100000, runner=lambda *a, **k: pytest.fail('runner called'))


def test_failed_first_install_removes_new_certificate(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'
    original_replace = https.os.replace
    def fail_key_install(src, dst):
        if Path(src).name == 'key.pem' and Path(dst).name.endswith('-key.pem'):
            raise OSError('private install error')
        return original_replace(src, dst)
    monkeypatch.setattr(https.os, 'replace', fail_key_install)
    with pytest.raises(HTTPSPreparationError, match='Could not install') as error:
        prepare_local_https(directory, runner=runner)
    assert 'private install error' not in str(error.value)
    assert not (directory / 'localhost.pem').exists()
    assert not (directory / 'localhost-key.pem').exists()
    assert not list(directory.glob('.https-*'))


def test_failed_cleanup_preserves_backup_and_redacts_error(tmp_path, monkeypatch):
    from api_agent.runtime import https
    root = tmp_path / 'ca'; root.mkdir()
    runner = FakeRunner(root)
    directory = tmp_path / '.certs'
    result = prepare_local_https(directory, runner=runner)
    result.material.cert_file.write_text('old certificate')
    result.material.key_file.write_text('old key')
    original_replace = https.os.replace
    original_unlink = Path.unlink
    def fail_install_and_restore(src, dst):
        source, target = Path(src), Path(dst)
        if source.name == 'key.pem' and target == result.material.key_file:
            raise OSError('private install error')
        if source.name == 'old-cert.pem' and target == result.material.cert_file:
            raise OSError('private restore error')
        return original_replace(src, dst)
    def fail_cleanup(path, *args, **kwargs):
        if path == result.material.cert_file:
            raise OSError('private cleanup error')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(https.os, 'replace', fail_install_and_restore)
    monkeypatch.setattr(Path, 'unlink', fail_cleanup)
    class Expired:
        generated = False
        def __call__(self, argv, *, timeout, interactive):
            if argv[:2] == ['mkcert', '-cert-file']:
                self.generated = True
            if not self.generated and '-checkend' in argv:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return runner(argv, timeout=timeout, interactive=interactive)
    with pytest.raises(HTTPSPreparationError, match='recover backup files from ') as error:
        prepare_local_https(directory, runner=Expired())
    recovery = Path(str(error.value).split('recover backup files from ', 1)[1])
    assert (recovery / 'old-cert.pem').read_text() == 'old certificate'
    assert result.material.key_file.read_text() == 'old key'
    assert all(secret not in str(error.value) for secret in ('private install error', 'private restore error', 'private cleanup error'))


def test_lock_works_without_nofollow_flag(tmp_path, monkeypatch):
    from api_agent.runtime import https
    monkeypatch.delattr(https.os, 'O_NOFOLLOW', raising=False)
    with https._directory_lock(tmp_path, timeout=1):
        assert (tmp_path / '.https.lock').is_file()
