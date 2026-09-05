#!/usr/bin/env python3
"""runjob — launch headless LLM jobs from any project and keep a shared machine log.

Supported adapters: claude, codex, grok, glm, agy, and local.

Two ways to use it:

  ONE-SHOT (fire a single job):
    runjob run --engine grok --model grok-4.5 --effort xhigh -f prompt.md
    runjob run --engine claude --model opus "summarise these notes"   # inline prompt
    runjob run --engine glm --model glm-5.2 -f p.md --wait            # block + tail

  FLEET (a file-based queue in the current project — jobs/*.md):
    runjob fleet run           # scheduler loop: caps, deps, limit-parking
    runjob fleet status        # one-shot table

  SHARED LOG + DASHBOARD (every job from every project, every engine):
    runjob log                 # tail the global job log
    runjob dash                # serve an auto-refreshing web dashboard

Design notes live in SKILL.md and references/. Credentials resolve from
process env → project .env → ~/.config/dev-jobs/secrets.env (first wins).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Iterable, NamedTuple, Sequence

# --- machine-wide home -------------------------------------------------------

HOME = Path(os.environ.get("DEV_JOBS_HOME", str(Path.home() / ".config" / "dev-jobs")))
SECRETS_ENV = HOME / "secrets.env"
JOBS_LOG = HOME / "jobs.jsonl"          # append-only, shared across all projects
GLM_CONFIG_DIR = HOME / "glm-config"    # isolated claude config for the z.ai gateway
LOCAL_CONFIG_DIR = HOME / "local-config"  # isolated config for an Anthropic-compatible gateway
SKILL_DIR = Path(__file__).resolve().parent

AGY_DEFAULT_MODEL = "gemini-3.8-flash-high"
# Mirrors `agy models` as of 2026-09-03. 3.5 Flash is gone from that listing (the
# CLI hard-errors on an unknown slug), so it is off the whitelist too — a clear
# refusal here beats burning a slot on a 20s CLI death.
AGY_MODELS = {
    "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
    "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low",
}
AGY_EFFORTS = {"low", "medium", "high"}
AGY_PRINT_TIMEOUT = "45m"
AGY_QUOTA_BINARY_DEFAULT = Path("/opt/homebrew/bin/antigravity-usage")
AGY_ARGV_HEADROOM = 32 * 1024

# Engine CLIs live in per-tool bin dirs that detached launch contexts (IDE
# shells, launchd, other agents' shells) often drop from PATH — on 2026-07-24
# that turned every grok launch into an instant exit-127. Ensure those dirs are
# searchable and resolve the binary to an absolute path before spawning, so a
# genuinely missing CLI fails loudly at launch time instead of in the job log.
ENGINE_BIN_DIRS = [Path.home() / ".grok" / "bin",
                   Path.home() / ".local" / "bin",
                   Path("/opt/homebrew/bin")]


def resolve_engine_binary(cmd: list[str], env: dict) -> list[str]:
    """Return cmd with argv[0] replaced by its absolute path, augmenting
    env["PATH"] with ENGINE_BIN_DIRS (also inherited by the child so any
    sub-tools the CLI shells out to resolve the same way)."""
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for d in ENGINE_BIN_DIRS:
        if d.is_dir() and str(d) not in parts:
            parts.append(str(d))
    env["PATH"] = os.pathsep.join(parts)
    exe = shutil.which(cmd[0], path=env["PATH"])
    if not exe:
        raise RuntimeError(f"engine binary {cmd[0]!r} not found on PATH "
                           f"(searched {env['PATH']})")
    return [exe, *cmd[1:]]


def now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def effective_model(engine: str, model: str) -> str:
    # The grok CLI's own default model is Composer 2.5 (a coding model), not
    # Grok — so when no model is given, force grok-4.5 as the sensible default.
    if engine == "grok" and not model:
        model = "grok-4.5"
    if engine == "agy" and not model:
        model = AGY_DEFAULT_MODEL
    return model


# --- credentials -------------------------------------------------------------

def _load_env_file(path: Path) -> None:
    """setdefault every KEY=VALUE (optionally `export `-prefixed) — first
    definition wins, so process env > project .env > global secrets."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def load_credentials(project_root: Path | None) -> None:
    """Resolution chain: process env (already set) → project .env → global.
    setdefault means the first one to define a key wins."""
    if project_root:
        _load_env_file(project_root / ".env")
    _load_env_file(SECRETS_ENV)


# --- project config ----------------------------------------------------------

DEFAULT_CONFIG = {
    # Conservative public defaults. Raise these in .dev-jobs.toml only after
    # checking the provider's concurrency, quota, and billing behavior.
    "caps": {"claude": 1, "codex": 1, "grok": 1, "glm": 1, "agy": 1,
             "local": 1},
    "branch_prefix": "job",
    # Gate-less / external-provider engines get sparse-checkout sanitized
    # worktrees so private content never enters their context. Empty by default.
    # `local` is omitted because it normally points at infrastructure controlled
    # by the operator. Add it here if your local gateway is actually remote.
    "sanitize_engines": ["codex", "grok", "glm", "agy"],
    "sanitize_excludes": [],
    "jobs_dir": "jobs",
    "worktrees_dir": ".worktrees",
    # Fleet job transcripts. Empty = "<jobs_dir>/logs". Set it to an existing
    # gitignored path if the project already ignores one (e.g. the original
    # orchestrator's "<tool>/orchestrate/logs", a sibling of jobs/).
    "logs_dir": "",
    # Optional live quota gates for the fleet. Per engine:
    #   {"cmd": "<shell cmd printing name:pct% and/or bare pct%>",
    #    "thresholds": {"session": 60, ...},   # defer if named metric >= value
    #    "max": 92}                            # defer if max(bare pct%) >= value
    # Missing/broken reads fail OPEN (never halt the fleet).
    "usage_gate": {},
}


def find_project_root(start: Path) -> Path:
    """Nearest ancestor containing .git or .dev-jobs.toml; else `start`."""
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".dev-jobs.toml").exists() or (p / ".git").exists():
            return p
    return cur


def git_common_dir(cwd: Path) -> Path | None:
    """Return git's resolved common directory for *cwd*, if it is a repo.

    Git owns this resolution: in a linked worktree it is the main repository's
    .git directory, while in an ordinary repository it is that repository's
    .git directory.  Do not parse a worktree's ``gitdir:`` pointer ourselves;
    this also correctly handles a cwd below the worktree root.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=cwd,
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip())
    return (common if common.is_absolute() else cwd / common).resolve()


def linked_worktree_common_dir(cwd: Path, project_root: Path) -> Path | None:
    """Return a linked worktree's common dir, or None for today's normal path."""
    # A .git *file* is git's worktree pointer.  Keep normal repositories on
    # their byte-identical command path even though git_common_dir also knows
    # how to resolve their .git directory.
    if not (project_root / ".git").is_file():
        return None
    return git_common_dir(cwd)


def _write_grok_sandbox(workspace: Path, git_dir: Path | None, *, fleet: bool = False,
                        extra_paths: Sequence[Path] = ()) -> Path:
    """Write a workspace-scoped grok profile that can write *git_dir* and *extra_paths*.

    The profile lives with the invocation's workspace rather than in a global
    grok configuration, so concurrent jobs in separate worktrees never race.
    extra_paths carries --grok-writable grants (e.g. the jobs ledger + a sibling
    repo's .git for an orchestrator job that must dispatch and merge — verified
    2026-08-12: the stock workspace profile blocks both).
    Return its path for callers that need to inspect it.
    """
    cfg_dir = workspace / ".grok"
    cfg_dir.mkdir(exist_ok=True)
    profile = cfg_dir / "sandbox.toml"
    grants = ([str(git_dir)] if git_dir else []) + [str(p) for p in extra_paths]
    profile.write_text(
        ("# Auto-written by runjob for grok fleet jobs — do not commit.\n"
         if fleet else "# Auto-written by runjob for a grok one-shot job — do not commit.\n") +
        "# Grants write access outside the `workspace` sandbox: a worktree's\n"
        "# main-repo .git (gitdir + object store), and any --grok-writable paths.\n"
        "[profiles.runjob]\n"
        'extends = "workspace"\n'
        + "read_write = [" + ", ".join(f'"{g}"' for g in grants) + "]\n",
        encoding="utf-8")
    # Belt-and-suspenders: keep the profile out of any `git add` the agent runs.
    try:
        excl = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=workspace,
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return profile
    if excl.returncode == 0:
        ep = (workspace / excl.stdout.strip()).resolve()
        try:
            prior = ep.read_text(encoding="utf-8") if ep.exists() else ""
            if ".grok/" not in prior:
                ep.parent.mkdir(parents=True, exist_ok=True)
                ep.write_text(prior + ("" if prior.endswith("\n") or not prior else "\n")
                              + ".grok/\n", encoding="utf-8")
        except OSError:
            pass  # exclude is a nicety; the profile write above is the real fix
    return profile


def load_config(project_root: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    tf = project_root / ".dev-jobs.toml"
    if tf.exists():
        try:
            import tomllib
            with tf.open("rb") as fh:
                user = tomllib.load(fh)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # noqa: BLE001
            print(f"runjob: warning — bad .dev-jobs.toml ({e}); using defaults",
                  file=sys.stderr)
    return cfg


# --- shared global job log ---------------------------------------------------

def log_event(**fields) -> None:
    """Append one event to the machine-wide job log. Never raises."""
    fields.setdefault("ts", iso())
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with JOBS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(fields, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"runjob: warning — could not write job log: {e}", file=sys.stderr)


def read_states() -> list[dict]:
    """Fold the append-only log into current per-uid state (latest wins)."""
    if not JOBS_LOG.exists():
        return []
    states: dict[str, dict] = {}
    for line in JOBS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = ev.get("uid")
        if not uid:
            continue
        st = states.setdefault(uid, {"uid": uid, "created": ev["ts"]})
        st.update(ev)
        st["updated"] = ev["ts"]
        # Remember when this pid was claimed so liveness can reject pid reuse.
        if ev.get("event") == "launch" and ev.get("pid") is not None:
            st["launched_at"] = ev.get("ts")
            st["launched_pid"] = ev.get("pid")
    return sorted(states.values(), key=lambda s: s.get("created", ""), reverse=True)


# --- process liveness (shared by fleet, log viewer, repair) ------------------

# A process that started more than this after the launch event cannot be the
# job we recorded: the pid was recycled by the OS.
_PID_REUSE_SLACK = timedelta(seconds=10)


def process_start_time(pid: int) -> datetime | None:
    """Best-effort process start time in the local timezone, or None."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = r.stdout.strip()
    if not raw:
        return None
    # lstart is like "Mon Aug  3 08:03:15 2026" (no tz; double-space before day).
    parts = raw.split()
    if len(parts) != 5:
        return None
    try:
        reconstructed = (f"{parts[0]} {parts[1]} {int(parts[2]):02d} "
                         f"{parts[3]} {parts[4]}")
        naive = datetime.strptime(reconstructed, "%a %b %d %H:%M:%S %Y")
    except (ValueError, IndexError):
        return None
    # lstart is wall-clock local time. astimezone() on a naive datetime applies
    # the historical offset from the tz database — critical across DST: stamping
    # today's fixed offset via .replace(tzinfo=now().tzinfo) can skew by 1h and
    # push a live process toward the "dead" (start > launched_at) side.
    return naive.astimezone()


# Liveness is tri-state. --active may hide only on positive dead evidence;
# unknown must remain visible (and flagged), never omitted.
LIVENESS_ALIVE = "alive"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"


def process_liveness(pid: int | None, launched_at: str | None = None) -> str:
    """Classify whether *pid* is still the process we launched.

    Returns one of LIVENESS_ALIVE / LIVENESS_DEAD / LIVENESS_UNKNOWN.

    Positive dead evidence: missing pid, ProcessLookupError on kill(0),
    confirmed zombie (`ps` stat starts with Z), or pid-reuse (start time
    clearly after launch). An empty/nonzero `ps` result after a successful
    kill(0) is UNKNOWN — not dead — so a reaper cannot hide a live job on
    one uncertain probe (B3).
    """
    if not pid:
        return LIVENESS_DEAD
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LIVENESS_DEAD
    except PermissionError:
        # Exists but we cannot signal it — still treat as present, then
        # fall through to zombie / reuse checks where possible.
        pass
    ps_uncertain = False
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=2)
        stat = r.stdout.strip()
        if stat.startswith("Z"):
            return LIVENESS_DEAD
        if not stat:
            # kill(0) succeeded; empty ps is transient/uncertain, not death.
            ps_uncertain = True
    except (OSError, subprocess.TimeoutExpired):
        # ps unavailable after kill(0) succeeded: trust the signal check.
        pass

    if launched_at:
        try:
            launched = datetime.fromisoformat(launched_at)
        except ValueError:
            launched = None
        if launched is not None:
            start = process_start_time(pid)
            if start is not None and start > launched + _PID_REUSE_SLACK:
                return LIVENESS_DEAD
    if ps_uncertain:
        return LIVENESS_UNKNOWN
    return LIVENESS_ALIVE


def process_is_alive(pid: int | None, launched_at: str | None = None) -> bool:
    """True unless liveness is positively dead.

    UNKNOWN fails open (True): callers that only need a bool must not treat
    uncertain probes as death. Prefer process_liveness when the caller can
    flag uncertainty (cmd_log --active).
    """
    return process_liveness(pid, launched_at=launched_at) != LIVENESS_DEAD


def _launched_at_for_state(s: dict) -> str | None:
    """ISO timestamp of the launch that claimed the current pid, if known."""
    if s.get("launched_at") and s.get("launched_pid") == s.get("pid"):
        return s["launched_at"]
    return s.get("updated") or s.get("created") or s.get("ts")


def _limited_retry_elapsed(retry_at: str | None) -> bool:
    """True when a limited row's retry window has elapsed or was never set.

    Display-only input for the † dagger. A parseable *future* retry_at means
    the job is parked and waiting (quota reset / fleet re-attempt) — not
    abandoned. Absent, empty, or unparseable retry_at counts as elapsed:
    standalone `runjob run` jobs classified limited by `_classify_and_log`
    never write retry_at, and those rows are the common abandoned population.
    """
    if retry_at is None:
        return True
    text = str(retry_at).strip()
    if not text:
        return True
    try:
        when = datetime.fromisoformat(text)
        return when < now()
    except (ValueError, TypeError):
        # Naive retry_at (or other compare failure) vs tz-aware now() raises
        # TypeError; treat as elapsed so one bad row cannot kill the whole
        # cmd_log / Fleet.status render (N-d).
        return True


