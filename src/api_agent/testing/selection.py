"""Deterministic overlapping test-group selection for the workbench."""
from dataclasses import dataclass
from .collection import CollectionError, select_tests


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
        self.selected_scopes = {"all"}

    def preselect(self, selectors):
        """Select startup matches against the full suite, retaining group intent."""
        self.selected_ids = {test.id for test in select_tests(self.tests, selectors)}
        self.selected_scopes = set()
        for row in self.groups():
            if row.kind == "file" and row.selected and any(
                selector == row.label or selector == row.label.split("::", 1)[1]
                for selector in selectors
            ):
                self.selected_scopes.add(row.id)
            elif row.kind == "tag" and row.selected and f"tag:{row.label}" in selectors:
                self.selected_scopes.add(row.id)
        if self.selected_ids == {test.id for test in self.tests}:
            self.selected_scopes.add("all")

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
            if row.kind != "test":
                self.selected_scopes.add(row.id)
        checked = {group.id for group in self.groups() if group.selected}
        self.selected_scopes.intersection_update(checked)

    def refresh_for_run(self, tests):
        """Recollect once, expanding checked groups but preserving exact exclusions."""
        checked = [row for row in self.groups() if row.selected and row.id in self.selected_scopes]
        all_selected = any(row.kind == "all" for row in checked)
        tags = {row.label for row in checked if row.kind == "tag"}
        files = {(test.repo, test.file) for test in self.tests
                 if any(row.kind == "file" and test.id in row.members for row in checked)}
        old_ids = {test.id for test in self.tests}
        selected = self.selected_ids.copy()
        self.tests = tuple(dict((test.id, test) for test in tests).values())
        self.selected_ids = {
            test.id for test in self.tests
            if test.id in selected or (test.id not in old_ids and (
                all_selected or bool(tags & test.tags) or (test.repo, test.file) in files))
        }

    @property
    def selected_tests(self):
        return tuple(test for test in self.tests if test.id in self.selected_ids)
