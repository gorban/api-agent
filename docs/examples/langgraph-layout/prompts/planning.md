# Illustrative planning prompt

Produce a structured retrieval plan for the supplied request. Use supplied history only for relevant user-facing continuity. Distinguish user requirements, proposed retrieval, and existing tool evidence.

Declare missing information rather than inventing it. The node owns the output schema and registered tools; the runtime owns timing and retries. Do not calculate or extend execution timeouts through prompt instructions.

This is an example fragment, not a replacement for an existing integration's prompt.
