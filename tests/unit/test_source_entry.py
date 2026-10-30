"""The actual internal source entry delegates to the established bootstrap."""
import os
from pathlib import Path
import runpy
import sys

import pytest

from api_agent.runtime import bootstrap


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('caller', [False, True])
@pytest.mark.parametrize('script', [False, True])
def test_entry_delegates_and_restores_caller_directory(tmp_path, monkeypatch, caller, script):
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.chdir(ROOT)
    if caller:
        monkeypatch.setenv('API_AGENT_CALLER_CWD', str(tmp_path))
    else:
        monkeypatch.delenv('API_AGENT_CALLER_CWD', raising=False)
    calls = []
    monkeypatch.setattr(bootstrap, 'main', lambda **kwargs: calls.append(kwargs) or 23)
    entry = str(ROOT / 'src/api_agent/runtime/entry.py')
    if script:
        with pytest.raises(SystemExit) as result:
            runpy.run_path(entry, run_name='__main__')
        assert result.value.code == 23
    else:
        namespace = runpy.run_path(entry)
        assert calls == []
        assert namespace['run']() == 23
    assert calls == [{'base_root': ROOT, '_replace_process': True}]
    assert Path.cwd() == (tmp_path if caller else ROOT)
    assert 'API_AGENT_CALLER_CWD' not in os.environ
