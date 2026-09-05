# Job report EOF trailer protocol

Every dispatched job **must** end its log with the trailer below. `runjob
report` normally reads a bounded tail (default **4096** bytes), but it may
return `absent` only after it has read the whole file: **`absent` implies
`bytes_skipped == 0`**. A bounded read that does not contain a valid frame is
always `window_clipped`, never successful absence.

## Trailer (exact keys, this order, one per line)

```
STATUS: COMPLETE | FIXED | BLOCKED | FAILED
CAUSE_CLASS: <short slug, or NONE>
TESTS: <n passed / n failed, or NONE>
RESIDUAL: <non-negative integer count>
COMMIT: OK + <sha> | NONE | DENIED + <error>
MODIFIED_PATHS: <comma-separated repo-relative paths, or NONE>
EVIDENCE_PATH: <repo-relative path to the full report, or NONE>
```

## Grammar

1. **Keys are case-sensitive.** Keys must start in column zero and use exactly
   `KEY:` followed by zero or one ASCII space. The keys occur once each and in
   the displayed order.
2. **Strict single-line.** Each key has exactly one physical UTF-8 line: no
   blank line inside the block, leading whitespace, C0/C1 controls (including
   ANSI ESC/CSI; CRLF/LF delimiters excepted), or wrapped value. A recognisable broken frame in a fully-read
   log is `malformed`, even if it is followed by ordinary prose.
3. **Scalars** (after the optional space):
   - `STATUS` is exactly `COMPLETE`, `FIXED`, `BLOCKED`, or `FAILED`.
   - `CAUSE_CLASS` is `NONE` or 1–64 characters from `[A-Za-z0-9._-]`.
   - `TESTS` is `N passed / M failed` (decimal integers) or `NONE`.
   - `RESIDUAL` is a non-negative decimal integer without a sign.
   - `COMMIT` is `OK + ` plus 4–64 hexadecimal characters, `NONE`, or
     `DENIED + ` plus non-whitespace text with no leading/trailing whitespace.
   - A repo-relative path is slash-separated nonempty segments from
     `[A-Za-z0-9._+=@-]+`; `.` and `..` are forbidden segments. Thus paths do
     not start with `/`, escape the repository, or contain spaces.
   - `MODIFIED_PATHS` is `NONE` or one or more repo-relative paths separated by
     commas. One ASCII space after each comma is accepted and normalized away
     in JSON output; spaces elsewhere are invalid.
   - `EVIDENCE_PATH` is one repo-relative path or `NONE`.
4. **EOF-anchored.** The trailer must be the final non-blank content. Only
   trailing blank lines and one literal `===== exit:N` launcher marker may
   follow. A second marker or any variation is malformed. Mid-log quotes,
   fences, and heredocs are not accepted.
5. **Size.** Encoded trailer size must be ≤`MAX_REPORT_TRAILER_BYTES`
   (32768). That cap is independent of the default **4096**-byte read
   window: a trailer between those sizes is `window_clipped` on the
   default read and `present` once the caller widens `--bytes` (or reads
   the whole file). An oversize trailer is `malformed` when fully read
   and `window_clipped` when bounded.
6. **No guessing.** The parser validates every key but does not attest git,
   tests, or ledger truth (self-attestation only).

## Consumer states (`runjob report --json`)

| `trailer_state` | meaning | exit |
|---|---|---|
| `present` | valid trailer at EOF | 0 |
| `absent` | whole file read; no trailer evidence | 0 |
| `malformed` | whole-file attempted trailer violates grammar | 3 |
| `window_clipped` | bounded read did not yield a valid trailer; widen `--bytes` | 4 |
| `missing_log` | log file missing (I/O error) | 1 |

`--bytes 0` is an empty window. It returns `window_clipped` for a nonempty
file, and `absent` only for an actually empty file. Unknown uid → exit 2.

## Example

```
Work finished; suite green; report at docs/gates/example.md.

STATUS: COMPLETE
CAUSE_CLASS: NONE
TESTS: 81 passed / 0 failed
RESIDUAL: 0
COMMIT: OK + 14ddd9b
MODIFIED_PATHS: runjob.py,docs/job-report-protocol.md
EVIDENCE_PATH: docs/gates/example.md
```
