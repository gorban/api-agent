"""Shared limits for recorded manifest publication and ingestion."""

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_FILES = 16_384


class ManifestBudgetError(ValueError):
    """The finalized manifest exceeds its publication budget."""


def check_manifest_budget(files, body):
    if len(files) > MAX_MANIFEST_FILES or len(body) > MAX_MANIFEST_BYTES:
        raise ManifestBudgetError("Allure manifest exceeds its publication budget")
