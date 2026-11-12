"""Install the pinned Node runtime from the checksums recorded in mise.lock."""
import hashlib
from pathlib import Path
import platform
import sys
import tarfile
import tempfile
import tomllib
import urllib.request


_DOWNLOAD_TIMEOUT_SECONDS = 120
_PLATFORMS = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64'}


def locked_node(lock, machine):
    """Return the locked (url, sha256) for a Linux machine architecture."""
    try:
        target = _PLATFORMS[machine]
    except KeyError:
        raise RuntimeError(f'no locked Node runtime for machine {machine}') from None
    entry = tomllib.loads(Path(lock).read_text())['tools']['node'][0]['platforms.' + target]
    algorithm, _, digest = entry['checksum'].partition(':')
    if algorithm != 'sha256':
        raise RuntimeError(f'unsupported Node runtime checksum algorithm: {algorithm}')
    return entry['url'], digest


def install(lock, destination, machine=None):
    """Download, verify, and unpack the locked Node runtime into ``destination``."""
    url, digest = locked_node(lock, machine or platform.machine())
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != digest:
        raise RuntimeError('downloaded Node runtime does not match its locked checksum')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as spool:
        spool.write(body)
        spool.seek(0)
        with tarfile.open(fileobj=spool) as archive:
            for member in archive.getmembers():
                # Drop the tarball's single versioned top directory.
                member.name = member.name.partition('/')[2]
                if member.name:
                    archive.extract(member, destination, filter='tar')
    return destination / 'bin' / 'node'


def main(arguments):
    """Install into the destination named by the second argument."""
    lock, destination = arguments
    print(install(lock, destination))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
