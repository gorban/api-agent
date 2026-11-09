# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a AS uv
FROM python@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS compile

COPY --from=uv /uv /uvx /bin/
WORKDIR /build
COPY pyproject.toml uv.lock LICENSE ./
COPY scripts/build_production_wheel.py scripts/build_production_wheel.py
COPY src src
COPY cfg cfg
COPY workflows/contracts workflows/contracts
COPY demo/simple/functions.py demo/simple/functions.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra tracing --no-editable --no-install-project && \
    python scripts/build_production_wheel.py && \
    uv pip install --python .venv/bin/python --no-deps dist-production/api_agent_runtime-*.whl && \
    find .venv -type d \( -name tests -o -name testing -o -name _tests -o -name __pycache__ \) -prune -exec rm -rf {} + && \
    find .venv -type f \( -name 'test_*.py' -o -name 'test_*.pyi' -o -name '*_support.pyi' -o -name '*.svg' \) -delete

FROM python@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS runtime
ENV PATH=/opt/api-agent/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_AGENT_STATE_DIR=/var/lib/api-agent
WORKDIR /opt/api-agent
COPY --from=compile /build/.venv /opt/api-agent/venv
COPY LICENSE /opt/api-agent/LICENSE
COPY demo/.private_resources/plans/useful_api_demo.json /opt/api-agent/plans/useful_api_demo.json
RUN groupadd --gid 10001 apiagent && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /var/lib/api-agent apiagent && \
    chown -R apiagent:apiagent /opt/api-agent /var/lib/api-agent
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "api_agent.runtime.production"]
