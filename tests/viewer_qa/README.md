# Workflow viewer QA

Run these tests explicitly with the optional viewer prerequisites available:

```sh
API_AGENT_MISE=1 python -m pytest tests/viewer_qa
```

The pinned PlantUML JAR and Java must be available. With `API_AGENT_MISE=1` the tests install both lazily from the project `mise.toml`, as `./launch.sh` does; otherwise set `API_AGENT_PLANTUML_JAR` to a verified pinned JAR and put Java on `PATH`. On headless Linux, install Xvfb (`xvfb-run`) or provide a working `DISPLAY`; macOS runs the headed browser directly. The process test also needs Playwright Chromium prepared by the viewer. These tests exercise real browser and staged-index behavior and are outside ordinary integration-suite selection. They use no provider login or graph execution.
