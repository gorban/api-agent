"""Explicit unit platform scope, independent of skip messages."""
import sys


_PLATFORMS = frozenset({'linux', 'darwin', 'win32'})
_PROPERTY = 'api_agent_unit_platform'
_INVALID = {'invalid': True}


def validate_platforms(values):
    if (type(values) not in (tuple, list)
            or any(type(value) is not str or value not in _PLATFORMS for value in values)
            or len(set(values)) != len(values)):
        raise ValueError('Invalid unit_platform declaration')
    return tuple(sorted(values))


def collected_platforms(item):
    markers = list(item.iter_markers('unit_platform'))
    if not markers:
        return ()
    if len(markers) != 1 or markers[0].kwargs or not markers[0].args:
        raise ValueError('unit_platform requires one nonempty platform declaration')
    return validate_platforms(markers[0].args)


def declaration(platforms, host):
    platforms = validate_platforms(platforms)
    if type(host) is not str or not host:
        raise ValueError('Invalid unit platform host')
    return {'platforms': list(platforms), 'host': host} if platforms else None


def explanation(platforms, host):
    return f'Not applicable on {host}; requires ' + ', '.join(validate_platforms(platforms)) + '.'


def _valid_report(value):
    if value is None:
        return True
    if type(value) is not dict or set(value) != {'platforms', 'host'}:
        return False
    try:
        expected = declaration(value['platforms'], value['host'])
    except ValueError:
        return False
    return expected is not None and type(value['platforms']) is list and value == expected


def reports_match(platforms, reports, host):
    expected = declaration(platforms, host)
    for report in reports:
        observed = report.get('applicability')
        if not _valid_report(observed) or observed != expected:
            return False
    return True


def unit_platform_plugin():
    """Load pytest only in the unit worker, keeping discovery imports lean."""
    import pytest

    class UnitPlatformPlugin:
        def pytest_configure(self, config):
            config.addinivalue_line('markers', 'unit_platform(*platforms): supported unit host platforms')

        def pytest_collection_modifyitems(self, items):
            for item in items:
                try:
                    item._api_agent_unit_platforms = collected_platforms(item)
                except ValueError as error:
                    raise pytest.UsageError(str(error)) from None

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self, item):
            platforms = item._api_agent_unit_platforms
            if platforms and sys.platform not in platforms:
                pytest.skip(explanation(platforms, sys.platform))

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item):
            outcome = yield
            report = outcome.get_result()
            scoped = declaration(item._api_agent_unit_platforms, sys.platform)
            if scoped is not None:
                report.user_properties.append((_PROPERTY, scoped))

    return UnitPlatformPlugin()


def report_declaration(report):
    values = [value for name, value in report.user_properties if name == _PROPERTY]
    if not values:
        return None
    if len(values) != 1 or not _valid_report(values[0]) or values[0] is None:
        return dict(_INVALID)
    return values[0]
