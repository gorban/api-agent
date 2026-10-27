"""Inert, declared-field projections of each selected case's effective controls."""
import re

from api_agent.testing.test_profiles import SHARED_KEYS, resolve_test_profiles


def profile_rows(tests, result, schema, owner_root):
    """Keep exact case identity and provenance; never export credential settings."""
    from .app import display_configuration
    profiles = resolve_test_profiles(tests, result.values, owner_root=owner_root, fields=result.fields)
    rows = []
    for test in tests:
        profile = profiles[test.id]
        keys = set(SHARED_KEYS) | set(profile['provenance']) | {
            name for name in schema.fields
            if re.fullmatch(r'[A-Z][A-Z0-9_]*(?:API_MODE|INFERENCE_TIMEOUT_SECONDS|MAX_OUTPUT_TOKENS|REASONING_EFFORT)', name)}
        for name in sorted(keys & schema.fields.keys() & profile['config'].keys()):
            field = schema.fields[name]
            resolved = result.fields.get(name)
            source = profile['provenance'].get(name)
            if source is None:
                source = 'application:' + (resolved.source if resolved else 'effective default')
            rows.append((f'{test.id}:{name}', (test.id, name,
                display_configuration(field, profile['config'][name]), source)))
    return rows


def profile_cost_configs(tests, result, context):
    """Fingerprint each distinct effective configuration once per picker build."""
    from api_agent.testing.cost_context import cost_config
    profiles = resolve_test_profiles(tests, result.values, owner_root=context.owner_root,
                                     fields=getattr(result, 'fields', None))
    groups, output = [], {}
    for test in tests:
        values = profiles[test.id]['config']
        effective = next((saved for original, saved in groups if original == values), None)
        if effective is None:
            effective = cost_config(context, values)
            groups.append((values, effective))
        output[test.id] = effective
    return output


class SelectedReview:
    """Configuration and profile source receipts accompany the rendered rows."""
    def __init__(self, snapshot, rows, source, revision):
        self.snapshot, self.rows, self.source, self.revision = snapshot, rows, source, revision

    def sources_current(self):
        return self.snapshot.sources_current() and _profile_revision(self.source) == self.revision


def _profile_revision(path):
    from api_agent.testing.cost_context import file_fingerprint
    return (path.resolve(), path.exists(), file_fingerprint(path)) if path is not None else None


def selected_review(session, tests):
    """All declaration discovery and snapshot resolution belong to the read worker."""
    from api_agent.providers.loading import provider_location
    if hasattr(session, 'select_operation'):
        session.select_operation(tests)
    snapshot = session.review_snapshot()
    source = None
    location = snapshot.execution.values.get('LLM_PROVIDER')
    if location:
        factory, _ = provider_location(location, owner_root=session.context.owner_root)
        source = factory.parent / 'test-profiles.toml'
    before = _profile_revision(source)
    rows = profile_rows(tests, snapshot.execution, snapshot.schema, session.context.owner_root)
    review = SelectedReview(snapshot, rows, source, before)
    if not review.sources_current():
        raise ValueError('Configuration sources changed during review')
    return review
