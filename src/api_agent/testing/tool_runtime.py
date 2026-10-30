"""Run admitted test tools through the existing Runtime and infrastructure owners."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import Event

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout
from api_agent.runtime.tasks import offload

MAX_PACKET_BYTES = 64 * 1024 * 1024
_PACKET = 'tool-plan.json'


def write_tool_packet(directory, plan, *, context=None):
    """Write only the parent's captured closure into its private worker directory."""
    sources = {name: {'source': item.source, 'explicit': item.explicit}
               for name, item in getattr(plan.config, 'fields', {}).items()}
    packet = {'version': 1, 'plan': plan.to_wire(), 'sources': sources}
    if context is not None:
        from api_agent.runtime.inventory import capture_inventory
        packet['inventory'] = capture_inventory(context, plan).to_wire()
    path = Path(directory) / _PACKET
    payload = json.dumps(packet, allow_nan=False, separators=(',', ':')).encode()
    if len(payload) > MAX_PACKET_BYTES:
        raise ContractError('Selected tool context exceeds its worker budget.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(payload)
    return {'file': _PACKET, 'sha256': sha256(payload).hexdigest()}


def read_tool_packet(context_file, descriptor, *, inventory_context=None):
    """Restore a launch-owned snapshot without expanding its executable closure."""
    from api_agent.tooling.models import ToolPlan
    if (type(descriptor) is not dict or set(descriptor) != {'file', 'sha256'}
            or descriptor['file'] != _PACKET or type(descriptor['sha256']) is not str
            or not re.fullmatch('[0-9a-f]{64}', descriptor['sha256'])):
        raise ContractError('Invalid selected tool context.')
    path = Path(context_file).parent / _PACKET
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_PACKET_BYTES):
            raise ContractError('Invalid selected tool context.')
        body = stream.read(MAX_PACKET_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(body) > MAX_PACKET_BYTES or sha256(body).hexdigest() != descriptor['sha256']
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise ContractError('Selected tool context changed after launch.')
    packet = json.loads(body)
    if (type(packet) is not dict or set(packet) not in ({'version', 'plan', 'sources'}, {'version', 'plan', 'sources', 'inventory'})
            or type(packet['version']) is not int or packet['version'] != 1
            or type(packet['sources']) is not dict):
        raise ContractError('Invalid selected tool context.')
    for name, source in packet['sources'].items():
        if (type(name) is not str or type(source) is not dict
                or set(source) != {'source', 'explicit'} or type(source['source']) is not str
                or type(source['explicit']) is not bool):
            raise ContractError('Invalid selected tool provenance.')
    plan = ToolPlan.from_wire(packet['plan'])
    if inventory_context is not None:
        from api_agent.runtime.inventory import InventoryCatalog
        from api_agent.runtime.ownership import StackScope
        scope = StackScope.from_roots(inventory_context['base_root'], inventory_context['pack_root'])
        catalog = InventoryCatalog.from_wire(packet.get('inventory'), scope=scope,
            authority=inventory_context.get('inventory_authority'))
        catalog.verify_plan(plan)
        return plan, packet['sources'], catalog
    return plan, packet['sources']


@dataclass
class PreparedToolOperation:
    plan: object
    runtime: object
    tools: object
    endpoints: object
    receipt: dict

    def endpoint_for(self, instance):
        matches = [tool.local_values.get('ENDPOINT') for tool in self.plan.tools if tool.id == instance]
        if len(matches) != 1 or not matches[0]:
            raise ContractError('Selected tool has no ready endpoint.')
        return matches[0]


class PreparedTestTools:
    """Keep test-owned cleanup on its creating loop, including failed releases."""
    def __init__(self, data, case, loop, *, manager_factory=None, runtime_factory=None,
                 engine_environment=None):
        self.loop = loop
        self.data = data
        if 'tool_plan' not in data or case not in data.get('tool_cases', {}):
            raise ContractError('Test has no launch-admitted tool context.')
        plan, self.sources, self.inventory = read_tool_packet(
            os.environ['API_AGENT_INTEGRATION_CONTEXT'], data['tool_plan'], inventory_context=data)
        self.plan = plan.for_case(data['tool_cases'][case])
        self.manager_factory, self.runtime_factory = manager_factory, runtime_factory
        self.engine_environment = engine_environment
        self.pending_cleanup = None
        self._active = False

    def definition_for_case(self, graph_id):
        matches = [item for item in self.plan.definitions if item.graph_id == graph_id]
        if len(matches) != 1:
            raise ContractError('Graph is outside this test admission.')
        return matches[0]

    @property
    def definition(self):
        if len(self.plan.definitions) != 1:
            raise ContractError('Test must choose one of its admitted graphs.')
        return self.plan.definitions[0]

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    async def aclose(self):
        if self.pending_cleanup is not None:
            await self.pending_cleanup.retry()
            self.pending_cleanup = None

    @asynccontextmanager
    async def open(self, definition):
        if self._active or self.pending_cleanup is not None:
            raise ContractError('Test tool operation is already owned or awaiting cleanup.')
        if definition not in self.plan.definitions:
            raise ContractError('Graph is outside this test admission.')
        from api_agent.api.application import Runtime
        from api_agent.config.resolve import ConfigResult, ResolvedValue
        from api_agent.runtime.compose import ENGINE_ENVIRONMENT_KEYS
        from api_agent.runtime.infrastructure_plan import MissingEngineTransport, plan_infrastructure
        from api_agent.runtime.infrastructure_session import InfrastructureSession
        from api_agent.runtime.operation import OperationCleanup
        from api_agent.runtime.ownership import StackScope
        from api_agent.runtime.prerequisites import PrerequisiteManager
        from api_agent.tooling.services import bind_tool_outputs, selected_contributions, tool_operation_values
        from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

        values = dict(self.data['config'])
        fields = {name: ResolvedValue(value, self.sources.get(name, {}).get('source', 'launch'),
                                    self.sources.get(name, {}).get('explicit', False))
                  for name, value in values.items()}
        config = ConfigResult(values, fields, (), (), (), tool_plan=self.plan)
        config = tool_operation_values(self.plan, config)
        plan = config.tool_plan
        scope = StackScope.from_roots(Path(self.data['base_root']), Path(self.data['pack_root']))
        environment = self.engine_environment
        if environment is None:
            environment = {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
        contributions = selected_contributions(plan)
        # Independent reporting/tracing profiles are not tool prerequisites.
        infrastructure_values = {name: value for name, value in config.values.items()
                                 if not name.startswith('ALLURE_') and name not in {'TRACE_MODE', 'TRACE_TARGET'}}
        try:
            selected = plan_infrastructure(infrastructure_values, plan.schema, contributions, scope,
                                           engine_environment=environment, inventory=self.inventory)
        except MissingEngineTransport:
            from api_agent.runtime.reporting_engine import verified_engine_binding
            _, transport = await offload(verified_engine_binding, environment)
            environment = {**environment, 'DOCKER_HOST': transport}
            selected = plan_infrastructure(infrastructure_values, plan.schema, contributions, scope,
                                           engine_environment=environment, inventory=self.inventory)
        cancelled = Event()
        factory = self.manager_factory or PrerequisiteManager
        manager = factory(selected, scope, Path(self.data['owner_root']) / 'artifacts' / 'prerequisites',
                          engine_environment=environment, cancelled=cancelled.is_set)
        infrastructure = InfrastructureSession(manager)
        runtime, lease, operation = None, None, None
        receipt = {'tool_count': len(plan.tools), 'service_count': len(contributions),
                   'closed_tools': 0, 'remaining_owners': 0, 'released': False}
        self._active = True
        try:
            async with component_timeout('Test tool prerequisites', 120):
                lease = await offload(infrastructure.acquire, cancellation=cancelled)
            plan = bind_tool_outputs(plan, infrastructure)
            # The worker snapshot is an admitted plain mapping, not a
            # ConfigResult; derive its explicit content flag directly.
            boundary = EmissionBoundary(ContentPolicy(values.get('LOG_CONTENT') is True))
            from .authority_scope import ajoin_current
            await ajoin_current('tools', boundary)
            runtime = (self.runtime_factory or Runtime)('selected-test-tools', tool_plan=plan,
                services={'owner_root': Path(self.data['owner_root'])}, emissions=boundary)
            await runtime.astart()
            operation = PreparedToolOperation(plan, runtime,
                runtime.tools_for(definition, endpoints=infrastructure.endpoints),
                infrastructure.endpoints, receipt)
            yield operation
        finally:
            import sys
            primary = sys.exc_info()[1]
            lease = lease or getattr(manager, 'lease', None)
            def complete():
                receipt.update(closed_tools=receipt['tool_count'], remaining_owners=0, released=True)
                self.pending_cleanup = None
                self._active = False
            cleanup = OperationCleanup(manager, runtime, None, lease, False, 15, complete=complete)
            try:
                await cleanup.retry()
            except (CleanupPendingError, asyncio.CancelledError) as error:
                pending = error if isinstance(error, CleanupPendingError) else getattr(error, 'cleanup_pending', None)
                if pending is not None:
                    self.pending_cleanup = pending
                    receipt['remaining_owners'] = len(getattr(runtime, 'tool_owners', {}))
                    if primary is not None:
                        primary.cleanup_pending = pending
                if primary is None:
                    raise
