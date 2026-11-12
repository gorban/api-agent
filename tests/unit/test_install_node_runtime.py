"""The image's Node runtime comes from the checksums locked in mise.lock."""
import hashlib
import io
from pathlib import Path
import runpy
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/install_node_runtime.py'


def _tarball():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, body in (('node-v1/bin/node', b'#!node\n'), ('node-v1/LICENSE', b'license')):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o755
            archive.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo('node-v1/bin/npm')
        link.type, link.linkname = tarfile.SYMTYPE, '../lib/npm-cli.js'
        archive.addfile(link)
    return buffer.getvalue()


def _lock(path, digest, url='https://example.test/node.tar.gz'):
    path.write_text(f'''[[tools.node]]
version = "1"

[tools.node."platforms.linux-x64"]
checksum = "sha256:{digest}"
url = "{url}"

[tools.node."platforms.linux-arm64"]
checksum = "md5:abc"
url = "{url}"
''')
    return path


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.fixture
def downloads(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    requested = []

    def serve(body):
        def urlopen(url, timeout):
            requested.append((url, timeout))
            return _Response(body)
        monkeypatch.setattr(module['urllib'].request, 'urlopen', urlopen)
        return module, requested
    return serve


@pytest.mark.parametrize('machine, platform', [('x86_64', 'linux-x64'), ('aarch64', 'linux-arm64')])
def test_the_repository_lock_names_a_checksummed_runtime_for_each_image_architecture(machine, platform):
    module = runpy.run_path(str(SCRIPT))

    url, digest = module['locked_node'](ROOT / 'mise.lock', machine)

    assert url.startswith('https://nodejs.org/dist/') and platform in url
    assert len(digest) == 64


def test_an_architecture_without_a_locked_runtime_is_rejected(tmp_path):
    module = runpy.run_path(str(SCRIPT))

    with pytest.raises(RuntimeError, match='no locked Node runtime for machine riscv64'):
        module['locked_node'](_lock(tmp_path / 'mise.lock', '0'), 'riscv64')


def test_a_non_sha256_lock_checksum_is_rejected(tmp_path):
    module = runpy.run_path(str(SCRIPT))

    with pytest.raises(RuntimeError, match='unsupported Node runtime checksum algorithm: md5'):
        module['locked_node'](_lock(tmp_path / 'mise.lock', '0'), 'aarch64')


def test_install_unpacks_the_verified_tarball_without_its_versioned_top_directory(tmp_path, downloads):
    body = _tarball()
    module, requested = downloads(body)
    lock = _lock(tmp_path / 'mise.lock', hashlib.sha256(body).hexdigest())

    node = module['install'](lock, tmp_path / 'node', 'x86_64')

    assert node == tmp_path / 'node/bin/node'
    assert node.read_bytes() == b'#!node\n'
    assert (tmp_path / 'node/LICENSE').read_bytes() == b'license'
    assert (tmp_path / 'node/bin/npm').is_symlink()
    assert requested == [('https://example.test/node.tar.gz', 120)]


def test_install_rejects_a_download_that_does_not_match_the_locked_checksum(tmp_path, downloads):
    module, _ = downloads(_tarball())
    lock = _lock(tmp_path / 'mise.lock', '0' * 64)

    with pytest.raises(RuntimeError, match='locked checksum'):
        module['install'](lock, tmp_path / 'node', 'x86_64')

    assert not (tmp_path / 'node').exists()


def test_install_defaults_to_the_host_architecture(tmp_path, downloads, monkeypatch):
    body = _tarball()
    module, _ = downloads(body)
    monkeypatch.setattr(module['platform'], 'machine', lambda: 'x86_64')
    lock = _lock(tmp_path / 'mise.lock', hashlib.sha256(body).hexdigest())

    assert module['install'](lock, tmp_path / 'node') == tmp_path / 'node/bin/node'


def test_the_script_installs_the_destination_named_on_its_command_line(tmp_path, downloads, monkeypatch, capsys):
    body = _tarball()
    module, _ = downloads(body)
    monkeypatch.setattr(module['platform'], 'machine', lambda: 'x86_64')
    lock = _lock(tmp_path / 'mise.lock', hashlib.sha256(body).hexdigest())

    assert module['main']([str(lock), str(tmp_path / 'node')]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / 'node/bin/node')


def test_running_the_script_exits_with_its_result(tmp_path, downloads, monkeypatch):
    body = _tarball()
    downloads(body)
    lock = _lock(tmp_path / 'mise.lock', hashlib.sha256(body).hexdigest())
    monkeypatch.setattr('sys.argv', [str(SCRIPT), str(lock), str(tmp_path / 'node')])
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('urllib.request.urlopen', lambda url, timeout: _Response(body))

    with pytest.raises(SystemExit) as outcome:
        runpy.run_path(str(SCRIPT), run_name='__main__')

    assert outcome.value.code == 0
