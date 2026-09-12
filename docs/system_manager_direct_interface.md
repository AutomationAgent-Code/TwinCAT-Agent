# System Manager direct interface

`tc_agent.system_manager_interface` is an optional, read-oriented adapter for
the embedded workbench's local System Manager WebSocket. It does not replace
the existing COM bridge and it does not activate configuration, log in to a
PLC, change targets, or auto-confirm XAE dialogs.

The adapter deliberately keeps three identities separate:

| Identity | Meaning |
| --- | --- |
| endpoint URL | A configured or uniquely probed local WebSocket endpoint |
| protocol project ID | The `pid` returned by `projectList` and used in System Manager commands |
| XAE process ID | The Windows process ID used by the existing COM/cache locks |

Endpoint resolution accepts `SYSMAN_WS_URL` first and `GAS_SERVER_URL` second.
There is no implicit 8088 selection. Candidate probing must be supplied by the
host and must verify the `flare` WebSocket subprotocol; multiple responders are
reported as ambiguous rather than selecting the first one.

The direct read path sends both `interface: null` and `implementation: null`
to `sm.plcpou`, as required by the observed protocol. Nested methods and
property accessors are addressed by the caller's explicit `tid`/`tname`; the
adapter does not infer a child from a shallow tree. `PlcSourceAdapter` falls
back to the existing COM reader on endpoint, protocol, timeout, or transport
failure and preserves the direct failure in `direct_error`.

`sm.plccompilermsg` responses are fail-closed. `level=10` and an empty message
array do not prove completeness. `combine_build_and_diagnostics` reports a
verified build only when `sm.build` was performed, the compiler response
explicitly proves completeness, and no fatal/error message remains.

`TreeItemChangeRouter` handles `sm.treeItemChanged` at document/member scope,
debounces through the existing `CacheRefreshQueue`, rejects unknown project
bindings and stale versions, and requests an explicit resync after disconnect.
It does not claim that notifications cover every unsaved editor buffer.

`BatchExecutor` combines read operations when the transport supports
`batch_request`. Mutations remain opt-in: they require permission, an expected
revision, preflight comparison, and optional post-write readback. Partial
success, conflict, timeout, cancellation, and uncertain in-flight results are
represented per item. A timeout is not treated as transport cancellation.

The reference portable bundle was audited as text only. Its WebSocket command
names and field shapes informed this compatibility layer; the bundle is not a
runtime dependency and was not executed. No live direct endpoint was available
during implementation, so protocol behavior against a particular TwinCAT/XAE
build remains unverified until an explicit endpoint is configured.
