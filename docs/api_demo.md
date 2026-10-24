# Bundled API demonstration

The bundled resource pack selects [its declared flow](../demo/simple/flow.md.html):
plan a response, check the plan, optionally execute a verified tool, and answer
using the resulting evidence. The separate [root flow](../flow.md.html) remains
a one-call chat example. The permanent qualification fixture is unchanged.

Start `./launch.py`, review configuration and choose **Run local**. Set
`RUN_UI=True` for Studio. Try “List the items available from the API.” A simple
greeting can take the no-tool path. Planning, review and the final response use
the selected LLM provider/model and may consume provider usage even when no tool
is needed. Invalid or rejected plans have a bounded retry budget; correctness,
grounding and relevance remain required on the final pass.

## API selection and setup

For this demo, an omitted or blank `API_ENDPOINT` starts a read-only synthetic
GraphQL API through the managed Compose infrastructure. It contains three
fictional items and needs no database. Its loopback port is **58092**; a conflicting
listener prevents startup. Stop Run local to release its owned services. Existing
managed-container and volume retention policies still apply.

A nonempty `API_ENDPOINT`, including a resource-pack default, selects that
endpoint instead and does not start the synthetic service. `API_HEADERS` accepts
a JSON object of fixed request headers. Keep credentials in personal
configuration rather than tracked files. The endpoint is the complete GraphQL
URL, such as `http://127.0.0.1:58092/graphql`. External resource packs retain their
own endpoint and infrastructure policy; they do not inherit this fallback.

The default API provider uses pinned `mcp-graphql@2.0.4` through an owned Node.js
stdio process. Node.js and npm must be available on `PATH`. Explicit provider
setup prepares the locked dependencies under the ignored `.api-agent/` directory;
the first preparation needs registry access. Discovery and execution do not
install packages. The shipped lock and installed asset tree are verified before
use. License notices are retained with the provider.

## Verification and limits

This first adapter supports one read-only query without variables. It obtains
the schema through MCP, validates the proposed query deterministically, then
requires an independent LLM relevance/grounding check before executing it once.
Mutation, subscription and variable requests fail before execution. A different
tool owner can expose another input contract; the workflow consumer is generic.

GraphQL responses containing both data and errors retain both for the final
answer. Transport failures remain failures rather than successful empty data.
Credentials are excluded independently of log/trace content settings. Tool
observations record timing and outcome without inventing LLM token usage.

Schema, query, result, timeout and retained-output bounds apply. The Node child
has a 128 MiB old-space limit, which is not a total process-memory limit: the
published MCP package parses upstream bodies before the parent can bound its
retained output. Use trusted endpoints. The synthetic server limits concurrent
workers and idle time, but its idle timeout is not an absolute connection lifetime.

The compiled-graph regression uses controlled LLM/tool owners, so it can check
guard and execution ordering without spending tokens. Human testing of the real
selected provider remains a separate acceptance check. See the
[requirements](prds/implementation_plan_o3.md) and [test controls](development.md#testing).
