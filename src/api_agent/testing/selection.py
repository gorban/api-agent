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
        self.exclusions = ()
        self._index_catalog()

    def set_defaults(self, repositories=None):
        """Select whole repository scopes, including future recollected tests."""
        self.selected_scopes = ({"all"} if repositories is None else
                                {f"repo:{repo}" for repo in repositories})
        self.selected_ids = {test.id for test in self.tests
                             if repositories is None or f"repo:{test.repo}" in self.selected_scopes}
        if self.exclusions:
            allowed = select_tests(self.tests, self.exclusions, allow_missing=True, allow_empty=True)
            self.selected_ids.intersection_update(test.id for test in allowed)

    def preselect(self, selectors):
        """Select startup matches against the full suite, retaining group intent."""
        self.exclusions = tuple(selector for selector in selectors if selector.startswith('-'))
        includes = tuple(selector for selector in selectors if not selector.startswith('-'))
        defaults = [test for test in self.tests if test.id in self.selected_ids]
        self.selected_ids = {test.id for test in select_tests(self.tests, selectors, defaults=defaults)}
        if includes:
            self.selected_scopes = set()
        for selector in includes:
            repo, separator, value = selector.partition("::")
            if not separator or repo not in {test.repo for test in self.tests}:
                repo, value = None, selector
            if repo is None and value in {test.repo for test in self.tests}:
                self.selected_scopes.add(f"repo:{value}")
            elif repo is None and value.startswith("repo:"):
                self.selected_scopes.add(value)
            elif value in {tag for test in self.tests if repo is None or test.repo == repo for tag in test.tags}:
                self.selected_scopes.add(f"{repo}::tag:{value}" if repo else f"tag:{value}")
            elif value.startswith("tag:"):
                self.selected_scopes.add(f"{repo}::tag:{value[4:]}" if repo else value)
            else:
                if value.startswith("file:"):
                    file_repo, separator, file_value = value[5:].partition("::")
                    if separator:
                        repo, value = file_repo, file_value
                    else:
                        value = file_repo
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
        if self.exclusions:
            allowed = select_tests(self.tests, self.exclusions, allow_missing=True, allow_empty=True)
            members &= {test.id for test in allowed}
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
        allowed_ids = ({test.id for test in allowed} if self.exclusions else {test.id for test in self.tests})
        checked = {scope for scope in self.selected_scopes if scope in self._spec_by_key
                   and self._spec_by_key[scope][1]
                   and self._spec_by_key[scope][1] & allowed_ids <= self.selected_ids}
        self.selected_scopes = {scope for scope in self.selected_scopes if scope in checked or (
            scope.startswith("repo:") and all(test.id in self.selected_ids
                for test in self.tests if test.repo == scope[5:] and test.id in allowed_ids)
            or "::tag:" in scope and any(test.repo == scope.split("::tag:", 1)[0]
                                      and scope.split("::tag:", 1)[1] in test.tags
                                      and test.id in self.selected_ids for test in self.tests))}

    def refresh_for_run(self, tests):
        """Recollect once, expanding checked groups but preserving exact exclusions."""
        allowed = {test.id for test in select_tests(self.tests, self.exclusions, allow_missing=True, allow_empty=True)} if self.exclusions else {test.id for test in self.tests}
        checked = {row.id for row in self.groups() if row.members and row.members & allowed <= self.selected_ids}
        scopes = {scope for scope in self.selected_scopes if scope in checked or "::tag:" in scope or scope.startswith("repo:")}
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
                "all" in scopes or f"repo:{test.repo}" in scopes or bool(tags & test.tags)
                or any(test.repo == repo and tag in test.tags for repo, tag in qualified_tags)
                or f"{test.repo}::{test.file}" in files))
        }
        if self.exclusions:
            allowed = select_tests(self.tests, self.exclusions, allow_missing=True, allow_empty=True)
            self.selected_ids.intersection_update(test.id for test in allowed)

    @property
    def selected_tests(self):
        return tuple(test for test in self.tests if test.id in self.selected_ids)
