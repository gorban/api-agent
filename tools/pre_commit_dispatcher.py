"""Stable index-snapshot pre-commit dispatcher. Copied to an owned Git hook."""
from pathlib import Path
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

IDENTITY = '# api-agent managed pre-commit dispatcher v1'
OPERATION_SECONDS = 180
INDEX_BYTES = 8 * 1024 * 1024
MAX_FILES = 10_000
BLOB_BYTES = 8 * 1024 * 1024
TOTAL_BLOB_BYTES = 64 * 1024 * 1024
CHECKER_OUTPUT_BYTES = 1024 * 1024


def _stop(process):
    # The leader may have exited while a child still owns our pipes. It stays
    # unreaped until wait(), so its PID still safely identifies our group.
    _kill_group(process.pid)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _kill_group(group):
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _finish(process, deadline):
    # WNOWAIT observes the leader without reaping it. Its reserved PID keeps
    # the owned process-group identity safe while we stop stray descendants.
    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Index check command timed out')
        time.sleep(min(.01, remaining))
    _kill_group(process.pid)
    return process.wait(timeout=max(0, deadline - time.monotonic()))


def _capture(command, *, cwd, env, deadline, seconds, output_limit):
    """Capture bounded output and kill the owned process group on failure."""
    if time.monotonic() >= deadline:
        raise TimeoutError('Index check deadline exceeded')
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    limit = time.monotonic() + seconds
    selector = selectors.DefaultSelector()
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    try:
        for pipe in output:
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            remaining = min(deadline, limit) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Index check command timed out')
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.fileobj].extend(chunk)
                if sum(map(len, output.values())) > output_limit:
                    raise ValueError('Index check command output exceeds limit')
        code = _finish(process, min(deadline, limit))
        return code, bytes(output[process.stdout]), bytes(output[process.stderr])
    except BaseException:
        _stop(process)
        raise
    finally:
        selector.close()
        for pipe in output:
            pipe.close()


def _git(root, environment, deadline, limit, *arguments):
    code, out, err = _capture(['git', *arguments], cwd=root, env=environment,
                              deadline=deadline, seconds=10, output_limit=limit)
    if code:
        raise ValueError(f'Git index command failed: {err[:512].decode(errors="replace")}')
    return out


def run(argv=None):
    root = Path.cwd()
    deadline = time.monotonic() + OPERATION_SECONDS
    try:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('GIT_')}
        actual = _git(root, environment, deadline, 4096, 'rev-parse', '--show-toplevel').decode().strip()
        root = Path(actual).resolve()
        git_dir = Path(_git(root, environment, deadline, 4096, 'rev-parse', '--absolute-git-dir').decode().strip()).resolve()
        selected_index = os.environ.get('GIT_INDEX_FILE')
        if selected_index:
            index = Path(selected_index)
            index = (index if index.is_absolute() else root / index).resolve()
            if not index.is_relative_to(git_dir) or not index.is_file():
                raise ValueError('Temporary Git index is outside the selected checkout')
            environment['GIT_INDEX_FILE'] = str(index)
        entries = _git(root, environment, deadline, INDEX_BYTES, 'ls-files', '--stage', '-z')
        with tempfile.TemporaryDirectory(prefix='api-agent-index-') as temporary:
            snapshot = Path(temporary)
            seen = set()
            total = 0
            for record in entries.split(b'\0'):
                if not record:
                    continue
                if len(seen) >= MAX_FILES or time.monotonic() >= deadline:
                    raise ValueError('Index snapshot exceeds file or time limit')
                metadata, separator, encoded = record.partition(b'\t')
                if not separator:
                    raise ValueError('Malformed index entry')
                mode, blob, stage = metadata.decode('ascii').split()
                path = Path(os.fsdecode(encoded))
                if (stage != '0' or mode not in ('100644', '100755') or path.is_absolute()
                        or any(part in ('', '.', '..') for part in path.parts)
                        or '\\' in str(path) or path in seen):
                    raise ValueError('Unsafe or conflicted index entry')
                seen.add(path)
                length = _git(root, environment, deadline, 64, 'cat-file', '-s', blob)
                if not length.strip().isdigit():
                    raise ValueError('Invalid staged blob size')
                size = int(length.strip())
                if size > BLOB_BYTES or total + size > TOTAL_BLOB_BYTES:
                    raise ValueError('Index snapshot exceeds blob or aggregate limit')
                data = _git(root, environment, deadline, size + 512, 'cat-file', 'blob', blob)
                if len(data) != size:
                    raise ValueError('Staged blob size changed')
                total += size
                target = snapshot / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(int(mode, 8) & 0o777)
            checker = snapshot / 'tools/checks.py'
            if not checker.is_file():
                raise ValueError('Staged tools/checks.py is missing')
            prepared = root / '.venv/bin/python'
            interpreter = prepared if prepared.is_file() else Path(sys.executable)
            code, version, _ = _capture([str(interpreter), '-c',
                                         'import sys; print(sys.version_info.major, sys.version_info.minor)'],
                                        cwd=snapshot, env=environment, deadline=deadline,
                                        seconds=5, output_limit=128)
            if code or tuple(map(int, version.split())) < (3, 13):
                raise ValueError('Workflow checks require Python 3.13 or newer')
            code, out, err = _capture([str(interpreter), '-I', str(checker), '--no-git', '--root', str(snapshot)],
                                      cwd=snapshot, env=environment, deadline=deadline,
                                      seconds=OPERATION_SECONDS, output_limit=CHECKER_OUTPUT_BYTES)
            sys.stdout.write(out.decode(errors='replace'))
            sys.stderr.write(err.decode(errors='replace'))
            return code
    except (OSError, ValueError, subprocess.SubprocessError, TimeoutError) as error:
        print(f'Index checks unavailable: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(run())
