# Interactive authentication in Run local

[Runtime requirements](implementation_plan_a9k.md)

- [x] The normal Run local presentation supplies the existing masked interactive
  authentication transport to its selected LLM provider and shared credential
  retrieval. Runtime code accepts a generic operation-owned interaction service;
  terminal consumers may omit it. Schema, help and configuration review remain inert.
- [x] Stop, Quit and unmount cancel only the current operation's interaction.
  Authentication cancellation and channel disposal run outside the presentation
  loop. Retain pending cleanup, retry failed disposal, and do not publish stopped
  until owned work settles. Borrowed root and sibling channels remain independent.
- [x] Independently review all new executable statements and reported branches,
  including joined normal-app provider recovery, masked replies, cancellation,
  cleanup retry, startup races and sibling isolation.
- [ ] Complete external and human acceptance of the normal launch interaction.

Private inputs follow the [clipboard-origin and paste requirements](implementation_plan_a9k2.md).
