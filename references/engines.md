# Engine adapters

Read this reference when choosing an engine, diagnosing authentication, or changing
an invocation. `runjob` supervises provider CLIs; it does not install, authenticate,
or make their network calls itself.

## Shared cautions

- All adapters use unattended approval modes. A provider CLI can read and modify
  anything its own sandbox and OS permissions allow.
- Model names, effort levels, subscription limits, and CLI flags change. Check the
  installed CLI's help before overriding a model or effort value.
- Prompts and output go to the configured provider unless the `local` adapter points
  at infrastructure you control.
- One-shot jobs run in place. Fleet jobs get isolated Git worktrees and can use
  sparse checkout to omit configured paths, but sparse checkout is not a complete
  sandbox.

## Built-in adapters

| Engine | Executable | Authentication | Notes |
|---|---|---|---|
| `claude` | `claude` | Claude Code login | Uses print mode and `bypassPermissions`; supports session resume. |
| `codex` | `codex` | Codex CLI login | Uses `codex exec --approve-for-me`; effort is passed as a config value. |
| `grok` | `grok` | Grok CLI login | Uses `--always-approve` and a workspace sandbox; supports session resume. |
| `agy` | `agy` | Antigravity CLI login | Gemini-only adapter; prompt is passed with `-p`; output must be JSON. |
| `glm` | `claude` | `GLM_API_KEY` | Points an isolated Claude Code config at z.ai's Anthropic-compatible endpoint. |
| `local` | `claude` | `LOCAL_API_KEY` | Points an isolated Claude Code config at `LOCAL_BASE_URL`. No endpoint is built in. |

The launcher augments `PATH` with `~/.grok/bin`, `~/.local/bin`, and
`/opt/homebrew/bin`, then resolves the executable before starting a child.

## Gateway configuration

Place gateway values in `~/.config/dev-jobs/secrets.env` with mode `600`, or export
them in the parent process:

```sh
GLM_API_KEY=...
# GLM_BASE_URL=https://api.z.ai/api/anthropic

LOCAL_API_KEY=...
LOCAL_BASE_URL=http://127.0.0.1:8080
```

`glm` and `local` blank `ANTHROPIC_API_KEY` for the child so Claude Code does not
silently bypass the selected gateway. Each uses a separate `CLAUDE_CONFIG_DIR` under
the jobs state directory.

## Git and sandbox behavior

Codex and Grok need write access to a linked worktree's shared Git directory in
order to commit. `runjob` resolves that directory through Git and scopes the adapter
grant to it. Grok's generated profile lives at `.grok/sandbox.toml` in the worktree
and is added to Git's local exclude file.

`--grok-writable PATH` adds another write grant for a one-shot Grok job. This is a
significant permission expansion; use the narrowest absolute path. Setting
`--grok-sandbox off` removes Grok's sandbox and should be reserved for a job that
must launch nested sandboxed CLIs.

## Antigravity specifics

The `agy` CLI adapter accepts only its supported Gemini agent rows and the effort
levels `low`, `medium`, and `high`. It passes prompts in argv because the supported
CLI does not accept prompt input on stdin. `runjob` checks the platform's argv limit
before launch and asks the operator to split an oversized prompt.

If `/opt/homebrew/bin/antigravity-usage` is installed, fleet mode reads its JSON
quota snapshot. A broken or missing quota reader fails open with a warning; only an
explicitly exhausted shared Gemini pool is held.

## Adding an adapter

An adapter is not complete when `build_command()` can construct argv. Also update:

- `VALID_ENGINES`, the CLI help, default caps, and credential preflight
- session creation/resume handling and terminal-status classification
- the default sanitization posture for remote providers
- tests for argv, environment isolation, exit handling, and fleet admission
- this reference and `secrets.env.example`

Use a conservative concurrency default. A successful dry run must prove both launch
and terminal ledger behavior; an adapter that starts but can never be admitted or
classified is not usable.
