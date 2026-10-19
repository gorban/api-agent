# Operation-specific browser visibility

[Included Studio](implementation_plan_i2a.md)

- [x] RUN_UI selects only the optional frontend in Run local. It appears in local configuration review/help, retains ordinary source precedence, and does not appear as an applicable integration/E2E configuration field.
- [x] Integration and E2E test browsers are headless regardless of RUN_UI unless DEBUG enables headed inspection. Preserve E2E_DEBUG compatibility for headed retained E2E inspection. Prepared test-owner inspection uses the same visibility decision; ordinary runs dispose browsers normally. This owner/selector policy does not implement the future E2E runner or certify its end-to-end retention.
- [x] E2E still requires its frontend independent of RUN_UI; integration acquires frontend services only through explicitly selected UI tests. Collection starts no browser/services. Run local keeps its headed UI policy and optional frontend allocation.
- [x] Keep the multiple-selected-tests warning specific to effective debug inspection, including E2E_DEBUG for E2E. RUN_UI alone does not request inspection or that warning. Use the existing selector confirmation; no new E2E runner is implied.
- [x] Prove the delivered owner/selector visibility and retention combinations and configuration applicability with meaningful controls and focused mutations; complete applicable coverage and independent review.
- [x] Human acceptance of the revised test visibility policy: integration behavior with and without DEBUG and NO_STD_IN is accepted; required human authentication remains a visible exception.

Required human authentication is an explicit exception: attended login and the existing bounded visible hardware/security handoff may open visibly. Automated test login remains headless absent debug inspection regardless of RUN_UI; preserve account binding, bounded retries and owned cleanup. Local RUN_UI retains its local browser meaning.

- [ ] Before claiming a complete E2E executor, normalize/share effective DEBUG (including E2E_DEBUG compatibility) across its UI, LLM and backend retention consumers. The current owner/selector policy is not a new E2E runner and does not guarantee retention outside those consumers.

The future Run local executor must keep all required local browsers headed, including later lazy authentication with the frontend disabled. Current direct-factory local RUN_UI/inspection controls are library-level behavior, not Run local runtime acceptance; the future local consumer owns the headed/inspection policy.
