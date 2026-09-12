# HMI variable discovery

Use `tc_hmi_variable_search` before finding PLC variables for a control.
The selected XAE supplies the PLC name, actual ADS port and target NetId; a
multi-PLC solution requires an explicit `plc`. No port ordering is assumed.

- `query` searches runtime symbol metadata; `type_filter` narrows the type.
- `parent` lists immediate struct/array children; use `next_offset` to continue.
- `source=auto` tries ADS, then falls back to exported TMC roots with the online
  error retained. `source=ads` never falls back. TMC results are not online proof.
- Runtime metadata is freshly loaded per request. No persistent cache is used.
  Searching skips array expansion and is capped at 50,000 visited symbols and
  eight nested levels. Explicit parent expansion is preferable for large arrays.
- Uncompiled declarations still use `plc_search` / `plc_read`. They must not be
  advertised as exported runtime symbols. Offline member expansion is not yet
  available in this tool.

An online result with an existing HMI mapping returns `binding_candidate`.
Pass it unchanged to `tc_hmi_bind_variable`, together with the page, control ID
and installed attribute name. Query `tc_hmi_control_schema` first. The binding
tool checks endpoint, mapping and current symbol type again, then previews or
applies through the existing DTE control editor. It never reloads the project
or writes PLC values. Candidate members use the registered root and HMI `::`
member syntax; nonzero or multidimensional/nested arrays require an explicit
verified member mapping. New mappings still use `tc_hmi_bind_plc` and its existing
configuration-update behavior; this feature does not implement hot mapping creation.

Readback means the page was saved, not that the HMI Server displays live values.
Follow with browser validation and the binding diagnosis when appropriate.

References: [SymbolExpression path tokens](https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/4707239947.html).
