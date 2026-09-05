# Fleet mode

Use fleet mode when jobs may edit a repository, several tasks can run concurrently,
or work needs dependencies and resumable checkpoints.

## Job cards

Create one Markdown file per job under the configured `jobs_dir` (default `jobs/`).
Files whose names start with `_` are ignored. The parser accepts a deliberately
small `key: value` frontmatter format; it is not general YAML.

```markdown
---
id: parser-review
engine: codex
model: MODEL
effort: high
after: parser-build
sanitize: yes
---

Review the parser changes against the specification. Run the focused tests, record
concrete findings, and commit only a report on your assigned branch.
```

Supported input fields:

- `id`: stable job identifier; defaults to the filename stem.
- `engine`: adapter name; defaults to `claude`.
- `model` and `effort`: optional provider selections.
- `branch`: branch name; defaults to `<branch_prefix>/<filename-stem>`.
- `after`: comma-separated job ids that must reach `done` first.
- `sanitize`: `yes` forces configured sparse-checkout exclusions; `no` disables
  them; omitted uses the engine default.

The runner owns `status`, `session`, `pid`, `attempts`, `retry_at`, `started`,
`launched_at`, and `updated`. Do not hand-edit those fields while a runner is active.

## Lifecycle

```text
queued -> running -> done
                  -> limited -> running
                  -> failed
```

Each eligible job gets a worktree at `<worktrees_dir>/<id>` and a dedicated branch.
The prompt tells the worker to maintain `PROGRESS.md`, commit on its assigned branch,
and end successful work with `DONE:`. A limited resumable session is retried after
its reset time. An unclassified, unwrapped fleet process is retried at most three
times before it becomes `failed`.

Stopping `runjob fleet run` detaches the supervisor; child jobs continue. Starting
the runner again reloads card state, checks process liveness, captures supported
session ids, and resumes eligible work.

## Configuration

Copy `config/project.example.toml` to the repository root as `.dev-jobs.toml`.
Keep top-level keys before TOML table headers.

The public defaults allow one concurrent job per engine. Raise caps only after
checking provider quota and billing. `usage_gate` commands can hold a fleet near a
quota threshold. Gate-read failures are intentionally fail-open, so a quota gate is
an operational convenience rather than a spending boundary.

`sanitize_excludes` removes configured repository paths from external-provider
worktrees through sparse checkout. It reduces accidental exposure but does not stop
an engine from accessing other paths allowed by its process sandbox.

## Operating commands

```sh
runjob fleet status
runjob fleet run
runjob log --active
runjob log --uid PROJECT-fleet-JOB_ID
```

Review the job branch and re-run its tests before merging. Fleet completion means
the worker reported a usable terminal signal; it is not an independent quality gate.
