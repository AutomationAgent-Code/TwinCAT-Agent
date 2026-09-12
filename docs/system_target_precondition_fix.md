# System target versus PLC runtime selection

2026-09-12: tc_restart was blocked before dispatch because the common target
identity branch used an intentionally empty PLC inventory and then unconditionally
required a selected PLC. No restart had executed in those failed records.

The shared gate now performs PLC selection only when the declared contract needs
ads_endpoint, runtime_selection or plc_runtime_run. Target-only SYSTEM/I/O/NC
contracts finish this common check after target identity validation. Existing
handler scope/state checks and outer approval policy are unchanged.

Regression tests cover restart, activate, Config/Run transitions with present and
missing target IDs, and enumerate every registered target-only contract. They
mock all handlers and COM calls: no real restart or engineering mutation occurs.
Existing multi-PLC and ADS write Run-state tests remain enabled.
