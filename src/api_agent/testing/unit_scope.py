"""Owner defaults for unit selection and coverage, independent of discovery."""
from pathlib import Path
from .collection import repository_label


def owner_unit_only(context, values):
    return (Path(context.owner_root).resolve() != Path(context.base_root).resolve()
            and not values.get("ALL_UNIT", False))


def unit_default_tests(context, tests, *, all_unit=False):
    tests = tuple(tests)
    if all_unit or Path(context.owner_root).resolve() == Path(context.base_root).resolve():
        return tests
    owner = repository_label(context, context.owner_root)
    return tuple(test for test in tests if test.repo == owner)


def unit_coverage_complete(context, tests, selected, *, all_unit=False):
    required = {test.id for test in unit_default_tests(context, tests, all_unit=all_unit)}
    return bool(required) and required <= {test.id for test in selected}


EMPTY_OWNER_UNITS = "No unit tests belong to the active owner. Use --all-unit to run dependency and base units."
