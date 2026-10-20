# Workflow viewer QA

Run these tests explicitly after preparing the optional viewer prerequisites:

```sh
python -m api_agent.workflows.viewer_build prepare
python -m pytest tests/viewer_qa
```

The pinned PlantUML JAR and Java must be available. On headless Linux, install Xvfb (`xvfb-run`) or provide a working `DISPLAY`; macOS runs the headed browser directly. The process test also needs Playwright Chromium prepared by the viewer. `API_AGENT_PLANTUML_JAR` may point to an already verified pinned JAR. These tests exercise real browser and staged-index behavior and are outside ordinary integration-suite selection. They use no provider login or graph execution.
