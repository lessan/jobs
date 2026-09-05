# jobs

`jobs` is an agent skill plus a single-file Python supervisor for running headless LLM
CLI jobs outside the current session. It launches one-shot tasks, manages isolated
Git-worktree fleets, records an append-only machine ledger, captures terminal
outcomes, and serves a local dashboard.

The project is deliberately local-first: there is no hosted service and no telemetry.
Prompts, logs, and state remain under `~/.config/dev-jobs` unless
`DEV_JOBS_HOME` overrides it.

## What it supports

- One-shot jobs with `runjob run`
- File-backed fleets with dependencies, concurrency caps, resumable sessions, and
  one Git worktree per job
- Claude Code, Codex CLI, Grok CLI, Google Antigravity (`agy`), z.ai through Claude
  Code (`glm`), and an operator-configured Anthropic-compatible local gateway
- A shared log, bounded report reader, stale-process repair command, and local web
  dashboard
- Codex and Claude Code skill discovery

`runjob` does not install or authenticate provider CLIs. Their command-line
interfaces change independently; verify the adapter notes in
[`references/engines.md`](references/engines.md) against the versions you use.

## Requirements

- Python 3.11 or newer
- Git 2.20 or newer for fleet worktrees
- macOS or Linux
- At least one supported, already-authenticated model CLI

No third-party Python package is required at runtime.

## Install

Clone the repository and expose the launcher:

```sh
git clone https://github.com/lessan/jobs.git "$HOME/.local/share/jobs"
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/jobs/runjob.py" "$HOME/.local/bin/runjob"
```

Ensure `~/.local/bin` is on `PATH`, then install the skill for the agent products
you use:

```sh
mkdir -p "$HOME/.codex/skills" "$HOME/.claude/skills"
ln -s "$HOME/.local/share/jobs" "$HOME/.codex/skills/jobs"
ln -s "$HOME/.local/share/jobs" "$HOME/.claude/skills/jobs"
```

Restart the agent application after adding a skill. Install only the symlink for
products you actually use.

## Quick start

Launch one job from the current directory:

```sh
runjob run --engine codex --effort high \
  "Review the parser and report concrete findings"
```

The command prints a uid and returns after launch. Inspect it with:

```sh
runjob log --uid UID
runjob log --active
runjob dash
```

Add `--wait` to stream the result until the process exits. Use `--file prompt.md`
for substantial prompts.

Important: one-shot jobs run directly in `--cwd` and can modify its current branch.
Use a disposable worktree for editing tasks, or use fleet mode, which creates a
dedicated branch and worktree for every job.

## Fleet mode

Copy [`config/project.example.toml`](config/project.example.toml) to
`.dev-jobs.toml` in a Git repository, create job cards under `jobs/`, then run:

```sh
runjob fleet status
runjob fleet run
```

The runner respects dependencies and per-engine caps, resumes supported sessions
after quota limits, and leaves completed work on each job's branch. See
[`references/fleet.md`](references/fleet.md) for the card format and lifecycle.

## Credentials and state

Provider CLIs normally use their own login. Gateway keys can be placed in
`~/.config/dev-jobs/secrets.env` (mode `600`) using
[`secrets.env.example`](secrets.env.example) as a starting point. Resolution order
is process environment, project `.env`, then the global secrets file; the first
value wins.

The state directory contains:

```text
~/.config/dev-jobs/
├── jobs.jsonl       append-only machine ledger
├── logs/            one-shot prompts and output
├── secrets.env      optional gateway credentials
└── *-config/        isolated CLI configuration for gateway adapters
```

Do not commit or share this directory. Logs can contain source code, prompts,
provider responses, branch names, and absolute paths.

## Safety model

Provider CLIs are launched in unattended modes. That is powerful and potentially
destructive. `runjob` supplies process supervision and, in fleet mode, Git isolation;
it is not a security boundary. Review each provider's sandbox flags, scope prompts
tightly, keep sensitive paths out of external-provider worktrees with
`sanitize_excludes`, and never assume that a successful process exit proves the
work is correct.

## Development

```sh
python3 -m unittest discover -v
uvx pyright runjob.py
python3 scripts/validate_skill.py
```

The production script is typed and the test suite exercises process liveness,
ledger repair, quota parking, Git isolation, output classification, and the report
trailer parser.

## License

MIT. See [`LICENSE`](LICENSE).
