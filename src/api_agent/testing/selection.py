"""Deterministic overlapping test-group selection for the workbench."""
from dataclasses import dataclass
from .collection import CollectionError


@dataclass(frozen=True)
class SelectionRow:
    id: str
    label: str
    kind: str
    members: frozenset[str]
    selected: bool
    count: int
    total: int


class SelectionModel:
    def __init__(self, tests):
        self.tests = tuple(dict((test.id, test) for test in tests).values())
        self.selected_ids = {test.id for test in self.tests}

    def groups(self):
        specifications = [("all", "All tests", "all", frozenset(test.id for test in self.tests))]
        for tag in sorted({tag for test in self.tests for tag in test.tags}):
            specifications.append((f"tag:{tag}", tag, "tag", frozenset(test.id for test in self.tests if tag in test.tags)))
        for repo, file in sorted({(test.repo, test.file) for test in self.tests}):
            specifications.append((f"file:{repo}::{file}", f"{repo}::{file}", "file", frozenset(test.id for test in self.tests if (test.repo, test.file) == (repo, file))))
        specifications.extend((test.id, test.id, "test", frozenset({test.id})) for test in self.tests)
        return tuple(SelectionRow(key, label, kind, members, bool(members) and members <= self.selected_ids,
                                  len(members & self.selected_ids), len(members))
                     for key, label, kind, members in specifications)

    def toggle(self, group_or_id):
        key = group_or_id.id if isinstance(group_or_id, SelectionRow) else group_or_id
        row = next((row for row in self.groups() if row.id == key), None)
        if row is None:
            raise CollectionError(f"Unknown selection group or test: {key}")
        if row.selected:
            self.selected_ids.difference_update(row.members)
        else:
            self.selected_ids.update(row.members)

    @property
    def selected_tests(self):
        return tuple(test for test in self.tests if test.id in self.selected_ids)
