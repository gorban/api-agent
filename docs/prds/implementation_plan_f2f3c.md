# High-versus-low reasoning comparison

[Direct reasoning comparison](implementation_plan_f2f3.md)

- [x] Compare low and high reasoning effort through the ordinary included GPT provider and application HTTP route. Require verified support for both efforts before inference. An available medium effort cannot substitute for missing high support.
- [x] Preserve the exact frozen code-generation prompt, model, shared per-pair marker, fresh markers between pairs and runs, three pairs, six concurrent inference requests and existing heartbeat, deadline, cancellation and cleanup ownership. Request settings remain identical within each pair except reasoning effort.
- [x] Keep stable record order low then high. Compute each signed difference as high reasoning tokens minus low reasoning tokens. Pass only when at least two gaps are >=50 and every gap is >-50. Preserve every unfavorable pair. Reject medium labels, absent usage and invalid measurements; do not alter or reorder the signed thresholds.
- [x] Controlled usage derives from actual native effort. Verify high/low transmission, nonce and request identity, conflicting provider defaults, and explicit effort ordering independently of transport arrival. Swapped native efforts and ignored effort must fail; medium substitution must fail preflight or scoring rather than silently retaining the earlier comparison.
- [ ] Observe one frozen six-call high/low suite. Preserve all results without retries, replacement pairs or prompt tuning.
- [ ] Complete independent review and joined validation.
- [ ] Human acceptance remains open.

The earlier successful and failed low/medium observations remain evidence for those original requests only. This wider effort contrast is an empirical smoke test, not a guarantee of token separation or population reliability. The independent [stability follow-up](implementation_plan_f2f3a.md) remains deferred.
