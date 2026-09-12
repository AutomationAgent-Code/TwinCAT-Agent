# HMI installed-version write contract

HMI source indexing accelerates reads; this separate, mandatory gate validates
candidate markup before writes. It is not disabled by the optional PLC review
toggle or automatic approval mode.

## Covered entry points

Agent, MCP and CLI calls through `ps_com`: `hmi-create-view`,
`hmi-user-control-create`, `hmi-control-edit`, `hmi-controls-batch`, `hmi-write-markup`.
Batch editing is exposed by Agent/MCP as `tc_hmi_controls_batch`; see
[batch controls](hmi_batch_controls.md) for the bounded operation contract.
Legacy `hmi_view.write_view` now uses the same guarded bridge. Event edits already
route through control editing. PowerShell apply paths reject missing validation
proofs instead of providing an unguarded alternate entry.

Flow: generate a side-effect-free preview → resolve exact `.hmiproj` Framework
and `packages.config` package versions → load installed descriptions and schemas →
validate candidate → apply only if project, schema file stamps, source baseline
and candidate hash still match → DTE save and exact readback.
Existing unsaved editor changes are not overwritten or implicitly saved. New
project items retain their existing authorized project reload workflow.

## Checks

- Exact installed control type and inherited attributes, not namespace prefixes.
- Unknown/read-only attributes; missing/duplicate IDs; inline/script duplicates.
- Draft-04 types, enums, bounds, objects, arrays and local schema references via
  jsonschema. JSON-valued HTML attributes are passed as JSON strings; direct child
  JSON script attributes are also checked. No remote schema retrieval occurs.
- Whole-page/new-page writes validate the whole candidate. Batch edits validate
  all added/updated targets using one catalog and one parsed candidate, retaining
  page-wide ID checks and source/schema conflict protection. Single-control edits
  validate the target plus page ID uniqueness, allowing repairs without requiring
  unrelated pre-existing errors to be fixed in the same operation. Removal is
  structural repair only. Browser validity is never inferred from a partial repair.
- Symbol expressions are syntax/bindability checked and explicitly deferred for
  online validation. They do not prove that an ADS symbol exists or its live value
  has the correct type. Unsupported/ambiguous schema references fail closed.

Use `tc_hmi_control_schema` / `hmi control-schema` before generating controls.
The backend now proactively injects installed-version preparation before each
model step for both HMI and PLC; see [generation preparation](generation_preparation.md).
Explicit schema reads remain necessary for attributes outside the bounded packet.
Without a type it lists installed types; with type it lists inherited attributes;
with an attribute it returns bounded related schema definitions. For example,
`data-tchmi-text-color` expects a SolidColor object such as `{"color":"#ffffff"}`,
not a plain `#ffffff` string. The exact installed version remains authoritative.
`tc_hmi_validate` also runs the same schema checks against registered saved markup,
so malformed values no longer yield a misleading structural-only zero-error result.

## Limits / verification

This gate does not execute JS, guarantee stylesheet/layout semantics, validate
every custom UserControl parameter schema, or activate/publish/start a server or
PLC. Unknown definitions are blocked, not guessed. Successful writes include
`browser_verification_required=true`; the Agent must use the existing read-only
browser validation on an already-running preview, and separately verify ADS if
authorized. No production interaction is triggered automatically.

Contract generation reads local package definitions on each operation so edits and
version changes are not hidden by a stale catalog. Current multi-description schema
name conflicts are rejected when referenced rather than picking an arbitrary winner.
The package includes jsonschema and its interpreter-matched dependencies; packaging
must smoke-test them with the embedded interpreter.

## Native event serialization

An installed event name such as `.onPressed` is an API selector, not necessarily
the executable saved registration name. Framework 14.3.360 and the reference
project's native event records use
`%ctx%owner::Id|EventRegistrationMode=Resolve%/ctx%.onPressed`.
The event catalog detects this support in the exact installed control implementation;
dedicated native-event writes then serialize owner-context registrations. Legacy
bare short names and control-qualified names can be migrated by preview/upsert.
No Custom event or JavaScript event-delegation workaround is introduced.

The symbol validator permits this exact owner expression and `.onName` suffix only
inside an `event` field. Ordinary value bindings still reject added prefixes,
suffixes and unknown tags. Browser clicks must verify event delivery: a schema-valid
Trigger alone did not catch the former short-name registration bug.