def display_is_zombie(
    status: str,
    *,
    pid_dead: bool,
    retry_at: str | None = None,
) -> bool:
    """Whether cmd_log / Fleet.status should mark this row with †.

    Display only — never writes. Covers:
      - status=running and pid dead (original honesty dagger)
      - status=limited, pid dead/unknown, and retry window elapsed or unset
        (F1 + F1b: abandoned limited must not look like a healthy ⏸ park)
    A limited row with a future retry_at, or with a live pid, is not daggered.
    """
    if status == "running":
        return pid_dead
    if status == "limited":
        return pid_dead and _limited_retry_elapsed(retry_at)
    return False


# --- activity floor / stall detection (observation only; never kills) --------
#
# The ledger reports status=running for as long as the process exists. It never
# asks whether that process is *doing anything*. On 2026-08-07 two codex jobs
# (fin-file-alerts-codex-7086f499, bc-step-floor-codex-6ae98357) sat as running
# for 60 and 178 minutes with only the launch banner on disk (60–64 B during
# the stall; 135–139 B after kill added the exit trailer) and 0:00.00
# accumulated CPU — the dynamic loader never entered engine code. Re-dispatch
# to another engine returned in ~4 minutes. The orchestrator that reaps
# *finished* jobs cannot see a never-finishing, never-writing job, so the cost
# is hours of false "work in progress".
#
# Calibrated 2026-08-07 against ~/.config/dev-jobs (1305 completed jobs with
# duration, 1797 log files). Full defence of the numbers:
# docs/gates/2026-08-07-job-liveness-floor.md
#
# Thresholds (conjunction — all must hold to call stalled):
#   age    ≥ STALL_MIN_AGE_SECONDS   — just-launched jobs are never stalled
#   log    ≤ STALL_LOG_BYTES         — still at launch-banner scale
#   cpu    ≤ STALL_CPU_SECONDS       — absolute tree CPU still near zero
#   delta  ≤ STALL_CPU_DELTA_SECONDS — tree CPU not growing over the sample window
#
# Engines buffer stdout to the end of the run, so in-flight logs sit at the
# launch banner for the whole job. Absolute+delta CPU is what separates a
# dyld/loader hang (0:00.00 forever) from a healthy engine that has started
# (~0.5 s at first instruction; keeps accruing while it works). Instantaneous
# absolute alone is not enough: a healthy client waiting on a slow server sits
# under 1.0 s for minutes (gate 2026-08-07).
#
# Missing observations fail open (not stalled). Detection never terminates.

STALL_MIN_AGE_SECONDS = 120.0
# Launch banners measure 46–74 B (p50=58 across 200 logs). The two codex stalls
# sat at 60–64 B. Kept as a conjunct for banner-only hangs; NOT a healthy-work
# discriminator in flight (engines buffer — logs stay at the banner until exit).
STALL_LOG_BYTES = 96
# Absolute tree-CPU floor. The pure hang is 0:00.00. Engine cold-start is ~0.5 s
# (claude -p probe; claude --version is 0.06 s). Floor sits between hang and
# started-engine so a healthy buffered job past grace cannot trip on absolute
# alone. Scheduling noise for a true hang stays well under this.
STALL_CPU_SECONDS = 0.25
# Sample window + near-zero growth floor. Stall requires BOTH absolute ≤ floor
# AND growth ≤ delta over the window — so a job that started (abs above floor)
# or is still burning CPU (delta above floor) reads ok. Window is short enough
# for interactive `runjob log` and long enough that a working engine's accrual
# clears the delta floor.
STALL_CPU_WINDOW_SECONDS = 2.0
STALL_CPU_DELTA_SECONDS = 0.05

STALL_OK = "ok"
STALL_STALLED = "stalled"
STALL_TOO_YOUNG = "too_young"
STALL_NOT_APPLICABLE = "not_applicable"
STALL_UNKNOWN = "unknown"  # observation failed — fail open, never claim stalled


def parse_ps_cputime(token: str) -> float | None:
    """Parse a `ps` TIME / cputime field into seconds.

    Accepts macOS forms (`0:00.00`, `12:34.56`, `1:02:03`) and Linux forms
    (`00:00:01`, `1-02:03:04`). Returns None if unparseable.
    """
    text = (token or "").strip()
    if not text or text == "-":
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return days * 86400.0 + int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return days * 86400.0 + int(m) * 60 + float(s)
    except ValueError:
        return None
    return None


def _read_ps_cputime_table() -> (
        tuple[dict[int, list[int]], dict[int, float]] | None):
    """One `ps` snapshot → (children_by_ppid, cputime_by_pid), or None."""
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,time="],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    children: dict[int, list[int]] = {}
    cputime: dict[int, float] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            p = int(parts[0])
            pp = int(parts[1])
        except ValueError:
            continue
        sec = parse_ps_cputime(parts[2])
        if sec is None:
            continue
        cputime[p] = sec
        children.setdefault(pp, []).append(p)
    return children, cputime


def _tree_cputime_from_table(
    pid: int,
    children: dict[int, list[int]],
    cputime: dict[int, float],
) -> float | None:
    """Sum *pid* + descendants from a parsed ps table. None if root missing."""
    # Root must appear in the process table. A missing root is unobservable,
    # not "zero CPU" — fail open so a flaky ps cannot invent a stall.
    if pid not in cputime:
        return None
    total = 0.0
    seen: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in cputime:
            total += cputime[cur]
        stack.extend(children.get(cur, ()))
    return total


def process_tree_cputime_seconds(pid: int) -> float | None:
    """Sum cumulative CPU seconds for *pid* and its descendants.

    The ledger records the `/bin/sh` runjob-wrapper pid (start_new_session), not
    the engine. A healthy engine child can burn minutes of CPU while the shell
    sits at 0:00.00 — so the tree sum is the observation that matters. Returns
    None when the process table is unreadable (fail open for stall detection).
    """
    if not pid:
        return None
    table = _read_ps_cputime_table()
    if table is None:
        return None
    children, cputime = table
    return _tree_cputime_from_table(int(pid), children, cputime)


def process_tree_cpu_window_samples(
    pids: Iterable[int],
    *,
    window_seconds: float | None = None,
) -> dict[int, tuple[float, float] | None]:
    """Sample process-tree CPU for many pids over **one** shared window.

    Takes one `ps` snapshot, sleeps once (default STALL_CPU_WINDOW_SECONDS),
    takes a second snapshot, then returns pid → (cpu_now, delta) or None.
    Empty *pids* returns {} without sleeping. Per-pid first-sample failure
    yields None for that pid without inventing a stall; if every first sample
    fails, no sleep is taken (matches single-pid short-circuit).
    """
    uniq: list[int] = []
    seen: set[int] = set()
    for raw in pids:
        if not raw:
            continue
        p = int(raw)
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    if not uniq:
        return {}
    window = (
        STALL_CPU_WINDOW_SECONDS if window_seconds is None else window_seconds
    )
    first_table = _read_ps_cputime_table()
    if first_table is None:
        return {p: None for p in uniq}
    children1, cpu1 = first_table
    first = {
        p: _tree_cputime_from_table(p, children1, cpu1) for p in uniq
    }
    if all(v is None for v in first.values()):
        return {p: None for p in uniq}
    try:
        time.sleep(max(0.0, float(window)))
    except (TypeError, ValueError):
        return {p: None for p in uniq}
    second_table = _read_ps_cputime_table()
    if second_table is None:
        return {p: None for p in uniq}
    children2, cpu2 = second_table
    out: dict[int, tuple[float, float] | None] = {}
    for p in uniq:
        f = first[p]
        if f is None:
            out[p] = None
            continue
        s = _tree_cputime_from_table(p, children2, cpu2)
        if s is None:
            out[p] = None
            continue
        out[p] = (s, max(0.0, s - f))
    return out


def process_tree_cpu_delta_seconds(
    pid: int,
    *,
    window_seconds: float | None = None,
) -> tuple[float, float] | None:
    """Sample process-tree CPU twice; return (cpu_now, delta) or None.

    Delta is max(0, second − first) over *window_seconds* (default
    STALL_CPU_WINDOW_SECONDS). A hang stays flat; a working engine accrues.
    Any failed sample fails open (None) — never invent a stall from flaky ps.

    Single-pid path used by classify_stall's live observation. Listings that
    classify many rows should call process_tree_cpu_window_samples once and
    inject the results instead, so N rows share one sleep.
    """
    if not pid:
        return None
    window = (
        STALL_CPU_WINDOW_SECONDS if window_seconds is None else window_seconds
    )
    first = process_tree_cputime_seconds(pid)
    if first is None:
        return None
    try:
        time.sleep(max(0.0, float(window)))
    except (TypeError, ValueError):
        return None
    second = process_tree_cputime_seconds(pid)
    if second is None:
        return None
    return second, max(0.0, second - first)


def stall_cpu_sample_pids(
    rows: Iterable[dict],
    *,
    now_dt: datetime | None = None,
) -> list[int]:
    """Pids for which classify_stall would live-sample tree CPU.

    Mirrors the cheap conjuncts: running + alive + past grace + banner-scale
    log. Rows that already fail a cheap conjunct (too young, large log,
    unobservable age/log, non-running, non-alive) are omitted so a listing
    never pays the sample window for them.
    """
    pids: list[int] = []
    seen: set[int] = set()
    for s in rows:
        if (s.get("status") or "") != "running":
            continue
        pid = s.get("pid")
        if not pid:
            continue
        launched = _launched_at_for_state(s)
        liv = process_liveness(pid, launched_at=launched)
        if liv != LIVENESS_ALIVE:
            continue
        age = job_age_seconds(launched, now_dt=now_dt)
        if age is None or age < STALL_MIN_AGE_SECONDS:
            continue
        size = job_log_byte_size(s.get("log"))
        if size is None or size > STALL_LOG_BYTES:
            continue
        p = int(pid)
        if p in seen:
            continue
        seen.add(p)
        pids.append(p)
    return pids


def job_log_byte_size(log_path: str | Path | None) -> int | None:
    """Current size of the job's output log, or None if unobservable.

    Missing path, missing file, and permissions errors all return None so
    stall detection fails open (bar 5 / B2). A missing file past the grace
    window is far more often a moved/pruned log or a DEV_JOBS_HOME mismatch
    than a still-unwritten banner — scoring it as 0 would invent a stall.
    """
    if not log_path:
        return None
    path = Path(log_path)
    try:
        if not path.exists():
            return None
        return path.stat().st_size
    except OSError:
        return None


def job_age_seconds(
    launched_at: str | None,
    *,
    now_dt: datetime | None = None,
) -> float | None:
    """Seconds since launch, or None if *launched_at* is missing/unparseable."""
    if not launched_at:
        return None
    try:
        started = datetime.fromisoformat(launched_at)
    except ValueError:
        return None
    current = now_dt if now_dt is not None else now()
    try:
        return (current - started).total_seconds()
    except TypeError:
        # naive vs aware — treat as unobservable rather than crash the viewer.
        return None


def classify_stall(
    *,
    status: str,
    pid: int | None = None,
    launched_at: str | None = None,
    log_path: str | Path | None = None,
    liveness: str | None = None,
    age_seconds: float | None = None,
    log_bytes: int | None = None,
    cpu_seconds: float | None = None,
    cpu_delta: float | None = None,
    now_dt: datetime | None = None,
) -> str:
    """Classify whether a ledger row is stalled (alive but doing nothing).

    Returns one of STALL_STALLED / STALL_OK / STALL_TOO_YOUNG /
    STALL_NOT_APPLICABLE / STALL_UNKNOWN.

    Pure observation — never signals, never writes the ledger. Injectable
    *age_seconds* / *log_bytes* / *cpu_seconds* / *cpu_delta* let tests pin
    thresholds without a live process table. When CPU injectables are omitted,
    tree CPU is sampled twice over STALL_CPU_WINDOW_SECONDS. Any failed
    observation yields STALL_UNKNOWN (fail open).

    Cheap conjuncts short-circuit before the CPU sample: non-running / non-alive
    / too-young / unobservable age-or-log / log already over STALL_LOG_BYTES
    never pay the window. Callers that classify many rows (cmd_log) should
    pre-sample once via process_tree_cpu_window_samples + inject, so N rows
    share one sleep rather than N.
    """
    if status != "running":
        return STALL_NOT_APPLICABLE

    liv = liveness
    if liv is None:
        liv = process_liveness(pid, launched_at=launched_at)
    if liv != LIVENESS_ALIVE:
        # Dead → dagger path; unknown → running? path. Neither is "stalled".
        return STALL_NOT_APPLICABLE

    age = age_seconds
    if age is None:
        age = job_age_seconds(launched_at, now_dt=now_dt)
    if age is None:
        return STALL_UNKNOWN
    if age < STALL_MIN_AGE_SECONDS:
        return STALL_TOO_YOUNG

    size = log_bytes
    if size is None:
        size = job_log_byte_size(log_path)
    if size is None:
        return STALL_UNKNOWN
    # Log already past the banner floor → cannot be stalled. Skip the
    # expensive CPU window (B3): a 200 KB log must not sleep 2 s to say "ok".
    if size > STALL_LOG_BYTES:
        return STALL_OK

    cpu = cpu_seconds
    delta = cpu_delta
    if cpu is None or delta is None:
        # Live delta observation (or complete a partial inject). Incomplete
        # inject without a pid cannot be finished — fail open.
        if not pid:
            return STALL_UNKNOWN
        sampled = process_tree_cpu_delta_seconds(pid)
        if sampled is None:
            return STALL_UNKNOWN
        if cpu is None:
            cpu = sampled[0]
        if delta is None:
            delta = sampled[1]

    if (
        size <= STALL_LOG_BYTES
        and cpu <= STALL_CPU_SECONDS
        and delta <= STALL_CPU_DELTA_SECONDS
    ):
        return STALL_STALLED
    return STALL_OK


