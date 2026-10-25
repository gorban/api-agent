"""Executable receipt origins and inert signatures precede every factory."""
import base64
from dataclasses import replace
from hashlib import sha256
from importlib import machinery, util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.tooling import admission
from api_agent.tooling.models import SelectedTool, ToolPlan
from api_agent.config.declarations import ConfigSchema


OWNER = '''class Owner:
    version = 1
    async def describe(self): return 'synthetic'
    async def validate(self, prepared): pass
    async def invoke(self, prepared): pass
    async def aclose(self): pass
def create_tool(config, services):
    services['events'].append('factory acquired')
    return Owner()
'''


@pytest.fixture
def installed_receipts(tmp_path, monkeypatch):
    root = tmp_path / 'installed'
    root.mkdir()
    packages, distributions = {}, {}

    def add(name, *, distribution=None, sources=None, requires=()):
        distribution = distribution or name.replace('_', '-')
        entries = []
        for logical, body in (sources or {name + '.py': 'MARKER = "receipt"\n'}).items():
            path = root / logical
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            digest = base64.urlsafe_b64encode(sha256(path.read_bytes()).digest()).decode().rstrip('=')
            entries.append(SimpleNamespace(as_posix=lambda value=logical: value,
                hash=SimpleNamespace(mode='sha256', value=digest), path=path))
        packages.setdefault(name, []).append(distribution)
        distributions[distribution] = SimpleNamespace(files=entries, metadata={'Name': distribution},
            version='1.0', read_text=lambda name: 'controlled receipt',
            locate_file=lambda entry: entry.path, requires=requires)
        return root

    def distribution(name):
        if name not in distributions:
            raise admission.metadata.PackageNotFoundError(name)
        return distributions[name]

    monkeypatch.setattr(admission.metadata, 'packages_distributions', lambda: packages)
    monkeypatch.setattr(admission.metadata, 'distribution', distribution)
    monkeypatch.syspath_prepend(str(root))
    return root, add


def test_receipted_submodule_keeps_its_admitted_parent_search_path(tmp_path, installed_receipts):
    root, add = installed_receipts
    add('sample_owned_dependency', sources={
        'sample_owned_dependency/__init__.py': '',
        'sample_owned_dependency/child.py': 'VALUE = 1\n',
    })
    selected = captured(tmp_path, 'tool', 'import sample_owned_dependency\n' + OWNER)
    ready = admission.prepare_tool_admission(selected)

    spec = admission.admitted_dependency_spec(ready, 'sample_owned_dependency.child',
        [str(root / 'sample_owned_dependency')])

    assert spec.origin == str(root / 'sample_owned_dependency/child.py')
    assert isinstance(spec.loader, admission._ReceiptSourceLoader)


def captured(root, name, source=OWNER):
    path = root / name / 'factory.py'
    path.parent.mkdir()
    path.write_text(source)
    return admission.capture_tool_factory(path, 'create_tool', root)


def runtime_for(captures, events):
    tools = tuple(SelectedTool('sample/' + str(index), 'verified-read@1', Path('/unused/factory.py'),
        'create_tool', item.sources[item.entry], {}, admission=item) for index, item in enumerate(captures))
    plan = ToolPlan(tools, ConfigSchema({}), {}, (), 'descriptor', 'operation')
    return Runtime('sample', tool_plan=plan, services={'events': events})


