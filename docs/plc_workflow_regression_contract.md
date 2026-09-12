# PLC workflow contract regression — 2026-09-12

## Implemented

- Complete candidate preflight now uses all blocking findings from the same
  `review_write_candidate` function used by source writes, plus dependency
  semantics. Previously it included only the two FB invocation rules.
- Syntax-invalid candidates skip COM dependency lookup, matching the write
  path's ordering. Result fields explicitly say no write authorization and no
  compiler verification.
- Successful fragment preflight no longer resets a project's recurring missing
  capability count. Successful writes or explicit library changes retain their
  previous reset behavior. Counters remain turn-local.
- `tests/test_plc_workflow_regression.py` exercises real preflight, guarded
  write/readback and build-state/token logic with a fake COM/compile boundary.
  Cases cover successful pipeline, compiler failure, invalid-source rejection,
  readback mismatch, full quality-rule visibility and repeat-gap convergence.

## Important distinctions

Full candidate preflight is conservative; it does not accept a caller-provided
baseline to waive old defects. Existing-object writes can use live delta review
to tolerate unchanged historical quality findings. Therefore a conservative
preflight rejection is not proof that every scoped patch must also fail.
Preflight passing does not waive dirty-source, target identity, authorization,
baseline, concurrent-edit, compiler or runtime checks.

The workflow tests are integration tests of Agent contracts, NOT an actual
TwinCAT compiler, a full model conversation or physical-device acceptance.
They do not write, save, build or start the user's current PLC.

## Still needed for product-wide acceptance

- Versioned document evidence ingestion beyond the currently supported public
  aliases and live interface subset; don't guess undocumented types.
- More representative offline fixtures (methods, templates, HMI, library/target
  changes), provider-driven conversation tests, and a user-authorized disposable
  engineering project for real compiler acceptance.
- Observe first-attempt valid-call success rate separately from legitimate
  compiler findings, permission refusals and unavailable external services.

No zero-error guarantee: correct rejection is not a tool defect, and hiding a
real failure does not improve task completion.