# --- engine command building -------------------------------------------------

VALID_ENGINES = {"claude", "codex", "grok", "glm", "agy", "local"}

LIMIT_RE = re.compile(
    r"(rate.?limit|usage limit|limit (reached|hit|exceeded)|hit the .{0,20}limit"
    r"|out of .{0,20}(quota|credits)|too many requests|overloaded|\b429\b|\b529\b"
    r"|5.?hour limit|resets? at)", re.I)
# Greppable summary when a clean exit coexists with LIMIT_RE prose (N-a residual).
# One `grep` over the ledger finds every instance — do not reword casually.
EXIT0_LIMIT_PROSE_SUMMARY = "exit 0 with limit text in tail"
DONE_RE = re.compile(r"^DONE:", re.M)
RESET_AT_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", re.I)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def validate_agy_request(model: str, effort: str = "") -> None:
    """Validate the subset of Antigravity rows supported by this adapter."""
    if not model.lower().startswith("gemini-"):
        raise ValueError(
            f"agy model {model!r} refused: this adapter accepts only Gemini rows")
    if model.startswith("gemini-2.5"):
        raise ValueError(
            f"agy model {model!r} refused: Gemini 2.5 rows are autocomplete-only, "
            "not runnable agent models")
    if model not in AGY_MODELS:
        allowed = ", ".join(sorted(AGY_MODELS))
        raise ValueError(f"agy model {model!r} is not an approved runnable Gemini model "
                         f"(pick: {allowed})")
    if effort and effort not in AGY_EFFORTS:
        raise ValueError(f"agy effort {effort!r} refused (pick: low, medium, high)")


def ensure_agy_argv_fits(cmd: list[str], env: dict[str, str],
                         *, arg_max: int | None = None) -> None:
    """Fail clearly before execve when agy's mandatory -p argument is too large.

    There is no stdin or prompt-file fallback in agy 1.1.9. The prompt is the final
    argv item built below, so calculate its current-process budget including the
    inherited environment and retain headroom for the shell wrapper/runtime.
    """
    if not cmd or Path(cmd[0]).name != "agy" or "-p" not in cmd:
        return
    prompt_index = cmd.index("-p") + 1
    prompt = cmd[prompt_index]
    if arg_max is None:
        try:
            arg_max = int(os.sysconf("SC_ARG_MAX"))
        except (AttributeError, OSError, ValueError):
            arg_max = 256 * 1024
    env_bytes = sum(len(f"{k}={v}".encode("utf-8")) + 1 for k, v in env.items())
    other_bytes = sum(len(a.encode("utf-8")) + 1 for i, a in enumerate(cmd)
                      if i != prompt_index)
    prompt_budget = max(0, arg_max - env_bytes - other_bytes - AGY_ARGV_HEADROOM)
    # Linux additionally caps each individual argv string at 128 KiB. agy is
    # currently macOS-only here, but retaining this makes the guard portable.
    if sys.platform.startswith("linux"):
        prompt_budget = min(prompt_budget, 128 * 1024 - 1)
    prompt_bytes = len(prompt.encode("utf-8")) + 1
    if prompt_bytes > prompt_budget:
        raise ValueError(
            f"agy prompt is {prompt_bytes - 1} bytes but the current exec argv budget "
            f"is {prompt_budget} bytes; agy 1.1.9 accepts prompts only through -p "
            "(stdin and prompt files do not work). Split or reduce the job; nothing "
            "was launched")


def _agy_bool(value: object) -> bool | None:
    """Parse the quota reader's boolean-like values without Python truthiness."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    return None


def _agy_number(value: object) -> float | None:
    """Parse a finite non-negative quota percentage, including JSON strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _parse_agy_quota(payload: str) -> dict:
    """Normalize antigravity-usage's quota snapshot for the shared Gemini pool."""
    try:
        snapshot = json.loads(payload)
    except json.JSONDecodeError as e:
        return {"available": False, "error": f"invalid JSON: {e}"}
    models = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(models, list):
        return {"available": False, "error": "quota JSON has no models list"}
    gemini = [m for m in models if isinstance(m, dict)
              and str(m.get("modelId", "")).startswith("gemini-")
              and not m.get("isAutocompleteOnly")
              and not str(m.get("modelId", "")).startswith("gemini-2.5")]
    if not gemini:
        return {"available": False, "error": "quota JSON has no runnable Gemini rows"}
    rows: list[tuple[dict, bool, float | None]] = []
    for model in gemini:
        exhausted = _agy_bool(model.get("isExhausted", False))
        remaining = _agy_number(model.get("remainingPercentage")) \
            if "remainingPercentage" in model else None
        # A malformed declared field is not evidence that this model is available.
        # Ignore that row; if all rows are malformed, retain the reader fail-open path.
        if exhausted is None or ("remainingPercentage" in model and remaining is None):
            continue
        rows.append((model, exhausted, remaining))
    if not rows:
        return {"available": False, "error": "quota JSON has no valid Gemini rows"}
    exhausted = any(is_exhausted or remaining == 0
                    for _, is_exhausted, remaining in rows)
    percentages = [remaining for _, _, remaining in rows if remaining is not None]
    remaining = min(percentages) if percentages else None
    # antigravity-usage currently emits fractional values (for example 0.77).
    # Preserve integer 1 as 1%, rather than scaling it to 100%, so a future
    # 0--100 integer schema cannot make a nearly exhausted pool look full.
    if remaining is not None and 0 < remaining < 1:
        remaining *= 100
    resets = [str(m["resetTime"]) for m, _, _ in rows if m.get("resetTime")]
    reset_ms = [int(m["timeUntilResetMs"]) for m, _, _ in rows
                if isinstance(m.get("timeUntilResetMs"), (int, float))]
    return {
        "available": True,
        "exhausted": exhausted,
        "remaining_percentage": remaining,
        "reset_time": min(resets) if resets else "",
        "time_until_reset_ms": min(reset_ms) if reset_ms else None,
    }


def read_agy_quota() -> dict:
    """Read the live Gemini pool. Missing/broken third-party tooling fails open."""
    configured_reader = Path(os.environ.get("AGY_QUOTA_BINARY",
                                            str(AGY_QUOTA_BINARY_DEFAULT)))
    reader = str(configured_reader) if configured_reader.exists() else shutil.which(
        "antigravity-usage")
    if not reader:
        return {"available": False, "error": "antigravity-usage is not installed"}
    try:
        result = subprocess.run([reader, "quota", "--json"], capture_output=True,
                                text=True, timeout=20)
    except (subprocess.SubprocessError, OSError) as e:
        return {"available": False, "error": str(e)}
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        return {"available": False,
                "error": detail[-240:] or f"reader exited {result.returncode}"}
    return _parse_agy_quota(result.stdout)


def _agy_retry_at(quota: dict) -> str:
    reset = quota.get("reset_time")
    if reset:
        try:
            return iso(datetime.fromisoformat(str(reset).replace("Z", "+00:00"))
                       + timedelta(minutes=2))
        except ValueError:
            pass
    wait_ms = quota.get("time_until_reset_ms")
    if isinstance(wait_ms, (int, float)) and wait_ms > 0:
        return iso(now() + timedelta(milliseconds=wait_ms, minutes=2))
    return iso(now() + timedelta(minutes=15))


def _agy_quota_fields(quota: dict) -> dict:
    if not quota.get("available"):
        return {"pool_check": "unavailable"}
    fields = {"pool_check": "exhausted" if quota.get("exhausted") else "available"}
    if quota.get("remaining_percentage") is not None:
        fields["pool_remaining_percentage"] = quota["remaining_percentage"]
    if quota.get("reset_time"):
        fields["pool_reset_time"] = quota["reset_time"]
    return fields


def _parse_agy_outcome(log_file: Path) -> dict | None:
    """Find agy's single JSON result in a transcript, even when a wrapper follows it."""
    if not log_file.exists():
        return None
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            outcome = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(outcome, dict) and "status" in outcome and "response" in outcome:
            return outcome
    return None


def _agy_outcome_fields(outcome: dict) -> dict:
    response = str(outcome.get("response") or "")
    done_lines = [ln.strip() for ln in response.splitlines()
                  if ln.strip().startswith("DONE:")]
    summary = done_lines[-1] if done_lines else " ".join(response.split())
    fields = {
        "summary": summary[:300],
        "outcome_status": str(outcome.get("status") or ""),
    }
    for key in ("conversation_id", "duration_seconds", "num_turns"):
        if outcome.get(key) is not None:
            fields[key] = outcome[key]
    if isinstance(outcome.get("usage"), dict):
        fields["usage"] = outcome["usage"]
    return fields


