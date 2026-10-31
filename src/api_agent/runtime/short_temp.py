"""Choose a bounded, trusted system temporary root for local IPC."""
import os
from pathlib import Path
import stat


def trusted_short_root(candidates, *, suffix, limit=100, uid=None):
    uid = os.geteuid() if uid is None else uid
    for candidate in candidates:
        try:
            root = Path(candidate).resolve(strict=True)
            info = root.stat()
            if (not stat.S_ISDIR(info.st_mode) or not info.st_mode & stat.S_ISVTX
                    or info.st_uid not in {0, uid}
                    or len(str(root / suffix).encode()) > limit):
                continue
            for parent in root.parents:
                evidence = parent.stat()
                if (not stat.S_ISDIR(evidence.st_mode) or evidence.st_uid not in {0, uid}
                        or (evidence.st_mode & 0o022 and not (evidence.st_uid == 0 and evidence.st_mode & stat.S_ISVTX))):
                    break
            else:
                return root
        except OSError:
            continue
    raise OSError('No trusted short system temporary directory')
