"""Credential CLIs share the caller's terminal session; other owned helpers do not.

Password managers may remember an authorization per terminal session, so a new
session for every lookup prompts again. Each helper still leads its own process
group, which keeps group cleanup available.
"""
import os
import subprocess
import sys

import pytest

from api_agent.auth import credentials
from api_agent.auth.owned_process import OwnedProcess

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='process sessions are POSIX')

PROBE = 'import os; print(os.getsid(0), os.getpgid(0), os.getpid())'


def _scope(output):
    session, group, pid = map(int, output.split())
    return session, group == pid


def test_credential_lookup_keeps_the_callers_session_in_its_own_group():
    session, leads_group = _scope(credentials._run([sys.executable, '-c', PROBE], 30))
    assert session == os.getsid(0)
    assert leads_group


def test_owned_helpers_start_a_new_session_by_default():
    owned = OwnedProcess([sys.executable, '-c', PROBE], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE)
    try:
        line = owned.stdout.readline().decode()
    finally:
        owned.dispose()
        owned.wait(timeout=30)
        owned.stdout.close()
    session, leads_group = _scope(line)
    assert session != os.getsid(0)
    assert leads_group