def build_command(engine: str, model: str, effort: str, prompt: str,
                  *, session: str = "", resume: bool = False,
                  repo_root: Path | None = None,
                  common_git_dir: Path | None = None,
                  grok_sandbox: str = "workspace") -> tuple[list[str], bool, dict]:
    """Return (argv, prompt_on_stdin, extra_env). extra_env is applied on top
    of os.environ (used by glm to redirect claude at the z.ai gateway).

    grok_sandbox names the grok --sandbox profile. Fleet worktree jobs pass
    "runjob" (a profile ensure_worktree writes that extends "workspace" plus
    the main repo's .git) so grok can commit; in-place `run` keeps "workspace".
    agy is the exception to the normal stdin posture: 1.1.9 requires -p PROMPT."""
    if engine == "claude":
        cmd = ["claude", "-p", "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd += ["--resume", session] if resume else ["--session-id", session]
        # Force the claude.ai subscription (Keychain OAuth): a project .env or shell
        # profile that exports ANTHROPIC_API_KEY would otherwise take precedence and
        # silently route claude jobs onto paid API credits (observed 2026-07-13 —
        # an overnight opus fleet drained a leftover API balance instead of using the
        # subscription). Empty string = claude ignores the API key and uses OAuth.
        return cmd, True, {"ANTHROPIC_API_KEY": ""}

    if engine == "glm":
        # Headless Claude Code pointed at z.ai's Anthropic-compatible gateway.
        # Isolated CLAUDE_CONFIG_DIR so it never touches the real subscription.
        base_url = os.environ.get("GLM_BASE_URL", "https://api.z.ai/api/anthropic")
        api_key = os.environ.get("GLM_API_KEY", "")
        env = {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_API_KEY": "",         # must be unset for the gateway
            "CLAUDE_CONFIG_DIR": str(GLM_CONFIG_DIR.resolve()),
        }
        GLM_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cmd = ["claude", "-p", "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["--model", model]
        cmd += ["--resume", session] if resume else ["--session-id", session]
        return cmd, True, env

    if engine == "local":
        # Headless Claude Code pointed at an operator-supplied Anthropic-compatible
        # gateway. No endpoint or credential is baked into this distribution.
        base_url = os.environ.get("LOCAL_BASE_URL", "")
        env = {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": os.environ.get("LOCAL_API_KEY", ""),
            "ANTHROPIC_API_KEY": "",         # must be empty or the gateway is bypassed
            "CLAUDE_CONFIG_DIR": str(LOCAL_CONFIG_DIR.resolve()),
        }
        LOCAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cmd = ["claude", "-p", "--permission-mode", "bypassPermissions"]
        # llama.cpp turns every tool's JSON schema into a GBNF grammar up front, and
        # its regex converter cannot handle an open-ended {n,} quantifier. The
        # Workflow tool's `resumeFromRunId` pattern "^wf_[a-z0-9-]{6,}$" therefore
        # kills the WHOLE request with
        #   400 "Failed to initialize samplers: failed to parse grammar"
        # — before a single token is generated, so every agentic job fails instantly.
        # Dropping it is appropriate for this adapter: a local model is not
        # expected to orchestrate Claude Code workflows.
        cmd += ["--disallowedTools", "Workflow"]
        if model:
            cmd += ["--model", model]
        cmd += ["--resume", session] if resume else ["--session-id", session]
        return cmd, True, env

    if engine == "agy":
        validate_agy_request(model, effort)
        cmd = ["agy", "--model", model, "--output-format", "json",
               "--print-timeout", AGY_PRINT_TIMEOUT,
               "--dangerously-skip-permissions"]
        if effort:
            cmd += ["--effort", effort]
        if resume and session:
            cmd += ["--conversation", session]
        cmd += ["-p", prompt]
        return cmd, False, {}

    if engine == "codex":
        env_git = {}
        extra = []
        if repo_root:
            writable_roots = [repo_root / ".git"]
            if common_git_dir and common_git_dir not in writable_roots:
                writable_roots.append(common_git_dir)
            extra = ["-c",
                     "sandbox_workspace_write.writable_roots=["
                     + ",".join(f'\"{path}\"' for path in writable_roots) + "]"]
        if resume and session:
            return (["codex", "exec", "--approve-for-me", "--color", "always", *extra,
                     "resume", session,
                     prompt], False, env_git)
        cmd = ["codex", "exec", "--approve-for-me", "--color", "always", *extra]
        if model:
            cmd += ["-c", f'model="{model}"']
        if effort:
            # codex has no --effort flag; reasoning effort is a config key. Passing it
            # per-invocation beats mutating the user's global ~/.codex/config.toml.
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += ["-"]
        return cmd, True, env_git

    if engine == "grok":
        # --sandbox <profile>: "workspace" confines writes to cwd. A fleet job's
        # cwd is a git WORKTREE whose real gitdir + object store live under the
        # MAIN repo's .git/ (outside cwd), so committing under "workspace" dies with
        # "index.lock: Operation not permitted". Fleet passes grok_sandbox="runjob",
        # a profile ensure_worktree writes that also grants the main .git — the grok
        # analogue of the codex writable_roots whitelist above.
        base = ["grok", "--single", prompt, "--always-approve",
                "--sandbox", grok_sandbox, "--output-format", "plain"]
        if resume and session:
            return (["grok", "-r", session, "--single", prompt, "--always-approve",
                     "--sandbox", grok_sandbox, "--output-format", "plain"], False, {})
        cmd = base + ["--session-id", session]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd, False, {}

    raise ValueError(f"unknown engine {engine!r}")


# --- one-shot launch ---------------------------------------------------------

def _short_uid(project_name: str, engine: str) -> str:
    return f"{project_name}-{engine}-{uuid.uuid4().hex[:8]}"


def cmd_run(args) -> int:
    # NOTE: `run` executes IN-PLACE in --cwd — it does NOT create a git
    # worktree/branch and does NOT inject the "work only in this worktree, never
    # touch main" trailer that `fleet run` does (see ensure_worktree / TRAILER).
    # So an agent (codex/grok/glm/claude) that commits will commit straight to
    # whatever branch --cwd is on — usually main.
    # If the job may commit, either use `fleet run` (isolated worktree+branch) or
    # point --cwd at a throwaway worktree, or tell the agent in its prompt to
    # create/use a branch first.
    engine = args.engine.lower()
    if engine not in VALID_ENGINES:
        print(f"runjob: unknown engine {engine!r} (pick: {', '.join(sorted(VALID_ENGINES))})",
              file=sys.stderr)
        return 2

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    project_root = find_project_root(cwd)
    common_git_dir = linked_worktree_common_dir(cwd, project_root)
    if (project_root / ".git").is_file() and common_git_dir is None:
        print("runjob: warning — .git is a worktree pointer but its common git "
              "directory could not be resolved; using the normal sandbox grant",
              file=sys.stderr)
    project_name = project_root.name
    load_credentials(project_root)

    eff_model = effective_model(engine, args.model or "")
    if engine == "agy":
        try:
            validate_agy_request(eff_model, args.effort or "")
        except ValueError as e:
            print(f"runjob: {e}", file=sys.stderr)
            return 2

    if engine == "glm" and not os.environ.get("GLM_API_KEY"):
        print("runjob: GLM_API_KEY not set — add it to ~/.config/dev-jobs/secrets.env",
              file=sys.stderr)
        return 2

    if engine == "local" and (
            not os.environ.get("LOCAL_API_KEY") or not os.environ.get("LOCAL_BASE_URL")):
        print("runjob: LOCAL_API_KEY and LOCAL_BASE_URL must be set in "
              "~/.config/dev-jobs/secrets.env",
              file=sys.stderr)
        return 2

    # prompt from -f/--file, positional, or stdin
    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8")
    elif args.prompt:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        print("runjob: no prompt (use -f FILE, a positional arg, or stdin)",
              file=sys.stderr)
        return 2

    session = str(uuid.uuid4()) if engine in ("claude", "grok", "glm") else ""
    try:
        grok_extra = [Path(p).expanduser().resolve()
                      for p in (getattr(args, "grok_writable", None) or [])]
        grok_profile = getattr(args, "grok_sandbox", None) or (
            "runjob" if (common_git_dir or grok_extra) else "workspace")
        if engine == "grok" and grok_profile == "runjob":
            # Grok resolves a custom profile from the process cwd rather than
            # walking up to the git toplevel.  Keep the profile beside the
            # directory Popen launches from so nested --cwd jobs can start.
            _write_grok_sandbox(cwd, common_git_dir, extra_paths=grok_extra)
        cmd, use_stdin, extra_env = build_command(
            engine, eff_model, args.effort or "", prompt,
            session=session, repo_root=project_root,
            common_git_dir=common_git_dir,
            grok_sandbox=grok_profile)
    except ValueError as e:
        print(f"runjob: {e}", file=sys.stderr)
        return 2

    uid = _short_uid(project_name, engine)
    HOME.mkdir(parents=True, exist_ok=True)
    logs = HOME / "logs"
    logs.mkdir(exist_ok=True)
    log_file = logs / f"{uid}.log"

    env = os.environ.copy()
    env.update({k: v for k, v in extra_env.items()})
    try:
        cmd = resolve_engine_binary(cmd, env)
        ensure_agy_argv_fits(cmd, env)
    except ValueError as e:
        print(f"runjob: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        sys.exit(f"runjob: {e}")

    quota: dict = {}
    if engine == "agy":
        quota = read_agy_quota()
        if not quota.get("available"):
            print(f"runjob: warning — agy quota reader unavailable ({quota.get('error')}); "
                  "launching fail-open", file=sys.stderr)
        elif quota.get("exhausted"):
            retry_at = _agy_retry_at(quota)
            reason = f"agy shared Gemini pool exhausted; retry after {retry_at}"
            log_file.write_text(f"===== {iso()} held agy\n{reason}\n", encoding="utf-8")
            log_event(event="limited", uid=uid, project=str(project_root),
                      project_name=project_name, engine=engine, model=eff_model,
                      effort=args.effort or "", status="limited", retry_at=retry_at,
                      log=str(log_file), cwd=str(cwd), summary=reason,
                      title=args.title or (prompt.strip().splitlines() or [""])[0][:80],
                      **_agy_quota_fields(quota))
            print(f"runjob: {reason}; nothing launched", file=sys.stderr)
            return 75
    logf = open(log_file, "a", encoding="utf-8")
    logf.write(f"===== {iso()} launch {engine} {args.model or ''} {args.effort or ''}\n")
    logf.flush()

    stdin_src: object
    if use_stdin:
        # write prompt to a sidecar file and feed it in
        pf = logs / f"{uid}.prompt.txt"
        pf.write_text(prompt, encoding="utf-8")
        stdin_src = open(pf, "r", encoding="utf-8")
    else:
        stdin_src = subprocess.DEVNULL

    # Wrap so the job records its own exit code: the reaper watches the pid from
    # outside and cannot waitpid() for a status it did not fork. Without this, a
    # crash and a success are indistinguishable in the shared log.
    # Pass the engine argv through positional parameters instead of interpolating
    # it into the shell program. Besides avoiding another quoting surface, this
    # prevents a large agy -p argument from expanding further through shell quotes.
    wrapper = ('"$@"\nec=$?\nprintf "\\n===== exit:%s\\n" "$ec"\nexit "$ec"')
    wrapped = ["/bin/sh", "-c", wrapper, "runjob-wrapper", *cmd]

    try:
        proc = subprocess.Popen(wrapped, cwd=cwd, stdin=stdin_src, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env=env)
    finally:
        if use_stdin and hasattr(stdin_src, "close"):
            stdin_src.close()  # type: ignore[union-attr]

    log_event(event="launch", uid=uid, project=str(project_root),
              project_name=project_name, engine=engine, model=eff_model,
              effort=args.effort or "", status="running", pid=proc.pid,
              log=str(log_file), cwd=str(cwd),
              title=args.title or (prompt.strip().splitlines() or [""])[0][:80],
              **(_agy_quota_fields(quota) if engine == "agy" else {}))

    if args.wait:
        rc = proc.wait()
        status = _classify_and_log(uid, log_file, engine, cwd=cwd, exit_code=rc)
        _tail_print(log_file)
        return 0 if rc == 0 and status in ("done", "exited") else 1

    # detached: spawn a reaper that updates the shared log when the job exits
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_reap",
         uid, str(proc.pid), str(log_file), engine, str(cwd)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"▶ {uid} [{engine} {args.model or ''}] pid={proc.pid}")
    print(f"  log:  {log_file}")
    print(f"  watch: runjob log   |   runjob dash")
    return 0


def _tail_print(log_file: Path, nbytes: int = 4000) -> None:
    if log_file.exists():
        data = log_file.read_bytes()[-nbytes:]
        sys.stdout.write(data.decode("utf-8", errors="replace"))


def _log_tail_text(log_file: Path, nbytes: int = 6000) -> str:
    if not log_file.exists():
        return ""
    return log_file.read_bytes()[-nbytes:].decode("utf-8", errors="replace")


EXIT_RE = re.compile(r"^===== exit:(\d+)\s*$", re.M)
# Estate jobs declare their final disposition in this deliberately small,
# machine-readable form.  Keep the entire matching line rather than just the
# token: operators can see exactly what the job reported without opening its
# transcript.
STATUS_LINE_RE = re.compile(r"^STATUS:[ \t]+\S+[ \t]*$", re.M)
# A literal placeholder is prompt-template syntax, not a terminal report.  In
# particular, jobs are told to replace ``<n>`` before reporting a blocking
# count.  Keeping this discriminator local to the two-line template preserves
# the established last-report convention for ordinary STATUS lines.
STATUS_PROMPT_CONTINUATION_RE = re.compile(r"^BLOCKING:[ \t]*<[^>]+>[ \t]*$")

# Engines whose *fleet* launches are wrapped, so the log carries a real
# `===== exit:N` marker written by /bin/sh. Fleet.launch wraps agy only; for
# every other engine a line-start exit marker in a fleet log is text the agent
# printed, never the process's status. `runjob run` (one-shot) wraps every
# engine, so the reaper always expects a marker. Keep this beside the wrapper in
# Fleet.launch — the two must move together.
FLEET_WRAPPED_ENGINES = {"agy"}
FLEET_MAX_ATTEMPTS = 3


class Verdict(NamedTuple):
    """One terminal decision about a job log, for either writer.

    status — reaper vocabulary (done|exited|limited|failed); the fleet has no
             "exited" and maps on `event`.
    event  — ledger event name (done|limited|failed).
    reason — stable discriminator so each call site can pick its side effects
             (retry_at source, summary text) without re-deciding the status.
    rc     — the trusted exit code, or None (missing, or not trusted here).
    """
    status: str
    event: str
    reason: str
    rc: int | None


def capture_outcome(log_file: Path, cwd: Path | None,
                    exit_code: int | None = None) -> dict:
    """Return best-effort terminal facts, with a null for every unavailable fact.

    This function is intentionally observational: a bad transcript, vanished
    worktree, or broken git executable must never alter the job's own result.
    Callers may safely splat its result into every terminal ledger event.
    """
    outcome = {
        "exit_code": exit_code,
        "status_line": None,
        "commit_sha": None,
        "branch": None,
        "dirty": None,
        "log_bytes": None,
    }
    try:
        try:
            outcome["log_bytes"] = log_file.stat().st_size
        except OSError:
            pass
        try:
            # Scan rather than loading a potentially very large transcript.
            # Retaining the last report implements the final-status convention,
            # except for an unfilled prompt template: a STATUS line immediately
            # followed by ``BLOCKING: <...>`` was echoed instruction text, not
            # a report the job completed.
            with log_file.open("r", encoding="utf-8", errors="replace") as fh:
                pending_status = None
                for line in fh:
                    line = line.rstrip("\r\n")
                    if pending_status is not None:
                        if not STATUS_PROMPT_CONTINUATION_RE.fullmatch(line):
                            outcome["status_line"] = pending_status
                        pending_status = None
                    if STATUS_LINE_RE.fullmatch(line):
                        pending_status = line
                if pending_status is not None:
                    outcome["status_line"] = pending_status
        except OSError:
            pass

        if cwd is None or not cwd.is_dir():
            return outcome

        def git(*args: str) -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    ["git", *args], cwd=cwd, capture_output=True, text=True,
                    timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                return None

        inside = git("rev-parse", "--is-inside-work-tree")
        if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
            return outcome

        head = git("rev-parse", "HEAD")
        if head is not None and head.returncode == 0:
            outcome["commit_sha"] = head.stdout.strip() or None
        branch = git("branch", "--show-current")
        if branch is not None and branch.returncode == 0:
            outcome["branch"] = branch.stdout.strip() or None
        status = git("status", "--porcelain")
        if status is not None and status.returncode == 0:
            outcome["dirty"] = bool(status.stdout)
    except Exception:
        # Outcome capture is post-mortem telemetry, never job control flow.
        pass
    return outcome


def classify_tail(tail: str, engine: str, *, agy_outcome: dict | None = None,
                  expect_exit_marker: bool = True,
                  attempts: int | None = None) -> Verdict:
    """THE terminal-status decision. One implementation, two call sites.

    Called by `_classify_from_log` (reaper / `runjob log` / `repair`) and by
    `Fleet.finalize`. Everything that was duplicated between them lives here;
    the two call sites differ only through the two parameters below, and each
    is a divergence deliberately kept because the callers' inputs really do
    differ (see docs/writer-agree.md).

    expect_exit_marker — is a `===== exit:N` marker written for this log?
        Reaper: always True (cmd_run wraps every engine), so a *missing* marker
        means the wrapper died and a *present* one is authoritative.
        Fleet: `engine in FLEET_WRAPPED_ENGINES`. For a non-agy fleet log no
        marker is ever written, so any match is quoted text and is ignored
        outright — that is what makes the fleet structurally immune to the
        SF-1 quoted-marker misparse rather than merely mitigated by last-match.
    attempts — the caller's retry budget already spent; None means "this caller
        cannot retry" (the reaper: a one-shot is never relaunched).

    The ladder is strongest-evidence-first:
      1. agy provider JSON — decisive whenever present (never retry a result
         the provider already returned; agy spends a shared Gemini pool).
         SUCCESS, or a non-SUCCESS result whose response already carries a
         DONE: trailer at exit 0, folds as done (cnd-ops-004: provider ERROR
         from post-work tooling must not override a completed DONE trailer).
      2. trusted rc == 0 — a clean wrapper exit outranks LIMIT prose (N-a/S-1).
      3. prose `DONE:` — only when rc cannot exist, where it is the strongest
         evidence there is (the fleet's ORCHESTRATOR CONTRACT asks for it).
      4. LIMIT prose — resumable park.
      5. trusted nonzero rc — hard failure.
      6. nothing conclusive — wrapper died (marker expected and absent), else
         retry until the attempt budget is gone.
    """
    rc = None
    if expect_exit_marker:
        # Wrapper marker is always last; .search would take a quoted earlier
        # match (SF-1: reports that quote ===== exit:N at line start).
        _exits = list(EXIT_RE.finditer(tail))
        rc = int(_exits[-1].group(1)) if _exits else None

    if engine == "agy" and agy_outcome is not None:
        # agy's machine result is positive completion evidence. A provider
        # SUCCESS at exit 0 is done; so is a non-SUCCESS result whose response
        # already printed DONE: after a post-work provider error. Nonzero
        # wrapper exit, or ERROR/FAILED with no DONE trailer, still fails
        # immediately and never parks as quota-limited.
        provider_status = str(agy_outcome.get("status", "")).upper()
        response = str(agy_outcome.get("response") or "")
        if rc == 0 and provider_status == "SUCCESS":
            return Verdict("done", "done", "agy_success", rc)
        if rc == 0 and DONE_RE.search(response):
            return Verdict("done", "done", "agy_done_trailer", rc)
        return Verdict("failed", "failed", "agy_result", rc)
    if rc == 0:
        # Clean wrapper exit outranks LIMIT_RE (N-a). A finished job whose report
        # merely *discusses* rate limits must not be filed as parked. Live
        # loopback 429 probes (2026-08-03): claude/glm/local/codex/grok all exit
        # nonzero on true parks; agy is immune by construction (needs SUCCESS
        # or a DONE trailer inside the provider response — see above).
        if engine == "agy":
            # Exit zero without a valid SUCCESS/DONE outcome is not success.
            return Verdict("failed", "failed", "agy_no_success", rc)
        if DONE_RE.search(tail):
            return Verdict("done", "done", "clean_exit_done", rc)
        return Verdict("exited", "done", "clean_exit", rc)
    if not expect_exit_marker and DONE_RE.search(tail):
        # No marker can exist for this log, so the DONE: trailer is the only
        # completion evidence available — and it outranks LIMIT prose, which is
        # the S-1 property on the path where S-1 actually applies (a finished
        # fleet job whose report discusses rate limits must not be relaunched).
        return Verdict("done", "done", "done_trailer", rc)
    if LIMIT_RE.search(tail):
        # Nonzero or missing exit + limit text: resumable park, not hard failure.
        # Kept after the rc==0 branch so prose cannot park a done job.
        return Verdict("limited", "limited", "limit_park", rc)
    if rc is not None:
        # Nonzero exit, no limit signal.
        return Verdict("failed", "failed", "nonzero_exit", rc)
    if expect_exit_marker:
        # Marker expected and absent, no limit signal: the wrapper itself died
        # (SIGKILL/OOM). Not retryable — nothing says the work can resume.
        return Verdict("failed", "failed", "wrapper_died", rc)
    if attempts is None or attempts + 1 >= FLEET_MAX_ATTEMPTS:
        # Give-up: existing status vocabulary. Readers key on status=failed.
        return Verdict("failed", "failed", "gave_up", rc)
    return Verdict("limited", "limited", "retry", rc)


def _classification_fields_from_log(log_file: Path, engine: str, *,
                                    exit_code: int | None = None) -> tuple[str, str, dict, int | None]:
    """Derive terminal verdict fields which do not depend on telemetry capture."""
    tail = _log_tail_text(log_file)
    agy_outcome = _parse_agy_outcome(log_file) if engine == "agy" else None
    # One-shot logs are always wrapped (cmd_run), and a one-shot is never
    # relaunched: marker expected, no retry budget.
    verdict = classify_tail(tail, engine, agy_outcome=agy_outcome,
                            expect_exit_marker=True, attempts=None)
    status, event, rc = verdict.status, verdict.event, verdict.rc

    outcome_fields = _agy_outcome_fields(agy_outcome) if agy_outcome else {}
    summary = str(outcome_fields.get("summary") or "")
    m = DONE_RE.search(tail)
    if not summary and m:
        summary = tail[m.start():m.start() + 200].splitlines()[0]
    elif not summary and status == "failed":
        # Surface why, so a dead job is legible in `runjob log` without opening it.
        lines = [ln for ln in tail.splitlines() if ln.strip() and not ln.startswith("=====")]
        summary = f"exit={rc if rc is not None else 'killed'}: {lines[-1][:160]}" if lines else f"exit={rc}"
    elif not summary and rc == 0 and LIMIT_RE.search(tail):
        # S1: make the accepted residual greppable. Without this, exit-0 + limit
        # prose writes summary="" and the gap is invisible in the ledger.
        summary = EXIT0_LIMIT_PROSE_SUMMARY
    outcome_fields["summary"] = summary
    # A wrapper marker is authoritative where it exists; a directly reaped
    # child rc is the fallback when the marker could not be read.
    return status, event, outcome_fields, rc if rc is not None else exit_code


def _classify_from_log(log_file: Path, engine: str, *, cwd: Path | None = None,
                       exit_code: int | None = None) -> tuple[str, str, dict]:
    """Derive (status, event, outcome_fields) from a job log without writing.

    Shared by the reaper (`_classify_and_log`) and opt-in `repair`, so a
    repaired row gets the same verdict the reaper would have written. The
    decision itself is `classify_tail`, shared with `Fleet.finalize`; this
    function only adds the ledger summary the reaper writes.
    """
    status, event, outcome_fields, captured_exit_code = _classification_fields_from_log(
        log_file, engine, exit_code=exit_code)
    outcome_fields.update(capture_outcome(log_file, cwd,
                                          exit_code=captured_exit_code))
    return status, event, outcome_fields


def _classify_and_log(uid: str, log_file: Path, engine: str, *,
                      cwd: Path | None = None,
                      exit_code: int | None = None) -> str:
    # Capture is optional post-mortem telemetry.  Keep it outside the decision
    # boundary so an interrupt while probing a worktree cannot erase the
    # terminal ledger event; after that event is durable, propagate the
    # interrupt normally.
    status, event, outcome_fields, captured_exit_code = _classification_fields_from_log(
        log_file, engine, exit_code=exit_code)
    try:
        outcome_fields.update(capture_outcome(
            log_file, cwd, exit_code=captured_exit_code))
    except BaseException:
        log_event(event=event, uid=uid, status=status, **outcome_fields)
        raise
    log_event(event=event, uid=uid, status=status, **outcome_fields)
    return status


def cmd_reap(args) -> int:
    """Internal: wait for a detached one-shot to exit, then update the log."""
    uid, pid, log_file, engine = args.uid, int(args.pid), Path(args.log), args.engine
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True)
            if not r.stdout.strip() or r.stdout.strip().startswith("Z"):
                break
        time.sleep(5)
    cwd_arg = getattr(args, "cwd", None)
    cwd = Path(cwd_arg) if cwd_arg else None
    _classify_and_log(uid, log_file, engine, cwd=cwd)
    return 0


# --- shared log viewer -------------------------------------------------------

STATUS_MARK = {"running": "▶", "done": "✔", "exited": "✔", "limited": "⏸",
               "failed": "✖", "queued": "·", "stalled": "⚠"}

# EOF trailer protocol (docs/job-report-protocol.md). Order is significant.
REPORT_TRAILER_KEYS = (
    "STATUS",
    "CAUSE_CLASS",
    "TESTS",
    "RESIDUAL",
    "COMMIT",
    "MODIFIED_PATHS",
    "EVIDENCE_PATH",
)
REPORT_TRAILER_STATUSES = frozenset({"COMPLETE", "FIXED", "BLOCKED", "FAILED"})
# Machine states for parse_report_trailer / cmd_report --json.
TRAILER_PRESENT = "present"
TRAILER_ABSENT = "absent"
TRAILER_MALFORMED = "malformed"
TRAILER_WINDOW_CLIPPED = "window_clipped"
TRAILER_MISSING_LOG = "missing_log"
# Exit status: present/absent → 0 (determined successfully); never map
# malformed/clipped/missing to success so unattended reapers cannot treat
# silence as "no trailer".
TRAILER_EXIT = {
    TRAILER_PRESENT: 0,
    TRAILER_ABSENT: 0,
    TRAILER_MALFORMED: 3,
    TRAILER_WINDOW_CLIPPED: 4,
    TRAILER_MISSING_LOG: 1,
}
# The default keeps routine report reads bounded. The read window stays
# small on purpose (cheap codex/hourly tails); the legal trailer cap is
# independent so a long MODIFIED_PATHS line can still parse when the
# caller widens --bytes (or when the whole file is small enough to fit).
DEFAULT_REPORT_BYTES = 4096
# Cap for a well-formed EOF trailer once it is fully in hand. Sized for a
# 100-path MODIFIED_PATHS line with deep repo-relative paths (~5.5 KiB)
# plus headroom; matches the tick's common --bytes 32768 widen step.
MAX_REPORT_TRAILER_BYTES = 32768
# Launcher may append exactly one literal exit marker after the trailer.
_WRAPPER_EXIT_RE = re.compile(r"^===== exit:\d+$")
# Scalar forms (strict single-line; no ANSI/control).
_RE_CAUSE_CLASS = re.compile(r"^(NONE|[A-Za-z0-9._-]{1,64})$")
_RE_TESTS = re.compile(r"^(NONE|\d+ passed / \d+ failed)$")
_RE_RESIDUAL = re.compile(r"^(0|[1-9]\d*)$")
_RE_COMMIT = re.compile(
    r"^(NONE|OK \+ [0-9a-fA-F]{4,64}|DENIED \+ \S(?:.*\S)?)$")
_RE_REPO_PATH_SEGMENT = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._+=@-]+$")


