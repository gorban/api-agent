# Large-suite interactive selection

[Test-picker responsiveness](implementation_plan_a12.md)

- [ ] Profile the ordinary interactive unit picker with at least 20,743 real collected cases. Measure initial population separately from navigation at three and six seconds after load, extended idle, individual/file/group toggles, range selection, search, mouse selection/copy, review entry/return and Start. Record input-to-visible-row latency, event-loop stalls, CPU, memory and queued refreshes; compare the smaller integration catalog.
- [ ] Attribute selection indexing, grouping, configuration work, table updates, cache invalidation, text layout and actual visible rendering separately. Determine what the table already virtualizes before recommending an additional window. A worker thread alone does not prove CPU contention or rendering is resolved.
- [ ] Present a bounded plan with measured causes, latency/memory targets and alternatives such as immutable membership indexes, incremental cell updates, coalesced background work, windowed rows or paging. Obtain human approval of that plan before the larger implementation.
- [ ] Preserve stable exact identities, overlapping groups, selection beyond the visible window, ranges, search, copy, costs, current configuration/account checks and authoritative execution admission. Scrolling must not change which tests run.
- [ ] Verify the eventual repair with representative real catalogs, independent/external review, applicable exact coverage and human responsiveness acceptance. Retain failed observations; do not declare the large-suite issue fixed from small-catalog measurements.

This is deferred work. Current noninteractive unit execution and coverage acceptance do not approve interactive picker performance.
