# Native Windows viewer listener isolation

[Allure viewers](implementation_plan_d1.md) · [Viewer lifetime](implementation_plan_d1e.md)

POSIX immediate reopen and active-listener refusal are verified separately. Windows currently preserves the prior non-reuse setting. That setting does not establish Winsock exclusive-address ownership; no native Windows exclusivity or immediate-reopen guarantee is claimed.

- [ ] Before claiming exclusive binding on Windows, evaluate and implement the explicit supported socket option before bind, with actionable failure handling and retained cleanup. Do not enable port sharing or adopt an existing listener. Keep POSIX behavior unchanged.
- [ ] On native Windows, test loopback and wildcard owners, competing default/reuse-enabled sockets, failed contender cleanup, real HTTP shutdown/reopen and process death. Establish the actual reopen policy rather than applying POSIX TIME_WAIT assumptions. Bound every proof and use disposable ports.
- [ ] Gate platform-specific regressions by their supported contract. Run applicable exact unit coverage, installed-package/lifecycle tests and independent review. Record native evidence before checking exclusivity acceptance; source inspection or Linux execution alone is insufficient.

Schedule this with broader reporting lifecycle hardening. It does not block the independently verified POSIX port-reuse repair and does not authorize changing user-owned listeners.
