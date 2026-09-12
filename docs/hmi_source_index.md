# HMI saved-source index

The Agent now uses the same read strategy as its PLC saved-source layer:
catalog first, exact indexed reads second, contiguous pages for large source.
This is not an XAE editor-buffer mirror or HMI/PLC runtime verification.

## Tools

- `tc_hmi_source_index(project?, refresh=false)`: incrementally index registered
  files and controls. Omit project to sync all HMI projects in the bound solution.
- `tc_hmi_source_catalog(project?, kind="files"|"controls", file?, query?, offset=0,
  limit=80)`: compact catalog, follow returned `next_offset`.
- `tc_hmi_read_smart(file, project?, area="auto", control_id?, ...)`: known file
  goes directly to its index; known control uses exact SQL filtering. `auto`
  returns controls for markup and source for scripts/configuration. `events` and
  `bindings` return selected control details. `source` returns file-wide source.

CLI equivalents: `hmi source-index`, `hmi source-catalog`, `hmi read-smart`.
MCP exposes the same three tools. Existing `tc_hmi_read` remains an explicit
compatibility path for files outside the supported saved-reference index.

## Scope and freshness

The backend binds the exact solution and XAE PID. Standalone calls resolve the
solution through the PID-bound bridge. Only `.sln`-referenced `.hmiproj` files and
their registered Content/Compile/None items are considered. Multiple HMI projects
require an exact selector for reads. Project-external linked files and MSBuild
globs/expressions are reported as unsupported, not guessed or recursively scanned.
Generated directories and package contents are excluded.

SQLite lives at `<solution directory>/.TwinCATAgent/hmi_source_index.sqlite` and
is disposable; it is independent of conversation history. First access indexes
the requested file only. Catalog/sync checks registered files incrementally.
File timestamp/size and the shared Agent cache invalidation revision are checked
on access. `refresh=true` forces a disk read. This stage is lazy synchronization,
not a background file watcher or editor push channel. External changes preserving
both timestamp and size need explicit refresh. Source is rechecked after reading;
concurrent changes cause retry errors instead of delivering stale data. XML errors,
duplicate control IDs, missing files and removed references cannot reuse old controls.

`source=disk_index`, `read_layer=hmi_sqlite`, `cache_hit`, `source_hash`,
`source_consistent`, and source stamps describe the read. `live_xae=false`,
`authoritative=false`, `dirty_unknown=true` explicitly mean unsaved XAE buffers
are not verified. Nothing automatically saves/reloads the editor, starts the
server, connects to PLC or writes engineering files.

## Completeness

Catalog `complete` describes index coverage; `next_offset` describes pagination.
Read `controls_truncated`/`next_control_offset` and
`truncated`/`next_content_offset` describe unread content. Source offsets are
UTF-16 units, copied directly from the result; CRLF and surrogate pairs are preserved.
Model-context pages keep records atomic and recalculate cursors for the records
actually delivered. Oversized individual records request source pagination rather
than splicing or silently truncating attributes. Bindings are scoped to the
selected control page, not a full-project semantic or online validation.

Direct child script-backed HMI attributes are read as saved text, without migration;
ambiguous inline/script duplicates are errors. Event strings and binding candidates
are evidence of stored content, not proof of valid TE2000 schema or working symbols.
