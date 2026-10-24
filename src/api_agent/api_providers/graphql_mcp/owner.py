"""Owned stdio bridge to the pinned mcp-graphql package.

The selected endpoint and headers are owner configuration. Model arguments contain
only a query; validation never sends a candidate to the MCP child.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any
from urllib.parse import urlsplit

from graphql import (
    FragmentDefinitionNode, OperationDefinitionNode, VariableNode,
    build_client_schema, get_introspection_query, parse, print_schema, validate,
)
from graphql.language import Visitor, visit
from graphql.language.ast import FieldNode, FragmentSpreadNode

from api_agent.providers.tools import PreparedCall, ToolResult, ToolSpec, ToolValidationError

MAX_QUERY = 8192
MAX_SCHEMA = 262144
MAX_SDL = 32768
MAX_RESULT = 262144
MAX_STDERR = 16384
MAX_DEPTH = 12
MAX_NODES = 400
_ERROR_PREFIX = "The GraphQL response has errors, please fix the query: "
_HTTP_PREFIX = "GraphQL request failed: "
_RENDERABLE_FAILURES = frozenset({
    "http_failure", "mcp_tool_error", "mcp_error", "protocol_output",
    "protocol_id", "mcp_content", "graphql_json", "graphql_shape",
    "mcp_envelope", "result_size", "result_depth", "result_secret_collision",
})
_ASSETS = Path(__file__).resolve().parent


def header_secret_values(key: str, value: str) -> frozenset[str]:
    """Credential spellings an HTTP peer may echo after header normalization."""
    if not isinstance(key, str) or not isinstance(value, str):
        return frozenset()
    trimmed = value.strip()
    if not trimmed:
        return frozenset()
    values = {value, trimmed}
    if key.lower() == "authorization":
        parts = trimmed.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "basic"}:
            token = parts[1].strip()
            values.update((parts[0] + " " + token, token))
    return frozenset(item for item in values if item)


class GraphQLMcpError(RuntimeError):
    """Sanitized, classified adapter failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class GraphQLQueryError(ToolValidationError):
    """Deterministic candidate rejection eligible for planning retry."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _QueryBounds(Visitor):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.max_depth = 0
        self.variable = False

    def enter(self, node, *_):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise GraphQLQueryError("query_complexity")
        if isinstance(node, VariableNode):
            self.variable = True
        if node.kind == "field":
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            if self.max_depth > MAX_DEPTH:
                raise GraphQLQueryError("query_depth")

    def leave(self, node, *_):
        if node.kind == "field":
            self.depth -= 1


class GraphQLMcpOwner:
    """One selected endpoint, one frozen schema and one owned MCP child."""

    def __init__(self, endpoint: str, *, headers: dict[str, str] | None = None,
                 root_fields: frozenset[str] | None = None,
                 node: str | None = None, assets_dir: Path | None = None, timeout: float = 10.0):
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise GraphQLMcpError("endpoint_invalid") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.fragment or parsed.query or
            port == 0):
            raise GraphQLMcpError("endpoint_invalid")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise GraphQLMcpError("timeout_invalid")
        headers = {} if headers is None else headers
        if (type(headers) is not dict or len(headers) > 16 or
            any(type(k) is not str or type(v) is not str or not k or
                len(k) > 128 or len(v) > 4096 or '\n' in k + v or '\r' in k + v
                for k, v in headers.items())):
            raise GraphQLMcpError("headers_invalid")
        self._endpoint = endpoint
        self._headers = {key: value.strip() for key, value in headers.items()}
        for key, value in self._headers.items():
            if key.lower() == "authorization":
                parts = value.split(None, 1)
                if len(parts) == 2 and parts[0].lower() in {"bearer", "basic"}:
                    self._headers[key] = parts[0] + " " + parts[1].strip()
        secrets = set()
        for key, value in headers.items():
            secrets.update(header_secret_values(key, value))
        self._secrets = tuple(sorted(secrets, key=len, reverse=True))
        if root_fields is not None and (type(root_fields) is not frozenset or
                                        not root_fields or any(type(x) is not str or not x for x in root_fields)):
            raise GraphQLMcpError("root_fields_invalid")
        self._root_fields = root_fields
        resolved_node = shutil.which(node or "node")
        if resolved_node is None:
            raise GraphQLMcpError("node_missing")
        self._node = str(Path(resolved_node).resolve())
        self._timeout = float(timeout)
        self._assets_dir = Path(assets_dir).resolve() if assets_dir is not None else _ASSETS
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 1
        self._schema = None
        self._spec = None
        self._lock = asyncio.Lock()
        self._describe_lock = asyncio.Lock()

    async def _start(self):
        if self._process is not None:
            return
        from .assets import installed_script
        script = installed_script(self._assets_dir)
        env = {k: v for k, v in os.environ.items() if k not in {"ENDPOINT", "HEADERS", "SCHEMA", "ALLOW_MUTATIONS"}}
        env.update(ENDPOINT=self._endpoint, HEADERS=json.dumps(self._headers), ALLOW_MUTATIONS="false")
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._node, "--max-old-space-size=128", str(script), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, limit=MAX_RESULT + 4096)
        except (OSError, ValueError) as exc:
            raise GraphQLMcpError("process_start") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "api-agent", "version": "1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await self._process.stdin.drain()
        except BaseException:
            await self.aclose()
            raise

    async def _drain_stderr(self):
        assert self._process and self._process.stderr
        total = 0
        while True:
            chunk = await self._process.stderr.read(4096)
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_STDERR:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                return
            # Intentionally discard: child diagnostics can contain credentials.

    def _send(self, message: dict):
        assert self._process and self._process.stdin
        self._process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    async def _rpc(self, method: str, params: dict) -> dict:
        assert self._process and self._process.stdout and self._process.stdin
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            await asyncio.wait_for(self._process.stdin.drain(), self._timeout)
            deadline = asyncio.get_running_loop().time() + self._timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                line = await asyncio.wait_for(self._process.stdout.readline(), max(remaining, 0))
                if not line or len(line) > MAX_RESULT:
                    raise GraphQLMcpError("protocol_output")
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise GraphQLMcpError("protocol_output") from exc
                if type(response) is not dict or response.get("jsonrpc") != "2.0":
                    raise GraphQLMcpError("protocol_output")
                if "id" not in response:  # permitted server notification
                    continue
                if response["id"] != request_id:
                    raise GraphQLMcpError("protocol_id")
                if "error" in response:
                    raise GraphQLMcpError("mcp_error")
                result = response.get("result")
                if type(result) is not dict:
                    raise GraphQLMcpError("protocol_output")
                return result
        except (TimeoutError, asyncio.LimitOverrunError) as exc:
            if isinstance(exc, asyncio.LimitOverrunError):
                raise GraphQLMcpError("protocol_output") from exc
            raise GraphQLMcpError("timeout") from exc

    async def _call(self, name: str, arguments: dict) -> dict:
        async with self._lock:
            try:
                await self._start()
                return await self._rpc("tools/call", {"name": name, "arguments": arguments})
            except BaseException:
                await self.aclose()
                raise

    @staticmethod
    def _text(result: dict) -> tuple[str, bool]:
        content = result.get("content")
        if (type(content) is not list or len(content) != 1 or
            type(content[0]) is not dict or content[0].get("type") != "text" or
            type(content[0].get("text")) is not str):
            raise GraphQLMcpError("mcp_content")
        value = content[0]["text"]
        if len(value.encode()) > MAX_RESULT:
            raise GraphQLMcpError("result_size")
        return value, result.get("isError") is True

    @staticmethod
    def _complete_introspection(data: dict) -> bool:
        """Check every field selected by our fixed query, including fragments."""
        document = parse(get_introspection_query())
        fragments = {item.name.value: item for item in document.definitions
                     if isinstance(item, FragmentDefinitionNode)}
        nullable = {"mutationType", "subscriptionType", "description", "fields",
                    "inputFields", "interfaces", "enumValues", "possibleTypes",
                    "defaultValue", "deprecationReason", "ofType", "name"}

        def check(value, selection_set):
            if type(value) is list:
                return all(check(item, selection_set) for item in value)
            if type(value) is not dict:
                return False
            for selection in selection_set.selections:
                if isinstance(selection, FragmentSpreadNode):
                    if not check(value, fragments[selection.name.value].selection_set):
                        return False
                elif isinstance(selection, FieldNode):  # pragma: no branch - fixed query uses fields and spreads
                    name = selection.name.value
                    key = selection.alias.value if selection.alias else name
                    if key not in value:
                        return False
                    item = value[key]
                    if item is None:
                        if name not in nullable:
                            return False
                    elif selection.selection_set and not check(item, selection.selection_set):
                        return False
            return True

        operation = next(item for item in document.definitions
                         if isinstance(item, OperationDefinitionNode))
        return check(data, operation.selection_set)

    async def describe(self) -> ToolSpec:
        if self._spec is not None:
            return self._spec
        async with self._describe_lock:
            if self._spec is not None:
                return self._spec
            # The package's SDL tool can omit introspection errors. The fixed trusted
            # query through MCP supplies the canonical, complete response instead.
            result = await self._call("query-graphql", {"query": get_introspection_query()})
            text, is_error = self._text(result)
            response = self._parse_json(text, is_error)
            if is_error or response.get("errors") or type(response.get("data")) is not dict:
                raise GraphQLMcpError("introspection_failed")
            if not self._complete_introspection(response["data"]):
                raise GraphQLMcpError("introspection_incomplete")
            try:
                schema = build_client_schema(response["data"])
                if schema.query_type is None:
                    raise ValueError("missing query root")
            except Exception as exc:
                raise GraphQLMcpError("introspection_incomplete") from exc
            self._schema = schema
            schema_sdl = self._scrub(print_schema(schema))
            if len(schema_sdl.encode()) > MAX_SDL:
                raise GraphQLMcpError("schema_size")
            fingerprint = hashlib.sha256(json.dumps(response["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self._spec = ToolSpec(name="graphql.query", description="One read-only GraphQL query; literals only; no variables", input_schema={"type": "object", "properties": {"query": {"type": "string", "maxLength": MAX_QUERY, "description": "Verified GraphQL schema:\n" + schema_sdl}}, "required": ["query"], "additionalProperties": False}, output_schema={}, schema_fingerprint=fingerprint)
            return self._spec

    def for_invocation(self) -> "GraphQLMcpOwner":
        """Fresh schema snapshot sharing this owner's bounded MCP transport."""
        return _InvocationView(self)

    @staticmethod
    def _parse_json(text: str, is_error: bool) -> dict:
        if text.startswith(_HTTP_PREFIX):
            raise GraphQLMcpError("http_failure")
        if is_error:
            if not text.startswith(_ERROR_PREFIX):
                raise GraphQLMcpError("mcp_tool_error")
            text = text[len(_ERROR_PREFIX):]
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise GraphQLMcpError("graphql_json") from exc
        if type(result) is not dict or ("data" not in result and "errors" not in result):
            raise GraphQLMcpError("graphql_shape")
        if "errors" in result and (type(result["errors"]) is not list or not result["errors"]):
            raise GraphQLMcpError("graphql_shape")
        if "data" in result and result["data"] is not None and type(result["data"]) is not dict:
            raise GraphQLMcpError("graphql_shape")
        if "errors" in result and any(type(error) is not dict for error in result["errors"]):
            raise GraphQLMcpError("graphql_shape")
        return result

    def _scrub(self, value: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise GraphQLMcpError("result_depth")
        if type(value) is str:
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if type(value) is list:
            return [self._scrub(item, depth + 1) for item in value]
        if type(value) is dict:
            result = {}
            for key, item in value.items():
                clean_key = self._scrub(key, depth + 1)
                if clean_key in result:
                    raise GraphQLMcpError("result_secret_collision")
                result[clean_key] = self._scrub(item, depth + 1)
            return result
        return value

    async def validate(self, prepared: PreparedCall) -> None:
        spec = await self.describe()
        if prepared.spec != spec or type(prepared.arguments) is not dict or set(prepared.arguments) != {"query"}:
            raise GraphQLQueryError("call_binding")
        query = prepared.arguments["query"]
        if type(query) is not str or not query or len(query.encode()) > MAX_QUERY:
            raise GraphQLQueryError("query_size")
        try:
            doc = parse(query)
        except Exception as exc:
            raise GraphQLQueryError("query_syntax") from exc
        operations = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
        if len(operations) != 1 or operations[0].operation.value != "query" or any(not isinstance(d, (OperationDefinitionNode, FragmentDefinitionNode)) for d in doc.definitions):
            raise GraphQLQueryError("query_operation")
        if operations[0].variable_definitions:
            raise GraphQLQueryError("query_variables")
        bounds = _QueryBounds()
        visit(doc, bounds)
        if bounds.variable:
            raise GraphQLQueryError("query_variables")
        if validate(self._schema, doc):
            raise GraphQLQueryError("query_schema")
        fragments = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}
        expanded = 0

        def check_selection(selection_set, depth, active):
            nonlocal expanded
            for selection in selection_set.selections:
                expanded += 1
                if expanded > MAX_NODES:
                    raise GraphQLQueryError("query_complexity")
                if isinstance(selection, FieldNode):
                    next_depth = depth + 1
                    if next_depth > MAX_DEPTH:
                        raise GraphQLQueryError("query_depth")
                    if depth == 0 and (selection.name.value.startswith("__") or
                                       (self._root_fields is not None and selection.name.value not in self._root_fields)):
                        raise GraphQLQueryError("query_root_field")
                    if selection.selection_set:
                        check_selection(selection.selection_set, next_depth, active)
                elif isinstance(selection, FragmentSpreadNode):
                    name = selection.name.value
                    if name in active:
                        raise GraphQLQueryError("query_fragment_cycle")
                    check_selection(fragments[name].selection_set, depth, active | {name})
                else:  # GraphQL selection AST is field, spread, or inline fragment.
                    check_selection(selection.selection_set, depth, active)

        check_selection(operations[0].selection_set, 0, frozenset())

    async def invoke(self, prepared: PreparedCall) -> ToolResult:
        await self.validate(prepared)
        try:
            result = await self._call("query-graphql", {"query": prepared.arguments["query"]})
            text, is_error = self._text(result)
            response = self._parse_json(text, is_error)
            data = self._scrub(response.get("data"))
            errors = tuple(self._scrub(response.get("errors", ())))
            if errors and not is_error:
                raise GraphQLMcpError("mcp_envelope")
            status = "partial" if errors and data is not None else "failure" if errors else "success"
            if status == "success" and data is None:
                raise GraphQLMcpError("graphql_shape")
            return ToolResult(status=status, data=data, errors=errors, diagnostic="graphql_errors" if errors else None)
        except GraphQLMcpError as exc:
            if exc.code not in _RENDERABLE_FAILURES:
                raise
            return ToolResult(status="failure", data=None, errors=(), diagnostic=exc.code)

    async def aclose(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        reaped = False
        drain_error = None
        try:
            await asyncio.wait_for(process.wait(), min(self._timeout, 3.0))
            reaped = True
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), 3.0)
                reaped = True
            except TimeoutError as exc:
                raise GraphQLMcpError("process_unreaped") from exc
        finally:
            if self._stderr_task:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    drain_error = exc
            if reaped:  # pragma: no branch - an unreaped child raises before the next statement
                self._process = None
                self._stderr_task = None
        if drain_error is not None:
            raise GraphQLMcpError("stderr_drain") from drain_error


class _InvocationView(GraphQLMcpOwner):
    def __init__(self, transport: GraphQLMcpOwner):
        super().__init__(transport._endpoint, headers=transport._headers,
                         root_fields=transport._root_fields, node=transport._node,
                         assets_dir=transport._assets_dir, timeout=transport._timeout)
        self._transport = transport

    async def _call(self, name: str, arguments: dict) -> dict:
        return await self._transport._call(name, arguments)

    async def aclose(self) -> None:
        # Runtime provider owns the shared child; a per-invocation view does not.
        return
