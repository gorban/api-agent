"""Logical-test archive counts from verified Allure result artifacts."""
import json
import re

from .allure_source import ViewerError


def _object(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ViewerError('Ambiguous Allure result counts')
    return row


def result_counts(manifest, files):
    groups, group_files = {}, {}
    for name, body in files.items():
        row = json.loads(body, object_pairs_hook=_object)
        parameters = row.get('parameters', [])
        semantic, seen, attempt = [], set(), 1
        for parameter in parameters:
            key = parameter['name']
            if key in seen:
                raise ViewerError('Ambiguous Allure result parameters')
            seen.add(key)
            if key == 'attempt' and parameter.get('excluded') is True:
                value = parameter['value']
                if type(value) is not str or re.fullmatch('[1-9][0-9]{0,3}', value) is None:
                    raise ViewerError('Invalid Allure attempt counter')
                attempt = int(value)
            elif parameter.get('excluded') is not True:
                semantic.append({'name': key, 'value': parameter['value']})
        identity = json.dumps([row.get('historyId') or row['uuid'], row.get('fullName'),
                               sorted(semantic, key=lambda item: item['name'])], sort_keys=True, allow_nan=False)
        attempts = groups.setdefault(identity, {})
        if attempt in attempts:
            raise ViewerError('Ambiguous Allure attempt sequence')
        attempts[attempt] = row['status']
        group_files.setdefault(identity, {})[attempt] = name
    first = final = 0
    uncertain = False
    expected = manifest.get('metadata', {}).get('e2e_reruns') if manifest['mode'] == 'e2e' else None
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ViewerError('Invalid Allure confirmation count')
    ledger = _confirmation_ledger(manifest, groups, group_files)
    for identity, attempts in groups.items():
        if sorted(attempts) != list(range(1, len(attempts) + 1)):
            raise ViewerError('Incomplete Allure attempt sequence')
        statuses = list(attempts[number] for number in range(1, len(attempts) + 1))
        if manifest['mode'] == 'e2e' and expected is not None and len(statuses) > expected + 1:
            raise ViewerError('Allure attempts exceed the confirmation policy')
        first += statuses[0] == 'passed'
        if ledger is not None:
            final += ledger[json.loads(identity)[0]] == 'passed'
        elif manifest['mode'] != 'e2e':
            final += statuses[-1] == 'passed'
        elif statuses[0] == 'passed':
            final += all(status == 'passed' for status in statuses)
        elif len(statuses) > 1 and expected is None:
            uncertain = True
        else:
            final += (expected is not None and expected > 0 and len(statuses) == expected + 1
                      and all(status == 'passed' for status in statuses[1:]))
    if uncertain or manifest['status'] == 'interrupted':
        final = None
    return {'first': first, 'final': final, 'total': len(groups)}


def _confirmation_ledger(manifest, groups, group_files):
    """Validate physical outcomes independently of their native Allure colors."""
    metadata = manifest.get('metadata', {})
    if manifest['mode'] != 'e2e' or 'e2e_outcomes' not in metadata:
        return None
    from .confirmations import confirmation_outcome
    rows = metadata['e2e_outcomes']
    error = 'Invalid Allure confirmation outcome ledger'
    if type(rows) is not list or len(rows) != len(groups) or 'e2e_reruns' not in metadata:
        raise ViewerError(error)
    ledger = {}
    by_history = {json.loads(identity)[0]: identity for identity in groups}
    if len(by_history) != len(groups):
        raise ViewerError(error)
    for row in rows:
        if (type(row) is not dict or set(row) != {'history_id', 'first_status', 'final_status',
                'attempts', 'complete', 'attempt_statuses', 'result_files'}
                or type(row['history_id']) is not str or row['history_id'] not in by_history
                or row['history_id'] in ledger or type(row['attempts']) is not int
                or type(row['complete']) is not bool or type(row['attempt_statuses']) is not list
                or type(row['result_files']) is not list):
            raise ViewerError(error)
        identity = by_history[row['history_id']]
        attempts = groups[identity]
        statuses = row['attempt_statuses']
        if (not statuses or row['attempts'] != len(statuses) or len(attempts) != len(statuses)
                or any(type(status) is not str or status not in {'passed', 'failed', 'broken',
                    'error', 'skipped', 'cancelled', 'interrupted'} for status in statuses)
                or row['result_files'] != [group_files[identity][number] for number in sorted(attempts)]
                or row['first_status'] != statuses[0]):
            raise ViewerError(error)
        try:
            final, incomplete = confirmation_outcome(statuses, metadata['e2e_reruns'])
        except ValueError:
            raise ViewerError(error) from None
        if incomplete:
            final = 'interrupted' if manifest['status'] == 'interrupted' else 'failed'
        if row['final_status'] != final or row['complete'] != (not incomplete):
            raise ViewerError(error)
        projected = ('broken' if final == 'passed' and len(statuses) > 1 else
                     'passed' if final == 'passed' else 'skipped' if final == 'skipped' else 'failed')
        expected = [{'error': 'broken', 'cancelled': 'broken', 'interrupted': 'broken'}.get(status, status)
                    for status in statuses[:-1]] + [projected]
        if [attempts[number] for number in sorted(attempts)] != expected:
            raise ViewerError(error)
        ledger[row['history_id']] = final
    return ledger


def count_label(mode, counts):
    total = counts['total']
    def fraction(value, percentage=False):
        if value is None:
            return 'unavailable'
        text = f'{value} / {total}'
        return text + f' ({100 * value / total if total else 0:.1f}%)' if percentage else text
    if mode == 'e2e':
        return f'first {fraction(counts["first"], True)} — final {fraction(counts["final"], True)}'
    return 'pass counts unavailable' if counts['final'] is None else fraction(counts['final']) + ' passed'