class TrailerParse(NamedTuple):
    """Structured result of parse_report_trailer.

    state: present | absent | malformed | window_clipped
    fields: key→value when present; else None
    reason: short machine slug for malformed / window_clipped
    """
    state: str
    fields: dict[str, str] | None = None
    reason: str | None = None


def _log_tail_lines(log_file: Path, nlines: int) -> str:
    """Last nlines of a job's own log. Bounded read — job logs get large."""
    text = log_file.read_bytes()[-400_000:].decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-nlines:])


def _read_log_tail_bytes(log_file: Path, nbytes: int) -> tuple[bytes, int, int]:
    """Return (tail_bytes, total_size, bytes_skipped). Bounded read from EOF."""
    size = log_file.stat().st_size
    skip = max(0, size - max(0, nbytes))
    with log_file.open("rb") as fh:
        if skip:
            fh.seek(skip)
        data = fh.read()
    return data, size, skip


def _line_has_controls(line: str) -> bool:
    """True if line contains a C0/C1 or ANSI control character."""
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in line)


def _is_repo_relative_path(value: str) -> bool:
    """Validate the published, deliberately small repo-relative path grammar."""
    return bool(value) and all(
        _RE_REPO_PATH_SEGMENT.fullmatch(segment) for segment in value.split("/"))


def _normalise_trailer_value(key: str, value: str) -> str:
    """Return the protocol's canonical scalar representation."""
    if key == "MODIFIED_PATHS" and value != "NONE":
        return ",".join(part.strip() for part in value.split(","))
    return value


def _split_trailer_key(line: str) -> tuple[str, str] | None:
    """Return (KEY, value) for a strict `KEY: value` / `KEY:value` line, or None.

    No leading whitespace before the key. Optional single space after the colon
    is stripped; further leading spaces on the value are not accepted.
    """
    if not line or line[0] in " \t":
        return None
    if _line_has_controls(line):
        return None
    for key in REPORT_TRAILER_KEYS:
        prefix = key + ":"
        if line.startswith(prefix):
            rest = line[len(prefix):]
            if rest.startswith(" "):
                rest = rest[1:]
            if rest.startswith(" ") or rest.startswith("\t"):
                return None
            return key, rest
    return None


def _validate_trailer_value(key: str, value: str) -> bool:
    """True when *value* matches the documented scalar form for *key*."""
    if _line_has_controls(value):
        return False
    if key == "STATUS":
        return value in REPORT_TRAILER_STATUSES
    if key == "CAUSE_CLASS":
        return bool(_RE_CAUSE_CLASS.match(value))
    if key == "TESTS":
        return bool(_RE_TESTS.match(value))
    if key == "RESIDUAL":
        return bool(_RE_RESIDUAL.match(value))
    if key == "COMMIT":
        return bool(_RE_COMMIT.match(value))
    if key == "MODIFIED_PATHS":
        if value == "NONE":
            return True
        # One optional ASCII space after each comma is accepted and normalized.
        return all(
            part == part.strip() and _is_repo_relative_path(part)
            for part in value.replace(", ", ",").split(","))
    if key == "EVIDENCE_PATH":
        if value == "NONE":
            return True
        return _is_repo_relative_path(value)
    return False


def _parse_trailer_block(seven: list[str]) -> TrailerParse:
    """Parse exactly seven consecutive lines as a trailer block."""
    fields: dict[str, str] = {}
    for i, expected in enumerate(REPORT_TRAILER_KEYS):
        line = seven[i]
        if line == "":
            return TrailerParse(TRAILER_MALFORMED, reason="blank-line")
        if line[0] in " \t":
            return TrailerParse(TRAILER_MALFORMED, reason="leading-whitespace")
        if _line_has_controls(line):
            return TrailerParse(TRAILER_MALFORMED, reason="control-chars")
        split = _split_trailer_key(line)
        if split is None:
            return TrailerParse(TRAILER_MALFORMED, reason="key-order")
        key, value = split
        if key != expected:
            return TrailerParse(TRAILER_MALFORMED, reason="key-order")
        if not _validate_trailer_value(key, value):
            return TrailerParse(TRAILER_MALFORMED, reason=f"bad-{key.lower()}")
        fields[key] = _normalise_trailer_value(key, value)
    return TrailerParse(TRAILER_PRESENT, fields=fields)


def _contains_protocol_evidence(lines: list[str]) -> bool:
    """Whether a complete read contains an attempted report frame anywhere."""
    for line in lines:
        stripped = line.lstrip()
        if any(stripped.startswith(key + ":") for key in REPORT_TRAILER_KEYS):
            return True
    # A log cut off while spelling its first key is still a recognisable attempt.
    if lines:
        last = lines[-1].lstrip()
        if len(last) >= 3 and any(
                key.startswith(last) for key in REPORT_TRAILER_KEYS):
            return True
    return False


def _strip_eof_trailer_region(lines: list[str]) -> list[str]:
    """Drop trailing blanks and at most one literal launcher exit marker."""
    out = list(lines)
    while out and out[-1].strip() == "":
        out.pop()
    if out and _WRAPPER_EXIT_RE.match(out[-1].strip()):
        out.pop()
        while out and out[-1].strip() == "":
            out.pop()
    return out


