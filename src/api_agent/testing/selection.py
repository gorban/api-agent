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
        self.selected_scopes = {"all"} if not selectors else set()
        for selector in selectors:
            repo, separator, value = selector.partition("::")
            if not separator or repo not in {test.repo for test in self.tests}:
                repo, value = None, selector
            if value.startswith("tag:"):
                self.selected_scopes.add(f"{repo}::tag:{value[4:]}" if repo else value)
            else:
                matches = [test for test in self.tests if (repo is None or test.repo == repo)
                           and value == test.file]
                self.selected_scopes.update(f"file:{test.repo}::{test.file}" for test in matches)

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
        self.toggle_many((key,))

    def toggle_many(self, keys):
        """Toggle the union once, so overlapping groups never toggle twice."""
        rows_by_key = {row.id: row for row in self.groups()}
        rows = []
        for key in keys:
            if key not in rows_by_key:
                raise CollectionError(f"Unknown selection group or test: {key}")
            rows.append(rows_by_key[key])
        if not rows:
            return
        members = frozenset().union(*(row.members for row in rows))
        if members <= self.selected_ids:
            self.selected_ids.difference_update(members)
        else:
            self.selected_ids.update(members)
            self.selected_scopes.update(row.id for row in rows if row.kind != 'test')
        checked = {group.id for group in self.groups() if group.selected}
        self.selected_scopes = {scope for scope in self.selected_scopes if scope in checked or (
            "::tag:" in scope and any(test.repo == scope.split("::tag:", 1)[0]
                                      and scope.split("::tag:", 1)[1] in test.tags
                                      and test.id in self.selected_ids for test in self.tests))}

    def refresh_for_run(self, tests):
        """Recollect once, expanding checked groups but preserving exact exclusions."""
        checked = {row.id for row in self.groups() if row.selected}
        scopes = {scope for scope in self.selected_scopes if scope in checked or "::tag:" in scope}
        qualified_tags = {(repo, tag) for scope in scopes
                          for repo, separator, tag in [scope.partition("::tag:")] if separator}
        tags = {scope[4:] for scope in scopes if scope.startswith("tag:")}
        files = {scope[5:] for scope in scopes if scope.startswith("file:")}
        old_ids = {test.id for test in self.tests}
        selected = self.selected_ids.copy()
        self.tests = tuple(dict((test.id, test) for test in tests).values())
        self.selected_ids = {
            test.id for test in self.tests
            if test.id in selected or (test.id not in old_ids and (
                "all" in scopes or bool(tags & test.tags)
                or any(test.repo == repo and tag in test.tags for repo, tag in qualified_tags)
                or f"{test.repo}::{test.file}" in files))
        }

    @property
    def selected_tests(self):
        return tuple(test for test in self.tests if test.id in self.selected_ids)
