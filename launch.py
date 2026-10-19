#!/usr/bin/env python3
"""Launch this checkout's shared foundation independently of the working directory."""
from pathlib import Path
import sys

BASE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_ROOT / 'src'))

if __name__ == '__main__':
    from api_agent.runtime.bootstrap import main
    raise SystemExit(main(base_root=BASE_ROOT, _replace_process=True))