def parse_report_trailer(
    text: str, *, bytes_skipped: int = 0,
) -> TrailerParse:
    """Parse the EOF-anchored report trailer from a (possibly bounded) tail.

    Protocol is strictly single-line, seven keys in order, terminated by EOF
    (or only trailing blanks / one launcher `===== exit:N` line). Mid-log
    quotes and heredocs are not accepted even when they contain seven matching
    lines — the block must sit at the end of the text.

    Returns TrailerParse with state:
      present        — valid trailer at EOF
      absent         — whole file read and no trailer evidence exists
      malformed      — candidate at EOF that violates the grammar
      window_clipped — bounded read did not yield a valid frame (caller must
                       widen --bytes before treating the log as absent)

    Threat model closed: accidental quoted templates / heredocs with following
    prose must not adjudicate the job. Not closed: a job that deliberately
    ends its log with a forged-but-well-formed trailer (self-attestation; A4).
    """
    raw_lines = text.splitlines()
    n = len(REPORT_TRAILER_KEYS)

    lines = _strip_eof_trailer_region(raw_lines)
    parsed: TrailerParse | None = None
    if len(lines) >= n:
        # EOF-anchored: only the final seven lines may form the trailer.
        candidate = lines[-n:]
        parsed = _parse_trailer_block(candidate)
        if parsed.state == TRAILER_PRESENT:
            encoded = ("\n".join(candidate) + "\n").encode("utf-8")
            if len(encoded) > MAX_REPORT_TRAILER_BYTES:
                if bytes_skipped > 0:
                    return TrailerParse(TRAILER_WINDOW_CLIPPED,
                                        reason="trailer-too-large")
                return TrailerParse(TRAILER_MALFORMED, reason="trailer-too-large")
            return parsed

    # This is the central transport invariant: an absence conclusion requires
    # byte 0. A bounded suffix that did not yield a valid frame is information
    # incomplete, even when it contains no recognizable key at all.
    if bytes_skipped > 0:
        return TrailerParse(TRAILER_WINDOW_CLIPPED,
                            reason=(parsed.reason if parsed else "bounded-read"))

    if _contains_protocol_evidence(lines):
        if parsed is not None:
            return TrailerParse(TRAILER_MALFORMED,
                                reason=parsed.reason or "grammar")
        if len(lines) < n:
            return TrailerParse(TRAILER_MALFORMED, reason="incomplete-block")
        return TrailerParse(TRAILER_MALFORMED, reason="grammar")

    return TrailerParse(TRAILER_ABSENT)


def _follow_file(path: Path) -> None:
    """Stream appends to one job's log (the --uid half of --follow)."""
    print("\n(following — Ctrl-C to stop)")
    seen = path.stat().st_size if path.exists() else 0
    try:
        while True:
            time.sleep(2)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size > seen:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(seen)
                    sys.stdout.write(fh.read())
                    sys.stdout.flush()
                seen = size
    except KeyboardInterrupt:
        pass


def cmd_log(args) -> int:
    states = read_states()
    if args.uid:
        states = [s for s in states if s["uid"] == args.uid]
        if not states:
            # Never say "no jobs logged yet" for a uid miss — that reads as "the
            # ledger is empty" and sends the caller hunting for the log elsewhere.
            print(f"no job with uid {args.uid} in {JOBS_LOG}")
            return 0
    if not states:
        print(f"no jobs logged yet ({JOBS_LOG})")
        return 0
    if args.active:
        states = [s for s in states if s.get("status") in ("running", "queued", "limited")]
        # Default: hide only positively-dead daggered corpses. UNKNOWN liveness
        # stays visible (shown-and-flagged below) — never omit on one uncertain
        # ps probe (B3). --include-abandoned restores full active list.
        if not getattr(args, "include_abandoned", False):
            live: list[dict] = []
            for s in states:
                status = s.get("status", "") or ""
                liv = process_liveness(
                    s.get("pid"), launched_at=_launched_at_for_state(s))
                pid_dead = liv == LIVENESS_DEAD
                if display_is_zombie(
                        status, pid_dead=pid_dead, retry_at=s.get("retry_at")):
                    continue
                live.append(s)
            states = live
    print(f"{'':1} {'UID':<28} {'PROJECT':<14} {'ENGINE':<7} {'MODEL':<24} "
          f"{'STATUS':<9} UPDATED")
    print("-" * 108)
    # One shared CPU window for the whole listing (B3). Cheap conjuncts
    # (age/log/liveness) filter candidates first so a large-log or young row
    # never enters the sample set; then every candidate classifies against the
    # same pair of snapshots.
    display_rows = list(states[:args.limit])
    cpu_by_pid = process_tree_cpu_window_samples(
        stall_cpu_sample_pids(display_rows))
    for s in display_rows:
        status = s.get("status", "") or "-"
        mark = STATUS_MARK.get(s.get("status", ""), "?")
        # Belt and braces: the ledger may still say "running" after the process
        # died without a terminal event, or "limited" after a runner abandoned a
        # park. Reuse Fleet.status()'s † convention. Display only — this path
        # never writes (see cmd_repair for opt-in fix; repair stays running-only).
        launched = _launched_at_for_state(s)
        liv = process_liveness(s.get("pid"), launched_at=launched)
        pid_dead = liv == LIVENESS_DEAD
        if display_is_zombie(
                status, pid_dead=pid_dead, retry_at=s.get("retry_at")):
            status = f"{status}†"
            # Glyph first: do not leave ▶/⏸ on a row the dagger has already
            # declared abandoned (N2). "?" matches "unknown / needs attention".
            mark = "?"
        elif liv == LIVENESS_UNKNOWN and status in ("running", "limited"):
            # Uncertain liveness: show and flag, never hide (B3).
            status = f"{status}?"
            mark = "?"
        elif status == "running" and liv == LIVENESS_ALIVE:
            # Activity floor: process exists but has produced nothing and
            # burned no CPU past the grace window. Display only — never
            # writes the ledger, never kills (see classify_stall).
            # Inject the shared listing sample when present so we do not
            # re-sleep per row. Non-candidates (large log / too young) are
            # absent from cpu_by_pid and short-circuit inside classify_stall
            # without sampling. A failed shared sample (None) is left
            # uninjected — classify_stall re-observes and fails open.
            stall_kwargs: dict = {
                "status": status,
                "pid": s.get("pid"),
                "launched_at": launched,
                "log_path": s.get("log"),
                "liveness": liv,
            }
            raw_pid = s.get("pid")
            if raw_pid is not None:
                sampled = cpu_by_pid.get(int(raw_pid))
                if sampled is not None:
                    stall_kwargs["cpu_seconds"] = sampled[0]
                    stall_kwargs["cpu_delta"] = sampled[1]
            if classify_stall(**stall_kwargs) == STALL_STALLED:
                status = "stalled"
                mark = STATUS_MARK["stalled"]
        print(f"{mark} {s['uid']:<28} {s.get('project_name', '-'):<14} "
              f"{s.get('engine', '-'):<7} {(s.get('model') or '-'):<24} "
              f"{status:<9} {s.get('updated', '')[11:19]}")
    if getattr(args, "outcome", False):
        print("\nOUTCOME")
        print(f"{'UID':<28} {'EXIT':<6} {'DIRTY':<7} {'STATUS LINE':<32} "
              f"{'BRANCH':<24} COMMIT")
        print("-" * 128)
        for s in states[:args.limit]:
            status_line = str(s.get("status_line") or "-")
            if len(status_line) > 32:
                status_line = status_line[:29] + "..."
            dirty = s.get("dirty")
            dirty_text = "-" if dirty is None else str(bool(dirty)).lower()
            print(f"{s['uid']:<28} {str(s.get('exit_code') if s.get('exit_code') is not None else '-'):<6} "
                  f"{dirty_text:<7} {status_line:<32} "
                  f"{(s.get('branch') or '-'):<24} {s.get('commit_sha') or '-'}")
    # `--uid X` means "show me that job", which every caller so far has taken to
    # mean its output, not a one-row table. Print the path and tail it here: a
    # table-only answer sends sessions hunting for the file themselves; broad
    # home-directory searches can also trigger macOS privacy prompts.
    job_log = None
    if args.uid:
        job_log = Path(states[0]["log"]) if states[0].get("log") else None
        if job_log is None:
            print("\n(no log path recorded for this job)")
        elif not job_log.exists():
            print(f"\nlog: {job_log}\n(log file is gone)")
        else:
            print(f"\nlog: {job_log}")
            print(_log_tail_lines(job_log, args.limit))
    if args.follow and job_log is not None:
        _follow_file(job_log)
    elif args.follow:
        print("\n(following — Ctrl-C to stop)")
        seen = JOBS_LOG.stat().st_size if JOBS_LOG.exists() else 0
        try:
            while True:
                time.sleep(2)
                if not JOBS_LOG.exists():
                    continue
                size = JOBS_LOG.stat().st_size
                if size > seen:
                    with JOBS_LOG.open("r", encoding="utf-8") as fh:
                        fh.seek(seen)
                        for line in fh:
                            try:
                                ev = json.loads(line)
                                print(f"  {STATUS_MARK.get(ev.get('status',''),'·')} "
                                      f"{ev.get('event'):<8} {ev.get('uid')}")
                            except json.JSONDecodeError:
                                pass
                    seen = size
        except KeyboardInterrupt:
            pass
    return 0


def cmd_report(args) -> int:
    """Print a bounded log tail (or structured trailer) for one job uid.

    Machine half of the reap-protocol fix: reapers must not read multi-MB
    tool transcripts when the adjudication signal is the last ~4 KB.

    Exit status (deliberate; reapers may branch on it):
      0  trailer present or absent (state determined successfully)
      1  missing log file (I/O error in both text and --json)
      2  unknown uid / bad --bytes
      3  malformed trailer candidate at EOF
      4  window_clipped (widen --bytes; do not treat as absent)
    """
    uid = args.uid
    states = [s for s in read_states() if s["uid"] == uid]
    if not states:
        print(f"runjob report: unknown uid {uid!r} (not in {JOBS_LOG})",
              file=sys.stderr)
        return 2
    state = states[0]
    engine = state.get("engine") or "-"
    log_path = Path(state["log"]) if state.get("log") else (HOME / "logs" / f"{uid}.log")
    # Explicit None → default. Zero is a real request for an empty window
    # (R1); do not coerce 0 to DEFAULT via `or`.
    if not hasattr(args, "bytes") or args.bytes is None:
        nbytes = DEFAULT_REPORT_BYTES
    else:
        nbytes = args.bytes
    if nbytes < 0:
        print("runjob report: --bytes must be >= 0", file=sys.stderr)
        return 2

    if not log_path.is_file():
        if args.json:
            obj: dict = {k: None for k in REPORT_TRAILER_KEYS}
            obj["trailer_state"] = TRAILER_MISSING_LOG
            obj["trailer_present"] = False
            obj["reason"] = "missing-log"
            obj["bytes_skipped"] = 0
            obj["tail"] = ""
            print(json.dumps(obj, ensure_ascii=False))
            return TRAILER_EXIT[TRAILER_MISSING_LOG]
        print(f"runjob report: log file missing for {uid}: {log_path}",
              file=sys.stderr)
        return TRAILER_EXIT[TRAILER_MISSING_LOG]

    data, _size, bytes_skipped = _read_log_tail_bytes(log_path, nbytes)
    tail_text = data.decode("utf-8", errors="replace")
    parsed = parse_report_trailer(tail_text, bytes_skipped=bytes_skipped)
    exit_code = TRAILER_EXIT[parsed.state]

    if args.json:
        obj = {k: None for k in REPORT_TRAILER_KEYS}
        obj["trailer_state"] = parsed.state
        obj["trailer_present"] = parsed.state == TRAILER_PRESENT
        obj["bytes_skipped"] = bytes_skipped
        if parsed.reason:
            obj["reason"] = parsed.reason
        if parsed.state == TRAILER_PRESENT and parsed.fields:
            obj.update(parsed.fields)
        else:
            obj["tail"] = tail_text
        print(json.dumps(obj, ensure_ascii=False))
        return exit_code

    # Human mode: state is always loud on the header line.
    reason_bit = f" reason={parsed.reason}" if parsed.reason else ""
    print(f"report uid={uid} engine={engine} bytes_skipped={bytes_skipped} "
          f"trailer_state={parsed.state}{reason_bit}")
    if parsed.state == TRAILER_PRESENT and parsed.fields:
        for key in REPORT_TRAILER_KEYS:
            print(f"{key}: {parsed.fields[key]}")
    else:
        # Binary-safe-ish stdout: write decoded text as-is (errors already replaced).
        sys.stdout.write(tail_text)
        if tail_text and not tail_text.endswith("\n"):
            sys.stdout.write("\n")
    return exit_code


def cmd_repair(args) -> int:
    """Opt-in: append terminal events for ledger rows stuck at status=running
    whose pid is gone (or whose pid was reused). Never runs from `log`/read.

    An operator must invoke this explicitly. Concurrent readers
    do not race here because they do not write on read — only this command does.

    Verdict is derived from the job log when one exists (same classifier the
    reaper uses) — hard-coding `failed` would mint wrong rows over completed
    work and trigger duplicate launches of already-accepted jobs.
    """
    states = read_states()
    if args.uid:
        states = [s for s in states if s["uid"] == args.uid]
    elif not args.all and not args.dry_run:
        # Unfiltered repair touches every historical phantom machine-wide.
        # Require --all (or --dry-run / --uid) so a bare `runjob repair` cannot
        # silently rewrite months of ledger state (N4).
        print("refusing unfiltered repair: pass --uid <uid>, --all, or --dry-run",
              file=sys.stderr)
        return 2
    candidates: list[dict] = []
    for s in states:
        if s.get("status") != "running":
            continue
        pid = s.get("pid")
        launched = _launched_at_for_state(s)
        if process_is_alive(pid, launched_at=launched):
            continue
        candidates.append(s)
    if not candidates:
        print("nothing to repair")
        return 0
    if not args.uid and len(candidates) > 1:
        print(f"repair sweep: {len(candidates)} dead-but-running row(s)")
    repaired: list[str] = []
    for s in candidates:
        pid = s.get("pid")
        launched = _launched_at_for_state(s)
        log_path = Path(s["log"]) if s.get("log") else None
        if log_path is not None and log_path.is_file():
            status, _ev, _fields = _classify_from_log(
                log_path, s.get("engine") or "")
            summary = (f"repaired: pid {pid} gone "
                       f"(ledger still said running; launched_at={launched}; "
                       f"classified from log as {status})")
        else:
            # No log evidence at all — only then default to failed.
            status = "failed"
            summary = (f"repaired: pid {pid} gone "
                       f"(ledger still said running; launched_at={launched}; "
                       f"no log evidence)")
        if args.dry_run:
            print(f"would repair {s['uid']} pid={pid} → {status}")
        else:
            log_event(event="repaired", uid=s["uid"], status=status,
                      summary=summary, pid=None)
            print(f"repaired {s['uid']} pid={pid} → {status}")
        repaired.append(s["uid"])
    verb = "would be repaired" if args.dry_run else "repaired"
    print(f"{len(repaired)} job(s) {verb}")
    return 0


