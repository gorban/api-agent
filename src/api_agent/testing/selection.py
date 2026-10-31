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
        self._index_catalog()

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

    def _index_catalog(self):
        tags, files = {}, {}
        for test in self.tests:
            files.setdefault((test.repo, test.file), set()).add(test.id)
            for tag in test.tags:
                tags.setdefault(tag, set()).add(test.id)
        specs = [('all', 'All tests', 'all', frozenset(test.id for test in self.tests))]
        specs.extend((f'tag:{tag}', tag, 'tag', frozenset(tags[tag])) for tag in sorted(tags))
        specs.extend((f'file:{repo}::{file}', f'{repo}::{file}', 'file', frozenset(files[repo, file]))
                     for repo, file in sorted(files))
        specs.extend((test.id, test.id, 'test', frozenset((test.id,))) for test in self.tests)
        self._specifications = tuple(specs)
        self._spec_by_key = {key: (kind, members) for key, _, kind, members in specs}

    def groups(self):
        selected = self.selected_ids
        return tuple(SelectionRow(key, label, kind, members, bool(members) and members <= selected,
                                  len(members & selected), len(members))
                     for key, label, kind, members in self._specifications)

    def toggle(self, group_or_id):
        key = group_or_id.id if isinstance(group_or_id, SelectionRow) else group_or_id
        self.toggle_many((key,))

    def toggle_many(self, keys):
        """Toggle the union once, so overlapping groups never toggle twice."""
        rows = []
        for key in keys:
            if key not in self._spec_by_key:
                raise CollectionError(f"Unknown selection group or test: {key}")
            kind, members = self._spec_by_key[key]
            rows.append((key, kind, members))
        if not rows:
            return
        members = frozenset().union(*(members for _, _, members in rows))
        self.toggle_members(members, (key for key, kind, _ in rows if kind != 'test'))

    def toggle_members(self, members, scopes=()):
        """Toggle a projected union while retaining only complete group intent."""
        members = frozenset(members)
        if not members:
            return
        if members <= self.selected_ids:
            previous_count = len(self.selected_ids)
            self.selected_ids.difference_update(members)
            if previous_count - len(self.selected_ids) > len(self.selected_ids):
                retained = self.selected_ids.copy()
                self.selected_ids.clear()
                self.selected_ids.update(retained)
        else:
            self.selected_ids.update(members)
            self.selected_scopes.update(scopes)
        checked = {scope for scope in self.selected_scopes if scope in self._spec_by_key
                   and self._spec_by_key[scope][1] and self._spec_by_key[scope][1] <= self.selected_ids}
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
        self._index_catalog()
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
