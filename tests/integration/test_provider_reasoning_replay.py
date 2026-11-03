"""Ordered native continuation through the selected real provider, when supplied."""
from dataclasses import replace
from uuid import uuid4

import pytest

from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
from api_agent.workflows.evidence import publish

pytestmark = [pytest.mark.feature('Native reasoning continuation'),
              pytest.mark.credential_authorities('coordinator')]


def test_provider_reasoning_replay(llm_provider, llm_selection):
    selection = next(row for row in llm_provider.describe() if row.selected_llm == llm_selection)
    maximum = min(4096, selection.max_output_tokens or 4096) if 'max_output_tokens' in selection.features else None
    effort = 'low' if selection.reasoning_efforts and 'low' in selection.reasoning_efforts else None
    first = LLMRequest(llm_selection, CompletionRequest((Message('user', (
        TextContent('Check that 17 times 19 equals 323. Reply with exactly: Ok'),)),),
        generation=GenerationOptions(max_output_tokens=maximum, reasoning_effort=effort,
                                    display_summary='display_reasoning' in selection.features)), str(uuid4()))
    answer = llm_provider.complete(first)
    assert answer.outcome == 'completed'
    publish({'kind':'native_reasoning_replay','records':[
        {'kind':'native_continuation_present','accepted':answer.payload.continuation is not None},
        {'kind':'display_reasoning_present','accepted':bool(answer.reasoning_summary)}]})
    # The exact assistant payload includes opaque state when the provider supplied
    # it. A provider without native state still exercises ordinary conversation.
    following = replace(first, request_id=str(uuid4()), payload=replace(first.payload,
        messages=(*first.payload.messages, answer.payload,
                  Message('user', (TextContent('Reply with exactly: Ok'),)))))
    final = llm_provider.complete(following)
    assert final.outcome == 'completed'
    assert ''.join(part.text for part in final.payload.content).strip().lower().rstrip('.') == 'ok'
