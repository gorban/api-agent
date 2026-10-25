"""The synthetic managed server handles the engine's normal termination signal."""
import json
import os
import selectors
import signal
import subprocess
import sys
import time

import pytest
from urllib.request import urlopen

from tests.unit.test_tool_fixture import FIXTURE


def read_ready_port(process, timeout=3):
    deadline = time.monotonic() + timeout
    announced = b''
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while b'\n' not in announced:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError('Fixture startup did not announce a ready port')
            fragment = os.read(process.stdout.fileno(), 32 - len(announced))
            if not fragment or len(announced) + len(fragment) >= 32:
                raise ValueError('Fixture startup announced an invalid port')
            announced += fragment
    return int(announced)


def test_ready_fixture_process_exits_cleanly_on_sigterm(tmp_path):
    program = '''
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location('synthetic_fixture', sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
server = fixture.serve(role='api', root=Path(sys.argv[2]), port=0,
                       schema_path=Path(sys.argv[1]).with_name('schema.json'))
print(server.server_port, flush=True)
fixture.run(server)
'''
    process = subprocess.Popen((sys.executable, '-u', '-c', program,
        str(FIXTURE / 'server.py'), str(tmp_path / 'data')),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        port = read_ready_port(process)
        with urlopen(f'http://127.0.0.1:{port}/ready', timeout=3) as response:
            assert json.load(response) == {'service': 'api'}

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)

        assert process.returncode == 0, stderr
        assert stdout == ''
        seed = json.loads((tmp_path / 'data/seed.json').read_text())
        assert seed['seed'] == 'done' and seed['runs'] == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)


@pytest.mark.parametrize('partial', ['', '58'])
def test_fixture_startup_wait_is_bounded_for_silent_or_partial_output(partial):
    program = 'import sys,time; sys.stdout.write(sys.argv[1]); sys.stdout.flush(); time.sleep(60)'
    process = subprocess.Popen((sys.executable, '-u', '-c', program, partial),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        with pytest.raises(TimeoutError, match='did not announce a ready port'):
            read_ready_port(process, timeout=.1)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
