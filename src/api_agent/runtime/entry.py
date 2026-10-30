"""Source entry used by launch.sh; application startup remains in bootstrap."""
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))


def run():
    caller = os.environ.pop('API_AGENT_CALLER_CWD', None)
    if caller is not None:
        os.chdir(caller)
        os.environ['PWD'] = caller
    from api_agent.runtime.bootstrap import main
    return main(base_root=ROOT, _replace_process=True)


if __name__ == '__main__':
    raise SystemExit(run())
