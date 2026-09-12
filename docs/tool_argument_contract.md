# Tool argument validation

All built-in Agent tools validate their advertised JSON Schema before XAE
precondition probes and dispatch. Foreground, background and CLI model loops
also validate before asking for execution permission. Existing documented HMI
legacy aliases and provider string-to-JSON/number/boolean normalization run first.

The top-level argument envelope rejects unknown keys. Nested user dictionaries
remain open according to their own schema (for example HMI attributes). Required
fields, types, enums and bounds are checked recursively. Values, source code and
secrets are not echoed in validation errors.

Failures return `status=invalid_arguments`, `error_type=tool_arguments`,
`not_executed=true`, field-level `issues`, `required_parameters`,
`allowed_parameters` and `next_action`. They are not ADS/COM failures. Only this
pre-dispatch validation path may assert that no operation was executed; an error
raised inside a tool is not retroactively labelled not executed.

## Runtime symbols

`plc_read_value` / `plc_write_value` use `name` for the actual ADS symbol name,
such as `MAIN.fbPid.bEnable` (an illustration, not a discovered symbol).
`TIPC^...` is an engineering tree path and is not an ADS symbol. No automatic
tree-path truncation or guessed symbol conversion is permitted. Batch scalar
tools validate each `symbols` / `values` item, including required type/value.

## Failure budget

Every tool call still receives a tool result. Consecutive unexecuted argument
errors in one model response consume at most one failure strike, allowing the
next model response to correct the batch. Repeating bad arguments over five
responses still reaches the existing stop threshold. Actual execution failures
count individually; permission and uncertain-write stop rules remain in place.

## Audit (2026-09-12)

206 built-in schemas checked; 107 declare mandatory arguments. The previous
dispatch layer normalized values but did not validate the full schema. Reproduced
examples include `path` instead of `name` for ADS reads, `name/new_name` instead
of `old/new` for PLC rename, `id` instead of `control_id` for HMI control editing,
and an unsupported PLC write `area`. Registry tests ensure missing required fields
fail before host probes or execution. This audit does not assert correctness of
all tool implementations or external MCP server contracts.
