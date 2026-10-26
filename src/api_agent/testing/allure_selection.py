"""Bind exact retained attempts into a coherent immutable viewer input."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib

from .allure_source import PreviewSnapshot, ViewerError, validate_manifest, verified_files


@dataclass(frozen=True)
class PreviewSelection:
    """Click-time retained bindings, resolved outside the UI event loop."""
    selection: tuple


def bind_selection(selection):
    """Freeze selected retained objects using only bounded identity metadata."""
    first = selection[0][1]
    fields = ('schema', 'repo', 'run_id', 'mode', 'started_at')
    binding = tuple(first.manifest.get(field) for field in fields)
    if not all(snapshot is first for _, snapshot in selection):
        if any(snapshot.preview is not True or tuple(snapshot.manifest.get(field) for field in fields) != binding
               for _, snapshot in selection):
            raise ValueError('Selected Allure attempts do not share a live run')
    names = tuple(snapshot.result_files[key] for key, snapshot in selection)
    return PreviewSelection(selection), names


def selected_snapshot(selection):
    """Combine live copies from one run without consulting mutable storage."""
    keys = [key for key, _ in selection]
    names = [snapshot.result_files.get(key) for key, snapshot in selection]
    if (len(set(keys)) != len(keys) or any(
            not isinstance(name, str) or not name.endswith('-result.json')
            or name not in snapshot.manifest.get('files', {})
            for name, (_, snapshot) in zip(names, selection))
            or len(set(names)) != len(names)):
        raise ValueError('Selected Allure identity bindings conflict')
    first = selection[0][1]
    if all(snapshot is first for _, snapshot in selection):
        return first
    fields = ('schema', 'repo', 'run_id', 'mode', 'started_at')
    binding = tuple(first.manifest.get(field) for field in fields)
    files = {}
    results = {}
    versions = []
    try:
        for key, snapshot in selection:
            if (snapshot.preview is not True or
                    tuple(snapshot.manifest.get(field) for field in fields) != binding):
                raise ValueError('Selected Allure attempts do not share a live run')
            validate_manifest(snapshot.manifest, preview=True)
            verified = verified_files(snapshot.manifest, snapshot.files.__getitem__)
            for name, body in verified.items():
                if name in files and files[name] != body:
                    raise ValueError('Selected Allure artifacts conflict')
                files[name] = body
            results[key] = snapshot.result_files[key]
            versions.append(snapshot.manifest['preview_version'])
        manifest = deepcopy(first.manifest)
        manifest['preview_version'] = max(versions)
        manifest['files'] = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
        validate_manifest(manifest, preview=True)
        return PreviewSnapshot(files, manifest, results)
    except (ViewerError, KeyError) as error:
        raise ValueError('Selected Allure evidence is unavailable') from error


_SELECTION_LABEL = 'api-agent.selected-result'


def marked_result(name, body, *, original=None):
    """Add derived provenance without changing stored artifacts or history IDs."""
    import json
    marker = hashlib.sha256(name.encode() + b'\0' + (body if original is None else original)).hexdigest()
    row = json.loads(body)
    labels = row.get('labels', [])
    if not isinstance(labels, list) or any(not isinstance(label, dict) for label in labels):
        raise ViewerError('The exact selected Allure result is unavailable')
    row['labels'] = [label for label in labels if label.get('name') != _SELECTION_LABEL]
    row['labels'].append({'name': _SELECTION_LABEL, 'value': marker})
    return json.dumps(row).encode(), marker


def verify_selected_cases(cases, expected):
    """Require a bijection from generated cases to immutable selected bytes."""
    import json
    observed = []
    try:
        for path in cases:
            labels = json.loads(path.read_bytes()).get('labels', [])
            markers = [label['value'] for label in labels if label.get('name') == _SELECTION_LABEL]
            if len(markers) != 1:
                raise ValueError()
            observed.append(markers[0])
        if sorted(observed) != sorted(expected):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ViewerError('Allure did not preserve the exact selected attempt') from None
