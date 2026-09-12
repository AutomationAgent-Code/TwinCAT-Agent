# TwinCAT Agent Static Analysis (TCSA)

`TCSA` is an offline Structured Text analyser supplied by TwinCAT Agent. It
reads PLC declarations, implementations and method bodies through the existing
COM bridge, but it does not require TE1200, build the project or modify XAE.

It is deliberately **not TE1200**. Every result contains
`te1200_executed: false` and `te1200_status: not-executed`. A clean TCSA report
must never be described as “TE1200 passed”. Use TE1200 when a release requires
the official analyser or its semantic checks.

## Use

```powershell
tc-template plc analyze
tc-template plc analyze --max-complexity 20
tc-template plc analyze --rule SA0038=error --rule SA0043=warning
```

The MCP/Agent tool is `plc_static_analysis(max_complexity=20, rule_severities={"SA0038":"error"})`. Both forms are
read-only. Error-level TCSA findings give the CLI a non-zero exit code; warnings
are reported for review but do not fail the command.

The existing pre-write `plc_review` / Agent write gate also runs TCSA on the
post-write candidate. Its error-level checks block a write; warnings remain
visible as advisories for incremental cleanup.

## Supported rules (independent implementation)

| TCSA rule | Inspired by | Detection scope | Severity |
|---|---|---|---|
| `TCSA0027` | SA0027 | Duplicate enum constants across supplied objects; `qualified_only` exemption | warning |
| `TCSA0033` | SA0033 | Unused local/output/constant/global variables in supplied sources | warning |
| `TCSA0038` | SA0038 | Read of own output, including method bodies and local shadowing | warning |
| `TCSA0043` | SA0043 | Global variable used by one supplied POU; external HMI/ADS requires review | warning |
| `TCSA0037` | SA0037 | Direct assignment to a `VAR_INPUT` variable | error |
| `TCSA0040` | SA0040 | Literal zero denominator; supported scalar intervals/guards for other denominators | error for literal zero; warning for possible zero |
| `TCSA0062` | SA0062 | Constant scalar conditions and Boolean assignments in a limited value model | warning |
| `TCSA0075` | SA0075 | CASE without ELSE, respecting nested blocks | warning |
| `TCSA0054` | SA0054 | Direct `=` / `<>` comparison involving declared `REAL` or `LREAL` | warning |
| `TCSA0140` | SA0140 | Commented-out ST control/assignment statement | warning |
| `TCSA0167` | SA0167 | `FB_*` or known standard stateful block in `VAR_TEMP` | warning |
| `TCSA0172` | SA0172 | Literal one-dimensional array bounds with supported index/loop/guard intervals | warning |
| `TCSA0178` | SA0178 | Token-based ST subset: nesting, Boolean chains, SEL/MUX/JMP; default threshold20 | warning |

The `inspired_by` field is a traceability hint only; it does not claim identical
implementation or coverage to any official `SAxxxx` rule.

`rule_severities` accepts `off`, `warning`, or `error` for a referenced SA rule;
it is a per-run explicit policy, not an import of XAE settings. The Agent must
not lower/disable rules without the user's requested policy. Candidate reviews
always use default policy and `project_wide=False`, so a partial candidate is
not presented as a whole-project global usage analysis.

## Known limits

- No compiler semantic model: no pointer/reference lifetime, type flow, alias
  analysis, array-bound proof or indirect-call resolution.
- No scheduler model: cross-task data races and multi-task output writes are not
  detected.
- Unknown divisors produce a possible-risk warning, not a claim of certain zero.
  Supported nonzero assignments, simple guards and integer-to-real conversions
  can establish a local nonzero fact. Unsupported calls invalidate known state.
- Interval analysis is a subset, not a full CFG/fixed-point/alias analysis.
  Mutable global initializers are not treated as constants: HMI/ADS may write them.
- Library symbols, general suppression pragmas and XAE rule configuration are
  not resolved/imported. C0555 remains a compiler encoding diagnostic.
- Complexity is validated against the published examples but is not guaranteed
  identical for all ST constructs, Boolean types or expression nesting.

Implementation: `tc_template/static_rules.py`. Official sources and comparison
boundaries: [official_static_analysis_alignment.md](official_static_analysis_alignment.md).

Run the regular coding-standard review (`plc lint`) and `plc build` alongside
TCSA. For a release governed by TE1200, run TE1200 separately and report its
actual output.
