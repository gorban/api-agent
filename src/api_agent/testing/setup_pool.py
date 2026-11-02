"""Owned shared-setup backends partitioned by immutable test configuration."""
from copy import deepcopy
import json

from .suite_lock import retain_suite_cleanup


class SetupPool:
    def __init__(self, profiles, owner_root, **services):
        self.profiles = deepcopy(profiles)
        self.config_fields = deepcopy(services.pop('config_fields', None))
        self.owner_root, self.services = owner_root, services
        self.owners = []
        self._test_owners = {}

    def start(self):
        shared = {}
        owners_by_key = {}
        for test_id, row in self.profiles.items():
            key = json.dumps(row['config'], sort_keys=True, default=str, allow_nan=False)
            if key not in shared:
                from .setup_backend import SetupBackend
                from .workflow_capture import capture_sources
                sources = capture_sources(row['config'], self.config_fields)
                owner = SetupBackend(row['config'], self.owner_root, **self.services,
                                     **({'capture_sources': sources} if sources else {}))
                self.owners.append(owner)
                owners_by_key[key] = owner
                retain_suite_cleanup(owner)
                shared[key] = owner.start()
            self._test_owners[test_id] = owners_by_key[key]
            if shared[key] is not None:
                row['setup_backend'] = shared[key]
        return deepcopy(self.profiles)

    def stderr_for(self, test_id):
        owner = self._test_owners.get(test_id)
        return '' if owner is None else getattr(owner, 'diagnostic_stderr', '')

    def _all(self, method, *args):
        first = None
        result = []
        for owner in self.owners:
            try:
                value = getattr(owner, method)(*args)
                if value:
                    result.extend(value)
            except BaseException as error:
                if first is None:
                    first = error
        if first is not None:
            raise first
        return tuple(result)

    def close(self):
        self._all('close')

    def prepare_publication(self):
        self._all('prepare_publication')

    def finish_run(self):
        return self._all('finish_run')

    def watch_inspection(self, callback):
        self._all('watch_inspection', callback)
