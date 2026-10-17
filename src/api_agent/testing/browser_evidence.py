"""Bounded original-pixel evidence from explicitly owned UI contexts.

Capture safety belongs to the selected UI provider. A policy must mask form
values and registered credentials before returning original PNG pixels; absent
or unverifiable safety means returning None. Core cannot certify arbitrary DOM,
canvas, embedded media or racing page content as safe. Never supply LLM-provider
authentication contexts to this UI-only helper.
"""
import itertools
import json

from api_agent.providers.progress import component_timeout

MAX_CONTEXTS = 16
MAX_PAGES = 32
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


async def capture_ui_evidence(contexts, boundary, submit, *, capture_policy=None,
                              phase='failure'):
    """Submit immutable attachments before caller-owned browser teardown.

    ``capture_policy(page, boundary)`` is async and returns safely masked PNG
    bytes or None. It must consult the live registered-secret boundary and own
    any page/frame-specific safety checks. It must not navigate or rearrange the
    page: failure evidence preserves the current viewport. Success-specific
    preparation belongs to the caller, before this function.

    ``submit((title, bytes, MIME))`` is synchronous, bounded and nonblocking;
    the caller binds it to the current test/attempt's immutable recording path.
    Ordinary capture/submission failures never replace the primary outcome.
    Cancellation and other BaseExceptions propagate. No raw browser strings or
    exception messages become metadata. Returned tuples contain successful
    submissions only; the caller need not retain them.
    """
    if boundary.policy.allow_content is not True:
        return ()
    if phase not in ('success', 'failure'):
        return ()
    submitted = []
    total = 0
    pages_seen = 0

    def emit(context, page, image=None):
        title = f'UI {phase} context {context} page {page}'
        if image is None:
            content = json.dumps({'status': 'unavailable', 'phase': phase,
                                  'context': context, 'page': page},
                                 sort_keys=True).encode()
            attachment = (title, content, 'application/json')
        else:
            attachment = (title, image, 'image/png')
        try:
            submit(attachment)
        except Exception:
            return
        submitted.append(attachment)

    try:
        owned = tuple(itertools.islice(contexts, MAX_CONTEXTS + 1))
    except Exception:
        emit(0, 0)
        return tuple(submitted)
    for context_index, context in enumerate(owned[:MAX_CONTEXTS], 1):
        try:
            pages = tuple(itertools.islice(context.pages, MAX_PAGES + 1))
        except Exception:
            emit(context_index, 0)
            continue
        for page_index, page in enumerate(pages, 1):
            if pages_seen >= MAX_PAGES:
                emit(context_index, 0)
                return tuple(submitted)
            pages_seen += 1
            emitted = False
            if capture_policy is not None:
                try:
                    async with component_timeout('UI browser evidence', 5):
                        candidate = await capture_policy(page, boundary)
                        image = None
                        if (type(candidate) is bytes and candidate.startswith(PNG_SIGNATURE)
                                and len(candidate) <= MAX_IMAGE_BYTES
                                and total + len(candidate) <= MAX_TOTAL_BYTES):
                            image = candidate
                            total += len(candidate)
                        # There must be no await between the provider's final
                        # live-secret check and immutable submission. Progress
                        # scope exit may yield while draining its heartbeat.
                        emit(context_index, page_index, image)
                        emitted = True
                except Exception:
                    pass
            if not emitted:
                emit(context_index, page_index)
    if len(owned) > MAX_CONTEXTS:
        emit(0, 0)
    return tuple(submitted)
