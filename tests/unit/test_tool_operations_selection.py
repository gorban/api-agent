"""Node Operations resolve against the selected instance's implementation."""
import pytest

from api_agent.tooling.admission import capture_tool_factory
from api_agent.tooling.models import ToolPlan, ToolSelectionError
from api_agent.tooling.operations import OPERATIONS_INTERFACE
from api_agent.workflows.layout import LayoutError
from tool_operations_support import GRAMMARS, build, compose


@pytest.fixture(params=GRAMMARS)
def grammar(request):
    return request.param


def test_declared_subsets_admit_and_carry_idempotency(tmp_path, grammar):
    plan = compose(tmp_path, build(tmp_path, grammar))

    [tool] = plan.tools
    assert tool.interface == OPERATIONS_INTERFACE
    assert tool.operations == {'introspect': True, 'query': True, 'mutate': False}
    assert tool.admission.interface == OPERATIONS_INTERFACE
    assert [(use.scope, use.operations) for use in plan.definitions[0].tool_usages] == [
        ('reader', ('introspect', 'query')), ('writer', ('mutate',))]


def test_declared_operations_survive_the_worker_packet(tmp_path, grammar):
    plan = compose(tmp_path, build(tmp_path, grammar))

    restored = ToolPlan.from_wire(plan.to_wire())

    assert restored.tools[0].operations == {'introspect': True, 'query': True, 'mutate': False}
    assert restored.tools[0].admission.interface == OPERATIONS_INTERFACE


def test_operations_interface_admission_requires_create_tool(tmp_path):
    (tmp_path / 'factory.py').write_text('def create_tool(config, services): pass\n')

    admission = capture_tool_factory(tmp_path / 'factory.py', 'create_tool', tmp_path,
                                     interface=OPERATIONS_INTERFACE)

    assert admission.interface == OPERATIONS_INTERFACE
    with pytest.raises(ValueError, match='Invalid selected tool factory'):
        capture_tool_factory(tmp_path / 'factory.py', 'create_other', tmp_path,
                             interface=OPERATIONS_INTERFACE)


@pytest.mark.parametrize('operations, name', [
    ('introspect, explode', 'explode'),
    ('describe', 'describe'),
    ('prepare, mutate', 'prepare'),
])
def test_operation_not_declared_by_implementation_is_rejected(tmp_path, grammar, operations, name):
    entry = build(tmp_path, grammar, reader=operations)

    with pytest.raises(LayoutError, match=f"operation '{name}' is not declared by .*tool.toml") as error:
        compose(tmp_path, entry)

    assert 'reader.md.html:' in str(error.value)
    assert 'introspect, mutate, query' in str(error.value)


def test_node_without_operations_column_is_rejected_for_declared_tools(tmp_path, grammar):
    entry = build(tmp_path, grammar)
    node = tmp_path / 'reader.md.html'
    node.write_text(node.read_text().replace('| Port | Instance | Operations |\n| --- | --- | --- |\n'
        '| lookup | [instance](instance.toml) | introspect, query |',
        '| Port | Instance |\n| --- | --- |\n| lookup | [instance](instance.toml) |'))

    with pytest.raises(LayoutError, match='must list Operations'):
        compose(tmp_path, entry)


def verified_read_graph(root, grammar, **kwargs):
    entry = build(root, grammar, interface='verified-read@1', **kwargs)
    (root / 'tool' / 'tool.toml').write_text(
        'version = 1\ninterface = "verified-read@1"\nfactory = "factory.py:create_tool"\n')
    return entry


def test_verified_read_instances_keep_their_fixed_vocabulary(tmp_path, grammar):
    plan = compose(tmp_path, verified_read_graph(tmp_path, grammar, reader='prepare, verify', writer='execute'))

    assert plan.tools[0].operations == {}
    assert [use.operations for use in plan.definitions[0].tool_usages] == [('prepare', 'verify'), ('execute',)]


def test_verified_read_instances_reject_implementation_style_names(tmp_path, grammar):
    entry = verified_read_graph(tmp_path, grammar, reader='prepare, introspect', writer='execute')

    with pytest.raises(LayoutError, match="operation 'introspect' is not supported by verified-read@1") as error:
        compose(tmp_path, entry)

    assert 'reader.md.html:' in str(error.value)


def test_interface_mismatch_between_instance_and_implementation_still_rejects(tmp_path, grammar):
    entry = build(tmp_path, grammar, interface='verified-read@1')

    with pytest.raises(ToolSelectionError, match='interface is incompatible'):
        compose(tmp_path, entry)
