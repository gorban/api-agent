"""Source checkout entrypoint for packaged workflow-view commands."""

from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
_source = _root / "src"
if not (_source / "api_agent/workflows/viewer_build.py").is_file():
    raise SystemExit("source workflow-view package is missing")
sys.path.insert(0, str(_source))

from api_agent.workflows.viewer_build import check_views, generate_views, main, prepare_renderer


if __name__ == '__main__':
    raise SystemExit(main())