@pytest.mark.asyncio
async def test_receipt_valid_cold_shadow_rejects_before_any_factory(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_owned_dependency')
    shadow = tmp_path / 'shadow'
    shadow.mkdir()
    (shadow / 'sample_owned_dependency.py').write_text('MARKER = "unadmitted"\n')
    monkeypatch.syspath_prepend(str(shadow))
    first = captured(tmp_path, 'first')
    second = captured(tmp_path, 'second', 'import sample_owned_dependency\n' + OWNER)
    events = []
    runtime = runtime_for((first, second), events)

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert 'import origin is outside' in str(error.value.primary_failure)
    assert events == []
    assert runtime.tool_owners == {}
    assert 'sample_owned_dependency' not in sys.modules


@pytest.mark.asyncio
async def test_receipt_valid_warm_shadow_rejects_before_any_factory(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_owned_dependency')
    shadow = tmp_path / 'shadow.py'
    shadow.write_text('MARKER = "unadmitted"\n')
    module = ModuleType('sample_owned_dependency')
    module.__file__ = str(shadow)
    module.__spec__ = util.spec_from_file_location(module.__name__, shadow)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    selected = captured(tmp_path, 'tool', 'import sample_owned_dependency\n' + OWNER)
    events = []

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime_for((selected,), events).astart()

    assert 'import origin is outside' in str(error.value.primary_failure)
    assert events == []


def test_namespace_search_location_requires_owned_executable_descendants(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_namespace', sources={'sample_namespace/owned.py': 'VALUE = 1\n'})
    shadow = tmp_path / 'namespace-shadow'
    (shadow / 'sample_namespace').mkdir(parents=True)
    monkeypatch.syspath_prepend(str(shadow))

    with pytest.raises(admission.ToolAdmissionError, match='search location is outside'):
        admission._dependency('sample_namespace')


def test_warm_package_cannot_expand_its_search_locations(tmp_path, installed_receipts, monkeypatch):
    root, add = installed_receipts
    add('sample_package', sources={'sample_package/__init__.py': '', 'sample_package/member.py': 'VALUE = 1\n'})
    spec = util.spec_from_file_location('sample_package', root / 'sample_package/__init__.py')
    module = util.module_from_spec(spec)
    module.__path__.append(str(tmp_path))
    monkeypatch.setitem(sys.modules, 'sample_package', module)

    with pytest.raises(admission.ToolAdmissionError, match='search location is outside'):
        admission._dependency('sample_package')


def test_receipt_without_owned_executable_rejects(installed_receipts):
    _, add = installed_receipts
    add('sample_package', sources={'sample_package/data.txt': 'data only'})

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_package')


def test_transitive_installed_dependency_also_binds_its_import_origin(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_parent', requires=('sample-child==1.0',))
    add('sample_child')
    shadow = tmp_path / 'transitive-shadow'
    shadow.mkdir()
    (shadow / 'sample_child.py').write_text('MARKER = "unadmitted"\n')
    monkeypatch.syspath_prepend(str(shadow))

    with pytest.raises(admission.ToolAdmissionError, match='import origin is outside'):
        admission._dependency('sample_parent')


@pytest.mark.parametrize('source', [
    'def wrong_name(config, services): pass\n',
    'async def create_tool(config, services): pass\n',
    'def create_tool(config): pass\n',
    'def create_tool(config, *, services): pass\n',
    'def create_tool(*args): pass\n',
    'def create_tool(config, services): yield object()\n',
    '@decorator\ndef create_tool(config, services): pass\n',
    'def create_tool(config, services): pass\ncreate_tool = replacement\n',
])
def test_invalid_factory_is_rejected_without_importing_its_entry(tmp_path, source):
    path = tmp_path / 'factory.py'
    path.write_text('raise AssertionError("entry imported")\n' + source)

    with pytest.raises(admission.ToolAdmissionError, match='factory|create_tool'):
        admission.capture_tool_factory(path, 'create_tool', tmp_path)


@pytest.mark.asyncio
async def test_later_forged_invalid_factory_is_rejected_before_first_factory(tmp_path):
    first, second = captured(tmp_path, 'first'), captured(tmp_path, 'second')
    sources = {'factory.py': b'def wrong_name(config, services): pass\n'}
    second = replace(second, sources=sources,
        identity=admission._identity(sources, second.dependencies, second.assets))
    events = []

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime_for((first, second), events).astart()

    assert 'direct synchronous create_tool' in str(error.value.primary_failure)
    assert events == []


@pytest.mark.asyncio
async def test_receipt_owned_dependency_executes_and_can_warm_without_changing_identity(tmp_path, installed_receipts):
    _, add = installed_receipts
    add('sample_owned_dependency')
    selected = captured(tmp_path, 'tool', 'import sample_owned_dependency\n' + OWNER.replace(
        "return 'synthetic'", 'return sample_owned_dependency.MARKER'))
    ready = admission.prepare_tool_admission(selected)
    events = []
    runtime = runtime_for((ready,), events)
    try:
        await runtime.astart()
        assert await runtime.borrow_tool('sample/0').describe() == 'receipt'
        admission.verify_tool_dependencies(ready)
    finally:
        await runtime.aclose()
        sys.modules.pop('sample_owned_dependency', None)
    assert events == ['factory acquired']


@pytest.mark.asyncio
async def test_dependency_source_change_between_preflight_and_import_rejects(tmp_path, installed_receipts):
    root, add = installed_receipts
    add('sample_owned_dependency')
    target = root / 'sample_owned_dependency.py'
    source = ('from pathlib import Path\nPath(' + repr(str(target)) + ').write_text("MARKER = \'changed\'\\n")\n'
              'import sample_owned_dependency\n' + OWNER)
    selected = captured(tmp_path, 'tool', source)
    events = []

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime_for((selected,), events).astart()

    assert 'source changed before import' in str(error.value.primary_failure)
    assert events == []
    assert 'sample_owned_dependency' not in sys.modules


def test_custom_import_finder_cannot_redirect_an_owned_dependency(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_owned_dependency')
    shadow = tmp_path / 'custom-shadow.py'
    shadow.write_text('MARKER = "unadmitted"\n')

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'sample_owned_dependency':
                return util.spec_from_file_location(fullname, shadow)
            return None

    monkeypatch.setattr(sys, 'meta_path', [Finder(), *sys.meta_path])

    with pytest.raises(admission.ToolAdmissionError, match='import origin is outside'):
        admission._dependency('sample_owned_dependency')


def test_multiple_distribution_claims_for_one_executable_are_ambiguous(installed_receipts):
    _, add = installed_receipts
    add('sample_owned_dependency', distribution='first-owner')
    add('sample_owned_dependency', distribution='second-owner')

    with pytest.raises(admission.ToolAdmissionError, match='ambiguous receipt ownership'):
        admission._dependency('sample_owned_dependency')


def test_selected_manifest_extra_includes_its_transitive_import_receipt(tmp_path, installed_receipts, monkeypatch):
    _, add = installed_receipts
    add('sample_parent', requires=('sample-child==1.0; extra == "feature"',))
    add('sample_child')
    shadow = tmp_path / 'extra-shadow'
    shadow.mkdir()
    (shadow / 'sample_child.py').write_text('MARKER = "unadmitted"\n')
    monkeypatch.syspath_prepend(str(shadow))
    captured(tmp_path, 'tool', 'import sample_parent\n' + OWNER)
    manifest = tmp_path / 'tool/provider-dependencies.toml'
    manifest.write_text('version=1\ndependencies=["sample-parent[feature]==1.0"]\n')
    selected = admission.capture_tool_factory(tmp_path / 'tool/factory.py', 'create_tool', tmp_path)

    # Its optional child is inactive without the selected extra.
    admission._dependency('sample_parent')
    with pytest.raises(admission.ToolAdmissionError, match='import origin is outside'):
        admission.prepare_tool_admission(selected)


@pytest.mark.parametrize('ending', [
    'del create_tool\n',
    'from .helper import invalid as create_tool\n',
    'import os as create_tool\n',
    'from .helper import *\n',
    'if True:\n    def create_tool(config): pass\n',
    'try:\n    pass\nexcept Exception as create_tool:\n    pass\n',
    'match {}:\n    case {**create_tool}: pass\n',
    'for create_tool in []:\n    pass\n',
    'with manager() as create_tool:\n    pass\n',
    'create_tool: object = replacement\n',
    'create_tool.__code__ = replacement.__code__\n',
])
def test_module_binding_forms_cannot_replace_or_delete_the_factory(tmp_path, ending):
    path = tmp_path / 'factory.py'
    path.write_text(OWNER + ending)

    with pytest.raises(admission.ToolAdmissionError, match='cannot be rebound'):
        admission.capture_tool_factory(path, 'create_tool', tmp_path)


@pytest.mark.parametrize('ending', ['del create_tool\n', 'from .helper import invalid as create_tool\n'])
@pytest.mark.asyncio
async def test_later_factory_rebinding_rejects_before_any_factory(tmp_path, ending):
    first, second = captured(tmp_path, 'first'), captured(tmp_path, 'second')
    sources = dict(second.sources, **{'factory.py': (OWNER + ending).encode(),
                                     'helper.py': b'def invalid(config): pass\n'})
    second = replace(second, sources=sources,
        identity=admission._identity(sources, second.dependencies, second.assets))
    events = []

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime_for((first, second), events).astart()

    assert 'cannot be rebound' in str(error.value.primary_failure)
    assert events == []


def test_function_and_class_local_names_do_not_rebind_the_module_factory(tmp_path):
    source = OWNER + '''
def helper():
    create_tool = 'local'
    return create_tool
class Holder:
    create_tool = 'class-local'
values = [create_tool for create_tool in ()]
'''
    path = tmp_path / 'factory.py'
    path.write_text(source)

    assert admission.capture_tool_factory(path, 'create_tool', tmp_path).entry == 'factory.py'


def test_parent_prepared_plan_rejects_dependency_change_before_worker_acquisition(tmp_path, installed_receipts):
    from api_agent.tooling.services import prepare_tool_plan, tool_configurations
    root, add = installed_receipts
    add('sample_owned_dependency')
    selected = captured(tmp_path, 'tool', 'import sample_owned_dependency\n' + OWNER)
    original = runtime_for((selected,), []).tool_plan

    ready = prepare_tool_plan(original)
    add('sample_owned_dependency', sources={'sample_owned_dependency.py': 'MARKER = "changed after parent admission"\n'})

    assert original.tools[0].admission.dependencies == (('sample_owned_dependency', None),)
    assert ready.tools[0].admission.dependencies[0][1] is not None
    with pytest.raises(admission.ToolAdmissionError, match='dependency identity changed'):
        tool_configurations(ready, {'events': []})


def test_parent_projection_reuses_identical_factory_receipt_without_merging_instances(tmp_path, monkeypatch):
    from api_agent.tooling.services import prepare_tool_plan
    selected = captured(tmp_path, 'tool')
    original = runtime_for((selected, selected), []).tool_plan
    actual = admission.prepare_tool_admission
    calls = []

    def prepare(captured):
        calls.append(captured.identity)
        return actual(captured)

    monkeypatch.setattr(admission, 'prepare_tool_admission', prepare)

    ready = prepare_tool_plan(original)

    assert calls == [selected.identity]
    assert ready.tools[0].id != ready.tools[1].id
    assert ready.tools[0].admission is ready.tools[1].admission
    assert ready is not original


@pytest.mark.parametrize('mode,code', [
    ('no-record', 'no installed file receipt'), ('unhashed', 'unhashed file'),
    ('missing-file', 'file is unavailable'), ('wrong-digest', 'differs from its receipt'),
    ('wrong-algorithm', 'exceeds admission limits'),
])
def test_installed_receipt_integrity_rejects_before_import(installed_receipts, mode, code):
    root, add = installed_receipts
    add('sample_owned_dependency')
    distribution = admission.metadata.distribution('sample-owned-dependency')
    entry = distribution.files[0]
    if mode == 'no-record': distribution.read_text = lambda name: None
    elif mode == 'unhashed': entry.hash = None
    elif mode == 'missing-file': (root / 'sample_owned_dependency.py').unlink()
    elif mode == 'wrong-digest': entry.hash.value = 'incorrect-digest'
    else: entry.hash.mode = 'sha512'

    with pytest.raises(admission.ToolAdmissionError, match=code):
        admission._dependency('sample_owned_dependency')


@pytest.mark.parametrize('mode,code', [
    ('missing', 'Transitive tool dependency'), ('version', 'incompatible version'),
    ('no-roots', 'no distribution-owned import roots'), ('direct-url', 'unadmitted direct URL'),
])
def test_transitive_distribution_requirements_are_admitted_not_assumed(installed_receipts, mode, code):
    _, add = installed_receipts
    requirement = 'sample-child==2.0' if mode == 'version' else 'sample-child==1.0'
    if mode == 'direct-url': requirement = 'sample-child @ https://example.test/child.whl'
    add('sample_parent', requires=(requirement,))
    if mode in ('version', 'no-roots'):
        add('sample_child')
    if mode == 'no-roots':
        admission.metadata.packages_distributions().pop('sample_child')

    with pytest.raises(admission.ToolAdmissionError, match=code):
        admission._dependency('sample_parent')


@pytest.mark.parametrize('budget,value', [('MAX_DEPENDENCY_BYTES', 1), ('MAX_DEPENDENCY_DISTRIBUTIONS', 1)])
def test_installed_dependency_closure_obeys_finite_budgets(installed_receipts, monkeypatch, budget, value):
    _, add = installed_receipts
    add('sample_parent', requires=('sample-child==1.0',))
    add('sample_child')
    monkeypatch.setattr(admission, budget, value)

    with pytest.raises(admission.ToolAdmissionError, match='exceeds admission limits'):
        admission._dependency('sample_parent')


@pytest.mark.parametrize('budget', ['MAX_RUNTIME_FILES', 'MAX_RUNTIME_BYTES'])
def test_trusted_runtime_identity_also_has_finite_capture_budgets(monkeypatch, budget):
    monkeypatch.setattr(admission, budget, 1)

    with pytest.raises(admission.ToolAdmissionError, match='Runtime dependency exceeds'):
        admission._dependency('api_agent')


@pytest.mark.parametrize('mode,code', [
    ('warm-none', 'unavailable warm module'), ('missing-spec', 'warm module identity'),
    ('wrong-spec-name', 'warm module identity'), ('different-owned-origin', 'warm and resolved'),
    ('different-owned-file', 'warm module origin changed'), ('removed-package-path', 'warm package search'),
])
def test_warm_dependency_identity_must_match_the_owned_cold_spec(installed_receipts, monkeypatch, mode, code):
    root, add = installed_receipts
    add('sample_package', sources={'sample_package/__init__.py': '', 'sample_package/alternate.py': 'VALUE=1\n'})
    initializer, alternate = root / 'sample_package/__init__.py', root / 'sample_package/alternate.py'
    spec = util.spec_from_file_location('sample_package', initializer)
    module = util.module_from_spec(spec)
    if mode == 'warm-none': module = None
    elif mode == 'missing-spec': module.__spec__ = None
    elif mode == 'wrong-spec-name': module.__spec__.name = 'another_package'
    elif mode == 'different-owned-origin': module.__spec__ = util.spec_from_file_location('sample_package', alternate)
    elif mode == 'different-owned-file': module.__file__ = str(alternate)
    else: module.__path__ = []
    monkeypatch.setitem(sys.modules, 'sample_package', module)

    with pytest.raises(admission.ToolAdmissionError, match=code):
        admission._dependency('sample_package')


def test_warm_submodule_cannot_invent_an_importable_parent_package(installed_receipts, monkeypatch):
    root, add = installed_receipts
    add('sample_owned_dependency')
    module = util.module_from_spec(util.spec_from_file_location('sample_owned_dependency.child', root / 'sample_owned_dependency.py'))
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(admission.ToolAdmissionError, match='parent is not an admitted package'):
        admission._dependency('sample_owned_dependency')


def test_resolving_another_owned_file_is_still_a_changed_import_identity(installed_receipts, monkeypatch):
    root, add = installed_receipts
    add('sample_package', sources={'sample_package/__init__.py': '', 'sample_package/alternate.py': 'VALUE=1\n'})

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'sample_package':
                return util.spec_from_file_location(fullname, root / 'sample_package/alternate.py')
            return None

    monkeypatch.setattr(sys, 'meta_path', [Finder(), *sys.meta_path])
    with pytest.raises(admission.ToolAdmissionError, match='resolved import origin differs'):
        admission._dependency('sample_package')


@pytest.mark.parametrize('origin', [None, 'built-in', 'frozen'])
def test_external_dependency_requires_a_physical_executable_origin(origin):
    with pytest.raises(admission.ToolAdmissionError, match='no owned executable origin'):
        admission._owned_origin(origin, {})


def test_missing_and_shapeless_specs_do_not_establish_package_ownership():
    with pytest.raises(admission.ToolAdmissionError, match='no importable owned module'):
        admission._validate_spec(None, {}, set())
    with pytest.raises(admission.ToolAdmissionError, match='no owned namespace location'):
        admission._validate_spec(machinery.ModuleSpec('sample', loader=None), {}, set())


def test_module_file_cannot_advertise_itself_as_a_package(installed_receipts):
    root, add = installed_receipts
    add('sample_owned_dependency')
    path = str(root / 'sample_owned_dependency.py')
    spec = machinery.ModuleSpec('sample_owned_dependency', loader=None, origin=path, is_package=True)
    spec.submodule_search_locations = [str(root)]

    with pytest.raises(admission.ToolAdmissionError, match='unowned package shape'):
        admission._validate_spec(spec, {path: 'controlled hash'}, {str(root)})


@pytest.mark.parametrize('budget', ['MAX_TOOL_FILES', 'MAX_TOOL_BYTES'])
def test_selected_executable_capture_obeys_finite_limits(tmp_path, monkeypatch, budget):
    (tmp_path / 'factory.py').write_text(OWNER)
    (tmp_path / 'sibling.py').write_text('VALUE = 1\n')
    monkeypatch.setattr(admission, budget, 1)

    with pytest.raises(admission.ToolAdmissionError, match='executable closure is too large'):
        admission.capture_tool_factory(tmp_path / 'factory.py', 'create_tool', tmp_path)


def test_licenses_count_toward_the_same_capture_budget(tmp_path, monkeypatch):
    (tmp_path / 'factory.py').write_text(OWNER)
    (tmp_path / 'LICENSE').write_text('Synthetic fixture license text\n')
    monkeypatch.setattr(admission, 'MAX_TOOL_BYTES', len(OWNER.encode()) + 1)

    with pytest.raises(admission.ToolAdmissionError, match='executable closure is too large'):
        admission.capture_tool_factory(tmp_path / 'factory.py', 'create_tool', tmp_path)


@pytest.mark.parametrize('mode,code', [
    ('wrong-factory', 'Invalid selected tool factory'), ('missing-entry', 'absent from captured closure'),
    ('invalid-sibling', 'Invalid selected tool source'), ('upward-import', 'escapes its captured package'),
    ('absolute-sibling', 'must use relative names'), ('invalid-module-return', 'Invalid selected tool source'),
])
def test_executable_declaration_failures_remain_inert(tmp_path, mode, code):
    factory = tmp_path / 'factory.py'
    factory.write_text(OWNER)
    name = 'create_tool'
    if mode == 'wrong-factory': name = 'create'
    elif mode == 'missing-entry': factory.unlink()
    elif mode == 'invalid-sibling': (tmp_path / 'sibling.py').write_text('if:\n')
    elif mode == 'upward-import': factory.write_text('from ..outside import value\n' + OWNER)
    elif mode == 'absolute-sibling':
        (tmp_path / 'sibling.py').write_text('VALUE=1\n')
        factory.write_text('import sibling\n' + OWNER)
    else: factory.write_text('return 1\n' + OWNER)

    with pytest.raises(admission.ToolAdmissionError, match=code):
        admission.capture_tool_factory(factory, name, tmp_path)


def test_snapshot_checks_reject_wrong_interfaces_and_unprepared_dependencies(tmp_path):
    selected = captured(tmp_path, 'tool', 'import unprepared_optional_library\n' + OWNER)
    with pytest.raises(admission.ToolAdmissionError, match='factory admission is required'):
        admission.prepare_tool_admission(replace(selected, interface='other@1'))
    with pytest.raises(admission.ToolAdmissionError, match='require preparation'):
        admission.verify_tool_dependencies(selected)


@pytest.mark.parametrize('ending', [
    'def helper():\n    global create_tool\n',
    'match []:\n    case [*create_tool]: pass\n',
    'match {}:\n    case create_tool: pass\n',
    'create_tool.metadata["selected"] = True\n',
])
def test_indirect_statement_binding_forms_are_also_rejected(tmp_path, ending):
    (tmp_path / 'factory.py').write_text(OWNER + ending)
    with pytest.raises(admission.ToolAdmissionError, match='cannot be rebound'):
        admission.capture_tool_factory(tmp_path / 'factory.py', 'create_tool', tmp_path)


def test_lambda_local_names_do_not_replace_factory_but_outer_defaults_cannot(tmp_path):
    factory = tmp_path / 'factory.py'
    factory.write_text(OWNER + 'helper = lambda create_tool=None: create_tool\n')
    admission.capture_tool_factory(factory, 'create_tool', tmp_path)
    factory.write_text(OWNER + 'helper = lambda value=(create_tool := None): value\n')
    with pytest.raises(admission.ToolAdmissionError, match='cannot be rebound'):
        admission.capture_tool_factory(factory, 'create_tool', tmp_path)


def manifest_only(root, name, requirements):
    selected = captured(root, name, OWNER.replace("return 'synthetic'", "return __import__('sample_owned_dependency').MARKER"))
    manifest = root / name / 'provider-dependencies.toml'
    manifest.write_text('version=1\ndependencies=' + repr(requirements).replace("'", '"') + '\n')
    (root / name / 'LICENSE').write_text('Synthetic fixture license\n')
    return admission.capture_tool_factory(root / name / 'factory.py', 'create_tool', root)


@pytest.mark.asyncio
async def test_manifest_only_dynamic_import_receipt_is_pinned_before_dispatch(tmp_path, installed_receipts):
    from api_agent.tooling.services import prepare_tool_plan
    _, add = installed_receipts
    add('sample_owned_dependency')
    selected = manifest_only(tmp_path, 'tool', ['sample-owned-dependency==1.0'])
    assert selected.dependencies == ()
    ready = prepare_tool_plan(runtime_for((selected,), []).tool_plan)
    assert [name for name, _ in ready.tools[0].admission.dependencies] == ['sample_owned_dependency']
    events = []
    runtime = Runtime('sample', tool_plan=ready, services={'events': events})
    try:
        await runtime.astart()
        assert await runtime.borrow_tool('sample/0').describe() == 'receipt'
    finally:
        await runtime.aclose()
        sys.modules.pop('sample_owned_dependency', None)
    assert events == ['factory acquired']


@pytest.mark.parametrize('mode,code', [('missing', 'not installed'), ('version', 'does not satisfy'),
                                     ('no-root', 'no known executable import roots')])
def test_manifest_dependency_readiness_errors_are_actionable(tmp_path, installed_receipts, mode, code):
    _, add = installed_receipts
    if mode != 'missing': add('sample_owned_dependency')
    if mode == 'no-root': admission.metadata.packages_distributions().clear()
    version = '2.0' if mode == 'version' else '1.0'
    selected = manifest_only(tmp_path, 'tool', ['sample-owned-dependency==' + version])

    with pytest.raises(admission.ToolAdmissionError, match=code):
        admission.prepare_tool_admission(selected)


def test_worker_verification_rejects_missing_or_changed_manifest_only_receipts(tmp_path, installed_receipts):
    _, add = installed_receipts
    add('sample_owned_dependency')
    selected = manifest_only(tmp_path, 'tool', ['sample-owned-dependency==1.0'])
    with pytest.raises(admission.ToolAdmissionError, match='manifest executable roots are not admitted'):
        admission.verify_tool_dependencies(selected)
    ready = admission.prepare_tool_admission(selected)
    add('sample_owned_dependency', sources={'sample_owned_dependency.py': 'MARKER = "new receipt"\n'})
    with pytest.raises(admission.ToolAdmissionError, match='dependency identity changed'):
        admission.verify_tool_dependencies(ready)


def test_inactive_manifest_requirement_does_not_require_or_admit_its_distribution(tmp_path, installed_receipts):
    selected = captured(tmp_path, 'tool')
    manifest = tmp_path / 'tool/provider-dependencies.toml'
    manifest.write_text('version=1\ndependencies=["inactive-package; python_version < \'0\'"]\n')
    selected = admission.capture_tool_factory(tmp_path / 'tool/factory.py', 'create_tool', tmp_path)

    assert admission.prepare_tool_admission(selected).dependencies == ()


def test_nested_factory_helpers_and_unrelated_metadata_mutation_are_valid(tmp_path):
    factory = tmp_path / 'factory.py'
    factory.write_text('''def create_tool(config, services):
    def values():
        yield 1
    return values
metadata = {}
metadata['enabled'] = True
''')
    assert admission.capture_tool_factory(factory, 'create_tool', tmp_path).entry == 'factory.py'


def receipt_admission(name, path, body):
    sources = {'factory.py': OWNER.encode()}
    receipt = (('owned-import-files', ((name, ((str(path.resolve()), sha256(body).hexdigest()),)),)),)
    dependencies = ((name, receipt),)
    return admission.FactoryAdmission('create_tool', 'factory.py', sources, dependencies,
                                     admission._identity(sources, dependencies, {}))


@pytest.mark.parametrize('changed', [False, True])
def test_native_dependency_guard_rechecks_receipt_without_executing_binary(tmp_path, monkeypatch, changed):
    name = 'sample_native_dependency'
    path = tmp_path / (name + machinery.EXTENSION_SUFFIXES[0])
    body = b'Synthetic native fixture; never executed'
    path.write_bytes(body)
    selected = receipt_admission(name, path, body)
    monkeypatch.syspath_prepend(str(tmp_path))
    if changed: path.write_bytes(b'Changed after admission')

    if changed:
        with pytest.raises(admission.ToolAdmissionError, match='executable changed before import'):
            admission.admitted_dependency_spec(selected, name, None)
    else:
        spec = admission.admitted_dependency_spec(selected, name, None)
        assert isinstance(spec.loader, machinery.ExtensionFileLoader)
        assert spec.origin == str(path)


def test_dependency_guard_rejects_nonreceipt_loader_contract(tmp_path, monkeypatch):
    path = tmp_path / 'sample_owned_dependency.py'
    body = b'VALUE=1\n'
    path.write_bytes(body)
    selected = receipt_admission('sample_owned_dependency', path, body)
    spec = util.spec_from_file_location('sample_owned_dependency', path)
    spec.loader = object()
    monkeypatch.setattr(machinery.PathFinder, 'find_spec', lambda fullname, path=None: spec)

    with pytest.raises(admission.ToolAdmissionError, match='loader is outside'):
        admission.admitted_dependency_spec(selected, 'sample_owned_dependency', None)


def test_namespace_dependency_guard_keeps_only_its_owned_search_directory(tmp_path, monkeypatch):
    package = tmp_path / 'sample_namespace'
    package.mkdir()
    path = package / 'owned.py'
    body = b'VALUE=1\n'
    path.write_bytes(body)
    selected = receipt_admission('sample_namespace', path, body)
    monkeypatch.syspath_prepend(str(tmp_path))

    spec = admission.admitted_dependency_spec(selected, 'sample_namespace', None)

    assert spec.origin is None
    assert tuple(spec.submodule_search_locations) == (str(package),)


@pytest.mark.parametrize('aliased', [False, True])
def test_installed_runtime_identity_uses_distribution_version_with_import_proof(tmp_path, monkeypatch, aliased):
    package = tmp_path / 'lib/site-packages/api_agent'
    (package / 'tooling').mkdir(parents=True)
    (package / '__init__.py').write_text('VERSION = "fixture"\n')
    module = package / 'tooling/admission.py'
    module.write_text('VALUE = 1\n')
    selected_module = module
    if aliased:
        (tmp_path / 'lib64').symlink_to(tmp_path / 'lib', target_is_directory=True)
        selected_module = tmp_path / 'lib64/site-packages/api_agent/tooling/admission.py'
    monkeypatch.setattr(admission, '__file__', str(selected_module))
    monkeypatch.setattr(admission.metadata, 'version', lambda name: '1.0-fixture')
    proofs = []

    def imported_identity(name, owned):
        proofs.append((name, tuple(sorted(owned))))
        return ('independently tested import proof',)

    monkeypatch.setattr(admission, '_validate_import_origins', imported_identity)

    receipt = admission._dependency('api_agent')

    assert receipt[0][1] == '1.0-fixture'
    assert proofs == [('api_agent', tuple(sorted((str(package / '__init__.py'), str(module)))))]


def test_canonical_runtime_owner_does_not_admit_symlinked_executable_children(tmp_path, monkeypatch):
    from api_agent.workflows.layout import LayoutError
    package = tmp_path / 'lib/site-packages/api_agent'
    (package / 'tooling').mkdir(parents=True)
    module = package / 'tooling/admission.py'
    module.write_text('VALUE=1\n')
    outside = tmp_path / 'outside.py'
    outside.write_text('VALUE=2\n')
    (package / 'tooling/unsafe.py').symlink_to(outside)
    (tmp_path / 'lib64').symlink_to(tmp_path / 'lib', target_is_directory=True)
    monkeypatch.setattr(admission, '__file__', str(tmp_path / 'lib64/site-packages/api_agent/tooling/admission.py'))

    with pytest.raises(LayoutError, match='unsafe or unavailable resource'):
        admission._dependency('api_agent')


def test_installed_inventory_metadata_and_bytecode_are_not_executable_receipts(installed_receipts):
    root, add = installed_receipts
    add('sample_owned_dependency')
    distribution = admission.metadata.distribution('sample-owned-dependency')
    distribution.files.extend((
        SimpleNamespace(as_posix=lambda: 'sample_owned_dependency-1.0.dist-info/METADATA', hash=None),
        SimpleNamespace(as_posix=lambda: 'sample_owned_dependency/__pycache__/cached.pyc', hash=None),
    ))

    receipt = admission._dependency('sample_owned_dependency')

    owned = next(row[1] for row in receipt if len(row) == 2 and row[0] == 'owned-import-files')
    assert tuple(dict(owned)['sample_owned_dependency']) == (
        (str(root / 'sample_owned_dependency.py'), sha256((root / 'sample_owned_dependency.py').read_bytes()).hexdigest()),)


@pytest.fixture
def inferred_cache_receipt(installed_receipts):
    root, add = installed_receipts
    add('sample_cache_dependency')
    distribution = admission.metadata.distribution('sample-cache-dependency')
    cache = root / '__pycache__/sample_cache_dependency.cpython-313.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'generated cache')
    distribution.files.append(SimpleNamespace(as_posix=lambda: '__pycache__/sample_cache_dependency.cpython-313.pyc',
                                             hash=None, path=cache))
    distribution.read_text = lambda name: None if name == 'top_level.txt' else 'controlled receipt'
    admission.metadata.packages_distributions()['__pycache__'] = ['sample-cache-dependency']
    return root, distribution


def test_generated_cache_inference_does_not_block_dependency_receipt(inferred_cache_receipt):
    _, distribution = inferred_cache_receipt

    identity = admission._dependency('sample_cache_dependency')

    owned = dict(next(row[1] for row in identity if row[0] == 'owned-import-files'))
    assert set(owned) == {'sample_cache_dependency'}
    assert any(path.endswith('sample_cache_dependency.py') for path, _ in owned['sample_cache_dependency'])


def test_manifest_imports_omit_only_inferred_generated_cache(inferred_cache_receipt):
    from packaging.requirements import Requirement

    imports = admission._manifest_imports((Requirement('sample-cache-dependency==1.0'),))

    assert imports == ('sample_cache_dependency',)


def test_direct_cache_import_is_still_rejected(inferred_cache_receipt):
    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('__pycache__')


@pytest.mark.parametrize('cache_entry', ['__pycache__/unowned.txt', '__pycache__/unowned.py',
                                        '__pycache__/unowned.so', '__pycache__/unowned.pyd'])
def test_non_generated_cache_root_is_not_exempt(inferred_cache_receipt, cache_entry):
    _, distribution = inferred_cache_receipt
    distribution.files.append(SimpleNamespace(as_posix=lambda: cache_entry, hash=None))

    with pytest.raises(admission.ToolAdmissionError, match='unhashed file'):
        admission._dependency('sample_cache_dependency')


def test_receipted_cache_bytecode_is_not_treated_as_generated(inferred_cache_receipt):
    _, distribution = inferred_cache_receipt
    distribution.files[-1].hash = SimpleNamespace(mode='sha256', value='receipt')

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_cache_dependency')


def test_explicit_cache_root_metadata_remains_strict(inferred_cache_receipt):
    _, distribution = inferred_cache_receipt
    distribution.read_text = lambda name: 'sample_cache_dependency\n__pycache__\n' if name == 'top_level.txt' else 'controlled receipt'

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_cache_dependency')


def test_unbacked_cache_inference_remains_strict(inferred_cache_receipt):
    _, distribution = inferred_cache_receipt
    distribution.files.pop()

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_cache_dependency')


def test_unbacked_ordinary_import_root_remains_strict(inferred_cache_receipt):
    admission.metadata.packages_distributions()['sample_missing_executable'] = ['sample-cache-dependency']

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_cache_dependency')


def test_transitive_generated_cache_inference_keeps_real_module_receipts(installed_receipts, inferred_cache_receipt):
    _, add = installed_receipts
    add('sample_cache_parent', requires=('sample-cache-dependency==1.0',))

    identity = admission._dependency('sample_cache_parent')

    owned = dict(next(row[1] for row in identity if row[0] == 'owned-import-files'))
    assert set(owned) == {'sample_cache_parent', 'sample_cache_dependency'}


@pytest.mark.parametrize('logical', ['__pycache__.py', '__pycache__.so'])
def test_generated_cache_does_not_hide_cache_named_executable(inferred_cache_receipt, logical):
    root, distribution = inferred_cache_receipt
    executable = root / logical
    executable.write_bytes(b'MARKER = "owned executable"\n')
    digest = base64.urlsafe_b64encode(sha256(executable.read_bytes()).digest()).decode().rstrip('=')
    distribution.files.append(SimpleNamespace(as_posix=lambda: logical,
        hash=SimpleNamespace(mode='sha256', value=digest), path=executable))

    identity = admission._dependency('sample_cache_dependency')

    owned = dict(next(row[1] for row in identity if row[0] == 'owned-import-files'))
    assert '__pycache__' in owned
    assert str(executable.resolve()) in dict(owned['__pycache__'])


def test_generated_cache_does_not_hide_top_level_sourceless_bytecode(inferred_cache_receipt):
    root, distribution = inferred_cache_receipt
    bytecode = root / '__pycache__.pyc'
    bytecode.write_bytes(b'sourceless bytecode')
    distribution.files.append(SimpleNamespace(as_posix=lambda: '__pycache__.pyc', hash=None, path=bytecode))

    with pytest.raises(admission.ToolAdmissionError, match='no owned executable'):
        admission._dependency('sample_cache_dependency')
