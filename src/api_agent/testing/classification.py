"""Explicit pytest feature metadata, independent of execution and reporting."""


def validate_features(values):
    if (type(values) not in (tuple, list) or len(values) > 16 or any(
            type(value) is not str or not value.strip() or len(value) > 100
            or not value.isprintable() for value in values)):
        raise ValueError('Features require at most 16 nonempty printable strings of at most 100 characters')
    return tuple(sorted(set(values)))


def collected_features(item):
    values = []
    for marker in item.iter_markers(name='feature'):
        if len(marker.args) != 1 or marker.kwargs:
            raise ValueError('Each feature marker requires exactly one positional string')
        values.append(marker.args[0])
    return validate_features(values)
