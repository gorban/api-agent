"""Exact overlapping text matches with linear scans and merged ranges."""


def overlapping_ranges(text: str, secret: str):
    """Find the exact union of overlapping occurrences in linear scan time.

    The prefix table lets a mismatch or completed match reuse its known suffix
    without rescanning text. Overlapping and adjacent matches extend one range;
    only disjoint ranges are yielded, rather than one record per occurrence.
    """
    if not secret:
        raise ValueError('Matching value must be nonempty.')
    prefix = [0] * len(secret)
    matched = 0
    for index in range(1, len(secret)):
        while matched and secret[index] != secret[matched]:
            matched = prefix[matched - 1]
        if secret[index] == secret[matched]:
            matched += 1
        prefix[index] = matched
    matched = 0
    run_start, run_stop = None, 0
    for index, character in enumerate(text):
        while matched and character != secret[matched]:
            matched = prefix[matched - 1]
        if character == secret[matched]:
            matched += 1
        if matched == len(secret):
            start, stop = index + 1 - len(secret), index + 1
            if run_start is None:
                run_start, run_stop = start, stop
            elif start <= run_stop:
                run_stop = stop
            else:
                yield run_start, run_stop
                run_start, run_stop = start, stop
            matched = prefix[matched - 1]
    if run_start is not None:
        yield run_start, run_stop