# --- dashboard (self-contained SPA over the shared log) ----------------------

def cmd_dash(args) -> int:
    import http.server
    import socketserver
    from urllib.parse import unquote, urlparse

    dash_html = (SKILL_DIR / "dash.html").read_text(encoding="utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # quiet
            pass

        def send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/jobs":
                self.send_json({"jobs": read_states(), "ts": iso()})
                return

            # Resolve the log from the folded job state; never accept a file path
            # from the browser.  This keeps the local API from becoming an
            # arbitrary-file reader.
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "log":
                uid = unquote(parts[3])
                state = next((s for s in read_states() if s.get("uid") == uid), None)
                if state is None:
                    self.send_json({"error": "unknown job"}, 404)
                    return
                log_name = state.get("log")
                if not log_name:
                    self.send_json({"uid": uid, "text": "", "available": False,
                                    "ts": iso()})
                    return
                try:
                    log_path = Path(log_name)
                    limit = 100_000
                    size = log_path.stat().st_size
                    with log_path.open("rb") as fh:
                        if size > limit:
                            fh.seek(-limit, os.SEEK_END)
                        data = fh.read()
                    text = data.decode("utf-8", errors="replace")
                    if size > limit and "\n" in text:
                        text = text.split("\n", 1)[1]
                    self.send_json({"uid": uid, "text": text, "available": True,
                                    "truncated": size > limit, "size": size,
                                    "ts": iso()})
                except OSError as e:
                    self.send_json({"uid": uid, "text": "", "available": False,
                                    "error": str(e), "ts": iso()})
                return

            body = dash_html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    port = args.port
    with Server(("127.0.0.1", port), Handler) as httpd:
        print(f"runjob dashboard → http://127.0.0.1:{port}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


# --- fleet (file-based queue in a project) -----------------------------------
# A file-backed orchestrator: one worktree and branch
# per job, caps per engine, dependency DAG, auto-park on limits + resume, and
# every transition mirrored into the shared global log.

TRAILER = """

--- ORCHESTRATOR CONTRACT ---
You are running headless under an orchestrator inside a dedicated git worktree
for branch `{branch}` (cwd is the worktree root). Rules:
1. Work only in this worktree. Never switch branches or touch main/other worktrees.
2. Commit early and often — small logical commits on `{branch}`.
3. Maintain PROGRESS.md at the worktree root (done / next / blockers). It is the
   resume checkpoint if a usage limit cuts this session.
4. If resumed: read PROGRESS.md first, continue, do not redo completed work.
5. External side effects (network sends, installs, deletions outside this
   worktree) are out of scope — note them in PROGRESS.md and stop that thread.
6. When fully complete: final commit, PROGRESS.md status COMPLETE, and end your
   reply with a single line starting "DONE:" plus a one-line summary.
"""

RESUME_PROMPT = (
    "You were interrupted (usage limit or crash). Re-read PROGRESS.md in the "
    "current directory and your original instructions, then continue from where "
    "you left off. Do not redo completed work. End with a line starting \"DONE:\" "
    "when fully complete.")

FLEET_META_ORDER = ["engine", "model", "effort", "branch", "after", "status",
                    "session", "pid", "attempts", "retry_at", "started",
                    "launched_at", "updated"]


class Fleet:
    def __init__(self, project_root: Path, cfg: dict):
        self.root = project_root
        self.cfg = cfg
        self.jobs_dir = project_root / cfg["jobs_dir"]
        self.wt_dir = project_root / cfg["worktrees_dir"]
        self.logs_dir = (project_root / cfg["logs_dir"]) if cfg.get("logs_dir") \
            else self.jobs_dir / "logs"
        self.procs: dict[int, subprocess.Popen] = {}
        self._usage_cache: dict[str, tuple[float, str]] = {}  # engine -> (mono, raw output)
        self._agy_quota_cache: tuple[float, dict] | None = None
        self._usage_retry_at: dict[str, str] = {}
        self._agy_quota_warning = ""

    # -- usage gating (optional, config-driven; fails open) --
    # Gate options per engine (all against the cmd's stdout):
    #   thresholds {name: pct}  — %USED style: defer if `name:pct%` >= pct
    #   max pct                 — %USED style: defer if max bare `pct%` >= pct
    #   min_left pct            — %LEFT style: defer if min `pct% left` <= pct
    def usage_defer(self, job: dict) -> str | None:
        if job["engine"] == "agy":
            hit = self._agy_quota_cache
            if hit and (time.monotonic() - hit[0]) < 120:
                quota = hit[1]
            else:
                quota = read_agy_quota()
                self._agy_quota_cache = (time.monotonic(), quota)
            if not quota.get("available"):
                warning = str(quota.get("error") or "unknown reader error")
                if warning != self._agy_quota_warning:
                    print(f"runjob: warning — agy quota reader unavailable ({warning}); "
                          "fleet is fail-open", file=sys.stderr)
                    self._agy_quota_warning = warning
                return None
            self._agy_quota_warning = ""
            if quota.get("exhausted"):
                self._usage_retry_at[job["id"]] = _agy_retry_at(quota)
                remaining = quota.get("remaining_percentage")
                pct = f" ({remaining:.0f}% remaining)" if remaining is not None else ""
                return f"shared Gemini pool exhausted{pct}"
            # Free-run posture: percentage alone never reserves or throttles the
            # pool. Only isExhausted/zero remaining can defer an agy job.
            return None
        gate = self.cfg.get("usage_gate", {}).get(job["engine"])
        if not gate or not gate.get("cmd"):
            return None
        hit = self._usage_cache.get(job["engine"])
        if hit and (time.monotonic() - hit[0]) < 120:
            out = hit[1]
        else:
            try:
                r = subprocess.run(shlex.split(gate["cmd"]), cwd=self.root,
                                   capture_output=True, text=True, timeout=20)
                out = (r.stdout or "") + (r.stderr or "")
            except (subprocess.SubprocessError, OSError):
                return None  # fail open
            self._usage_cache[job["engine"]] = (time.monotonic(), out)
        named = {m.group(1): float(m.group(2))
                 for m in re.finditer(r"(\w+):(\d+(?:\.\d+)?)%", out)}
        for name, thresh in (gate.get("thresholds") or {}).items():
            if named.get(name, 0.0) >= float(thresh):
                return f"{name} {named[name]:.0f}% ≥ {thresh}%"
        if gate.get("max") is not None:
            bare = [float(x) for x in re.findall(r"(?<![:\w])(\d+(?:\.\d+)?)%", out)]
            if bare and max(bare) >= float(gate["max"]):
                return f"usage {max(bare):.0f}% ≥ {gate['max']}%"
        if gate.get("min_left") is not None:
            lefts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%\s*left", out)]
            if lefts and min(lefts) <= float(gate["min_left"]):
                return f"only {min(lefts):.0f}% left ≤ {gate['min_left']}%"
        return None

    # -- job files --
    def parse(self, path: Path) -> dict | None:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return None
        try:
            _, fm, body = text.split("---", 2)
        except ValueError:
            return None
        meta: dict = {}
        for line in fm.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        prefix = self.cfg["branch_prefix"]
        return {
            "path": path, "id": meta.get("id") or path.stem,
            "engine": (meta.get("engine") or "claude").lower(),
            "model": meta.get("model") or "", "effort": meta.get("effort") or "",
            "sanitize": meta.get("sanitize", "").lower(),
            "branch": meta.get("branch") or f"{prefix}/{path.stem}",
            "after": [a.strip() for a in (meta.get("after") or "").split(",") if a.strip()],
            "status": meta.get("status") or "queued",
            "session": meta.get("session") or "",
            "pid": int(meta["pid"]) if meta.get("pid", "").isdigit() else None,
            "attempts": int(meta.get("attempts") or 0),
            "retry_at": meta.get("retry_at") or "",
            "started": meta.get("started") or "",
            # Per-launch bound for pid-reuse checks. Distinct from started
            # (first-ever start); refreshed on every launch including resumes.
            "launched_at": meta.get("launched_at") or "",
            "updated": meta.get("updated") or "",
            "body": body.lstrip("\n"),
        }

    def save(self, job: dict) -> None:
        job["updated"] = iso()
        lines = ["---", f"id: {job['id']}"]
        for k in FLEET_META_ORDER:
            v = job.get(k)
            if k == "after":
                v = ", ".join(job["after"]) if job["after"] else ""
            if v in (None, ""):
                continue
            lines.append(f"{k}: {v}")
        lines.append("---")
        job["path"].write_text("\n".join(lines) + "\n\n" + job["body"], encoding="utf-8")

    def load(self) -> list[dict]:
        if not self.jobs_dir.exists():
            return []
        out = []
        for p in sorted(self.jobs_dir.glob("*.md")):
            if p.name.startswith("_"):
                continue
            j = self.parse(p)
            if j:
                out.append(j)
        return out

    def fleet_uid(self, job: dict) -> str:
        return f"{self.root.name}-fleet-{job['id']}"

    # -- process --
    def pid_alive(self, pid: int | None, launched_at: str | None = None) -> bool:
        if not pid:
            return False
        # Prefer the Popen handle when we still own the child — poll() cannot
        # be fooled by pid reuse the way a bare kill(0) can.
        proc = self.procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        return process_is_alive(pid, launched_at=launched_at)

    def git(self, a: list[str], cwd: Path | None = None):
        return subprocess.run(["git", *a], cwd=cwd or self.root,
                              capture_output=True, text=True)

    def ensure_worktree(self, job: dict) -> Path:
        wt = self.wt_dir / job["id"]
        if wt.exists():
            return wt
        self.wt_dir.mkdir(exist_ok=True)
        br = job["branch"]
        have = bool(self.git(["branch", "--list", br]).stdout.strip())
        r = (self.git(["worktree", "add", str(wt), br]) if have
             else self.git(["worktree", "add", "-b", br, str(wt), "HEAD"]))
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
        sanitize = ((job["engine"] in self.cfg["sanitize_engines"]
                     and job.get("sanitize") != "no") or job.get("sanitize") == "yes")
        excludes = self.cfg["sanitize_excludes"]
        if sanitize and excludes:
            r = self.git(["sparse-checkout", "set", "--no-cone", "/*",
                          *[f"!{p}" for p in excludes]], cwd=wt)
            if r.returncode != 0:
                raise RuntimeError(f"sparse-checkout failed: {r.stderr.strip()}")
        if job["engine"] == "grok":
            self._write_grok_sandbox(wt)
        return wt

    def _write_grok_sandbox(self, wt: Path) -> None:
        """grok's `workspace` sandbox confines writes to the worktree, but a
        worktree's gitdir + shared object store live under the main repo's .git/,
        so `git commit` there is denied. Write a per-worktree `runjob` profile
        that extends `workspace` and adds the main .git as writable — the grok
        equivalent of the codex writable_roots whitelist. Kept per-worktree (not
        global) so concurrent jobs don't race and each is scoped to its own repo."""
        _write_grok_sandbox(wt, (self.root / ".git").resolve(), fleet=True)

    def log_file(self, job: dict) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self.logs_dir / f"{job['id']}.log"

    def launch(self, job: dict, resume: bool = False) -> None:
        wt = self.ensure_worktree(job)
        if job["engine"] in ("claude", "grok", "glm") and not job["session"]:
            job["session"] = str(uuid.uuid4())
        prompt = RESUME_PROMPT if resume else (
            job["body"].rstrip() + TRAILER.format(branch=job["branch"]))
        eff_model = effective_model(job["engine"], job["model"])
        cmd, use_stdin, extra_env = build_command(
            job["engine"], eff_model, job["effort"], prompt,
            session=job["session"], resume=resume, repo_root=self.root,
            grok_sandbox="runjob" if job["engine"] == "grok" else "workspace")
        if job["engine"] == "glm" and not os.environ.get("GLM_API_KEY"):
            raise RuntimeError("GLM_API_KEY not set")
        if job["engine"] == "local" and (
                not os.environ.get("LOCAL_API_KEY") or not os.environ.get("LOCAL_BASE_URL")):
            raise RuntimeError("LOCAL_API_KEY and LOCAL_BASE_URL must be set")
        pf = self.log_file(job).with_suffix(".prompt.txt")
        pf.write_text(prompt, encoding="utf-8")
        logf = open(self.log_file(job), "a", encoding="utf-8")
        logf.write(f"\n===== {iso()} {'resume' if resume else 'launch'} {job['engine']}\n")
        logf.flush()
        env = os.environ.copy()
        env.update(extra_env)
        cmd = resolve_engine_binary(cmd, env)
        ensure_agy_argv_fits(cmd, env)
        prompt_stream: IO[str] | None = (
            open(pf, "r", encoding="utf-8") if use_stdin else None)
        stdin_src: IO[str] | int = prompt_stream or subprocess.DEVNULL
        spawn_cmd = cmd
        if job["engine"] in FLEET_WRAPPED_ENGINES:
            # Whatever this set contains is exactly what finalize may trust as a
            # real exit marker (classify_tail's expect_exit_marker) — wrapping a
            # new engine here changes the classification of its fleet logs.
            wrapper = ('"$@"\nec=$?\nprintf "\\n===== exit:%s\\n" "$ec"\nexit "$ec"')
            spawn_cmd = ["/bin/sh", "-c", wrapper, "runjob-wrapper", *cmd]
        try:
            proc = subprocess.Popen(spawn_cmd, cwd=wt, stdin=stdin_src, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    env=env)
        finally:
            if prompt_stream is not None:
                prompt_stream.close()
        self.procs[proc.pid] = proc
        job["pid"] = proc.pid
        job["status"] = "running"
        job["retry_at"] = ""
        if not job["started"]:
            job["started"] = iso()  # first-ever start; never refreshed
        # Unconditional per-launch stamp: pid-reuse checks (tick/status after a
        # runner restart, when self.procs is empty) must bound against THIS
        # process, not the original started time (Blocker 1 / risk 3).
        job["launched_at"] = iso()
        self.save(job)
        log_event(event="launch", uid=self.fleet_uid(job), project=str(self.root),
                  project_name=self.root.name, engine=job["engine"], model=eff_model,
                  effort=job["effort"], status="running", pid=proc.pid,
                  log=str(self.log_file(job)), cwd=str(wt), branch=job["branch"],
                  title=job["id"],
                  **(_agy_quota_fields(self._agy_quota_cache[1])
                     if job["engine"] == "agy" and self._agy_quota_cache else {}))
        print(f"  ▶ {job['id']} [{job['engine']}] pid={proc.pid}"
              f"{' (resume)' if resume else ''}")

    def parse_reset(self, tail: str) -> str:
        m = RESET_AT_RE.search(tail)
        if not m:
            return iso(now() + timedelta(minutes=20))
        hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        t = now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now():
            t += timedelta(days=1)
        return iso(t + timedelta(minutes=2))

    def finalize(self, job: dict) -> None:
        proc = self.procs.pop(job["pid"] or -1, None)
        # `tick` calls finalize only after liveness says the child is gone; when
        # we still own the Popen handle, poll() also gives the real exit code
        # for unwrapped fleet engines.
        exit_code = proc.poll() if proc is not None else None
        log_file = self.log_file(job)
        tail = _log_tail_text(log_file)
        uid = self.fleet_uid(job)
        engine = job["engine"]
        agy_outcome = _parse_agy_outcome(log_file) \
            if engine == "agy" else None
        if agy_outcome and agy_outcome.get("conversation_id"):
            job["session"] = str(agy_outcome["conversation_id"])
        # Same decision function as the reaper (`_classify_from_log`). The two
        # parameters are the whole of the difference between the writers:
        #   expect_exit_marker — Fleet.launch wraps only FLEET_WRAPPED_ENGINES,
        #     so for any other engine a line-start `===== exit:N` is text the
        #     agent printed and must not be read as the process's status.
        #   attempts — only the fleet can relaunch, so only the fleet has the
        #     retry/give-up arm.
        verdict = classify_tail(
            tail, engine, agy_outcome=agy_outcome,
            expect_exit_marker=engine in FLEET_WRAPPED_ENGINES,
            attempts=job["attempts"])
        fields = _agy_outcome_fields(agy_outcome) if agy_outcome else {}
        capture_interrupt = None
        try:
            fields.update(capture_outcome(
                log_file, self.wt_dir / job["id"],
                exit_code=verdict.rc if verdict.rc is not None else exit_code))
        except BaseException:
            # Match the one-shot path: a Ctrl-C during optional telemetry must
            # not leave this fleet job's terminal ledger state as "running".
            # Delay (never swallow) the interrupt until the terminal event is
            # written below.
            capture_interrupt = sys.exc_info()

        def propagate_capture_interrupt() -> None:
            if capture_interrupt is not None:
                _kind, exc, tb = capture_interrupt
                assert exc is not None
                raise exc.with_traceback(tb)

        job["pid"] = None

        if verdict.event == "done":
            # Fleet vocabulary has no "exited"; map on the ledger event.
            job["status"] = "done"
            self.save(job)
            log_event(event="done", uid=uid, status="done", **fields)
            propagate_capture_interrupt()
            print(f"  ✔ {job['id']} done")
            return
        if verdict.reason == "limit_park":
            job["status"] = "limited"
            quota = read_agy_quota() if engine == "agy" else {}
            if engine == "agy":
                self._agy_quota_cache = (time.monotonic(), quota)
            job["retry_at"] = (_agy_retry_at(quota)
                               if quota.get("available") and quota.get("exhausted")
                               else self.parse_reset(tail))
            self.save(job)
            log_event(event="limited", uid=uid, status="limited",
                      retry_at=job["retry_at"], **fields)
            propagate_capture_interrupt()
            print(f"  ⏸ {job['id']} limited — retry {job['retry_at']}")
            return
        if verdict.reason == "retry":
            # Must write a ledger event: without it the machine-wide log keeps
            # the launch row's status="running" forever. Fleet status alone is
            # not enough — other
            # readers only see JOBS_LOG.
            #
            # status=limited still means "will retry if a runner is alive" —
            # same vocabulary as quota-park. "Gave up" is status=failed below.
            # Carry attempts so a reader can tell attempt N of 3 from a
            # permanent abandoned row (retry_at in the past, no later launch).
            # agy is unreachable here (it expects a marker, so it can only reach
            # wrapper_died); do not splat fields (would TypeError on summary=).
            job["attempts"] += 1
            job["status"] = "limited"
            job["retry_at"] = iso(now() + timedelta(minutes=2))
            log_event(event="limited", uid=uid, status="limited",
                      retry_at=job["retry_at"],
                      attempts=job["attempts"],
                      summary=f"exited without DONE "
                              f"(attempt {job['attempts']}/{FLEET_MAX_ATTEMPTS})")
            propagate_capture_interrupt()
            print(f"  ↻ {job['id']} exited without DONE — retry {job['retry_at']}")
            self.save(job)
            return
        job["status"] = "failed"
        if verdict.reason == "gave_up":
            # attempts on the event makes the attempt count machine-readable.
            job["attempts"] += 1
            self.save(job)
            log_event(event="failed", uid=uid, status="failed",
                      attempts=job["attempts"],
                      summary="exited without DONE (gave up)", **fields)
            propagate_capture_interrupt()
        else:
            self.save(job)
            log_event(event="failed", uid=uid, status="failed", **fields)
            propagate_capture_interrupt()
        print(f"  ✖ {job['id']} failed")

    def capture_codex_session(self, job: dict) -> None:
        if job["engine"] != "codex" or job["session"]:
            return
        m = UUID_RE.search(_log_tail_text(self.log_file(job), 8000))
        if m:
            job["session"] = m.group(0)
            self.save(job)

    def eligible(self, job: dict, by_id: dict) -> bool:
        if job["status"] == "queued":
            return all(by_id.get(a, {}).get("status") == "done" for a in job["after"])
        if job["status"] == "limited":
            if not job["retry_at"]:
                return True
            try:
                return now() >= datetime.fromisoformat(job["retry_at"])
            except ValueError:
                return True
        return False

    def tick(self) -> tuple[int, int]:
        jobs = self.load()
        by_id = {j["id"]: j for j in jobs}
        for j in jobs:
            if j["status"] == "running":
                self.capture_codex_session(j)
                # Per-launch bound, not first-start `started` — attempt 2+ pids
                # start long after `started` and would look recycled (Blocker 1).
                if not self.pid_alive(j["pid"],
                                      launched_at=j.get("launched_at") or None):
                    self.finalize(j)
        jobs = self.load()
        by_id = {j["id"]: j for j in jobs}
        caps = dict(self.cfg["caps"])
        for j in jobs:
            if j["status"] == "running":
                caps[j["engine"]] = caps.get(j["engine"], 0) - 1
        for j in jobs:
            if caps.get(j["engine"], 0) <= 0 or not self.eligible(j, by_id):
                continue
            defer = self.usage_defer(j)
            if defer:
                j["status"] = "limited"
                j["retry_at"] = self._usage_retry_at.pop(
                    j["id"], iso(now() + timedelta(minutes=15)))
                self.save(j)
                print(f"  ⏳ {j['id']} held ({defer}) — recheck {j['retry_at']}")
                continue
            try:
                self.launch(j, resume=(j["status"] == "limited" and bool(j["session"])))
                caps[j["engine"]] -= 1
            except Exception as e:  # noqa: BLE001
                j["attempts"] += 1
                j["status"] = "failed" if j["attempts"] >= 3 else "queued"
                self.save(j)
                print(f"  ✖ launch error {j['id']}: {e}", file=sys.stderr)
        jobs = self.load()
        active = sum(1 for j in jobs if j["status"] == "running")
        pending = sum(1 for j in jobs if j["status"] in ("queued", "limited"))
        return active, pending

    def status(self) -> None:
        jobs = self.load()
        if not jobs:
            print(f"no jobs in {self.jobs_dir}")
            return
        print(f"{'ID':<24} {'ENGINE':<7} {'MODEL':<24} {'STATUS':<9} {'PID':<7} "
              f"{'RETRY_AT':<20} AFTER")
        print("-" * 108)
        for j in jobs:
            pid_dead = not self.pid_alive(
                j["pid"], launched_at=j.get("launched_at") or None)
            dead = ("†" if display_is_zombie(
                        j["status"],
                        pid_dead=pid_dead,
                        retry_at=j.get("retry_at") or None)
                    else "")
            print(f"{j['id']:<24} {j['engine']:<7} {(j['model'] or '-'):<24} "
                  f"{j['status'] + dead:<9} {str(j['pid'] or '-'):<7} "
                  f"{(j['retry_at'] or '-'):<20} {', '.join(j['after']) or '-'}")


def cmd_fleet(args) -> int:
    project_root = find_project_root(Path.cwd())
    load_credentials(project_root)
    fleet = Fleet(project_root, load_config(project_root))
    if args.action == "status":
        fleet.status()
        return 0
    if args.action == "run":
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True)
        print(f"fleet: project={project_root.name} caps={fleet.cfg['caps']} "
              f"poll=60s (Ctrl-C detaches; jobs keep running)")
        while True:
            active, pending = fleet.tick()
            if active == 0 and pending == 0:
                print("all jobs settled. exiting.")
                fleet.status()
                return 0
            time.sleep(60)
    print("runjob fleet {run|status}", file=sys.stderr)
    return 2


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    description = (__doc__ or "runjob — launch headless LLM jobs").splitlines()[0]
    p = argparse.ArgumentParser(prog="runjob", description=description)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="launch a single headless job")
    r.add_argument("--engine", "-e", required=True,
                   help="claude | codex | grok | glm | agy | local")
    r.add_argument("--model", "-m", default="")
    r.add_argument("--effort", default="", help="e.g. low|medium|high|xhigh (engine-dependent)")
    r.add_argument("prompt", nargs="?", help="inline prompt (or use -f / stdin)")
    r.add_argument("--file", "-f", help="read the prompt from a file")
    r.add_argument("--cwd", help="working dir for the job (default: current). NOTE: `run` "
                   "works in-place here with NO worktree/branch isolation — if the job commits, "
                   "it commits to this dir's branch (usually main). Prefer `fleet run`, or point "
                   "this at a throwaway worktree, when the agent will commit.")
    r.add_argument("--title", help="short label for the dashboard")
    r.add_argument("--wait", action="store_true", help="block until done and tail output")
    r.add_argument("--grok-writable", action="append", metavar="PATH",
                   help="grok only: grant the sandbox write access to PATH (repeatable). "
                   "Needed when a grok job must reach outside its workspace — e.g. "
                   "merging into another repo (that repo's .git). Ignored for other engines. "
                   "NOT sufficient for a job that DISPATCHES other jobs — see --grok-sandbox.")
    r.add_argument("--grok-sandbox", metavar="PROFILE",
                   help="grok only: override the sandbox profile (default: workspace, or "
                   "runjob when a worktree/--grok-writable grant is in play). Pass 'off' "
                   "for an orchestrator job that must dispatch child engines: macOS forbids "
                   "nesting one seatbelt inside another, so codex/grok children die under ANY "
                   "parent profile. 'off' matches the "
                   "posture of claude jobs, which run with bypassPermissions and no sandbox.")
    r.set_defaults(fn=cmd_run)

    f = sub.add_parser("fleet", help="file-based job queue in this project")
    f.add_argument("action", choices=["run", "status"])
    f.set_defaults(fn=cmd_fleet)

    lg = sub.add_parser("log", help="show the machine-wide job log")
    lg.add_argument("--uid", help="one job: its row plus a tail of its own log at "
                                  "~/.config/dev-jobs/logs/<uid>.log")
    lg.add_argument("--active", action="store_true",
                    help="only running/queued/limited that are not abandoned "
                         "(daggered corpses hidden; see --include-abandoned)")
    lg.add_argument("--include-abandoned", action="store_true",
                    help="with --active: also show daggered running†/limited† rows")
    lg.add_argument("--follow", action="store_true",
                    help="stream new events, or that job's log when --uid is given")
    lg.add_argument("--outcome", action="store_true",
                    help="also show captured exit, STATUS line, dirty flag, branch, and commit")
    lg.add_argument("--limit", type=int, default=40,
                    help="table rows, or tail lines when --uid is given (default 40)")
    lg.set_defaults(fn=cmd_log)

    rep = sub.add_parser(
        "report",
        help="bounded log tail / EOF trailer for one job (reap-protocol)")
    rep.add_argument("uid", help="job uid from the shared ledger")
    rep.add_argument("--bytes", type=int, default=DEFAULT_REPORT_BYTES,
                     help=f"tail window in bytes (default {DEFAULT_REPORT_BYTES})")
    rep.add_argument("--json", action="store_true",
                     help="structured parse: trailer_state="
                          "present|absent|malformed|window_clipped|missing_log; "
                          "exit 0/0/3/4/1 respectively")
    rep.set_defaults(fn=cmd_report)

    rr = sub.add_parser(
        "repair",
        help="opt-in: write terminal events for dead jobs still marked running")
    rr.add_argument("--uid", help="only repair this uid")
    rr.add_argument("--all", action="store_true",
                    help="repair every dead-but-running row machine-wide "
                         "(required when neither --uid nor --dry-run is set)")
    rr.add_argument("--dry-run", action="store_true",
                    help="print what would be repaired; do not write")
    rr.set_defaults(fn=cmd_repair)

    d = sub.add_parser("dash", help="serve the web dashboard over the shared log")
    d.add_argument("--port", type=int, default=8787)
    d.set_defaults(fn=cmd_dash)

    rp = sub.add_parser("_reap", help=argparse.SUPPRESS)
    rp.add_argument("uid")
    rp.add_argument("pid")
    rp.add_argument("log")
    rp.add_argument("engine")
    rp.add_argument("cwd", nargs="?", help=argparse.SUPPRESS)
    rp.set_defaults(fn=cmd_reap)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
