# Persistent startup summary after authentication

[Startup presentation](implementation_plan_k2b.md).

- [x] Keep the Run local component/address/dashboard summary visible and reachable after real password-manager recovery screens open and close. Focusing or streaming logs must not scroll the entire screen away from the summary.
- [x] Bound summary and diagnostic pane heights independently from logs. Large component/address sets and narrow or short terminals must retain useful log space and navigation; provide independent scrolling to reach every address, applicable dashboard login and diagnostic line. Preserve mouse text-selection focus and existing copy shortcuts while allowing keyboard summary focus.
- [x] Preserve literal selectable summary text, source/browser menus, lifecycle status and credentials only in the explicit local presentation. Never copy dashboard credentials into structured snapshots, log events or artifacts to make them visible.
- [x] Reproduce using the actual TUIInteraction screen push/pop with ready state generated while recovery is open. Verify rendered compositor frames, regions, focus and scrolling at ordinary and small terminal sizes; complete causal checks, applicable coverage and independent review.
- [ ] Human acceptance of the post-login summary, log space and summary navigation.
