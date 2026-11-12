# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a AS uv
FROM python@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS compile

COPY --from=uv /uv /uvx /bin/
WORKDIR /build
COPY pyproject.toml uv.lock LICENSE mise.lock ./
COPY scripts/build_production_wheel.py scripts/install_node_runtime.py scripts/
COPY src src
COPY cfg cfg
COPY workflows/contracts workflows/contracts
COPY demo/simple/functions.py demo/simple/functions.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra tracing --extra bedrock --extra graphql --no-editable --no-install-project && \
    python scripts/build_production_wheel.py && \
    uv pip install --python .venv/bin/python --no-deps dist-production/api_agent-*.whl && \
    python scripts/install_node_runtime.py mise.lock /opt/node && \
    PATH=/opt/node/bin:$PATH .venv/bin/python -c "import asyncio,sys; from api_agent.api_providers.graphql_mcp import prepare_assets; asyncio.run(prepare_assets(sys.argv[1], npm='/opt/node/bin/npm'))" /build/graphql-mcp && \
    find .venv -type d \( -name tests -o -name testing -o -name _tests -o -name __pycache__ \) -prune -exec rm -rf {} + && \
    find .venv -type f \( -name 'test_*.py' -o -name 'test_*.pyi' -o -name '*_support.pyi' -o -name '*.svg' \) -delete

FROM python@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39 AS runtime
ENV PATH=/opt/api-agent/venv/bin:/opt/api-agent/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_AGENT_STATE_DIR=/var/lib/api-agent \
    GRAPHQL_MCP_ASSETS_DIR=/opt/api-agent/graphql-mcp
WORKDIR /opt/api-agent
COPY --from=compile --chown=10001:10001 /build/.venv /opt/api-agent/venv
COPY --from=compile --chown=10001:10001 /opt/node/bin/node /opt/api-agent/bin/node
# The built-in GraphQL MCP provider reads its prepared Node assets from
# GRAPHQL_MCP_ASSETS_DIR, whatever directory holds the plan.
COPY --from=compile --chown=10001:10001 /build/graphql-mcp /opt/api-agent/graphql-mcp
COPY --chown=10001:10001 LICENSE /opt/api-agent/LICENSE
COPY --chown=10001:10001 demo/.private_resources/plans/useful_api_demo.json /opt/api-agent/plans/useful_api_demo.json
RUN groupadd --gid 10001 apiagent && \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /var/lib/api-agent apiagent && \
    chown apiagent:apiagent /opt/api-agent /var/lib/api-agent
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "api_agent.runtime.production"]
