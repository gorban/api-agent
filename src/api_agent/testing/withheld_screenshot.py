"""Fixed parent-owned evidence when UI content capture is disabled."""
import hashlib
import os
from pathlib import Path
import stat


ASSET = Path(__file__).with_name('screenshot_withheld.png')
ASSET_BYTES = 29575
ASSET_SHA256 = 'b3bf13785e2bdc4276a902b144466538f8ac4af7e92d316f82a984a3d2808c05'
TITLE = 'UI screenshot withheld (LOG_CONTENT=False)'
EXPLANATION = (
    'UI screenshot withheld. Screenshots may contain confidential data. '
    'This test used LOG_CONTENT=False. Run tests with LOG_CONTENT=True to enable '
    'original UI screenshots. Credential masking and provider safety checks still apply.'
)


def withheld_attachment():
    """Use only reviewed static pixels; never inspect browser or worker evidence.

    Missing or altered installed resources degrade to the same fixed text,
    without exporting their contents or changing the recorded test outcome.
    """
    try:
        if ASSET.is_symlink():
            raise ValueError('Invalid static image')
        descriptor = os.open(ASSET, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != ASSET_BYTES:
                raise ValueError('Invalid static image')
            data = stream.read(ASSET_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != ASSET_SHA256:
            raise ValueError('Invalid static image')
    except (OSError, ValueError):
        return TITLE, EXPLANATION.encode(), 'text/plain'
    return TITLE, data, 'image/png'
