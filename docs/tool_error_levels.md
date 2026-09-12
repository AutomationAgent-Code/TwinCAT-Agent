# Tool error impact

Both foreground and worker execution annotate tool results with `error_policy`.
This metadata does not change success, acceptance evidence, permission or retry safety.

| Level | Meaning | Execution policy |
| --- | --- | --- |
| L0 | Success | Continue |
| L1 | Successful with warnings | Continue, retain warnings |
| L2 | Failed step | Correct prerequisites/arguments using evidence; no blind write retry |
| L3 | Dependency or authorization blocked | Exhausted read recovery/semantic capability pauses dependent work, not the whole turn; authorization still follows existing approval gates |
| L4 | Uncertain effects / failed write verification | Stop automatic execution and reconcile actual state read-only |

Do not report completion while dependent steps remain blocked. If no independent,
authorized work remains, report partial completion and the required prerequisite.
Existing deterministic request suppression, write preconditions, batch gates and
consecutive-failure budgets remain enabled. A successful unrelated read is not
evidence that a missing compiler capability has been repaired.

The machine-readable catalog is `tc_agent/tool_error_catalog.py`: error types,
precondition names and failure statuses map to argument, source, conflict,
identity, state, authorization, capability, diagnostics, transport, uncertain,
cancelled and unknown families. Classification uses structured fields, not
localized error prose. The dispatch entry point annotates results as well as the
foreground/worker policy boundaries. Partial creation and unconfirmed readback
statuses are failures, never success evidence.

`tests/test_tool_error_catalog.py` audits all literal `error_type` and `condition`
emitters in both Python packages, including keyword arguments and `_condition`
contracts. Adding a new literal requires adding a deliberate classification.
Dynamic exception classes and third-party MCP/vendor errors cannot be exhaustively
enumerated: unknown codes are retained with `classified=false`, not silently
treated as successful or automatically retryable. Numerical vendor codes remain
opaque without a verified vendor/domain mapping. This catalog does not claim
complete coverage of arbitrary external errors or parse nested source/user data
as execution failures. UI and model summaries receive the same metadata.
