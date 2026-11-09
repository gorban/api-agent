"""Boundary validation for the simplified compiled-plan serialization."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
from types import MappingProxyType, SimpleNamespace

import pytest

from artifact_unit_support import load_built, v2_built, v3_built  # noqa: F401
from api_agent.workflows import artifact
from api_agent.workflows.artifact import ArtifactRejected


def _value(built):
    return json.loads(built.plan)


@pytest.mark.parametrize(
    ('filename', 'expected'),
    [
        ('package/__init__.py', 'package'),
        ('package/member.py', 'package.member'),
        ('package/data.txt', None),
        ('bad-name/module.py', None),
        ('module.py', 'module'),
    ],
)
def test_module_file_names_are_mapped_only_when_importable(filename, expected):
    assert artifact.module_for_file(filename) == expected


def test_compiled_plan_identity_requires_an_object():
    with pytest.raises(ValueError, match='requires an object'):
        artifact.compiled_plan_identity(())


@pytest.mark.parametrize('path', ['', '/absolute.py', 'relative\\path.py', 'package/../module.py'])
def test_record_paths_reject_unsafe_spelling(path):
    with pytest.raises(ArtifactRejected, match='unsafe distribution path'):
        artifact._safe_relative(path)


def test_record_parser_and_verifier_accept_a_receipted_file():
    body = b'VALUE = 1\n'
    digest = base64.urlsafe_b64encode(__import__('hashlib').sha256(body).digest()).rstrip(b'=').decode()
    entries = artifact.parse_record(
        f'package/module.py,sha256={digest},{len(body)}\npackage-1.0.dist-info/RECORD,,\n'.encode(),
        'package-1.0.dist-info',
    )

    assert artifact.verify_record(entries, {
        'package/module.py': body,
        'package-1.0.dist-info/RECORD': b'',
    }) == {'package/module.py': __import__('hashlib').sha256(body).hexdigest()}


@pytest.mark.parametrize(
    ('record', 'message'),
    [
        (b'\xff', 'RECORD is not valid CSV text'),
        (b'bad,row\n', 'RECORD rows require path'),
        (b'package.py,sha256=bad,1\npackage.py,sha256=bad,1\n', 'RECORD entry requires'),
        (b'package-1.0.dist-info/RECORD,sha256=bad,1\n', 'RECORD must not describe'),
        (b'package.py,sha256=bad,1\n', 'RECORD entry requires'),
    ],
)
def test_record_parser_rejects_malformed_receipts(record, message):
    with pytest.raises(ArtifactRejected, match=message):
        artifact.parse_record(record, 'package-1.0.dist-info')


def test_record_parser_rejects_duplicate_and_missing_record_entries():
    digest = base64.urlsafe_b64encode(hashlib.sha256(b'x').digest()).rstrip(b'=').decode()

    with pytest.raises(ArtifactRejected, match='duplicate RECORD'):
        artifact.parse_record(
            f'package.py,sha256={digest},1\npackage.py,sha256={digest},1\n'
            'package-1.0.dist-info/RECORD,,\n'.encode(),
            'package-1.0.dist-info',
        )
    with pytest.raises(ArtifactRejected, match='does not list itself'):
        artifact.parse_record(f'package.py,sha256={digest},1\n'.encode(), 'package-1.0.dist-info')


@pytest.mark.parametrize(
    ('entries', 'files', 'message'),
    [
        ({'package.py': ('0' * 64, 1)}, {}, 'wheel differs from RECORD'),
        ({'package.py': ('0' * 64, 2)}, {'package.py': b'x'}, 'file size'),
        ({'package.py': ('0' * 64, 1)}, {'package.py': b'x'}, 'file hash'),
    ],
)
def test_record_verifier_rejects_missing_or_changed_files(entries, files, message):
    with pytest.raises(ArtifactRejected, match=message):
        artifact.verify_record(entries, files)


@pytest.mark.parametrize(
    ('files', 'dist_info', 'message'),
    [
        ({}, 'package-1.0.dist-info', 'METADATA is not part'),
        ({'package-1.0.dist-info/METADATA': b'Name: package\nVersion: wrong\n'},
         'package-1.0.dist-info', 'does not match'),
    ],
)
def test_distribution_identity_rejects_inconsistent_metadata(files, dist_info, message):
    with pytest.raises(ArtifactRejected, match=message):
        artifact.distribution_identity(files, dist_info)


def test_distribution_identity_normalizes_valid_metadata():
    assert artifact.distribution_identity(
        {'package-name-1.0.dist-info/METADATA': b'Name: package_name\nVersion: 1.0\n'},
        'package-name-1.0.dist-info',
    ) == ('package-name', '1.0')


@pytest.mark.parametrize('logical', ['outside/file', 'base', 'base/../file', 7])
def test_virtual_paths_require_a_safe_declared_root(logical):
    with pytest.raises(ValueError, match='invalid logical path'):
        artifact._virtual(logical)


@pytest.mark.parametrize(
    ('encoded', 'message'),
    [
        ({'$i': ['base/prompts.md', 7]}, 'invalid inline prompt reference'),
        ({'$m': [[7, 'value']]}, 'invalid or duplicate mapping key'),
        ({'$m': [['value', 1], ['value', 2]]}, 'invalid or duplicate mapping key'),
        ({'unknown': 'value'}, 'invalid plan value'),
    ],
)
def test_encoded_values_reject_invalid_tags(encoded, message):
    with pytest.raises(ValueError, match=message):
        artifact._decode(encoded)


def test_encoded_values_reject_objects_without_exactly_one_tag():
    with pytest.raises(ValueError, match='invalid plan value'):
        artifact._decode({'$p': 'base/file', 'unexpected': 'value'})


def test_encoded_values_preserve_typed_paths_and_mappings():
    decoded = artifact._decode({'$m': [['path', {'$p': 'base/value.txt'}], ['flag', True]]})

    assert decoded['path'].as_posix() == '/artifact/base/value.txt'
    assert decoded['flag'] is True


@pytest.mark.parametrize(
    ('body', 'message'),
    [
        (b'{"duplicate":1,"duplicate":2}', 'strict JSON'),
        (b'NaN', 'strict JSON'),
        (b'[]', 'JSON object'),
        (b'{"value":1 }', 'canonical JSON'),
    ],
)
def test_parser_rejects_noncanonical_or_nonobject_json(body, message):
    with pytest.raises(ArtifactRejected, match=message):
        artifact._parse(body)


def test_parser_reports_an_unencodable_decoded_value(monkeypatch):
    monkeypatch.setattr(artifact, 'canonical_json', lambda value: (_ for _ in ()).throw(TypeError()))

    with pytest.raises(ArtifactRejected, match='canonical JSON'):
        artifact._parse(b'{}')


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda value: value.update(format='other'), 'not an api-agent'),
        (lambda value: value.update(plan_version=True), 'not an api-agent'),
        (lambda value: value.update(compiled_plan_identity='bad'), 'identity is invalid'),
        (lambda value: value.pop('state'), 'missing or unknown sections'),
    ],
)
def test_version_check_rejects_invalid_plan_headers(v3_built, change, message):
    value = _value(v3_built)
    change(value)

    with pytest.raises(ArtifactRejected, match=message):
        artifact._check_versions(value)


@pytest.mark.parametrize(
    ('row', 'message'),
    [
        ({}, 'invalid resource'),
        ({'kind': 'unknown', 'path': 'base/file', 'sha256': '0' * 64, 'size': 0, 'content': None},
         'invalid resource'),
        ({'kind': 'prompt', 'path': 'base/file', 'sha256': '0' * 64, 'size': 0, 'content': None},
         'data resource requires content'),
        ({'kind': 'native_source', 'path': 'base/file', 'sha256': '0' * 64, 'size': 0,
          'content': {'key': {'$p': 'base/file'}, 'b64': ''}}, 'source resource must not'),
        ({'kind': 'prompt', 'path': 'base/wrong', 'sha256': '0' * 64, 'size': 0,
          'content': {'key': {'$p': 'base/file'}, 'b64': ''}}, 'does not name'),
    ],
)
def test_resource_decoder_rejects_malformed_rows(row, message):
    with pytest.raises(ValueError, match=message):
        artifact._decode_resources([row])


def test_resource_decoder_preserves_inline_content():
    row = {'kind': 'prompt', 'path': 'base/file', 'sha256': '0' * 64, 'size': 0,
           'content': {'key': {'$p': 'base/file'}, 'b64': ''}}

    resources, contents = artifact._decode_resources([row])

    assert resources[0].content_bytes == b''
    assert contents[next(iter(contents))] == b''


@pytest.mark.parametrize(
    ('distribution', 'binding', 'message'),
    [
        ({}, None, 'invalid distribution'),
        ({'name': 'valid', 'version': '1.0', 'dist_info': 'valid-1.0.dist-info',
          'files': (), 'top_levels': []}, None, 'invalid distribution'),
        ({'name': 'valid', 'version': '1.0', 'dist_info': 'valid-1.0.dist-info',
          'files': [], 'top_levels': []}, {}, 'invalid binding'),
        ({'name': 'valid', 'version': '1.0', 'dist_info': 'valid-1.0.dist-info',
          'files': [], 'top_levels': []},
         {'source': 'base/native.py', 'distribution': 'valid', 'module': 'bad-name',
          'file': 'native.py', 'sha256': '0' * 64}, 'invalid binding'),
    ],
)
def test_binding_decoder_rejects_malformed_distribution_or_binding(distribution, binding, message):
    plan = {'distributions': [distribution], 'bindings': [] if binding is None else [binding]}

    with pytest.raises(ValueError, match=message):
        artifact._decode_bindings(plan, ())


def test_binding_decoder_rejects_incomplete_native_bindings(v3_built):
    value = _value(v3_built)
    resources, _ = artifact._decode_resources(value['resources'])
    value['bindings'] = []

    with pytest.raises(ArtifactRejected, match='native sources and plan bindings differ'):
        artifact._decode_bindings(value, resources)


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda value: value.update(graph_id=7), 'invalid identity'),
        (lambda value: value.update(entry_root='outside'), 'invalid identity'),
        (lambda value: value['layout'].update(steps={}), 'invalid layout'),
        (lambda value: value['layout']['steps'][0].update(line=0), 'invalid layout step'),
        (lambda value: value['layout'].update(tables={'$m': [['wrong', []]]}), 'invalid layout tables'),
        (lambda value: value['state'].clear(), 'invalid state section'),
    ],
)
def test_plan_decoder_rejects_invalid_graph_shape(v3_built, change, message):
    value = _value(v3_built)
    change(value)

    with pytest.raises(ValueError, match=message):
        artifact._decode_plan(value, 3)


def test_plan_decoder_rejects_helper_native_sources(v3_built):
    value = _value(v3_built)
    value['state']['helper_sources'] = ['base/helper.py']

    with pytest.raises(ArtifactRejected, match='do not support helper'):
        artifact._decode_plan(value, 3)


def test_plan_decoder_rejects_a_changed_definition_identity(v3_built):
    value = _value(v3_built)
    value['definition_id'] = 'changed'

    with pytest.raises(ArtifactRejected, match='definition_id'):
        artifact._decode_plan(value, 3)


def test_plan_decoder_rejects_invalid_state_rows(v3_built):
    value = _value(v3_built)
    value['state']['nodes'] = [{'$m': [['invalid', 'row']]}]

    with pytest.raises(ValueError, match='invalid state rows'):
        artifact._decode_plan(value, 3)


@pytest.mark.parametrize('value', [(), ('only',), ['not-a-tuple']])
def test_rows_require_a_decoded_tuple_of_the_expected_size(value):
    with pytest.raises(ValueError, match='invalid fixture row'):
        artifact._row(value, 2, 'fixture')


def test_compiled_resource_retrieval_requires_embedded_content(v2_built):
    definition = load_built(v2_built)

    with pytest.raises(KeyError) as error:
        definition.resource('base/missing')

    assert error.value.args == ('base/missing',)


def test_compiled_resource_retrieval_returns_embedded_content(v2_built):
    definition = load_built(v2_built)
    resource = next(item for item in definition.resources if item.content_bytes is not None)

    assert definition.resource(resource.logical_path) == resource.content_bytes


def _decoded_v2(functions, steps):
    return {
        'layout': SimpleNamespace(
            steps=tuple(SimpleNamespace(id=identifier, source=source) for identifier, source in steps),
            tables={'Functions': tuple(functions)},
        ),
    }


def test_binding_import_failure_is_reported_as_an_artifact_rejection(monkeypatch):
    source = PurePosixPath('/artifact/base/native.py')
    decoded = _decoded_v2(({'ID': 'node', 'Registry': 'function'},), (('node', source),))
    bindings = (MappingProxyType({'source': 'base/native.py', 'module': 'missing'}),)
    monkeypatch.setattr(artifact.importlib, 'import_module',
                        lambda name: (_ for _ in ()).throw(ImportError(name)))

    with pytest.raises(ArtifactRejected, match='cannot import bound module'):
        artifact._bind(2, decoded, bindings)


def test_binding_requires_a_declared_source_mapping(monkeypatch):
    source = PurePosixPath('/artifact/base/native.py')
    decoded = _decoded_v2(({'ID': 'node', 'Registry': 'function'},), (('node', source),))
    monkeypatch.setattr(artifact.importlib, 'import_module', lambda name: SimpleNamespace(__name__=name))

    with pytest.raises(ArtifactRejected, match='does not name a binding'):
        artifact._bind(2, decoded, ())


def test_binding_requires_the_declared_module_callable(monkeypatch):
    source = PurePosixPath('/artifact/base/native.py')
    decoded = _decoded_v2(({'ID': 'node', 'Registry': 'function'},), (('node', source),))
    bindings = (MappingProxyType({'source': 'base/native.py', 'module': 'module'}),)
    monkeypatch.setattr(artifact.importlib, 'import_module', lambda name: SimpleNamespace(__name__=name))

    with pytest.raises(ArtifactRejected, match='does not define declared binding'):
        artifact._bind(2, decoded, bindings)


def test_binding_rejects_one_registry_name_for_different_callables(monkeypatch):
    first_source = PurePosixPath('/artifact/base/first.py')
    second_source = PurePosixPath('/artifact/base/second.py')
    decoded = _decoded_v2(
        ({'ID': 'first', 'Registry': 'function'}, {'ID': 'second', 'Registry': 'function'}),
        (('first', first_source), ('second', second_source)),
    )
    bindings = (
        MappingProxyType({'source': 'base/first.py', 'module': 'first_module'}),
        MappingProxyType({'source': 'base/second.py', 'module': 'second_module'}),
    )

    def function():
        return None

    first = SimpleNamespace(__name__='first_module', function=function)
    second = SimpleNamespace(__name__='second_module', function=lambda: None)
    first.function.__module__ = first.__name__
    first.function.__qualname__ = 'function'
    second.function.__module__ = second.__name__
    second.function.__qualname__ = 'function'
    monkeypatch.setattr(artifact.importlib, 'import_module',
                        lambda name: {'first_module': first, 'second_module': second}[name])

    with pytest.raises(ArtifactRejected, match='registry id'):
        artifact._bind(2, decoded, bindings)


def test_compiled_catalog_rejects_a_missing_declared_callable(v3_built):
    definition = load_built(v3_built)
    module = definition.modules['aq_bindings.native']
    original = module.State
    del module.State
    try:
        with pytest.raises(ArtifactRejected, match='unbound declared callable'):
            definition.load_catalog()
    finally:
        module.State = original


@pytest.mark.parametrize('plan', [None, b'x' * (artifact.MAX_PLAN_BYTES + 1)])
def test_loader_requires_a_bounded_byte_plan(plan):
    with pytest.raises(ArtifactRejected, match='bytes within'):
        artifact.load_artifact(plan)


def test_loader_wraps_a_malformed_decoded_graph(v3_built):
    value = _value(v3_built)
    value['layout']['steps'][0]['line'] = 0
    value['compiled_plan_identity'] = artifact.compiled_plan_identity(value)

    with pytest.raises(ArtifactRejected, match='malformed plan'):
        artifact.load_artifact(artifact.canonical_json(value))


def test_loader_propagates_an_artifact_rejection_from_decoding(v3_built):
    value = _value(v3_built)
    value['state']['helper_sources'] = ['base/helper.py']
    value['compiled_plan_identity'] = artifact.compiled_plan_identity(value)

    with pytest.raises(ArtifactRejected, match='do not support helper'):
        artifact.load_artifact(artifact.canonical_json(value))
