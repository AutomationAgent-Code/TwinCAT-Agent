# Paired POU source update

`plc_write(name, tree_path, area="declaration", code=<full declaration>,
implementation=<full implementation>)` supports existing top-level ST program,
function and function block objects. Existing per-area calls remain unchanged.
Method, property, interface, DUT and GVL paired updates are intentionally rejected.

The existing outer approval and source-conflict gate still applies. The candidate
combines both new regions for full quality/dependency review. Preflight approval
is not reused as write permission; the live baseline is read again. Empty
declarations are rejected by full-candidate preflight, including empty placeholder
objects whose earlier approval was misleading.

The native bridge receives the reviewed two-region baseline and compares both
regions before applying changes. Both COM setters execute in the same native
request, with intermediate checks and final readback. This is a compensated
operation, **not** an atomic COM transaction: XAE can observe an intermediate
state. A failed update attempts to restore the prior text only when buffers still
match known old/new values. Unknown content or failed restoration is `uncertain`,
never success. Legacy PowerShell fallback must not silently ignore the paired
implementation. No SaveAll, build, login, PLC start or additional XAE is invoked.

Baseline comparison uses `newline_normalized_v1`: CRLF/CR become LF and terminal
newline characters are ignored, matching the paged read transport. Spaces, tabs,
case and interior blank lines remain significant. The same comparison applies
before each setter, on readback and when deciding whether rollback is safe.
Original text is retained for restoration; normalization is not a source rewrite.
Initial conflicts return expected/actual normalized SHA-256 hashes and changed
areas without source text. Missing/malformed baselines remain non-executing
conflicts. This fixes unchanged CRLF documents being rejected against LF read
results; it does not disable concurrent-edit protection.

Diagnostic reports now parse JSON tool results before recursive source/credential
redaction. Findings and capability reasons remain structured even after long
earlier fields; selected diagnostic lists are not silently cut at 80 entries.
Reports cannot reconstruct results already truncated before report creation.
