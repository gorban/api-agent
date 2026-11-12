"""Identity-based plans for configuration actions on a displayed snapshot."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BulkPlan:
    names: tuple[str, ...]
    skipped: tuple[str, ...]
    copy_text: str | None = None


def _plan(names, eligible, values=None):
    names = tuple(dict.fromkeys(names))
    selected = tuple(name for name in names if name in eligible)
    skipped = tuple(name for name in names if name not in eligible)
    copy_text = None
    if values is not None and selected:
        # Retain the single-row clipboard contract. Labels only appear for a
        # multi-row selection, including one with skipped entries.
        copy_text = (values[selected[0]] if len(names) == 1 else
                     '\n'.join(f'{name}={values[name]}' for name in selected))
    return BulkPlan(selected, skipped, copy_text)


def _copyable(field, resolved):
    return resolved is not None and not (field.sensitive and resolved.value)


def _display(value):
    # Match the existing single-row presentation for structured values.
    from .app import display
    return display(value)


def plan_review(snapshot, names, action):
    fields, result = snapshot.schema.fields, snapshot.review
    if action == 'accept':
        eligible = {name for name, resolved in result.fields.items()
                    if name in fields and not resolved.explicit and fields[name].acknowledge_default}
        return _plan(names, eligible)
    if action == 'unset':
        return _plan(names, snapshot.saved_names & fields.keys())
    if action == 'copy':
        values = {name: _display(resolved.value) for name, resolved in result.fields.items()
                  if name in fields and name not in result.missing and _copyable(fields[name], resolved)}
        return _plan(names, values, values)
    raise ValueError(f'Unknown review action: {action}')


def plan_cleanup(rows, names, action, *, membership, caches=()):
    """``caches`` holds the keys of persisted caches that still have content; they can only be purged."""
    rows = {row.name: row for row in rows if row.name in membership}
    if action == 'delete':
        return _plan(names, {name for name, row in rows.items() if row.present} | set(caches))
    if action == 'copy':
        values = {name: _display(row.value) for name, row in rows.items()
                  if _copyable(row.field, row.resolved)}
        return _plan(names, values, values)
    raise ValueError(f'Unknown cleanup action: {action}')
