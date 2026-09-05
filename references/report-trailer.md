# Structured job endings

Use this only when a caller needs a bounded, machine-readable result from a long
transcript. The parser's normative grammar is in
[`docs/job-report-protocol.md`](../docs/job-report-protocol.md).

Append this instruction to the job prompt:

```text
End your final output with exactly these seven consecutive lines. Keep every value
on one line and put explanatory prose above the block.

STATUS: COMPLETE | FIXED | BLOCKED | FAILED
CAUSE_CLASS: <short-slug or NONE>
TESTS: <n passed / n failed, or NONE>
RESIDUAL: <integer count>
COMMIT: OK + <sha> | NONE | DENIED + <error>
MODIFIED_PATHS: <comma-separated repo-relative paths, or NONE>
EVIDENCE_PATH: <repo-relative path to the full report, or NONE>
```

Read the ending with:

```sh
runjob report UID --json
```

The default read is bounded to 4096 bytes. If the result is `window_clipped`, widen
once up to the independent 32768-byte trailer cap:

```sh
runjob report UID --json --bytes 32768
```

`present` means the block is syntactically valid; it does not prove its claims.
`absent`, `malformed`, `window_clipped`, and `missing_log` must remain distinct. Do
not infer success from prose when the structured ending is missing or invalid.
