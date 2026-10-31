"""Portable certificate inspection without changing host trust."""
import os
import subprocess

import pytest

from api_agent.runtime import https
from test_https import FakeRunner, certificate_text

NAMES = 'DNS:localhost, IP Address:127.0.0.1, IP Address:::1'


@pytest.mark.parametrize('critical', [False, True])
def test_portable_certificate_text_generates_and_reuses_material(tmp_path, critical):
    ca = tmp_path / 'ca'; ca.mkdir()
    base = FakeRunner(ca)

    def runner(argv, **kwargs):
        if '-ext' in argv:
            return subprocess.CompletedProcess(argv, 1, '', 'unknown option -ext')
        result = base(argv, **kwargs)
        if '-text' in argv and critical:
            result.stdout = result.stdout.replace('Alternative Name:', 'Alternative Name: critical')
        return result

    directory = tmp_path / 'certs'
    first = https.prepare_local_https(directory, runner=runner)
    again = https.prepare_local_https(directory, runner=runner)
    assert first.material == again.material
    assert again.diagnostics[0] == 'Local certificate reused.'
    assert len([argv for argv, _, _ in base.calls if '-cert-file' in argv]) == 1
    assert len([argv for argv, _, _ in base.calls if '-text' in argv]) == 3
    assert all('-ext' not in argv for argv, _, _ in base.calls)


@pytest.mark.parametrize('text', [
    '',
    'Subject: DNS:localhost\n',
    '        X509v3 extensions:\n    Signature Algorithm: sha256\n',
    '        X509v3 extensions:\n',
    certificate_text(NAMES) + certificate_text(NAMES),
    certificate_text(NAMES).replace('Subject Alternative Name', 'Basic Constraints'),
    certificate_text(NAMES).replace('    Signature Algorithm:',
        '            X509v3 Subject Alternative Name:\n                ' + NAMES + '\n    Signature Algorithm:'),
    certificate_text(NAMES).replace('            X509v3 Subject Alternative Name:',
        '            X509v3 Basic Constraints:\n                X509v3 Subject Alternative Name:'),
    certificate_text('').replace('                \n', ''),
])
def test_missing_ambiguous_or_nested_san_is_not_hostname_evidence(text):
    assert https._san_extension(text) == ''


def test_only_san_values_are_read_across_wrapped_lines_and_extensions():
    text = ('Subject: DNS:outside.example\n'
            '        X509v3 extensions:\n'
            '            X509v3 Basic Constraints: critical\n                CA:FALSE\n'
            '            X509v3 Subject Alternative Name:\n'
            '                DNS:localhost,\n\n                IP Address:127.0.0.1\n'
            '            X509v3 Extended Key Usage:\n                DNS:outside.example\n')
    assert https._san_extension(text) == 'DNS:localhost,\nIP Address:127.0.0.1'


def test_san_at_end_of_text_is_read():
    text = certificate_text(NAMES).split('    Signature Algorithm:')[0]
    assert https._san_extension(text) == NAMES


@pytest.mark.parametrize('operation, stage', [('-checkend', 'expiry'), ('-text', 'subject alternative names'),
    ('-pubkey', 'key pair'), ('pkey', 'key pair'), ('verify', 'CA chain')])
def test_failed_generated_validation_reports_only_its_fixed_stage(tmp_path, operation, stage):
    ca = tmp_path / 'ca'; ca.mkdir()
    base = FakeRunner(ca)

    def runner(argv, **kwargs):
        if argv[0] == 'openssl' and operation in argv:
            return subprocess.CompletedProcess(argv, 1, '', 'private diagnostic content')
        return base(argv, **kwargs)

    with pytest.raises(https.HTTPSPreparationError, match=f'Generated certificate failed validation \\({stage}\\)') as caught:
        https.prepare_local_https(tmp_path / 'certs', runner=runner)
    assert 'private diagnostic content' not in str(caught.value)
    assert not (tmp_path / 'certs/localhost.pem').exists()
    assert not (tmp_path / 'certs/localhost-key.pem').exists()


def test_managed_mkcert_preparation_precedes_system_executable(tmp_path, monkeypatch):
    from api_agent.runtime import mise_tools
    old = tmp_path / 'system'; old.mkdir()
    managed = tmp_path / 'managed'; managed.mkdir()
    marker = tmp_path / 'selected'
    for folder in (old, managed):
        script = folder / 'mkcert'
        script.write_text('#!/bin/sh\nprintf %s ' + folder.name + ' > "$HTTPS_TEST_MARKER"\nexit 2\n')
        script.chmod(0o700)
    monkeypatch.setenv('HTTPS_TEST_MARKER', str(marker))
    which = https.shutil.which
    monkeypatch.setattr(https.shutil, 'which', lambda name: '/fake/openssl' if name == 'openssl' else which(name))
    monkeypatch.setenv('PATH', str(old) + os.pathsep + os.environ.get('PATH', ''))
    calls = []

    def prepare(*names, timeout):
        calls.append((names, timeout))
        monkeypatch.setenv('PATH', str(managed) + os.pathsep + os.environ['PATH'])

    monkeypatch.setattr(mise_tools, 'ensure_tools', prepare)
    with pytest.raises(https.HTTPSPreparationError, match='mkcert trust setup failed'):
        https.prepare_local_https(tmp_path / 'certs', timeout=3, interactive=False)
    assert calls == [(('mkcert',), 3)]
    assert marker.read_text() == 'managed'


@pytest.mark.parametrize('names', [
    'URI:DNS:localhost, URI:IP Address:127.0.0.1, URI:IP Address:::1',
    'URI:ignored,DNS:localhost,IP Address:127.0.0.1,IP Address:::1',
    'IP Address:localhost, IP Address:127.0.0.1, IP Address:::1',
    'DNS:localhost, IP Address:not-an-address, IP Address:::1',
])
def test_unrecognized_or_malformed_general_names_cannot_supply_host_identities(tmp_path, names):
    ca = tmp_path / 'ca'; ca.mkdir()
    base = FakeRunner(ca)
    def runner(argv, **kwargs):
        if '-text' in argv:
            return subprocess.CompletedProcess(argv, 0, certificate_text(names), '')
        return base(argv, **kwargs)
    with pytest.raises(https.HTTPSPreparationError, match='subject alternative names'):
        https.prepare_local_https(tmp_path / 'certs', runner=runner)
    assert not (tmp_path / 'certs/localhost.pem').exists()
