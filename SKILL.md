---
name: jobs
description: Launch, monitor, and manage off-session LLM work with runjob. Use when the user asks to hand work to another model, run an independent implementation/review/research job asynchronously, operate a file-backed job fleet, or inspect existing runjob jobs. Do not use for in-session subagent delegation or launch paid/external model work without the user's authorization.
---

# Jobs

Use `runjob` to hand bounded work to an installed headless model CLI and to keep a
machine-wide execution ledger. Confirm that `runjob` is on `PATH`; if it is not,
point the user to the repository's installation steps instead of improvising a
machine-wide install.

Launching a job can consume quota or incur provider charges. Treat an explicit
request to dispatch or hand off work as authorization for that launch. A request
to explain, inspect, draft, or plan is not launch authorization.

## Choose the execution shape

- Use `runjob run` for one bounded job. It runs in the selected working directory
  and does not create a branch or worktree. For work that may edit or commit, use
  a disposable Git worktree or the fleet mode.
- Use `runjob fleet run` for several jobs, dependency ordering, automatic isolated
  worktrees, resumable sessions, or per-engine concurrency caps. Read
  [references/fleet.md](references/fleet.md) before creating fleet files.
- Use `runjob log`, `runjob report`, and `runjob dash` to inspect work already in
  flight. These are read-only; `runjob repair` is an explicit ledger mutation.

Do not substitute this skill for an in-session subagent when the user asked for
delegation within the current conversation.

## One-shot jobs

Use a prompt file for substantial instructions so the dispatch is reviewable:

```sh
runjob run --engine codex --effort high \
  --cwd /absolute/path/to/worktree --file /absolute/path/to/prompt.md
```

Omit `--model` or `--effort` when the user has not selected them and the engine has
a sensible CLI default. Do not invent current model names. Read
[references/engines.md](references/engines.md) when engine-specific flags,
authentication, or sandbox behavior matters.

`runjob run` returns after launch unless `--wait` is supplied. Keep the printed uid.
Inspect it with:

```sh
runjob log --uid UID
runjob log --active
runjob report UID --json
runjob dash
```

Logs and prompts may contain source code or sensitive instructions. They live under
`${DEV_JOBS_HOME:-~/.config/dev-jobs}`; do not publish or attach them without a
separate review.

## Completion and handoff

Report the uid, engine/model actually requested, working directory, and whether the
job is merely launched or has completed. Do not describe a process launch as a
successful result. When waiting, verify the terminal ledger state and summarize the
job's actual output, including failures or residual work.

For machine-readable job endings, use the exact protocol in
[references/report-trailer.md](references/report-trailer.md). Treat its fields as
the job's claims; independently verify commits, tests, and modified paths before
merging or publishing anything.
