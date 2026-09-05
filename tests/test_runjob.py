import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import runjob


class OutcomeCaptureTests(unittest.TestCase):
    def test_exit_zero_status_line_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "job.log"
            log.write_text("work complete\nSTATUS: COMPLETE\n===== exit:0\n",
                           encoding="utf-8")
            expected_bytes = log.stat().st_size
            status, event, fields = runjob._classify_from_log(log, "claude")

        self.assertEqual((status, event), ("exited", "done"))
        self.assertEqual(fields["exit_code"], 0)
        self.assertEqual(fields["status_line"], "STATUS: COMPLETE")
        self.assertEqual(fields["log_bytes"], expected_bytes)

    def test_nonzero_exit_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "job.log"
            log.write_text("fatal failure\n===== exit:23\n", encoding="utf-8")
            status, event, fields = runjob._classify_from_log(log, "claude")

        self.assertEqual((status, event), ("failed", "failed"))
        self.assertEqual(fields["exit_code"], 23)
        self.assertIsNone(fields["status_line"])

    def test_status_line_discards_unfilled_prompt_template_but_keeps_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            echoed = root / "echoed.log"
            echoed.write_text(
                "Your prompt must end with:\nSTATUS: CLEAR\nBLOCKING: <n>\n\n"
                "...work...\nTraceback: boom\n===== exit:23\n",
                encoding="utf-8")
            self.assertIsNone(runjob.capture_outcome(
                echoed, None, exit_code=23)["status_line"])

            later = root / "later.log"
            later.write_text(
                "Your prompt must end with:\nSTATUS: CLEAR\nBLOCKING: <n>\n\n"
                "...work...\nSTATUS: STILL-BLOCKING\n===== exit:23\n",
                encoding="utf-8")
            self.assertEqual(runjob.capture_outcome(
                later, None, exit_code=23)["status_line"],
                "STATUS: STILL-BLOCKING")

            teardown = root / "teardown.log"
            teardown.write_text("finished\nSTATUS: COMPLETE\n===== exit:23\n",
                                encoding="utf-8")
            self.assertEqual(runjob.capture_outcome(
                teardown, None, exit_code=23)["status_line"], "STATUS: COMPLETE")

            echoed_zero = root / "echoed-zero.log"
            echoed_zero.write_text(
                "Your prompt must end with:\nSTATUS: CLEAR\nBLOCKING: <n>\n\n"
                "...work...\n===== exit:0\n", encoding="utf-8")
            self.assertIsNone(runjob.capture_outcome(
                echoed_zero, None, exit_code=0)["status_line"])

    def test_interrupt_during_capture_still_writes_terminal_event(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "job.log"
            log.write_text("finished\n===== exit:23\n", encoding="utf-8")
            with mock.patch.object(runjob, "capture_outcome",
                                   side_effect=KeyboardInterrupt), \
                 mock.patch.object(runjob, "log_event") as record:
                with self.assertRaises(KeyboardInterrupt):
                    runjob._classify_and_log("probe-uid", log, "grok")

        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["uid"], "probe-uid")
        self.assertEqual(record.call_args.kwargs["event"], "failed")
        self.assertEqual(record.call_args.kwargs["status"], "failed")

    def test_non_git_cwd_records_explicit_null_git_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "job.log"
            log.write_text("STATUS: CLEAR\n===== exit:0\n", encoding="utf-8")
            _status, _event, fields = runjob._classify_from_log(
                log, "claude", cwd=root)

        self.assertIsNone(fields["commit_sha"])
        self.assertIsNone(fields["branch"])
        self.assertIsNone(fields["dirty"])

    def test_dirty_git_worktree_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "worktree"
            root.mkdir()

            def git(*args):
                result = subprocess.run(["git", *args], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

            git("init")
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("-c", "user.name=Outcome Test", "-c",
                "user.email=outcome@example.test", "commit", "-m", "initial")
            (root / "tracked.txt").write_text("after\n", encoding="utf-8")
            log = Path(td) / "job.log"
            log.write_text("STATUS: FIXED\n===== exit:0\n", encoding="utf-8")
            _status, _event, fields = runjob._classify_from_log(
                log, "claude", cwd=root)

        self.assertTrue(fields["dirty"])
        self.assertRegex(fields["commit_sha"] or "", r"^[0-9a-f]{40}$")
        self.assertTrue(fields["branch"])

    def test_old_format_event_still_folds_without_outcome_fields(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "jobs.jsonl"
            ledger.write_text(json.dumps({
                "event": "done", "uid": "old-job", "status": "exited",
                "ts": "2026-08-06T12:00:00+10:00",
            }) + "\n", encoding="utf-8")
            with mock.patch.object(runjob, "JOBS_LOG", ledger):
                states = runjob.read_states()

        self.assertEqual(states[0]["uid"], "old-job")
        self.assertEqual(states[0]["status"], "exited")
        self.assertNotIn("exit_code", states[0])

    def test_log_outcome_view_surfaces_status_line_and_dirty(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "jobs.jsonl"
            ledger.write_text(json.dumps({
                "event": "done", "uid": "outcome-job", "status": "done",
                "status_line": "STATUS: CLEAR", "dirty": True,
                "exit_code": 0, "ts": "2026-08-06T12:00:00+10:00",
            }) + "\n", encoding="utf-8")
            args = argparse.Namespace(uid=None, active=False, follow=False,
                                      limit=40, outcome=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_DEAD), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_log(args)

        self.assertIn("STATUS: CLEAR", out.getvalue())
        self.assertIn("true", out.getvalue())


class AgyCommandTests(unittest.TestCase):
    def test_builds_json_argv_command_with_required_safety_and_timeout(self):
        cmd, use_stdin, extra_env = runjob.build_command(
            "agy", "gemini-3.6-flash-high", "high", "reply PONG")

        self.assertFalse(use_stdin)
        self.assertEqual(extra_env, {})
        self.assertEqual(cmd[-2:], ["-p", "reply PONG"])
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "45m")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_resume_uses_conversation_id(self):
        cmd, _, _ = runjob.build_command(
            "agy", "gemini-3.6-flash-low", "", "continue",
            session="conversation-123", resume=True)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "conversation-123")

    def test_defaults_to_approved_gemini(self):
        # Tracks AGY_DEFAULT_MODEL, which 0ed02da bumped 3.6 -> 3.7 on 2026-08-13
        # after a live CLI probe; this assertion was left behind and the suite has
        # been red ever since. Assert against the constant so the next bump cannot
        # silently re-red it, and pin the roster membership separately.
        self.assertEqual(runjob.effective_model("agy", ""), runjob.AGY_DEFAULT_MODEL)
        # 2026-09-03: bumped 3.7 -> 3.8 after `agy models` showed the 3.8 rows live.
        self.assertEqual(runjob.AGY_DEFAULT_MODEL, "gemini-3.8-flash-high")
        self.assertIn(runjob.AGY_DEFAULT_MODEL, runjob.AGY_MODELS)

    def test_roster_tracks_agy_models_listing(self):
        # `agy models` on 2026-09-03: 3.8/3.7/3.6 flash triples + 3.1 pro pair.
        # 3.5 Flash is gone from the listing, so it is off the whitelist too.
        self.assertEqual(runjob.AGY_MODELS, {
            "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
            "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
            "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
            "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        })

    def test_retired_gemini_3_5_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not an approved runnable Gemini"):
            runjob.build_command("agy", "gemini-3.5-flash-high", "", "do nothing")

    def test_rejects_non_gemini_rows(self):
        with self.assertRaisesRegex(ValueError, "accepts only Gemini rows"):
            runjob.build_command("agy", "claude-sonnet-4-6", "", "do nothing")

    def test_rejects_autocomplete_only_gemini(self):
        with self.assertRaisesRegex(ValueError, "autocomplete-only"):
            runjob.build_command("agy", "gemini-2.5-pro", "", "do nothing")

    def test_rejects_unapproved_future_or_mistyped_gemini(self):
        with self.assertRaisesRegex(ValueError, "not an approved runnable Gemini"):
            runjob.build_command("agy", "gemini-made-up", "", "do nothing")

    def test_wrong_case_gemini_is_reported_as_unapproved_not_non_gemini(self):
        with self.assertRaisesRegex(ValueError, "not an approved runnable Gemini"):
            runjob.build_command("agy", "GEMINI-3.6-flash-high", "", "do nothing")

    def test_rejects_unsupported_effort(self):
        with self.assertRaisesRegex(ValueError, "pick: low, medium, high"):
            runjob.build_command("agy", "gemini-3.6-flash-high", "xhigh", "do nothing")

    def test_large_prompt_fails_before_exec_with_actionable_error(self):
        cmd, _, _ = runjob.build_command(
            "agy", "gemini-3.6-flash-low", "", "x" * 10_000)
        with self.assertRaisesRegex(ValueError, "stdin and prompt files do not work"):
            runjob.ensure_agy_argv_fits(cmd, {}, arg_max=40_000)


class LocalCommandTests(unittest.TestCase):
    def test_uses_only_operator_supplied_endpoint_and_key(self) -> None:
        with mock.patch.dict(os.environ, {
                "LOCAL_BASE_URL": "http://127.0.0.1:9999",
                "LOCAL_API_KEY": "test-key-not-real",
        }, clear=False):
            _cmd, use_stdin, env = runjob.build_command(
                "local", "test-model", "", "reply PONG", session="local-1")

        self.assertTrue(use_stdin)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9999")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-key-not-real")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    def test_has_no_built_in_endpoint_or_model(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cmd, _use_stdin, env = runjob.build_command(
                "local", "", "", "reply PONG", session="local-2")

        self.assertEqual(env["ANTHROPIC_BASE_URL"], "")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertNotIn("--model", cmd)


class AgyQuotaTests(unittest.TestCase):
    def snapshot(self, runnable, autocomplete=None):
        models = [runnable]
        if autocomplete:
            models.append(autocomplete)
        return json.dumps({"method": "google", "models": models})

    def test_normalizes_fraction_and_ignores_autocomplete_exhaustion(self):
        quota = runjob._parse_agy_quota(self.snapshot(
            {"modelId": "gemini-3.6-flash-high", "remainingPercentage": 0.77,
             "isExhausted": False, "resetTime": "2026-08-09T00:00:00Z"},
            {"modelId": "gemini-2.5-pro", "remainingPercentage": 0,
             "isExhausted": True, "isAutocompleteOnly": True}))
        self.assertTrue(quota["available"])
        self.assertFalse(quota["exhausted"])
        self.assertEqual(quota["remaining_percentage"], 77)

    def test_zero_remaining_exhausts_shared_pool(self):
        quota = runjob._parse_agy_quota(self.snapshot(
            {"modelId": "gemini-3.1-pro-high", "remainingPercentage": 0,
             "isExhausted": False, "timeUntilResetMs": 60_000}))
        self.assertTrue(quota["exhausted"])

    def test_coerces_string_quota_values_before_declaring_pool_available(self):
        exhausted = runjob._parse_agy_quota(self.snapshot(
            {"modelId": "gemini-3.1-pro-high", "remainingPercentage": "0",
             "isExhausted": "false"}))
        self.assertTrue(exhausted["available"])
        self.assertTrue(exhausted["exhausted"])

        explicitly_exhausted = runjob._parse_agy_quota(self.snapshot(
            {"modelId": "gemini-3.1-pro-high", "remainingPercentage": "25",
             "isExhausted": "true"}))
        self.assertTrue(explicitly_exhausted["exhausted"])
        self.assertEqual(explicitly_exhausted["remaining_percentage"], 25)

    def test_integer_one_is_one_percent_not_a_fractional_hundred_percent(self):
        quota = runjob._parse_agy_quota(self.snapshot(
            {"modelId": "gemini-3.1-pro-high", "remainingPercentage": 1,
             "isExhausted": False}))
        self.assertEqual(quota["remaining_percentage"], 1)

    def test_invalid_reader_output_fails_open(self):
        quota = runjob._parse_agy_quota("not json")
        self.assertFalse(quota["available"])

    def test_quota_binary_environment_is_read_at_call_time(self):
        fixture = Path(__file__).parent / "fixtures" / "antigravity-usage"
        payload = self.snapshot(
            {"modelId": "gemini-3.1-pro-high", "remainingPercentage": 50,
             "isExhausted": False})
        result = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        with mock.patch.dict(os.environ, {"AGY_QUOTA_BINARY": str(fixture)}):
            with mock.patch.object(runjob.subprocess, "run", return_value=result) as call:
                quota = runjob.read_agy_quota()
        self.assertTrue(quota["available"])
        self.assertEqual(call.call_args.args[0][0], str(fixture))

    def test_fleet_free_runs_at_low_percentage_but_holds_exhaustion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {"engine": "agy", "id": "quota-test"}
            low = {"available": True, "exhausted": False,
                   "remaining_percentage": 1, "reset_time": ""}
            with mock.patch.object(runjob, "read_agy_quota", return_value=low):
                self.assertIsNone(fleet.usage_defer(job))
            fleet._agy_quota_cache = None
            empty = {"available": True, "exhausted": True,
                     "remaining_percentage": 0, "reset_time": ""}
            with mock.patch.object(runjob, "read_agy_quota", return_value=empty):
                self.assertIn("exhausted", fleet.usage_defer(job))
            self.assertIn("quota-test", fleet._usage_retry_at)


class AgyOutcomeTests(unittest.TestCase):
    def test_error_json_with_done_trailer_and_exit0_is_done(self):
        """cnd-ops-004: provider ERROR after a completed DONE trailer is done.

        Provider-shaped regression: agy
        returned status ERROR (ContentOffset / artifact-path tooling faults),
        exit marker 0, and a response that already trailed DONE: with the
        deliverable committed. Pre-fix classify_tail required SUCCESS and
        filed these as failed.
        """
        outcome = {
            "conversation_id": "f7e2e687-8754-4cb5-af15-4b375c8eefa2",
            "status": "ERROR",
            "response": (
                "The deliverable is committed.\n"
                "DONE: Completed adversarial cross-check FC-1 and committed "
                "the deliverable report.\n"
            ),
            "error": "ContentOffset 40000 exceeds line range size 35470",
            "duration_seconds": 291.3,
            "num_turns": 1,
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "thinking_tokens": 5, "cache_read_tokens": 10,
                      "total_tokens": 135},
        }
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "agy.log"
            log.write_text(
                "===== 2026-08-24T13:07:08+10:00 launch agy gemini-3.7-flash-high high\n"
                + json.dumps(outcome) + "\n\n===== exit:0\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                status = runjob._classify_and_log("sample-agy-f4279536", log, "agy")

        self.assertEqual(status, "done")
        fields = record.call_args.kwargs
        self.assertEqual(fields["event"], "done")
        self.assertEqual(fields["outcome_status"], "ERROR")
        self.assertTrue(fields["summary"].startswith("DONE:"))
        self.assertEqual(fields["exit_code"], 0)

    def test_fleet_error_json_with_done_trailer_is_done(self):
        """Fleet path must match the one-shot cnd-ops-004 classification."""
        outcome = {
            "conversation_id": "bdaca136-b31a-4d90-9934-d6214e7e6672",
            "status": "ERROR",
            "response": "DONE: Completed FC-2 and committed FC-2-check.md.\n",
            "error": (
                "declaring permissions: cortex tool write_to_file: "
                "invalid tool call error (invalid_args)"
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {
                "path": jobs_dir / "error-done.md", "id": "error-done",
                "engine": "agy", "model": "gemini-3.6-flash-low", "effort": "low",
                "sanitize": "", "branch": "job/error-done", "after": [],
                "status": "running", "session": "", "pid": 12345, "attempts": 0,
                "retry_at": "", "started": "2026-08-24T00:00:00+10:00",
                "updated": "", "body": "fixture",
            }
            fleet.log_file(job).write_text(
                json.dumps(outcome) + "\n===== exit:0\n", encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)

        self.assertEqual(job["status"], "done")
        self.assertEqual(record.call_args.kwargs["outcome_status"], "ERROR")
        self.assertTrue(record.call_args.kwargs["summary"].startswith("DONE:"))

    def test_fleet_fails_non_success_json_without_done_or_retry(self):
        """Non-SUCCESS with no DONE trailer still fails immediately (no retry)."""
        outcome = {"status": "FAILED", "response": "provider rejected work"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {
                "path": jobs_dir / "provider-failed.md", "id": "provider-failed",
                "engine": "agy", "model": "gemini-3.6-flash-low", "effort": "low",
                "sanitize": "", "branch": "job/provider-failed", "after": [],
                "status": "running", "session": "", "pid": 12345, "attempts": 0,
                "retry_at": "", "started": "2026-08-03T00:00:00+10:00",
                "updated": "", "body": "fixture",
            }
            fleet.log_file(job).write_text(json.dumps(outcome) + "\n===== exit:0\n",
                                           encoding="utf-8")
            with mock.patch.object(runjob, "log_event"):
                fleet.finalize(job)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 0)

    def test_failed_json_without_done_in_response_stays_failed_even_if_log_has_done(self):
        """A bare log DONE: line does not override provider FAILED with empty work.

        The DONE trailer that counts for agy lives inside the JSON response
        field (production --output-format json). A standalone DONE: outside
        that object is not provider completion evidence.
        """
        outcome = {
            "conversation_id": "failed-conversation",
            "status": "FAILED",
            "response": "provider rejected work before any deliverable",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {
                "path": jobs_dir / "failed-fixture.md", "id": "failed-fixture",
                "engine": "agy", "model": "gemini-3.6-flash-low", "effort": "low",
                "sanitize": "", "branch": "job/failed-fixture", "after": [],
                "status": "running", "session": "", "pid": 12345, "attempts": 0,
                "retry_at": "", "started": "2026-08-03T00:00:00+10:00",
                "updated": "", "body": "fixture",
            }
            log = fleet.log_file(job)
            log.write_text(json.dumps(outcome) + "\nDONE: spoofed completion\n===== exit:0\n",
                           encoding="utf-8")
            with mock.patch.object(runjob, "log_event"):
                fleet.finalize(job)

        self.assertEqual(job["status"], "failed")

    def test_fleet_fails_success_json_when_wrapper_exit_is_nonzero(self):
        outcome = {"status": "SUCCESS", "response": "provider claimed success"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {
                "path": jobs_dir / "exit-failed.md", "id": "exit-failed",
                "engine": "agy", "model": "gemini-3.6-flash-low", "effort": "low",
                "sanitize": "", "branch": "job/exit-failed", "after": [],
                "status": "running", "session": "", "pid": 12345, "attempts": 0,
                "retry_at": "", "started": "2026-08-03T00:00:00+10:00",
                "updated": "", "body": "fixture",
            }
            fleet.log_file(job).write_text(json.dumps(outcome) + "\n===== exit:1\n",
                                           encoding="utf-8")
            with mock.patch.object(runjob, "log_event"):
                fleet.finalize(job)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 0)

    def test_fleet_finalize_uses_success_json_and_records_conversation(self):
        outcome = {
            "conversation_id": "fleet-conversation",
            "status": "SUCCESS",
            "response": "DONE: fleet fixture complete",
            "duration_seconds": 2,
            "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "thinking_tokens": 1, "cache_read_tokens": 0,
                      "total_tokens": 16},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = {
                "path": jobs_dir / "fleet-fixture.md", "id": "fleet-fixture",
                "engine": "agy", "model": "gemini-3.6-flash-low", "effort": "low",
                "sanitize": "", "branch": "job/fleet-fixture", "after": [],
                "status": "running", "session": "", "pid": 12345, "attempts": 0,
                "retry_at": "", "started": "2026-08-03T00:00:00+10:00",
                "updated": "", "body": "fixture",
            }
            log = fleet.log_file(job)
            log.write_text(json.dumps(outcome) + "\n===== exit:0\n", encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)

        self.assertEqual(job["status"], "done")
        self.assertEqual(job["session"], "fleet-conversation")
        self.assertEqual(record.call_args.kwargs["summary"],
                         "DONE: fleet fixture complete")
        self.assertEqual(record.call_args.kwargs["usage"]["total_tokens"], 16)

    def test_runjob_subprocess_holds_exhausted_pool_without_launching(self):
        fixtures = Path(__file__).parent / "fixtures"
        script = Path(runjob.__file__)
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["DEV_JOBS_HOME"] = td
            env["AGY_QUOTA_BINARY"] = str(fixtures / "antigravity-usage")
            env["AGY_FIXTURE_EXHAUSTED"] = "1"
            env["PATH"] = str(fixtures) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([
                sys.executable, str(script), "run", "-e", "agy", "-m",
                "gemini-3.6-flash-low", "held fixture",
            ], capture_output=True, text=True, env=env, cwd=script.parent)
            events = [json.loads(line) for line in
                      (Path(td) / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result.returncode, 75)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "limited")
        self.assertEqual(events[0]["pool_check"], "exhausted")
        self.assertIn("nothing launched", result.stderr)

    def test_runjob_subprocess_records_done_summary_usage_and_pool(self):
        fixtures = Path(__file__).parent / "fixtures"
        script = Path(runjob.__file__)
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["DEV_JOBS_HOME"] = td
            env["AGY_QUOTA_BINARY"] = str(fixtures / "antigravity-usage")
            env["PATH"] = str(fixtures) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([
                sys.executable, str(script), "run", "-e", "agy", "-m",
                "gemini-3.6-flash-high", "--effort", "high", "--wait",
                "fixture smoke",
            ], capture_output=True, text=True, env=env, cwd=script.parent)
            events = [json.loads(line) for line in
                      (Path(td) / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual(events[-1]["summary"], "fixture completed: fixture smoke")
        self.assertEqual(events[-1]["usage"]["total_tokens"], 135)
        self.assertEqual(events[0]["pool_remaining_percentage"], 42)

    def test_success_json_becomes_done_with_summary_and_full_usage(self):
        outcome = {
            "conversation_id": "conv-1",
            "status": "SUCCESS",
            "response": "Implemented the requested change.",
            "duration_seconds": 8.5,
            "num_turns": 2,
            "usage": {
                "input_tokens": 18_000,
                "output_tokens": 120,
                "thinking_tokens": 40,
                "cache_read_tokens": 500,
                "total_tokens": 18_660,
            },
        }
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "agy.log"
            log.write_text("launch\n" + json.dumps(outcome) + "\n===== exit:0\n",
                           encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                status = runjob._classify_and_log("uid", log, "agy")

        self.assertEqual(status, "done")
        fields = record.call_args.kwargs
        self.assertEqual(fields["summary"], "Implemented the requested change.")
        self.assertEqual(fields["outcome_status"], "SUCCESS")
        self.assertEqual(fields["usage"], outcome["usage"])
        self.assertEqual(fields["duration_seconds"], 8.5)
        self.assertEqual(fields["num_turns"], 2)

    def test_exit_zero_without_agy_json_is_failed_not_exited(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "agy.log"
            log.write_text("plain text only\n===== exit:0\n", encoding="utf-8")
            with mock.patch.object(runjob, "log_event"):
                status = runjob._classify_and_log("uid", log, "agy")
        self.assertEqual(status, "failed")


def _fleet_job(jobs_dir: Path, job_id: str = "retry-fixture", **overrides) -> dict:
    job = {
        "path": jobs_dir / f"{job_id}.md", "id": job_id,
        "engine": "claude", "model": "opus", "effort": "high",
        "sanitize": "", "branch": f"job/{job_id}", "after": [],
        "status": "running", "session": "", "pid": 2668, "attempts": 0,
        "retry_at": "", "started": "2026-08-03T08:03:14+10:00",
        "launched_at": "2026-08-03T08:03:14+10:00",
        "updated": "", "body": "fixture body",
    }
    job.update(overrides)
    return job


class LedgerTerminalEventTests(unittest.TestCase):
    """Regression: dead jobs must not stay 'running' in the ledger or viewer.

    Root cause (2026-08-03): Fleet.finalize() retry/limited path printed
    'exited without DONE' and set fleet status to limited but wrote no ledger
    event, so runjob log kept showing ▶ running for hours after the pid died.
    """

    def test_finalize_retry_path_writes_limited_ledger_event(self):
        """The 1315–1327 path must append a terminal event (not leave 'running')."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir)
            # No DONE trailer, no rate-limit text — falls into attempts/retry branch.
            fleet.log_file(job).write_text(
                "Execution error: something went wrong\n===== exit:1\n",
                encoding="utf-8")
            recorded = []

            def capture(**fields):
                recorded.append(fields)

            with mock.patch.object(runjob, "log_event", side_effect=capture):
                fleet.finalize(job)

        self.assertEqual(job["status"], "limited")
        self.assertEqual(job["attempts"], 1)
        self.assertTrue(job["retry_at"])
        self.assertEqual(len(recorded), 1, "retry path must write a ledger event")
        self.assertEqual(recorded[0]["event"], "limited")
        self.assertEqual(recorded[0]["status"], "limited")
        self.assertEqual(recorded[0]["uid"], fleet.fleet_uid(job))
        self.assertIn("retry_at", recorded[0])

    def test_finalize_retry_path_third_failure_writes_failed_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, attempts=2)
            fleet.log_file(job).write_text("boom\n===== exit:1\n", encoding="utf-8")
            recorded = []
            with mock.patch.object(runjob, "log_event",
                                   side_effect=lambda **f: recorded.append(f)):
                fleet.finalize(job)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(recorded[0]["event"], "failed")
        self.assertEqual(recorded[0]["status"], "failed")

    def test_dead_running_ledger_row_viewer_marks_dagger(self):
        """cmd_log must show † when status=running but the pid is gone.

        This is the belt-and-braces half: even if no terminal event was written,
        the shared viewer must not claim the job is live.
        """
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch",
                "uid": "sample-fleet-stale-job-a001",
                "project_name": "sample",
                "engine": "claude",
                "model": "opus",
                "status": "running",
                "pid": 2668,
                "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_DEAD), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("running†", text)
        # Must not look like a clean live running row (▶ alone with bare 'running ').
        self.assertNotRegex(text, r"▶\s+sample-fleet-stale-job-a001\b.*\brunning\s")

    def test_live_running_job_is_not_marked_dead(self):
        """False positive is worse than false negative: never dagger a live pid."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch",
                "uid": "live-job-uid",
                "project_name": "proj",
                "engine": "claude",
                "model": "opus",
                "status": "running",
                "pid": os.getpid(),  # this process is alive
                "ts": runjob.iso(),
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_log(args)
            text = out.getvalue()
        self.assertIn("live-job-uid", text)
        self.assertNotIn("running†", text)
        self.assertIn("running", text)

    def test_pid_reuse_is_not_treated_as_alive(self):
        """A live unrelated process that recycled our pid must not keep us 'running'."""
        launched = "2026-08-03T08:03:14+10:00"
        # Process start clearly after the job's launch → recycled pid.
        later_start = datetime.fromisoformat("2026-08-03T12:00:00+10:00")
        with mock.patch.object(runjob.os, "kill", return_value=None), \
             mock.patch.object(runjob.subprocess, "run") as ps_run, \
             mock.patch.object(runjob, "process_start_time",
                               return_value=later_start):
            ps_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="S\n", stderr="")
            alive = runjob.process_is_alive(2668, launched_at=launched)
        self.assertFalse(alive)

    def test_pid_alive_with_matching_start_time_stays_alive(self):
        launched = "2026-08-03T08:03:14+10:00"
        start = datetime.fromisoformat("2026-08-03T08:03:13+10:00")
        with mock.patch.object(runjob.os, "kill", return_value=None), \
             mock.patch.object(runjob.subprocess, "run") as ps_run, \
             mock.patch.object(runjob, "process_start_time", return_value=start):
            ps_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="S\n", stderr="")
            alive = runjob.process_is_alive(2668, launched_at=launched)
        self.assertTrue(alive)

    def test_cmd_log_does_not_mutate_ledger(self):
        """Design choice: display-only. Read path must not append correcting events."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch", "uid": "phantom-uid", "status": "running",
                "pid": 99999, "engine": "claude", "model": "opus",
                "project_name": "x", "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            before = log_path.read_text(encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_DEAD), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                runjob.cmd_log(args)
            after = log_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_cmd_log_uid_prints_path_and_tail_of_that_jobs_log(self):
        """`log --uid X` must answer with the job's OUTPUT, not just its row.

        Motivating incident: an operator asked for a job's log
        this way, got a one-row table, and fell through to `find ~ -maxdepth 4`,
        which raised six macOS TCC prompts against the bare claude binary and
        stalled the job 95 seconds.
        """
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            job_log = home / "logs" / "sample-project-codex-c26ba9b1.log"
            job_log.parent.mkdir(parents=True)
            job_log.write_text(
                "\n".join(f"line {i}" for i in range(100)) + "\nDONE\n",
                encoding="utf-8")
            row = {
                "event": "exited", "uid": "sample-project-codex-c26ba9b1",
                "project_name": "sample-project", "engine": "codex",
                "model": "gpt-5.6-sol", "status": "exited",
                "log": str(job_log), "ts": "2026-08-04T19:15:22+10:00",
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid="sample-project-codex-c26ba9b1", active=False,
                follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(str(job_log), text,
                      "must name the file, so it is never hunted for")
        self.assertIn("DONE", text)
        self.assertIn("line 99", text)
        self.assertNotIn("line 59", text, "--limit must bound the tail to 40 lines")

    def test_cmd_log_unknown_uid_does_not_claim_the_ledger_is_empty(self):
        """A uid miss must say so — "no jobs logged yet" reads as an empty ledger."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            row = {"event": "launch", "uid": "real-uid", "status": "running",
                   "engine": "claude", "model": "opus", "project_name": "x",
                   "ts": "2026-08-04T19:15:22+10:00"}
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid="typo-uid", active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("typo-uid", text)
        self.assertNotIn("no jobs logged yet", text)

    def test_cmd_log_uid_survives_a_deleted_log_file(self):
        """A ledger row whose file is gone must report that, not traceback."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            gone = home / "logs" / "gone.log"
            row = {"event": "exited", "uid": "gone-uid", "status": "exited",
                   "engine": "claude", "model": "opus", "project_name": "x",
                   "log": str(gone), "ts": "2026-08-04T19:15:22+10:00"}
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid="gone-uid", active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(str(gone), text)
        self.assertIn("log file is gone", text)

    def test_repair_writes_terminal_event_for_dead_running(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch",
                "uid": "sample-fleet-stale-job-a001",
                "status": "running", "pid": 2668, "engine": "claude",
                "model": "opus", "project_name": "sample",
                "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            args = argparse.Namespace(uid=None, dry_run=False, all=True)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_is_alive", return_value=False), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_repair(args)
                states = runjob.read_states()
            lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[-1]["event"], "repaired")
        self.assertEqual(lines[-1]["status"], "failed")
        self.assertEqual(states[0]["status"], "failed")
        self.assertIn("repaired", out.getvalue())

    def test_repair_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch", "uid": "phantom-uid", "status": "running",
                "pid": 1, "engine": "claude", "model": "x", "project_name": "p",
                "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            before = log_path.read_text(encoding="utf-8")
            args = argparse.Namespace(uid=None, dry_run=True, all=False)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_is_alive", return_value=False), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_repair(args)
            self.assertEqual(log_path.read_text(encoding="utf-8"), before)
            self.assertIn("would repair", out.getvalue())

    def test_repair_skips_genuinely_running(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            launch = {
                "event": "launch", "uid": "live-uid", "status": "running",
                "pid": 42, "engine": "claude", "model": "x", "project_name": "p",
                "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            args = argparse.Namespace(uid=None, dry_run=False, all=True)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_is_alive", return_value=True), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_repair(args)
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("nothing to repair", out.getvalue())

    def test_process_is_alive_real_child_recent_launched_at(self):
        """Non-mocked: a live sleep child with launched_at a few seconds ago is alive.

        Also pins that launched_at persists through fleet save/load (the bound
        tick uses after a runner restart). On 36e8be8 launched_at is not in
        FLEET_META_ORDER, so this fails before the liveness assert.

        The reuse-rejection half (stale bound → False) needs process_start_time
        from `ps`; when the OS denies `ps` (start is None) the guard fails open
        toward alive, which is the designed safe direction — so that half is
        only asserted when start time is actually available.
        """
        child = subprocess.Popen(["sleep", "60"])
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs_dir = root / "jobs"
                jobs_dir.mkdir()
                fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
                launched = runjob.iso(runjob.now() - timedelta(seconds=3))
                hour_ago = runjob.iso(runjob.now() - timedelta(hours=1))
                job = _fleet_job(
                    jobs_dir, job_id="live-child",
                    pid=child.pid, status="running",
                    started=hour_ago,
                    launched_at=launched,
                )
                fleet.save(job)
                loaded = fleet.load()[0]
                self.assertTrue(
                    loaded.get("launched_at"),
                    "launched_at must persist in the job file for restarted runners")
                alive = runjob.process_is_alive(
                    loaded["pid"], launched_at=loaded["launched_at"])
                self.assertTrue(
                    alive,
                    "live child with recent launched_at must be process_is_alive True")
                # Document the Blocker 1 shape when start-time is readable.
                start = runjob.process_start_time(child.pid)
                if start is not None:
                    self.assertFalse(
                        runjob.process_is_alive(child.pid, launched_at=loaded["started"]),
                        "stale started bound must reject a process that started later")
        finally:
            child.kill()
            child.wait(timeout=5)

    def test_tick_does_not_finalize_relaunched_job_when_procs_empty(self):
        """Blocker 1: attempt 2+ with empty procs must not reap a live pid.

        started is first-launch (hours ago); the live pid is from launch #2.
        After a runner restart self.procs is empty, so liveness falls through
        to process_is_alive with the reuse bound. That bound must be
        launched_at (this launch), not started.

        Real child for kill(0); process_start_time stubbed only so the reuse
        half is exercised even when the host denies `ps` (sandbox). On 36e8be8
        tick passes `started` → False → finalize; post-fix passes launched_at.
        """
        child = subprocess.Popen(["sleep", "60"])
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs_dir = root / "jobs"
                jobs_dir.mkdir()
                fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
                hour_ago = runjob.iso(runjob.now() - timedelta(hours=1))
                just_now = runjob.iso(runjob.now() - timedelta(seconds=2))
                # OS would report start ≈ this launch, not the first attempt.
                recent_start = runjob.now() - timedelta(seconds=1)
                job = _fleet_job(
                    jobs_dir, job_id="relaunched",
                    pid=child.pid, attempts=1, status="running",
                    started=hour_ago, launched_at=just_now,
                )
                fleet.save(job)
                self.assertEqual(fleet.procs, {}, "fresh runner has empty procs")
                with mock.patch.object(runjob, "process_start_time",
                                       return_value=recent_start), \
                     mock.patch.object(fleet, "finalize") as fin, \
                     mock.patch.object(fleet, "launch") as launch, \
                     mock.patch.object(fleet, "usage_defer", return_value=None):
                    fleet.tick()
                fin.assert_not_called()
                launch.assert_not_called()
                reloaded = [j for j in fleet.load() if j["id"] == "relaunched"][0]
                self.assertEqual(reloaded["status"], "running")
                self.assertEqual(reloaded["pid"], child.pid)
        finally:
            child.kill()
            child.wait(timeout=5)

    def test_repair_classifies_done_from_log_not_hardcoded_failed(self):
        """Blocker 2: repair must not write failed over a log that says DONE + exit:0."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            job_log = home / "proj-j1.log"
            job_log.write_text(
                "working…\nDONE: migrated 12 files, all tests pass\n===== exit:0\n",
                encoding="utf-8")
            launch = {
                "event": "launch",
                "uid": "proj-j1",
                "status": "running",
                "pid": 999999,
                "engine": "claude",
                "model": "opus",
                "project_name": "proj",
                "log": str(job_log),
                "ts": "2026-08-03T08:03:14+10:00",
            }
            log_path.write_text(json.dumps(launch) + "\n", encoding="utf-8")
            args = argparse.Namespace(uid="proj-j1", dry_run=False, all=False)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_is_alive", return_value=False), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_repair(args)
                states = runjob.read_states()
            lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1]["event"], "repaired")
        self.assertEqual(
            lines[-1]["status"], "done",
            "repair must classify DONE+exit:0 as done, not hard-code failed")
        self.assertEqual(states[0]["status"], "done")
        self.assertIn("→ done", out.getvalue())
        self.assertNotIn("→ failed", out.getvalue())

    # -- F1 / F1b: abandoned limited rows must dagger (display only) ----------

    def test_abandoned_limited_past_retry_at_daggers(self):
        """(a) limited + past retry_at + dead pid → limited† (reviewer F1)."""
        past = "2026-08-03T09:07:00+10:00"
        self.assertTrue(
            runjob.display_is_zombie("limited", pid_dead=True, retry_at=past),
            "past retry_at + dead pid must be abandoned")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            row = {
                "event": "limited",
                "uid": "p-c",
                "project_name": "p",
                "engine": "claude",
                "model": "opus",
                "status": "limited",
                "pid": 999993,
                "retry_at": past,
                "attempts": 1,
                "summary": "exited without DONE (attempt 1/3)",
                "ts": "2026-08-03T09:05:00+10:00",
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_DEAD), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("limited†", text,
                      "past retry_at + dead pid must dagger abandoned limited")
        self.assertNotRegex(text, r"⏸\s+p-c\b")

    def test_abandoned_limited_no_retry_at_daggers(self):
        """(b) limited + no retry_at + dead/unknown pid → limited† (F1b).

        Real shape from tick 14 / the refute job itself: standalone runjob run
        classified limited by _classify_and_log writes no retry_at and often
        no pid. A past-retry_at-only predicate daggers nothing for this
        population.
        """
        self.assertTrue(
            runjob.display_is_zombie("limited", pid_dead=True, retry_at=None),
            "absent retry_at + dead pid must be abandoned (F1b)")
        self.assertTrue(
            runjob.display_is_zombie("limited", pid_dead=True, retry_at=""),
            "empty retry_at + dead pid must be abandoned (F1b)")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            # Exact motivating ledger shape (no retry_at, no pid, empty summary).
            row = {
                "event": "limited",
                "uid": "sample-review-claude-9f4ef90b",
                "status": "limited",
                "summary": "",
                "ts": "2026-08-03T15:13:21+10:00",
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                # No process_is_alive patch: missing pid → not alive.
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("limited†", text,
                      "absent retry_at + unknown pid must dagger (F1b)")
        self.assertIn("sample-review-claude-9f4ef90b", text)

    def test_parked_limited_future_retry_at_not_daggered(self):
        """(c) freshly parked limited (future retry_at) must stay ⏸, not †."""
        future = runjob.iso(runjob.now() + timedelta(minutes=30))
        # Predicate pin first — fails on 58ede7b (no display_is_zombie).
        self.assertFalse(
            runjob.display_is_zombie("limited", pid_dead=True, retry_at=future),
            "future retry_at must not dagger even when pid is gone")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            row = {
                "event": "limited",
                "uid": "parked-quota",
                "project_name": "p",
                "engine": "claude",
                "model": "opus",
                "status": "limited",
                "pid": None,
                "retry_at": future,
                "ts": runjob.iso(),
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_log(args)
            text = out.getvalue()
        self.assertIn("parked-quota", text)
        self.assertNotIn("limited†", text)
        self.assertIn("limited", text)
        self.assertRegex(text, r"⏸\s+parked-quota\b")

    def test_limited_alive_pid_not_daggered(self):
        """(d) limited row whose pid is still alive must not dagger."""
        past = "2026-08-03T09:00:00+10:00"
        # Even past retry_at must not dagger while the process is live.
        self.assertFalse(
            runjob.display_is_zombie("limited", pid_dead=False, retry_at=past),
            "live pid must not dagger limited rows")
        self.assertFalse(
            runjob.display_is_zombie("limited", pid_dead=False, retry_at=None),
            "live pid must not dagger even with no retry_at")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            row = {
                "event": "limited",
                "uid": "limited-live-pid",
                "project_name": "p",
                "engine": "claude",
                "model": "opus",
                "status": "limited",
                "pid": os.getpid(),
                "retry_at": past,
                "ts": "2026-08-03T08:55:00+10:00",
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_log(args)
            text = out.getvalue()
        self.assertIn("limited-live-pid", text)
        self.assertNotIn("limited†", text)
        self.assertIn("limited", text)

    def test_fleet_status_daggers_abandoned_limited(self):
        """Fleet.status shares the same F1/F1b dagger (both display sites)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            past = runjob.iso(runjob.now() - timedelta(minutes=10))
            job = _fleet_job(
                jobs_dir, job_id="abandoned-lim",
                pid=999991, status="limited", retry_at=past, attempts=1)
            fleet.save(job)
            with mock.patch.object(fleet, "pid_alive", return_value=False), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                fleet.status()
            text = out.getvalue()
        self.assertIn("limited†", text)
        self.assertIn("abandoned-lim", text)

    def test_cmd_log_limited_dagger_does_not_mutate_ledger(self):
        """Read-may-write invariant: daggering limited is display-only."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            log_path = home / "jobs.jsonl"
            row = {
                "event": "limited",
                "uid": "sample-review-claude-9f4ef90b",
                "status": "limited",
                "summary": "",
                "ts": "2026-08-03T15:13:21+10:00",
            }
            log_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            before = log_path.read_text(encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, follow=False, limit=40)
            with mock.patch.object(runjob, "JOBS_LOG", log_path), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                runjob.cmd_log(args)
            after = log_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


# Real job logs captured 2026-08-03 from ~/.config/dev-jobs/logs/ (N-a specimens).
_FIXTURE_LOGS = Path(__file__).parent / "fixtures" / "logs"


class LimitClassificationTests(unittest.TestCase):
    """N-a: LIMIT_RE must not outrank a clean exit-0 completion.

    Base (7596c24) evaluates LIMIT_RE before rc, so any finished job whose
    report *mentions* rate limits is filed ⏸ limited. Verified: each of the
    three named 2026-08-03 specimens classifies `limited` under base logic
    (reproduced by inlining the old order before the discriminator landed).
    """

    def _status(self, name: str, engine: str = "claude") -> str:
        log = _FIXTURE_LOGS / name
        self.assertTrue(log.is_file(), f"missing fixture {log}")
        status, _event, _fields = runjob._classify_from_log(log, engine)
        return status

    # -- (a) three named exit-0 specimens must not be limited ---------------

    def test_specimen_completed_prose_one_not_limited(self):
        """Exit-0 report quoting 429 is not limited.

        Base failure: old order matched LIMIT_RE on prose '429' → limited.
        Ran against base logic (LIMIT before rc): status=limited. Expected after: exited.
        """
        status = self._status("completed-prose-1.log", "claude")
        self.assertNotEqual(status, "limited")
        self.assertEqual(status, "exited")  # exit 0, no DONE: trailer

    def test_specimen_completed_prose_two_not_limited(self):
        """A second exit-0 report discussing a 429 is not limited.

        Base failure: LIMIT_RE on prose '429' → limited. Base check: limited.
        """
        status = self._status("completed-prose-2.log", "claude")
        self.assertNotEqual(status, "limited")
        self.assertEqual(status, "exited")

    def test_specimen_completed_prose_three_not_limited(self):
        """Exit-0 report discussing overloaded/rate-limit text is not limited.

        Base failure: LIMIT_RE on 'overloaded'/'rate limit' in the handoff prompt
        quoting the residual table → limited. Base check: limited.
        """
        status = self._status("completed-prose-3.log", "claude")
        self.assertNotEqual(status, "limited")
        self.assertEqual(status, "exited")

    # -- (b) residual limited path still parks when exit is not clean -------

    def test_no_exit_marker_with_limit_signal_stays_limited(self):
        """No ===== exit:N marker + LIMIT_RE → limited (err toward limited).

        Synthetic source excerpt contains rate_limit_exceeded. Base and new both
        classify limited.
        Not a verified engine-park specimen — genuine parks are a declared gap
        (survey: zero true-park logs machine-wide). This pins the residual path
        that must remain limited when evidence of completion is absent.
        Base check: limited (LIMIT_RE before rc; rc is None).
        """
        status = self._status("limit-no-exit.log", "codex")
        self.assertEqual(status, "limited")

    def test_nonzero_exit_with_limit_signal_is_limited_not_failed(self):
        """Nonzero exit + LIMIT_RE → limited (park, not hard fail).

        Reconstructs the residual park shape: engine-ish limit line + exit:1.
        The limit string 'usage limit reached' is the exact mock string found in
        the real worker-recovery-codex-d9e651ee.log (a test fixture the job
        wrote); exit:1 is a normal failed-process shape.
        No full genuine park log exists on disk (declared gap). Direction:
        when the process did not exit 0, limit text outranks failed.
        Base check: limited (LIMIT_RE before rc — same answer, different order).
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "park.log"
            log.write_text(
                "===== 2026-08-03T12:00:00+10:00 launch codex\n"
                "usage limit reached\n"
                "===== exit:1\n",
                encoding="utf-8")
            status, event, _ = runjob._classify_from_log(log, "codex")
        self.assertEqual(status, "limited")
        self.assertEqual(event, "limited")

    # -- (c) ambiguous case: exit 0 + LIMIT prose, resolution asserted ------

    def test_exit_zero_with_limit_prose_is_exited_not_limited(self):
        """Ambiguous: clean exit 0 and LIMIT_RE in the body → exited.

        Resolution: rc==0 outranks LIMIT_RE. Direction justified: the entire
        observed machine-wide limited population (12/12) was this shape and
        wrong; mis-parking finished work as limited is noisy, while treating a
        genuine exit-0 park as exited would be the dangerous direction — but
        live loopback 429 probes closed that for all six engines (grok exits 1).
        Err-toward-limited is preserved for rc is None / rc!=0 (see tests above).
        Base check: limited (LIMIT_RE before rc). Ran against base: limited.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "prose.log"
            log.write_text(
                "===== 2026-08-03T12:00:00+10:00 launch claude opus high\n"
                "We investigated false rate-limit parks and the 429 noise.\n"
                "Report complete.\n"
                "===== exit:0\n",
                encoding="utf-8")
            status, event, fields = runjob._classify_from_log(log, "claude")
        self.assertEqual(status, "exited")
        self.assertEqual(event, "done")
        self.assertNotEqual(status, "limited")
        # S1: residual must be greppable in the ledger (empty summary on base).
        self.assertEqual(fields.get("summary"), runjob.EXIT0_LIMIT_PROSE_SUMMARY)

    def test_exit_zero_with_limit_prose_summary_is_greppable(self):
        """S1: rc==0 + LIMIT_RE + no DONE → summary carries a fixed greppable prefix.

        Without this annotation the accepted residual is invisible: base and the
        pre-S1 lane both write summary="" for this shape, so a future grok park
        (or any exit-0+LIMIT) cannot be counted with one grep over the ledger.
        Base check: status=limited and summary="" (LIMIT_RE before rc; no
        EXIT0_LIMIT_PROSE_SUMMARY constant). Must fail on 7596c24.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "s1-prose.log"
            log.write_text(
                "===== 2026-08-03T12:00:00+10:00 launch claude opus high\n"
                "Discussed rate limit parks and 429 false positives.\n"
                "===== exit:0\n",
                encoding="utf-8")
            status, _event, fields = runjob._classify_from_log(log, "claude")
        self.assertEqual(status, "exited")
        self.assertEqual(fields.get("summary"), "exit 0 with limit text in tail")
        # Machine-greppable contract: one fixed string, not free prose.
        self.assertEqual(fields["summary"], runjob.EXIT0_LIMIT_PROSE_SUMMARY)

    def test_exit_zero_with_done_and_limit_prose_is_done(self):
        """exit 0 + DONE: + LIMIT prose → done (completion outranks prose).

        DONE trailer supplies the summary; S1 annotation only fills empty
        summary, so a real DONE line is preserved. Base check: limited.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "done-prose.log"
            log.write_text(
                "===== 2026-08-03T12:00:00+10:00 launch claude opus high\n"
                "Discussed rate limits and 429 handling.\n"
                "DONE: wrote the report\n"
                "===== exit:0\n",
                encoding="utf-8")
            status, event, fields = runjob._classify_from_log(log, "claude")
        self.assertEqual(status, "done")
        self.assertEqual(event, "done")
        self.assertTrue(fields.get("summary", "").startswith("DONE:"))

    def test_classify_agy_exit0_limit_prose_no_success_is_failed(self):
        """S2 one-shot half: agy rc==0 + LIMIT prose + no SUCCESS → failed.

        Base check: limited (LIMIT_RE evaluated before the agy/rc branch).
        Must fail on 7596c24 with status=limited.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "agy-prose.log"
            log.write_text(
                "plain text discussing rate limit and 429 handling\n"
                "===== exit:0\n",
                encoding="utf-8")
            status, event, _ = runjob._classify_from_log(log, "agy")
        self.assertEqual(status, "failed")
        self.assertEqual(event, "failed")

    def test_fleet_agy_exit0_limit_prose_no_success_is_failed(self):
        """S2 fleet half: Fleet.finalize aligns with _classify_from_log.

        agy, exit 0, LIMIT prose in log, no SUCCESS object → failed (not limited).
        Base check: limited — finalize took the LIMIT_RE park path when no
        SUCCESS object was present. Must fail on 7596c24 with status=limited.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "agy-limit-prose", engine="agy",
                             model="gemini-3.6-flash-low", effort="low")
            fleet.log_file(job).write_text(
                "agent wrote about rate limits and a 429 response shape\n"
                "===== exit:0\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["pid"])
        self.assertEqual(record.call_args.kwargs["status"], "failed")
        self.assertEqual(record.call_args.kwargs["event"], "failed")

    def test_fleet_agy_nonzero_limit_still_limited(self):
        """S2 residual: agy nonzero exit + LIMIT_RE still parks (not hard-fail).

        Alignment only changes the clean-exit-0 path; true parks (rc!=0 or
        missing exit + limit text) stay limited. Base and new agree: limited.
        Docstring says so — passes on 7596c24 by design.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "agy-true-park", engine="agy",
                             model="gemini-3.6-flash-low", effort="low")
            fleet.log_file(job).write_text(
                "usage limit reached\n"
                "===== exit:1\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event"):
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")

    def test_fleet_non_agy_limit_prose_with_done_is_done(self):
        """S-1's property, production-shaped: finished non-agy fleet job is not parked.

        The S-1 gate's central case is "a finished job whose report merely
        *discusses* rate limits must not be parked and re-launched up to 3×".
        On the fleet path the completion evidence for a non-agy engine is the
        DONE: trailer the ORCHESTRATOR CONTRACT asks for — Fleet.launch writes
        no exit wrapper for those engines, so the log has no marker at all
        (observed in historical logs). This is
        that case in its production shape, and it must stay done.

        Successor to test_fleet_non_agy_exit0_limit_prose_no_done_is_done, which
        pinned the same property through a `===== exit:0` line a claude fleet log
        can never contain — see writer-agree lane / docs/writer-agree.md.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "s1-central", engine="claude")
            fleet.log_file(job).write_text(
                "===== 2026-08-03T12:00:00+10:00 launch claude\n"
                "We discussed rate limits and 429 false parks.\n"
                "DONE: report complete.\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "done")
        self.assertIsNone(job["pid"])
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(record.call_args.kwargs["status"], "done")
        self.assertEqual(record.call_args.kwargs["event"], "done")

    def test_fleet_non_agy_quoted_exit0_limit_prose_no_done_is_limited(self):
        """Reversal (writer-agree): a quoted exit:0 no longer finishes a fleet job.

        Was test_fleet_non_agy_exit0_limit_prose_no_done_is_done (S-1), which
        asserted done. Fleet.launch wraps only FLEET_WRAPPED_ENGINES, so for a
        claude fleet job this `===== exit:0` is a line the agent printed, not the
        process's status; classify_tail is given expect_exit_marker=False and
        ignores it. With no DONE: trailer and limit prose in the tail, the job
        has no completion evidence and parks — which is also what the reaper
        would do if it were handed a log whose marker it did not trust.

        The one-shot/reaper direction of S-1 is unchanged: see
        test_exit_zero_with_limit_prose_is_exited_not_limited.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "s1-central", engine="claude")
            fleet.log_file(job).write_text(
                "===== 2026-08-03T12:00:00+10:00 launch claude\n"
                "We discussed rate limits and 429 false parks.\n"
                "Report complete.\n"
                "===== exit:0\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")
        self.assertIsNone(job["pid"])
        self.assertEqual(record.call_args.kwargs["status"], "limited")
        self.assertIn("retry_at", record.call_args.kwargs)

    def test_fleet_non_agy_nonzero_limit_still_limited(self):
        """S-1 residual: non-agy nonzero exit + LIMIT_RE still parks.

        True parks (proven: claude/codex/glm/local/grok exit 1 on live 429)
        must remain limited after S-1. Passes on pre-S-1 by design.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "true-park", engine="claude")
            fleet.log_file(job).write_text(
                "API Error: Request rejected (429) · per-5-hour rate limit\n"
                "===== exit:1\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")
        self.assertEqual(record.call_args.kwargs["status"], "limited")

    def test_fleet_grok_quoted_exit0_limit_prose_is_limited(self):
        """Reversal (writer-agree): was test_fleet_grok_exit0_limit_prose_is_done.

        The loopback 429 evidence behind S-1 (grok 0.2.118 exits 1 on a true
        park, so rc0+LIMIT is a *false* park) still holds — for logs whose exit
        marker the wrapper wrote. Fleet.launch does not wrap grok, so this
        marker is agent output; finalize ignores it and, with no DONE: trailer,
        parks. The rc0-outranks-LIMIT discriminator survives untouched on the
        one-shot path (test_fleet_grok_nonzero_limit_still_limited keeps the
        true-park direction) and on agy fleet logs, which are wrapped.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "grok-s1", engine="grok", model="grok-4.5")
            fleet.log_file(job).write_text(
                "===== 2026-08-03T12:00:00+10:00 launch grok\n"
                "Table of deferred rate-limit work and 429 handling notes.\n"
                "===== exit:0\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")
        self.assertEqual(record.call_args.kwargs["status"], "limited")
        self.assertIn("retry_at", record.call_args.kwargs)

    def test_fleet_grok_nonzero_limit_still_limited(self):
        """Grok true-park shape: exit 1 + LIMIT_RE → limited (not done).

        Matches the loopback 429 specimen: rate-limit error text + exit 1.
        S-1 must not swallow this path. Passes on pre-S-1 by design.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "grok-true-park", engine="grok",
                             model="grok-4.5")
            fleet.log_file(job).write_text(
                "tokens: Rate limit exceeded. Please try again later. (resets at 12:00)\n"
                "Error: tokens: Rate limit exceeded. Please try again later. (resets at 12:00)\n"
                "===== exit:1\n",
                encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")
        self.assertEqual(record.call_args.kwargs["status"], "limited")
        self.assertIn("retry_at", record.call_args.kwargs)

    # -- SF-1: EXIT_RE last-match (quoted exit before real wrapper) ---------

    def test_exitre_quoted_exit1_before_real_exit0_is_done(self):
        """Live misparse direction: quoted exit:1 before real exit:0 → finished.

        Reports that quote runjob's ===== exit:N format at line start made
        EXIT_RE.search take the first (quoted) match. Live specimens of this
        shape (e.g. jobs-limit-s1-grok-d57569df) were filed limited at exit 0.
        Both writers must take the last marker.

        Fixture deliberately has **no** DONE: trailer, so the reaper half
        genuinely requires last-match rc=0 rather than short-circuiting on
        DONE_RE.

        The fleet half now asserts the opposite outcome (limited), and no
        longer witnesses last-match at all: grok fleet logs are unwrapped, so
        finalize ignores every marker in them (writer-agree). The fleet's
        last-match pin moved to the engine that is wrapped —
        test_exitre_fleet_agy_quoted_exit1_before_real_exit0_is_done.
        """
        text = (
            "===== 2026-08-03T22:00:00+10:00 launch grok\n"
            "Probe output quote:\n"
            "===== exit:1\n"
            "usage limit reached and rate-limit 429 discussed in report\n"
            "===== exit:0\n"
        )
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "quoted1.log"
            log.write_text(text, encoding="utf-8")
            status, event, fields = runjob._classify_from_log(log, "grok")
        # No DONE trailer: clean last exit 0 → exited (not limited on first-match).
        self.assertEqual(status, "exited")
        self.assertEqual(event, "done")
        self.assertNotEqual(status, "limited")
        self.assertEqual(fields.get("summary"), runjob.EXIT0_LIMIT_PROSE_SUMMARY)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(
                root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "quoted-exit1", engine="grok",
                             model="grok-4.5")
            fleet.log_file(job).write_text(text, encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        # Unwrapped engine: both markers are agent output, neither is read.
        # No DONE: and limit prose in the tail → park, and the quoted-marker
        # misparse is structurally impossible rather than merely mitigated.
        self.assertEqual(job["status"], "limited")
        self.assertEqual(record.call_args.kwargs["status"], "limited")

    def test_exitre_fleet_agy_quoted_exit1_before_real_exit0_is_done(self):
        """Fleet-side last-match pin, on the only engine Fleet.launch wraps.

        agy fleet logs carry a real `===== exit:N` (FLEET_WRAPPED_ENGINES), so
        finalize does read the marker — and must read the *last* one. A report
        that quotes `===== exit:1` at line start before the wrapper's real
        `===== exit:0` must not turn a SUCCESS result into failed.

        Reverting classify_tail's `_exits[-1]` to `_exits[0]` (or to
        EXIT_RE.search) must fail this test: rc=1 with a provider result present
        is decisive failure.
        """
        text = (
            "===== 2026-08-03T22:00:00+10:00 launch agy\n"
            "Probe output quote:\n"
            "===== exit:1\n"
            + json.dumps({"status": "SUCCESS", "response": "ok",
                          "conversation_id": "c-42"}) + "\n"
            "===== exit:0\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(
                root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "quoted-agy", engine="agy",
                             model="gemini-3.6-flash-low")
            fleet.log_file(job).write_text(text, encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record, \
                 mock.patch.object(runjob, "read_agy_quota", return_value={}):
                fleet.finalize(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(record.call_args.kwargs["status"], "done")
        self.assertEqual(job["session"], "c-42")
        self.assertNotEqual(job["status"], "failed")

    def test_exitre_quoted_exit0_before_real_exit1_keeps_park(self):
        """Latent misparse direction: quoted exit:0 before real exit:1 → park kept.

        Under S-1, first-match on this shape files done and silently loses the
        park (no retry). Last-match must keep limited + retry_at. A lost park
        leaves no dagger; that is the failure that must not ship.
        """
        text = (
            "===== 2026-08-03T12:00:00+10:00 launch claude\n"
            "Quoted clean exit from an earlier example:\n"
            "===== exit:0\n"
            "API Error: Request rejected (429) · per-5-hour rate limit\n"
            "===== exit:1\n"
        )
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "quoted0.log"
            log.write_text(text, encoding="utf-8")
            status, event, _ = runjob._classify_from_log(log, "claude")
        self.assertEqual(status, "limited")
        self.assertEqual(event, "limited")
        self.assertNotEqual(status, "done")
        self.assertNotEqual(status, "exited")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            fleet = runjob.Fleet(
                root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
            job = _fleet_job(jobs_dir, "quoted-exit0", engine="claude")
            fleet.log_file(job).write_text(text, encoding="utf-8")
            with mock.patch.object(runjob, "log_event") as record:
                fleet.finalize(job)
        self.assertEqual(job["status"], "limited")
        self.assertEqual(record.call_args.kwargs["status"], "limited")
        self.assertIn("retry_at", record.call_args.kwargs)
        self.assertNotEqual(job["status"], "done")

    def test_classify_tail_attempts_contract(self):
        """The `attempts` parameter: retry budget, and None = cannot retry.

        The reaper passes attempts=None. That value is unreachable in practice —
        expect_exit_marker=True sends a marker-less log to wrapper_died first —
        so no fixture through _classify_from_log can pin it. Pin it here, on the
        shared function, since it is half of the lane's residual divergence.
        """
        tail = "===== 2026-08-03T12:00:00+10:00 launch claude\nordinary work\n"
        kw = dict(expect_exit_marker=False)
        self.assertEqual(
            runjob.classify_tail(tail, "claude", attempts=0, **kw).reason,
            "retry")
        self.assertEqual(
            runjob.classify_tail(tail, "claude", attempts=1, **kw).reason,
            "retry")
        # attempts is pre-increment: 2 + 1 >= FLEET_MAX_ATTEMPTS → give up.
        self.assertEqual(runjob.FLEET_MAX_ATTEMPTS, 3)
        self.assertEqual(
            runjob.classify_tail(tail, "claude", attempts=2, **kw).reason,
            "gave_up")
        gave_up = runjob.classify_tail(tail, "claude", attempts=None, **kw)
        self.assertEqual(gave_up.reason, "gave_up")
        self.assertEqual(gave_up.status, "failed")

    def test_two_writer_divergence_instrument(self):
        """Two-writer divergence instrument with marker-multiplicity axis.

        Base axes (historical 48): engine in {claude, agy} × rc in {0,1,None}
        × DONE × LIMIT × agy-outcome for agy only → 12 + 36 = 48.

        Multiplicity axis (this lane): {single, retry_disagree, quoted_before}
        → 48 × 3 = 144. `single` restates the old 48 so 15/48 stays comparable;
        the multi values reach fleet append-mode retry logs and quoted line-start
        exit markers that the old instrument could not represent.

        Attempts: measured and pinned at attempts=0 (historical fixture default)
        and attempts=2 (give-up arm).

        Re-pinned by the writer-agree lane, which put both writers on one
        decision function (runjob.classify_tail): attempts=0 went 45/144 → 24/144
        (single 15/48 → 8/48) and attempts=2 went 39/144 → 18/144 (13/48 → 6/48),
        added 3 / removed 24 at each regime. What survives is exactly the two
        parameters the call sites must differ on — expect_exit_marker and
        attempts — so every remaining row is a named, justified divergence and
        none is a second implementation. See docs/writer-agree.md.

        The set is the instrument; the count is not.

        retry_disagree is agy-only in production (Fleet.launch wraps only agy);
        non-agy multi rows are synthetic over-coverage. Prior for multi is
        enumerated: when final rc is set, prior = opposite(rc); when final is
        None, prior ∈ {0, 1} (the hazardous exit:0-only direction is covered
        by an explicit sub-check, not folded into the 144 headline product).
        """
        import itertools

        def opposite_rc(rc):
            # Disagreeing prior marker: flip 0↔1; when final has no marker,
            # the 144 product still uses prior=1 (failed-then-retry shape).
            # prior=0 for rc=None is covered separately below (repair 4).
            return 1 if rc != 1 else 0

        def make_log(rc, done, limit, agy_outcome, mult, prior=None):
            """Build a synthetic job log for one instrument class.

            mult:
              single         — one exit marker (historical make_log)
              retry_disagree — Fleet.launch append path: prior attempt exit,
                               then resume header, then final attempt
                               (production: agy-only — Fleet.launch wraps only
                               agy; non-agy multi rows are synthetic)
              quoted_before  — bare ===== exit:N at line start in the report
                               body before the real wrapper marker
            prior: used by multi shapes; defaults to opposite_rc(rc).
            """
            def final_body():
                lines = []
                if limit:
                    lines.append("usage limit reached and rate-limit 429")
                else:
                    lines.append("ordinary agent work output")
                if done:
                    lines.append("DONE: finished the task")
                if agy_outcome is not None:
                    lines.append(json.dumps({
                        "status": agy_outcome, "response": "x",
                        "conversation_id": "c-1"}))
                return lines

            if mult == "single":
                lines = ["===== 2026-08-03T12:00:00+10:00 launch eng"]
                lines.extend(final_body())
                if rc is not None:
                    lines.append(f"===== exit:{rc}")
                return "\n".join(lines) + "\n"

            if prior is None:
                prior = opposite_rc(rc)
            if mult == "retry_disagree":
                # Derived from Fleet.launch (runjob.py) for agy only:
                #   logf = open(self.log_file(job), "a", ...)
                #   logf.write(f"\n===== {iso()} {'resume' if resume else 'launch'} ...")
                # Wrapper then appends ===== exit:N. Retries/resumes therefore
                # accumulate markers in one file; first and last may disagree.
                lines = [
                    "===== 2026-08-03T12:00:00+10:00 launch eng",
                    "first attempt ordinary work",
                    f"===== exit:{prior}",
                    "",  # open(...,"a") write starts with \n before next header
                    "===== 2026-08-03T12:05:00+10:00 resume eng",
                ]
                lines.extend(final_body())
                if rc is not None:
                    lines.append(f"===== exit:{rc}")
                return "\n".join(lines) + "\n"

            if mult == "quoted_before":
                # Report quotes a bare exit marker at line start before the
                # real wrapper marker (live EXIT_RE misparse shape).
                lines = [
                    "===== 2026-08-03T12:00:00+10:00 launch eng",
                    "Probe output quote from earlier run:",
                    f"===== exit:{prior}",
                ]
                lines.extend(final_body())
                if rc is not None:
                    lines.append(f"===== exit:{rc}")
                return "\n".join(lines) + "\n"

            raise ValueError(f"unknown multiplicity: {mult!r}")

        def classify_norm(log_path, engine):
            status, _event, _fields = runjob._classify_from_log(log_path, engine)
            return "done" if status == "exited" else status

        def fleet_status(log_text, engine, attempts=0):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                jobs_dir = root / "jobs"
                jobs_dir.mkdir()
                fleet = runjob.Fleet(
                    root, json.loads(json.dumps(runjob.DEFAULT_CONFIG)))
                # attempts is explicit: _fleet_job defaults 0 and fleet_status
                # never used to override it, so the give-up arm at finalize
                # (attempts >= 3 after +=1) was unreachable. Drive both 0 and 2.
                job = _fleet_job(
                    jobs_dir, "enum", engine=engine,
                    model=("gemini-3.6-flash-low" if engine == "agy" else "opus"),
                    attempts=attempts)
                fleet.log_file(job).write_text(log_text, encoding="utf-8")
                # SF-2: mock quota so the agy LIMIT arm never shells out to
                # antigravity-usage (status under test does not depend on it).
                with mock.patch.object(runjob, "log_event"), \
                     mock.patch.object(runjob, "read_agy_quota", return_value={}):
                    fleet.finalize(job)
                return job["status"]

        MULTS = ("single", "retry_disagree", "quoted_before")
        cases = []
        for mult in MULTS:
            for rc, done, limit in itertools.product(
                    [0, 1, None], [True, False], [True, False]):
                cases.append(("claude", rc, done, limit, None, mult))
            for rc, done, limit, outcome in itertools.product(
                    [0, 1, None], [True, False], [True, False],
                    [None, "SUCCESS", "FAILED"]):
                cases.append(("agy", rc, done, limit, outcome, mult))
        self.assertEqual(len(cases), 144, "48 base × 3 multiplicity")

        def collect(attempts):
            divergent = []
            for engine, rc, done, limit, outcome, mult in cases:
                text = make_log(rc, done, limit, outcome, mult)
                with tempfile.TemporaryDirectory() as td:
                    log = Path(td) / "j.log"
                    log.write_text(text, encoding="utf-8")
                    c = classify_norm(log, engine)
                f = fleet_status(text, engine, attempts=attempts)
                if c != f:
                    divergent.append(
                        (engine, rc,
                         "DONE" if done else "noDONE",
                         "LIMIT" if limit else "noLIMIT",
                         outcome or "noOutcome", mult, c, f))
            return set(divergent)

        def fmt(rows):
            # rc may be None; plain sorted() cannot order None vs int.
            return sorted(rows, key=lambda r: (
                r[0], r[1] is None, r[1] if r[1] is not None else -1,
                r[2], r[3], r[4], r[5], r[6], r[7]))

        def clone_full(single_set):
            # Under last-match, multi shapes currently clone the single
            # divergent axes (same (classify, finalize) pairs).
            full = set()
            for mult in MULTS:
                for row in single_set:
                    full.add(row[:5] + (mult,) + row[6:])
            return full

        def pin_slice(divergent_set, expected_single, expected_full,
                      single_n, full_n, label):
            single_divergent = {
                row for row in divergent_set if row[5] == "single"}
            self.assertEqual(
                len(single_divergent), single_n,
                f"{label} single slice expected {single_n}/48, "
                f"got {len(single_divergent)}/48: {fmt(single_divergent)}")
            single_added = single_divergent - expected_single
            single_removed = expected_single - single_divergent
            self.assertEqual(
                single_added, set(),
                f"{label} single slice added: {fmt(single_added)}")
            self.assertEqual(
                single_removed, set(),
                f"{label} single slice removed: {fmt(single_removed)}")
            # Count is labelled, not the tripwire: under a first-match mutation
            # of both EXIT_RE sites the total stays 45 (16 multi in, 16 out)
            # while only the set-difference assertions fire. The set is the
            # instrument; the count is not.
            self.assertEqual(
                len(divergent_set), full_n,
                f"{label} count (not the tripwire — see set diffs) expected "
                f"{full_n}/144, got {len(divergent_set)}/144: "
                f"{fmt(divergent_set)}")
            added = divergent_set - expected_full
            removed = expected_full - divergent_set
            self.assertEqual(
                added, set(),
                f"{label} full instrument added: {fmt(added)}")
            self.assertEqual(
                removed, set(),
                f"{label} full instrument removed: {fmt(removed)}")

        # --- attempts=0: historical fixture default; 24/144 and 8/48 ---
        # Re-pinned by the writer-agree lane (was 45/144, 15/48). Both writers
        # now call runjob.classify_tail; every row below is a *parameter*
        # divergence, not an implementation divergence:
        #   expect_exit_marker — True for the reaper (cmd_run wraps every
        #     engine), engine in FLEET_WRAPPED_ENGINES for the fleet. Every
        #     claude row here is a row where the reaper reads an exit marker the
        #     fleet must not read.
        #   attempts — only the fleet can relaunch.
        # Diff vs the previous pin: removed 24 (all agy — agy gets identical
        # parameters at both call sites, so it is now divergence-free by
        # construction), added 3 (the (0,noDONE,LIMIT) claude class × 3 mults).
        expected_single_a0 = {
            # attempts-path: reaper reads exit 0 → done, fleet has no marker to
            # read and no DONE: trailer → retry.
            ("claude", 0, "noDONE", "noLIMIT", "noOutcome", "single",
             "done", "limited"),
            # ADDED by this lane: same cause, with LIMIT prose so the fleet
            # parks instead of retrying. The fleet no longer honours a quoted
            # `===== exit:0` (old S-1 arm) — see
            # test_fleet_non_agy_quoted_exit0_limit_prose_no_done_is_limited.
            ("claude", 0, "noDONE", "LIMIT", "noOutcome", "single",
             "done", "limited"),
            ("claude", 1, "DONE", "LIMIT", "noOutcome", "single",
             "limited", "done"),
            ("claude", 1, "DONE", "noLIMIT", "noOutcome", "single",
             "failed", "done"),
            ("claude", 1, "noDONE", "noLIMIT", "noOutcome", "single",
             "failed", "limited"),
            ("claude", None, "DONE", "LIMIT", "noOutcome", "single",
             "limited", "done"),
            ("claude", None, "DONE", "noLIMIT", "noOutcome", "single",
             "failed", "done"),
            ("claude", None, "noDONE", "noLIMIT", "noOutcome", "single",
             "failed", "limited"),
        }
        expected_full_a0 = clone_full(expected_single_a0)
        divergent_a0 = collect(attempts=0)
        # Reversal of the S-1 central-case assertion (was assertNotIn). The
        # instrument hands ONE byte-string to both writers, but the same bytes
        # mean different things depending on which writer produced the log: a
        # one-shot log's `===== exit:0` was written by the wrapper, a claude
        # fleet log's was typed by the agent. Both writers are right about their
        # own log; the divergence is the instrument's provenance-blindness.
        for mult in MULTS:
            self.assertIn(
                ("claude", 0, "noDONE", "LIMIT", "noOutcome", mult,
                 "done", "limited"),
                divergent_a0,
                f"expected the provenance split under mult={mult} attempts=0")
        # Structural result of unification: identical parameters ⇒ identical
        # verdict. agy is the engine where expect_exit_marker is True on both
        # sides and the retry arm is unreachable, so no agy class may diverge.
        self.assertEqual(
            {r for r in divergent_a0 if r[0] == "agy"}, set(),
            "agy takes identical classify_tail parameters at both call sites "
            "and must not diverge")
        pin_slice(divergent_a0, expected_single_a0, expected_full_a0,
                  8, 24, "attempts=0")

        # --- attempts=2: give-up arm; 18/144, single 6/48, ('done','failed') ---
        # Re-pinned by the writer-agree lane (was 39/144, 13/48; same 24 agy
        # rows removed, same 3 added). classify_tail decides retry vs give-up
        # from the pre-increment attempts (attempts + 1 >= FLEET_MAX_ATTEMPTS),
        # so at attempts=2 the retry rows file failed instead of limited:
        #   (claude, 0, noDONE, noLIMIT): done/limited → done/failed (still
        #     diverges — reaper done, fleet gave up)
        #   (claude, 1|None, noDONE, noLIMIT): failed/limited → failed/failed
        #     (agree; leave the set)
        # The LIMIT park arm never consults attempts (a quota park is resumable
        # for as long as the quota says so), so (0,noDONE,LIMIT) is unchanged.
        # Pairs: {('done','failed'): 3, ('done','limited'): 3,
        #         ('failed','done'): 6, ('limited','done'): 6}
        expected_single_a2 = {
            ("claude", 0, "noDONE", "noLIMIT", "noOutcome", "single",
             "done", "failed"),
            ("claude", 0, "noDONE", "LIMIT", "noOutcome", "single",
             "done", "limited"),
            ("claude", 1, "DONE", "LIMIT", "noOutcome", "single",
             "limited", "done"),
            ("claude", 1, "DONE", "noLIMIT", "noOutcome", "single",
             "failed", "done"),
            ("claude", None, "DONE", "LIMIT", "noOutcome", "single",
             "limited", "done"),
            ("claude", None, "DONE", "noLIMIT", "noOutcome", "single",
             "failed", "done"),
        }
        expected_full_a2 = clone_full(expected_single_a2)
        divergent_a2 = collect(attempts=2)
        self.assertEqual(
            {r for r in divergent_a2 if r[0] == "agy"}, set(),
            "agy must not diverge at attempts=2 either (the retry/give-up arm "
            "is unreachable when a marker is expected)")
        pin_slice(divergent_a2, expected_single_a2, expected_full_a2,
                  6, 18, "attempts=2")
        # Named pair the attempts=0 pin cannot see.
        self.assertTrue(
            any((r[6], r[7]) == ("done", "failed") for r in divergent_a2),
            "attempts=2 must surface ('done','failed') give-up divergence")

        # --- prior ∈ {0, 1} for final rc=None (repair 4) ---
        # opposite_rc(None)==1 hard-wired prior to nonzero exit, so a killed
        # job whose only marker is a succeeded prior (exit:0) was never built.
        # That direction files classify=done (rc==0 → exited → done). Cover both.
        for mult in ("retry_disagree", "quoted_before"):
            for prior in (0, 1):
                text = make_log(
                    None, False, False, None, mult, prior=prior)
                with tempfile.TemporaryDirectory() as td:
                    log = Path(td) / "j.log"
                    log.write_text(text, encoding="utf-8")
                    c = classify_norm(log, "claude")
                f = fleet_status(text, "claude", attempts=0)
                if prior == 0:
                    self.assertEqual(
                        (c, f), ("done", "limited"),
                        f"rc=None prior=0 mult={mult}: hazardous direction "
                        f"must be classify=done finalize=limited, got "
                        f"({c!r}, {f!r})")
                else:
                    self.assertEqual(
                        (c, f), ("failed", "limited"),
                        f"rc=None prior=1 mult={mult}: expected "
                        f"failed/limited, got ({c!r}, {f!r})")

    def test_naive_retry_at_does_not_raise(self):
        """N-d regression: tz-naive retry_at must not TypeError the render path."""
        # Base raised TypeError; after N-d returns True (treat as elapsed).
        self.assertTrue(runjob._limited_retry_elapsed("2026-08-04T09:00"))


class OneShotLinkedWorktreeSandboxTests(unittest.TestCase):
    """One-shot `run` must grant git's common dir, not a .git pointer file."""

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def linked_worktree(self, td: str) -> tuple[Path, Path]:
        main = Path(td) / "main"
        worktree = Path(td) / "linked"
        main.mkdir()
        self.git(main, "init")
        (main / "seed.txt").write_text("seed\n", encoding="utf-8")
        self.git(main, "add", "seed.txt")
        self.git(main, "-c", "user.name=Sandbox Test", "-c",
                 "user.email=sandbox@example.test", "commit", "-m", "seed")
        self.git(main, "worktree", "add", "-b", "linked-branch", str(worktree))
        return main, worktree

    def launched_argv(self, engine: str, cwd: Path, home: Path) -> list[str]:
        """Run the one-shot setup through Popen, retaining its emitted argv."""
        args = argparse.Namespace(
            engine=engine, cwd=str(cwd), model="", effort="", prompt="prompt",
            file=None, title="", wait=True)
        proc = mock.Mock(pid=12345)
        proc.wait.return_value = 0
        actual_popen = subprocess.Popen
        spawned: list[list[str]] = []
        log_streams = []

        def capture_engine_spawn(argv, *args, **kwargs):
            if argv[0] == "/bin/sh":
                spawned.append(argv)
                log_streams.append(kwargs["stdout"])
                return proc
            return actual_popen(argv, *args, **kwargs)

        with mock.patch.object(runjob, "HOME", home), \
             mock.patch.object(runjob, "load_credentials"), \
             mock.patch.object(runjob, "resolve_engine_binary", side_effect=lambda c, e: c), \
             mock.patch.object(runjob, "log_event"), \
             mock.patch.object(runjob, "_classify_and_log", return_value="done"), \
             mock.patch.object(runjob, "_tail_print"), \
             mock.patch.object(runjob.subprocess, "Popen", side_effect=capture_engine_spawn):
            self.assertEqual(runjob.cmd_run(args), 0)
        for stream in log_streams:
            stream.close()
        # /bin/sh -c WRAPPER runjob-wrapper <engine argv...>
        self.assertEqual(len(spawned), 1)
        return spawned[0][4:]

    def launched_grok(self, cwd: Path, home: Path) -> tuple[list[str], Path]:
        """Capture Grok's argv and the cwd of the wrapped engine spawn."""
        args = argparse.Namespace(
            engine="grok", cwd=str(cwd), model="", effort="", prompt="prompt",
            file=None, title="", wait=True)
        proc = mock.Mock(pid=12345)
        proc.wait.return_value = 0
        actual_popen = subprocess.Popen
        spawned: list[tuple[list[str], Path]] = []
        log_streams = []

        def capture_engine_spawn(argv, *args, **kwargs):
            if argv[0] == "/bin/sh":
                spawned.append((argv, Path(kwargs["cwd"])))
                log_streams.append(kwargs["stdout"])
                return proc
            return actual_popen(argv, *args, **kwargs)

        with mock.patch.object(runjob, "HOME", home), \
             mock.patch.object(runjob, "load_credentials"), \
             mock.patch.object(runjob, "resolve_engine_binary", side_effect=lambda c, e: c), \
             mock.patch.object(runjob, "log_event"), \
             mock.patch.object(runjob, "_classify_and_log", return_value="done"), \
             mock.patch.object(runjob, "_tail_print"), \
             mock.patch.object(runjob.subprocess, "Popen", side_effect=capture_engine_spawn):
            self.assertEqual(runjob.cmd_run(args), 0)
        for stream in log_streams:
            stream.close()
        self.assertEqual(len(spawned), 1)
        # /bin/sh -c WRAPPER runjob-wrapper <engine argv...>
        return spawned[0][0][4:], spawned[0][1]

    def test_ordinary_repo_argv_is_byte_identical_to_existing_path(self):
        """Normal repos must keep main's codex and grok argv exactly unchanged."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "normal"
            root.mkdir()
            self.git(root, "init")
            common = runjob.linked_worktree_common_dir(root, root)
            self.assertIsNone(common)

            legacy_codex = runjob.build_command(
                "codex", "", "", "prompt", repo_root=root)[0]
            new_codex = runjob.build_command(
                "codex", "", "", "prompt", repo_root=root,
                common_git_dir=common)[0]
            self.assertEqual(new_codex, legacy_codex)

            legacy_grok = runjob.build_command("grok", "", "", "prompt")[0]
            new_grok = runjob.build_command(
                "grok", "", "", "prompt",
                grok_sandbox="runjob" if common else "workspace")[0]
            self.assertEqual(new_grok, legacy_grok)

    def test_linked_worktree_grants_common_dir_to_codex_and_grok(self):
        with tempfile.TemporaryDirectory() as td:
            main, worktree = self.linked_worktree(td)
            project_root = runjob.find_project_root(worktree)
            common = runjob.linked_worktree_common_dir(worktree, project_root)
            self.assertEqual(project_root, worktree.resolve())
            self.assertEqual(common, (main / ".git").resolve())

            codex = self.launched_argv("codex", worktree, Path(td) / "home")
            setting = next(arg for arg in codex
                           if arg.startswith("sandbox_workspace_write.writable_roots="))
            writable_roots = json.loads(setting.split("=", 1)[1])
            self.assertIn(str(common), writable_roots)

            grok = self.launched_argv("grok", worktree, Path(td) / "home")
            profile = project_root / ".grok" / "sandbox.toml"
            self.assertEqual(grok[grok.index("--sandbox") + 1], "runjob")
            self.assertIn(f'read_write = ["{common}"]',
                          profile.read_text(encoding="utf-8"))

            # This is the lock parent a sandbox must grant for `git commit`.
            self.assertEqual((common / "index.lock").parent, common)

    def test_subdirectory_of_linked_worktree_resolves_same_common_dir(self):
        with tempfile.TemporaryDirectory() as td:
            main, worktree = self.linked_worktree(td)
            nested = worktree / "src" / "nested"
            nested.mkdir(parents=True)
            project_root = runjob.find_project_root(nested)
            common = runjob.linked_worktree_common_dir(nested, project_root)

        self.assertEqual(project_root, worktree.resolve())
        self.assertEqual(common, (main / ".git").resolve())

    def test_nested_grok_launch_writes_profile_at_spawn_cwd(self):
        """A nested linked-worktree launch must find runjob's profile from its cwd."""
        with tempfile.TemporaryDirectory() as td:
            main, worktree = self.linked_worktree(td)
            nested = worktree / "src" / "nested"
            nested.mkdir(parents=True)

            grok, spawn_cwd = self.launched_grok(nested, Path(td) / "home")
            profile = nested / ".grok" / "sandbox.toml"

            self.assertEqual(spawn_cwd, nested.resolve())
            self.assertEqual(grok[grok.index("--sandbox") + 1], "runjob")
            self.assertTrue(profile.is_file())
            self.assertIn(f'read_write = ["{(main / ".git").resolve()}"]',
                          profile.read_text(encoding="utf-8"))
            # A .grok/ pattern without a leading slash excludes nested profiles.
            ignored = self.git(worktree, "check-ignore", "-q",
                               "src/nested/.grok/sandbox.toml")
            self.assertEqual(ignored.returncode, 0)

    def test_unresolved_worktree_common_dir_warns_before_normal_grant(self):
        with tempfile.TemporaryDirectory() as td:
            _main, worktree = self.linked_worktree(td)
            args = argparse.Namespace(
                engine="codex", cwd=str(worktree), model="", effort="", prompt="prompt",
                file=None, title="", wait=True)
            proc = mock.Mock(pid=12345)
            proc.wait.return_value = 0
            stderr = io.StringIO()

            with mock.patch.object(runjob, "HOME", Path(td) / "home"), \
                 mock.patch.object(runjob, "load_credentials"), \
                 mock.patch.object(runjob, "linked_worktree_common_dir", return_value=None), \
                 mock.patch.object(runjob, "resolve_engine_binary", side_effect=lambda c, e: c), \
                 mock.patch.object(runjob, "log_event"), \
                 mock.patch.object(runjob, "_classify_and_log", return_value="done"), \
                 mock.patch.object(runjob, "_tail_print"), \
                 mock.patch.object(runjob.subprocess, "Popen", return_value=proc) as popen, \
                 mock.patch("sys.stderr", stderr):
                self.assertEqual(runjob.cmd_run(args), 0)
            popen.call_args.kwargs["stdout"].close()

            self.assertIn(".git is a worktree pointer but its common git directory "
                          "could not be resolved", stderr.getvalue())


# --- reap protocol: trailer parse, report subcommand, --active filter --------

_SAMPLE_TRAILER = (
    "STATUS: COMPLETE\n"
    "CAUSE_CLASS: NONE\n"
    "TESTS: 81 passed / 0 failed\n"
    "RESIDUAL: 0\n"
    "COMMIT: OK + abcdef1\n"
    "MODIFIED_PATHS: runjob.py,docs/job-report-protocol.md\n"
    "EVIDENCE_PATH: docs/gates/example.md\n"
)


def _trailer_fields(text: str, **kwargs) -> dict[str, str]:
    """Assert present and return fields (test helper)."""
    got = runjob.parse_report_trailer(text, **kwargs)
    assert got.state == runjob.TRAILER_PRESENT, (
        f"expected present, got {got.state} reason={got.reason}")
    assert got.fields is not None
    return got.fields


class JobReportProtocolDocTests(unittest.TestCase):
    """S1 — protocol document exists and pins the frozen key set."""

    def test_protocol_doc_lists_keys_in_order(self):
        doc = (Path(runjob.__file__).resolve().parent
               / "docs" / "job-report-protocol.md")
        self.assertTrue(doc.is_file(), "docs/job-report-protocol.md must exist")
        text = doc.read_text(encoding="utf-8")
        self.assertLess(len(text.splitlines()), 100, "keep the protocol under 100 lines")
        # Keys appear in the frozen order in the doc body.
        positions = [text.index(f"{k}:") for k in runjob.REPORT_TRAILER_KEYS]
        self.assertEqual(positions, sorted(positions))
        for status in runjob.REPORT_TRAILER_STATUSES:
            self.assertIn(status, text)
        self.assertIn("case-sensitive", text.lower())
        self.assertIn("eof-anchored", text.lower())
        self.assertIn("malformed", text.lower())
        self.assertIn("window_clipped", text)


class ReportTrailerParseTests(unittest.TestCase):
    """S2 — EOF-anchored parse; loud malformed / window_clipped states."""

    def test_parses_complete_trailer(self):
        fields = _trailer_fields("prose before\n\n" + _SAMPLE_TRAILER)
        self.assertEqual(fields["STATUS"], "COMPLETE")
        self.assertEqual(fields["CAUSE_CLASS"], "NONE")
        self.assertEqual(fields["TESTS"], "81 passed / 0 failed")
        self.assertEqual(fields["RESIDUAL"], "0")
        self.assertEqual(fields["COMMIT"], "OK + abcdef1")
        self.assertEqual(fields["MODIFIED_PATHS"],
                         "runjob.py,docs/job-report-protocol.md")
        self.assertEqual(fields["EVIDENCE_PATH"], "docs/gates/example.md")

    def test_real_trailer_after_quoted_template_is_present(self):
        """A real EOF trailer wins; earlier quoted template is ignored."""
        quoted = (
            "Template to end with:\n"
            "STATUS: BLOCKED\n"
            "CAUSE_CLASS: example\n"
            "TESTS: NONE\n"
            "RESIDUAL: 99\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
            "\n"
            "...agent work...\n"
            "\n"
            + _SAMPLE_TRAILER
        )
        fields = _trailer_fields(quoted)
        self.assertEqual(fields["STATUS"], "COMPLETE")
        self.assertEqual(fields["RESIDUAL"], "0")
        self.assertNotEqual(fields["STATUS"], "BLOCKED")

    def test_missing_trailer_is_absent(self):
        got = runjob.parse_report_trailer(
            "lots of tool output\nDONE: finished\n===== exit:0\n")
        self.assertEqual(got.state, runjob.TRAILER_ABSENT)
        self.assertIsNone(got.fields)

    def test_malformed_status_is_malformed(self):
        bad = _SAMPLE_TRAILER.replace("STATUS: COMPLETE", "STATUS: SUCCESS")
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "bad-status")

    def test_wrong_key_order_is_malformed(self):
        shuffled = (
            "CAUSE_CLASS: NONE\n"
            "STATUS: COMPLETE\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        got = runjob.parse_report_trailer(shuffled)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_case_sensitive_keys_absent_when_end_is_noise(self):
        # Lowercased keys at EOF do not form a candidate block of KEY: lines
        # under strict split — still loud if STATUS-shaped; here end looks
        # like free text after strip of exit? Actually "status: COMPLETE"...
        lower = _SAMPLE_TRAILER.replace("STATUS:", "status:")
        got = runjob.parse_report_trailer(lower)
        # First line is status: not STATUS: — may be absent or malformed.
        self.assertIn(got.state, {
            runjob.TRAILER_ABSENT, runjob.TRAILER_MALFORMED})
        self.assertNotEqual(got.state, runjob.TRAILER_PRESENT)

    # --- A1 probe matrix (refute 2026-08-06) ---------------------------------

    def test_a1_continuation_lines_are_malformed(self):
        body = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: NONE\n"
            "RESIDUAL:\n"
            "  still wrapping\n"
            "  more wrap\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_a1_space_before_status_is_malformed(self):
        bad = " " + _SAMPLE_TRAILER
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "leading-whitespace")

    def test_a1_space_before_colon_is_malformed(self):
        bad = _SAMPLE_TRAILER.replace("STATUS:", "STATUS :")
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_a1_crlf_line_endings_present(self):
        crlf = _SAMPLE_TRAILER.replace("\n", "\r\n")
        fields = _trailer_fields(crlf)
        self.assertEqual(fields["STATUS"], "COMPLETE")

    def test_a1_colon_inside_cause_class_is_malformed(self):
        body = _SAMPLE_TRAILER.replace(
            "CAUSE_CLASS: NONE", "CAUSE_CLASS: tool:error")
        got = runjob.parse_report_trailer(body)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "bad-cause_class")

    def test_a1_ansi_around_status_is_malformed(self):
        bad = _SAMPLE_TRAILER.replace(
            "STATUS: COMPLETE", "\x1b[32mSTATUS: COMPLETE\x1b[0m")
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "control-chars")

    def test_a1_blank_line_inside_block_is_malformed(self):
        # Exactly seven logical slots with a blank where a key must be.
        body = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: NONE\n"
            "\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "blank-line")

    def test_a1_truncated_during_seventh_line_is_malformed(self):
        # Six full keys + cut-off seventh — incomplete block at EOF.
        partial = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: docs/ga"
        )  # no trailing newline; truncated path
        got = runjob.parse_report_trailer(partial)
        # Path may still match _RE_REPO_PATH ("docs/ga") — force real truncate:
        partial2 = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PAT"
        )
        got = runjob.parse_report_trailer(partial2)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_a1_duplicate_status_before_full_block_present(self):
        body = "STATUS: FAILED\n" + _SAMPLE_TRAILER
        fields = _trailer_fields(body)
        self.assertEqual(fields["STATUS"], "COMPLETE")

    def test_a1_duplicate_residual_inside_block_malformed(self):
        body = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "RESIDUAL: 1\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_a1_two_complete_blocks_last_at_eof_wins(self):
        body = (
            "STATUS: FAILED\n"
            "CAUSE_CLASS: first\n"
            "TESTS: NONE\n"
            "RESIDUAL: 1\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
            "\n"
            + _SAMPLE_TRAILER
        )
        fields = _trailer_fields(body)
        self.assertEqual(fields["STATUS"], "COMPLETE")

    def test_a1_complete_old_then_partial_retry_is_malformed(self):
        """Incomplete later frame must not fall back to an older complete block."""
        body = (
            _SAMPLE_TRAILER
            + "STATUS: FAILED\n"
            "CAUSE_CLASS: retry\n"
            "TESTS: NONE\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertNotEqual(
            got.state, runjob.TRAILER_PRESENT,
            "stale COMPLETE must not win over incomplete retry")

    def test_a1_invalid_scalar_values_are_malformed(self):
        """Every key is validated — not only STATUS (refute B1)."""
        bad = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: bananas\n"
            "RESIDUAL: -999\n"
            "COMMIT: OK + definitely-not-a-sha\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "bad-tests")

    def test_a1_bad_residual_alone_is_malformed(self):
        bad = _SAMPLE_TRAILER.replace("RESIDUAL: 0", "RESIDUAL: -999")
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "bad-residual")

    def test_a1_bad_commit_sha_is_malformed(self):
        bad = _SAMPLE_TRAILER.replace(
            "COMMIT: OK + abcdef1", "COMMIT: OK + definitely-not-a-sha")
        got = runjob.parse_report_trailer(bad)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "bad-commit")

    def test_wrapper_exit_line_after_trailer_still_present(self):
        body = _SAMPLE_TRAILER + "===== exit:0\n"
        fields = _trailer_fields(body)
        self.assertEqual(fields["STATUS"], "COMPLETE")

    # --- B2 forgery: quotes / heredocs must not adjudicate -------------------

    def test_b2_markdown_fenced_quote_not_accepted(self):
        body = (
            "Prompt says emit exactly:\n"
            "```\n"
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: 95 passed / 0 failed\n"
            "RESIDUAL: 0\n"
            "COMMIT: OK + abc1234\n"
            "MODIFIED_PATHS: runjob.py\n"
            "EVIDENCE_PATH: docs/report.md\n"
            "```\n"
            "No report was emitted.\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertNotEqual(got.state, runjob.TRAILER_PRESENT)
        self.assertIn(got.state, {
            runjob.TRAILER_ABSENT, runjob.TRAILER_MALFORMED})

    def test_b2_heredoc_quote_not_accepted(self):
        body = (
            "cat <<'EOF'\n"
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: forged\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
            "EOF\n"
            "tool output continues here\n"
        )
        got = runjob.parse_report_trailer(body)
        self.assertNotEqual(got.state, runjob.TRAILER_PRESENT)

    def test_b2_quoted_in_tail_real_outside_window_not_present(self):
        """Bounded tail with only a quote must not return the quoted STATUS."""
        real = (
            "STATUS: FAILED\n"
            "CAUSE_CLASS: real-failure\n"
            "TESTS: NONE\n"
            "RESIDUAL: 1\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
        )
        noise = "n" * 5000
        quote = (
            "example only:\n"
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: quoted-example\n"
            "TESTS: NONE\n"
            "RESIDUAL: 0\n"
            "COMMIT: NONE\n"
            "MODIFIED_PATHS: NONE\n"
            "EVIDENCE_PATH: NONE\n"
            "No actual retry report was emitted.\n"
        )
        full = real + noise + quote
        # Simulate default 4096-byte tail.
        raw = full.encode("utf-8")
        tail = raw[-4096:].decode("utf-8", errors="replace")
        skipped = len(raw) - 4096
        got = runjob.parse_report_trailer(tail, bytes_skipped=skipped)
        self.assertNotEqual(got.state, runjob.TRAILER_PRESENT)
        if got.fields:
            self.assertNotEqual(got.fields.get("STATUS"), "COMPLETE")

    # --- B1 window-clipped ---------------------------------------------------

    def test_b1_window_clipped_when_tail_starts_mid_block(self):
        """Default 4KiB tail that begins after STATUS is window_clipped, not absent."""
        paths = ",".join(f"pkg/module_{i:03d}.py" for i in range(400))
        big = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: 95 passed / 0 failed\n"
            "RESIDUAL: 0\n"
            "COMMIT: OK + abcdef12\n"
            f"MODIFIED_PATHS: {paths}\n"
            "EVIDENCE_PATH: docs/report.md\n"
        )
        raw = big.encode("utf-8")
        self.assertGreater(len(raw), runjob.DEFAULT_REPORT_BYTES)
        self.assertLess(len(raw), runjob.MAX_REPORT_TRAILER_BYTES)
        # Fully visible and under the independent trailer cap → present.
        full = runjob.parse_report_trailer(big)
        self.assertEqual(full.state, runjob.TRAILER_PRESENT)
        self.assertIsNotNone(full.fields)
        assert full.fields is not None
        self.assertEqual(full.fields["STATUS"], "COMPLETE")
        # Default read window still clips mid-block → window_clipped, not absent.
        tail = raw[-runjob.DEFAULT_REPORT_BYTES:].decode("utf-8", errors="replace")
        got = runjob.parse_report_trailer(
            tail, bytes_skipped=len(raw) - runjob.DEFAULT_REPORT_BYTES)
        self.assertEqual(got.state, runjob.TRAILER_WINDOW_CLIPPED)
        self.assertNotEqual(got.state, runjob.TRAILER_ABSENT)

    def test_b1_widened_read_recovers_trailer_between_window_and_cap(self):
        """Trailer > default window and < MAX_REPORT_TRAILER_BYTES.

        Default read reports window_clipped; a widened read recovers present.
        """
        # 100 deep paths: larger than 4 KiB, smaller than the 32 KiB cap.
        paths = ",".join(
            f"src/package/subpkg/module_name_{i:03d}/implementation.py"
            for i in range(100))
        body = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: 100 passed / 0 failed\n"
            "RESIDUAL: 0\n"
            "COMMIT: OK + abcdef12\n"
            f"MODIFIED_PATHS: {paths}\n"
            "EVIDENCE_PATH: docs/report.md\n"
        )
        raw = body.encode("utf-8")
        self.assertGreater(len(raw), runjob.DEFAULT_REPORT_BYTES)
        self.assertLess(len(raw), runjob.MAX_REPORT_TRAILER_BYTES)

        default_tail = raw[-runjob.DEFAULT_REPORT_BYTES:].decode(
            "utf-8", errors="replace")
        clipped = runjob.parse_report_trailer(
            default_tail,
            bytes_skipped=len(raw) - runjob.DEFAULT_REPORT_BYTES)
        self.assertEqual(clipped.state, runjob.TRAILER_WINDOW_CLIPPED)
        self.assertNotEqual(clipped.state, runjob.TRAILER_PRESENT)

        widened = runjob.parse_report_trailer(body, bytes_skipped=0)
        self.assertEqual(widened.state, runjob.TRAILER_PRESENT)
        assert widened.fields is not None
        self.assertEqual(widened.fields["STATUS"], "COMPLETE")
        self.assertEqual(widened.fields["RESIDUAL"], "0")
        self.assertTrue(widened.fields["MODIFIED_PATHS"].startswith(
            "src/package/subpkg/module_name_000/implementation.py"))

    def test_b1_genuinely_over_cap_still_trailer_too_large(self):
        """A trailer larger than MAX_REPORT_TRAILER_BYTES stays malformed."""
        # Inflate MODIFIED_PATHS until encoded size exceeds the independent cap.
        n = 2000
        paths = ",".join(f"pkg/module_{i:04d}.py" for i in range(n))
        body = (
            "STATUS: COMPLETE\n"
            "CAUSE_CLASS: NONE\n"
            "TESTS: 1 passed / 0 failed\n"
            "RESIDUAL: 0\n"
            "COMMIT: OK + abcdef12\n"
            f"MODIFIED_PATHS: {paths}\n"
            "EVIDENCE_PATH: docs/report.md\n"
        )
        raw = body.encode("utf-8")
        self.assertGreater(len(raw), runjob.MAX_REPORT_TRAILER_BYTES)
        got = runjob.parse_report_trailer(body, bytes_skipped=0)
        self.assertEqual(got.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(got.reason, "trailer-too-large")




class CmdReportTests(unittest.TestCase):
    """S2 — `runjob report <uid>` text + --json modes."""

    def _ledger_with_log(self, home: Path, uid: str, body: str,
                         engine: str = "codex",
                         create_log: bool = True) -> Path:
        job_log = home / "logs" / f"{uid}.log"
        job_log.parent.mkdir(parents=True, exist_ok=True)
        if create_log:
            job_log.write_text(body, encoding="utf-8")
        ledger = home / "jobs.jsonl"
        row = {
            "event": "done", "uid": uid, "status": "exited",
            "engine": engine, "model": "gpt-5.6-sol",
            "project_name": "proj", "log": str(job_log),
            "ts": "2026-08-06T12:00:00+10:00",
        }
        ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return ledger

    def test_report_text_prints_header_and_fields_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            prefix = ("x" * 5000) + "\n"
            body = prefix + _SAMPLE_TRAILER
            ledger = self._ledger_with_log(home, "big-codex-uid", body)
            total = len(body.encode("utf-8"))
            args = argparse.Namespace(
                uid="big-codex-uid", bytes=4096, json=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("uid=big-codex-uid", text)
        self.assertIn("engine=codex", text)
        self.assertIn("trailer_state=present", text)
        skipped = int(re.search(r"bytes_skipped=(\d+)", text).group(1))
        self.assertEqual(skipped, total - 4096)
        self.assertGreater(skipped, 0)
        self.assertIn("STATUS: COMPLETE", text)

    def test_report_json_with_trailer(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = self._ledger_with_log(
                home, "trailer-uid", "work done\n" + _SAMPLE_TRAILER)
            args = argparse.Namespace(
                uid="trailer-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(obj["trailer_present"])
        self.assertEqual(obj["trailer_state"], "present")
        self.assertEqual(obj["STATUS"], "COMPLETE")
        self.assertEqual(obj["RESIDUAL"], "0")
        self.assertNotIn("tail", obj)

    def test_report_json_missing_trailer_returns_nulls_and_tail(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            body = "tool noise\nno structured trailer here\n===== exit:0\n"
            ledger = self._ledger_with_log(home, "no-trailer-uid", body)
            args = argparse.Namespace(
                uid="no-trailer-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertFalse(obj["trailer_present"])
        self.assertEqual(obj["trailer_state"], "absent")
        for k in runjob.REPORT_TRAILER_KEYS:
            self.assertIsNone(obj[k], f"{k} must be null when trailer missing")
        self.assertIn("tail", obj)
        self.assertIn("no structured trailer", obj["tail"])
        self.assertIsNone(obj["STATUS"])

    def test_report_json_last_trailer_wins(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            body = (
                "STATUS: FAILED\n"
                "CAUSE_CLASS: quoted\n"
                "TESTS: NONE\n"
                "RESIDUAL: 1\n"
                "COMMIT: NONE\n"
                "MODIFIED_PATHS: NONE\n"
                "EVIDENCE_PATH: NONE\n"
                "\nreal end:\n"
                + _SAMPLE_TRAILER
            )
            ledger = self._ledger_with_log(home, "last-wins-uid", body)
            args = argparse.Namespace(
                uid="last-wins-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(obj["trailer_present"])
        self.assertEqual(obj["STATUS"], "COMPLETE")
        self.assertEqual(obj["CAUSE_CLASS"], "NONE")

    def test_report_json_malformed_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bad = _SAMPLE_TRAILER.replace("STATUS: COMPLETE", "STATUS: SUCCESS")
            ledger = self._ledger_with_log(home, "bad-uid", bad)
            args = argparse.Namespace(uid="bad-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 3)
        self.assertEqual(obj["trailer_state"], "malformed")
        self.assertFalse(obj["trailer_present"])
        self.assertIn("tail", obj)

    def test_report_json_missing_log_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = self._ledger_with_log(
                home, "gone-uid", "", create_log=False)
            # ledger points at logs/gone-uid.log which was not created
            args = argparse.Namespace(uid="gone-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(obj["trailer_state"], "missing_log")
        self.assertFalse(obj["trailer_present"])

    def test_report_json_window_clipped_exits_4(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            paths = ",".join(f"pkg/m_{i:03d}.py" for i in range(400))
            body = (
                "STATUS: COMPLETE\n"
                "CAUSE_CLASS: NONE\n"
                "TESTS: 1 passed / 0 failed\n"
                "RESIDUAL: 0\n"
                "COMMIT: OK + abcd1234\n"
                f"MODIFIED_PATHS: {paths}\n"
                "EVIDENCE_PATH: docs/x.md\n"
            )
            ledger = self._ledger_with_log(home, "clip-uid", body)
            args = argparse.Namespace(uid="clip-uid", bytes=4096, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        self.assertEqual(rc, 4)
        self.assertEqual(obj["trailer_state"], "window_clipped")
        self.assertFalse(obj["trailer_present"])

    def test_report_bytes_zero_is_empty_window_not_default(self):
        """R1 — --bytes 0 means 0 bytes, not a silent fallback to 4096."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = self._ledger_with_log(
                home, "z-uid", "work\n" + _SAMPLE_TRAILER)
            args = argparse.Namespace(uid="z-uid", bytes=0, json=True)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            obj = json.loads(out.getvalue())
        # Empty window cannot see the trailer.
        self.assertFalse(obj["trailer_present"])
        self.assertEqual(obj.get("tail", ""), "")
        self.assertGreater(obj["bytes_skipped"], 0)
        self.assertEqual(obj["trailer_state"], "window_clipped")
        self.assertEqual(rc, 4)

    def test_report_unknown_uid_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            ledger.write_text(json.dumps({
                "event": "launch", "uid": "real-uid", "status": "running",
                "engine": "grok", "ts": "2026-08-06T12:00:00+10:00",
            }) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid="missing-uid", bytes=4096, json=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO), \
                 mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                rc = runjob.cmd_report(args)
            self.assertEqual(rc, 2)
            self.assertIn("missing-uid", err.getvalue())

    def test_report_text_malformed_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            bad = _SAMPLE_TRAILER.replace("RESIDUAL: 0", "RESIDUAL: nope")
            ledger = self._ledger_with_log(home, "loud-uid", bad)
            args = argparse.Namespace(uid="loud-uid", bytes=4096, json=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_report(args)
            text = out.getvalue()
        self.assertEqual(rc, 3)
        self.assertIn("trailer_state=malformed", text)


class ActiveLogAbandonedFilterTests(unittest.TestCase):
    """S3 — --active hides daggered rows; --include-abandoned restores them."""

    def test_active_excludes_abandoned_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            rows = [
                {"event": "launch", "uid": "live-running", "status": "running",
                 "pid": 111, "engine": "claude", "model": "opus",
                 "project_name": "a", "ts": "2026-08-06T16:00:00+10:00"},
                {"event": "launch", "uid": "dead-running", "status": "running",
                 "pid": 2668, "engine": "claude", "model": "opus",
                 "project_name": "b", "ts": "2026-08-03T08:03:14+10:00"},
                {"event": "limited", "uid": "abandoned-limited",
                 "status": "limited", "pid": 999, "engine": "grok",
                 "model": "grok-4.5", "project_name": "c",
                 "retry_at": "2026-08-03T12:00:00+10:00",
                 "ts": "2026-08-03T11:00:00+10:00"},
                {"event": "limited", "uid": "parked-limited",
                 "status": "limited", "pid": 222, "engine": "grok",
                 "model": "grok-4.5", "project_name": "d",
                 "retry_at": "2099-01-01T00:00:00+10:00",
                 "ts": "2026-08-06T15:00:00+10:00"},
            ]
            ledger.write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

            def liveness(pid, launched_at=None):
                if pid in (111, 222):
                    return runjob.LIVENESS_ALIVE
                return runjob.LIVENESS_DEAD

            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   side_effect=liveness), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("live-running", text)
        self.assertIn("parked-limited", text)
        self.assertNotIn("dead-running", text)
        self.assertNotIn("abandoned-limited", text)
        self.assertNotIn("running†", text)
        self.assertNotIn("limited†", text)

    def test_include_abandoned_restores_daggered_rows(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            rows = [
                {"event": "launch", "uid": "live-running", "status": "running",
                 "pid": 111, "engine": "claude", "model": "opus",
                 "project_name": "a", "ts": "2026-08-06T16:00:00+10:00"},
                {"event": "launch", "uid": "dead-running", "status": "running",
                 "pid": 2668, "engine": "claude", "model": "opus",
                 "project_name": "b", "ts": "2026-08-03T08:03:14+10:00"},
            ]
            ledger.write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

            def liveness(pid, launched_at=None):
                return (runjob.LIVENESS_ALIVE if pid == 111
                        else runjob.LIVENESS_DEAD)

            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=True,
                follow=False, limit=40, outcome=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   side_effect=liveness), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                runjob.cmd_log(args)
            text = out.getvalue()
        self.assertIn("live-running", text)
        self.assertIn("dead-running", text)
        self.assertIn("running†", text)

    def test_b3_empty_ps_after_kill0_does_not_hide_live_job(self):
        """B3 — uncertain ps must show-and-flag, never omit from --active."""
        child = subprocess.Popen(["sleep", "60"])
        try:
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                ledger = home / "jobs.jsonl"
                row = {
                    "event": "launch", "uid": "uncertain-live",
                    "status": "running", "pid": child.pid,
                    "engine": "claude", "model": "opus",
                    "project_name": "a",
                    "ts": "2026-08-06T16:00:00+10:00",
                }
                ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

                def fake_ps(cmd, **kwargs):
                    # ps -p PID -o stat= → empty (uncertain)
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

                args = argparse.Namespace(
                    uid=None, active=True, include_abandoned=False,
                    follow=False, limit=40, outcome=False)
                with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                     mock.patch.object(runjob, "HOME", home), \
                     mock.patch.object(runjob.subprocess, "run",
                                       side_effect=fake_ps), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    # kill(0) is real against the sleep child.
                    rc = runjob.cmd_log(args)
                text = out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("uncertain-live", text,
                          "live job must remain visible under --active")
            self.assertIn("running?", text,
                          "uncertain liveness must be flagged, not silent")
            self.assertNotIn("running†", text)
        finally:
            child.kill()
            child.wait(timeout=5)

    def test_b3_process_liveness_empty_ps_is_unknown(self):
        with mock.patch.object(runjob.os, "kill", return_value=None), \
             mock.patch.object(runjob.subprocess, "run") as ps_run:
            ps_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="")
            liv = runjob.process_liveness(4242)
        self.assertEqual(liv, runjob.LIVENESS_UNKNOWN)
        with mock.patch.object(runjob.os, "kill", return_value=None), \
             mock.patch.object(runjob.subprocess, "run") as ps_run:
            ps_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="")
            self.assertTrue(
                runjob.process_is_alive(4242),
                "UNKNOWN must fail open for bool callers")


class LivenessFloorTests(unittest.TestCase):
    """Activity floor: stalled vs healthy-slow vs just-launched.

    Rule (fix1 / gate B1): stall requires age + banner-scale log + absolute
    tree CPU ≤ STALL_CPU_SECONDS + near-zero CPU *delta* over the sample
    window. Engines buffer stdout, so in-flight logs stay at the banner;
    absolute+delta CPU is the healthy-work discriminator.

    Real anchors:
      stalled:  fin-file-alerts-codex-7086f499 — 60 B banner, 0:00.00 CPU,
                ~60 min alive before kill (ledger: launch 06:05, failed 07:04)
      stalled:  bc-step-floor-codex-6ae98357 — 64 B banner, 0:00.00 CPU, ~178 min
      slow ok:  thinking hard (banner log, high abs CPU / growing delta)
      wait ok:  engine started then blocked on I/O (abs ~0.5–0.7 s > floor)
      young:    any job seconds old with only the launch banner — not stalled

    Mutation proofs: every threshold is broken in isolation and a named test
    reds. A threshold whose mutation leaves the suite green is decoration.
    """

    # --- pure helpers --------------------------------------------------------

    def test_parse_ps_cputime_macos_and_linux(self):
        self.assertEqual(runjob.parse_ps_cputime("0:00.00"), 0.0)
        self.assertAlmostEqual(runjob.parse_ps_cputime("0:01.50"), 1.5)
        self.assertEqual(runjob.parse_ps_cputime("1:02:03"), 3723.0)
        self.assertEqual(runjob.parse_ps_cputime("00:00:01"), 1.0)
        self.assertEqual(runjob.parse_ps_cputime("1-02:03:04"), 93784.0)
        self.assertIsNone(runjob.parse_ps_cputime(""))
        self.assertIsNone(runjob.parse_ps_cputime("not-a-time"))

    def test_process_tree_cputime_sums_descendants(self):
        """Wrapper shell at 0:00.00 + engine child at 0:05.00 → 5.0 s."""
        table = (
            "  100   1  0:00.00\n"   # wrapper (ledger pid)
            "  101 100  0:05.00\n"   # engine child
            "  200   1  9:99.99\n"   # unrelated
        )
        with mock.patch.object(runjob.subprocess, "run") as ps_run:
            ps_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=table, stderr="")
            total = runjob.process_tree_cputime_seconds(100)
        self.assertAlmostEqual(total, 5.0)

    def test_process_tree_cputime_missing_root_is_none(self):
        with mock.patch.object(runjob.subprocess, "run") as ps_run:
            ps_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="  200   1  0:01.00\n", stderr="")
            self.assertIsNone(runjob.process_tree_cputime_seconds(100))

    def test_process_tree_cpu_delta_samples_twice_over_window(self):
        """Delta observation: two tree-CPU samples separated by the window."""
        samples = [0.10, 0.30]
        with mock.patch.object(
                runjob, "process_tree_cputime_seconds",
                side_effect=lambda pid: samples.pop(0)), \
             mock.patch.object(runjob.time, "sleep") as slept:
            out = runjob.process_tree_cpu_delta_seconds(100)
        self.assertIsNotNone(out)
        cpu, delta = out
        self.assertAlmostEqual(cpu, 0.30)
        self.assertAlmostEqual(delta, 0.20)
        slept.assert_called_once_with(runjob.STALL_CPU_WINDOW_SECONDS)

    def test_process_tree_cpu_delta_fails_open_on_missing_sample(self):
        with mock.patch.object(
                runjob, "process_tree_cputime_seconds",
                side_effect=[0.0, None]), \
             mock.patch.object(runjob.time, "sleep"):
            self.assertIsNone(runjob.process_tree_cpu_delta_seconds(100))

    def test_process_tree_cpu_window_samples_one_sleep_for_many_pids(self):
        """B3: N candidate pids share one window sleep, not N."""
        # Two ps snapshots: first all at 0.10, second all at 0.30.
        tables = [
            ({1: [100, 200]}, {100: 0.10, 200: 0.10, 1: 0.0}),
            ({1: [100, 200]}, {100: 0.30, 200: 0.30, 1: 0.0}),
        ]

        def fake_read():
            return tables.pop(0)

        with mock.patch.object(
                runjob, "_read_ps_cputime_table", side_effect=fake_read), \
             mock.patch.object(runjob.time, "sleep") as slept:
            out = runjob.process_tree_cpu_window_samples([100, 200, 100])
        self.assertEqual(set(out), {100, 200})
        self.assertAlmostEqual(out[100][0], 0.30)
        self.assertAlmostEqual(out[100][1], 0.20)
        self.assertAlmostEqual(out[200][0], 0.30)
        self.assertAlmostEqual(out[200][1], 0.20)
        slept.assert_called_once_with(runjob.STALL_CPU_WINDOW_SECONDS)

    def test_process_tree_cpu_window_samples_empty_no_sleep(self):
        with mock.patch.object(runjob.time, "sleep") as slept:
            self.assertEqual(runjob.process_tree_cpu_window_samples([]), {})
        slept.assert_not_called()

    def test_job_log_byte_size_missing_file_is_none(self):
        """B2: missing log file is unobservable, not size 0."""
        self.assertIsNone(
            runjob.job_log_byte_size("/no/such/runjob-log-does-not-exist.log"))
        self.assertIsNone(runjob.job_log_byte_size(None))
        self.assertIsNone(runjob.job_log_byte_size(""))

    def test_job_log_byte_size_existing_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 60)
            path = fh.name
        try:
            self.assertEqual(runjob.job_log_byte_size(path), 60)
        finally:
            os.unlink(path)

    # --- case 1: stalled (the 2026-08-07 defect) -----------------------------

    def test_stalled_header_only_zero_cpu_past_grace(self):
        """fin-file-alerts-codex-7086f499 shape: 60 B, 0 CPU, 0 delta, age 60 min."""
        result = runjob.classify_stall(
            status="running",
            pid=58714,
            launched_at="2026-08-07T06:05:16+10:00",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=3600.0,   # measured wall ~59.5 min
            log_bytes=60,         # banner-only during the stall
            cpu_seconds=0.0,      # 0:00.00 process tree
            cpu_delta=0.0,        # flat across the sample window
        )
        self.assertEqual(result, runjob.STALL_STALLED)

    def test_stalled_bc_step_floor_shape(self):
        """bc-step-floor-codex-6ae98357 shape: 64 B, 0 CPU, 0 delta, age 178 min."""
        result = runjob.classify_stall(
            status="running",
            pid=52623,
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=10663.0,
            log_bytes=64,
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_STALLED)

    def test_mutation_stalled_log_floor_is_load_bearing(self):
        """Break the log-floor conjunct → stalled case no longer fires.

        STALL_LOG_BYTES = 0 makes `60 <= 0` false. If the suite still went
        green, the log floor would not be part of the decision.
        """
        with mock.patch.object(runjob, "STALL_LOG_BYTES", 0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            )
        self.assertEqual(
            result, runjob.STALL_OK,
            "raising the log bar past the banner must clear the stall call")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_STALLED,
        )

    def test_mutation_stalled_cpu_abs_floor_is_load_bearing(self):
        """Break the absolute CPU floor → pure hang no longer fires.

        STALL_CPU_SECONDS = -1 makes `0.0 <= -1` false.
        """
        with mock.patch.object(runjob, "STALL_CPU_SECONDS", -1.0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            )
        self.assertEqual(
            result, runjob.STALL_OK,
            "negative absolute floor must drop the hang detection")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_STALLED,
        )

    def test_mutation_stalled_cpu_delta_floor_is_load_bearing(self):
        """Break the delta floor → pure hang no longer fires.

        STALL_CPU_DELTA_SECONDS = -1 makes `0.0 <= -1` false.
        """
        with mock.patch.object(runjob, "STALL_CPU_DELTA_SECONDS", -1.0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            )
        self.assertEqual(
            result, runjob.STALL_OK,
            "negative delta floor must drop the hang detection")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_STALLED,
        )

    # --- case 2: healthy-but-slow (must NOT stall) ---------------------------

    def test_healthy_slow_thinking_hard_zero_log_growth(self):
        """Thinking hard: banner log, but absolute tree CPU well above floor."""
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=1948.0,   # researcher-claude-cd025ba9 duration
            log_bytes=60,         # still quiet on disk (buffered engines)
            cpu_seconds=120.0,    # minutes of engine CPU
            cpu_delta=0.5,        # still accruing across the window
        )
        self.assertEqual(result, runjob.STALL_OK)

    def test_healthy_engine_started_low_growth_cleared_by_abs(self):
        """Gate B1 shape: engine started (~0.65 s), then blocked on I/O.

        Absolute is above STALL_CPU_SECONDS (0.25) so the row stays running
        even though delta is near zero and the log is still the banner.
        """
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=135.0,
            log_bytes=52,
            cpu_seconds=0.65,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_OK)

    def test_healthy_low_abs_cleared_by_cpu_growth(self):
        """Gray-zone absolute, but tree CPU is growing → not stalled."""
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=130.0,
            log_bytes=60,
            cpu_seconds=0.10,     # still under absolute floor
            cpu_delta=0.20,       # growth clears the delta conjunct
        )
        self.assertEqual(result, runjob.STALL_OK)

    def test_healthy_slow_with_real_log_growth(self):
        """Large log alone clears stall even with zero CPU."""
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=1948.0,
            log_bytes=1915,
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_OK)

    def test_large_log_short_circuits_cpu_sample(self):
        """B3: log over STALL_LOG_BYTES → STALL_OK with zero CPU samples.

        Spy/counter proof — not a timing assertion. A 200 KB log can never
        be stalled, so the 2 s window must not run at all.
        """
        with mock.patch.object(
                runjob, "process_tree_cpu_delta_seconds") as delta_spy, \
             mock.patch.object(
                runjob, "process_tree_cputime_seconds") as tree_spy, \
             mock.patch.object(
                runjob, "process_tree_cpu_window_samples") as win_spy:
            result = runjob.classify_stall(
                status="running",
                pid=100,
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=200_000,  # well over STALL_LOG_BYTES (96)
            )
        self.assertEqual(result, runjob.STALL_OK)
        delta_spy.assert_not_called()
        tree_spy.assert_not_called()
        win_spy.assert_not_called()

    def test_too_young_short_circuits_cpu_sample(self):
        """Grace path must not pay the CPU window either."""
        with mock.patch.object(
                runjob, "process_tree_cpu_delta_seconds") as delta_spy:
            result = runjob.classify_stall(
                status="running",
                pid=100,
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=5.0,
                log_bytes=60,
            )
        self.assertEqual(result, runjob.STALL_TOO_YOUNG)
        delta_spy.assert_not_called()

    def test_not_applicable_short_circuits_cpu_sample(self):
        with mock.patch.object(
                runjob, "process_tree_cpu_delta_seconds") as delta_spy:
            result = runjob.classify_stall(
                status="done",
                pid=100,
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
            )
        self.assertEqual(result, runjob.STALL_NOT_APPLICABLE)
        delta_spy.assert_not_called()

    def test_mutation_healthy_abs_floor_is_load_bearing(self):
        """Raise absolute floor → started-then-blocked job is falsely stalled.

        Gate B1 evidence: abs ~0.65 s with flat delta and banner log. The
        absolute floor is what exempts it; raise the floor and it collapses
        into the hang shape.
        """
        with mock.patch.object(runjob, "STALL_CPU_SECONDS", 10_000.0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=135.0,
                log_bytes=52,
                cpu_seconds=0.65,
                cpu_delta=0.0,
            )
        self.assertEqual(
            result, runjob.STALL_STALLED,
            "without a tight absolute floor, started-engine wait looks stalled")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=135.0,
                log_bytes=52,
                cpu_seconds=0.65,
                cpu_delta=0.0,
            ),
            runjob.STALL_OK,
        )

    def test_mutation_healthy_delta_floor_is_load_bearing(self):
        """Raise delta floor → low-abs growing job is falsely stalled."""
        with mock.patch.object(runjob, "STALL_CPU_DELTA_SECONDS", 10_000.0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=130.0,
                log_bytes=60,
                cpu_seconds=0.10,
                cpu_delta=0.20,
            )
        self.assertEqual(
            result, runjob.STALL_STALLED,
            "without a tight delta floor, growing-CPU work looks stalled")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=130.0,
                log_bytes=60,
                cpu_seconds=0.10,
                cpu_delta=0.20,
            ),
            runjob.STALL_OK,
        )

    def test_mutation_cpu_window_is_load_bearing(self):
        """Zero sample window → delta always 0 → low-abs growing job stalls.

        Live path: tree CPU accrues only across a non-zero window. With
        window=0 both samples read the same value, growth collapses to 0,
        and a job whose only exemption was delta is falsely stalled.
        """
        slept: list[float] = []

        def fake_sleep(seconds):
            slept.append(seconds)

        call = [0]

        def fake_tree(pid):
            call[0] += 1
            if call[0] == 1:
                return 0.10
            # Second sample: growth only if the window was non-zero.
            if slept and slept[0] > 0:
                return 0.30
            return 0.10

        with mock.patch.object(
                runjob, "process_tree_cputime_seconds", side_effect=fake_tree), \
             mock.patch.object(runjob.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(runjob, "STALL_CPU_WINDOW_SECONDS", 0.0):
            result = runjob.classify_stall(
                status="running",
                pid=100,
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=130.0,
                log_bytes=60,
            )
        self.assertEqual(
            result, runjob.STALL_STALLED,
            "zero window must collapse delta and false-stall growing work")

        # Unmutated window: same fake tree shows growth → ok.
        slept.clear()
        call[0] = 0
        with mock.patch.object(
                runjob, "process_tree_cputime_seconds", side_effect=fake_tree), \
             mock.patch.object(runjob.time, "sleep", side_effect=fake_sleep):
            result = runjob.classify_stall(
                status="running",
                pid=100,
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=130.0,
                log_bytes=60,
            )
        self.assertEqual(result, runjob.STALL_OK)
        self.assertEqual(slept, [runjob.STALL_CPU_WINDOW_SECONDS])

    def test_mutation_cpu_window_magnitude_is_load_bearing(self):
        """R4: window magnitude is load-bearing, not only non-zeroness.

        Gate residual: STALL_CPU_WINDOW_SECONDS 2.0 → 0.001 reds nothing when
        tests only check sleep>0. Pin the calibrated magnitude so shrinking
        the window to a near-zero still-nonzero value fails the suite.
        """
        self.assertEqual(
            runjob.STALL_CPU_WINDOW_SECONDS, 2.0,
            "window magnitude must stay at the calibrated 2.0 s; "
            "0.001 is non-zero but too short to observe engine accrual")

    # --- case 3: just-launched (must NOT stall) ------------------------------

    def test_just_launched_tiny_log_zero_cpu_is_not_stalled(self):
        """Seconds-old job: only the launch banner, no CPU yet — not stalled."""
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=5.0,
            log_bytes=58,   # p50 banner size
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_TOO_YOUNG)

    def test_mutation_just_launched_grace_is_load_bearing(self):
        """Break the grace window → a 5 s job is falsely stalled.

        STALL_MIN_AGE_SECONDS = 0 removes the too-young gate. If the suite
        still went green, grace would not be part of the decision.
        """
        with mock.patch.object(runjob, "STALL_MIN_AGE_SECONDS", 0.0):
            result = runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=5.0,
                log_bytes=58,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            )
        self.assertEqual(
            result, runjob.STALL_STALLED,
            "zero grace must reclassify a just-launched banner-only job")
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=5.0,
                log_bytes=58,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_TOO_YOUNG,
        )

    # --- fail-open + non-running ---------------------------------------------

    def test_unobservable_cpu_fails_open(self):
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=3600.0,
            log_bytes=60,
            cpu_seconds=None,  # observe path — but pid is also None
        )
        self.assertEqual(result, runjob.STALL_UNKNOWN)

    def test_missing_log_file_fails_open(self):
        """B2 / bar 5: missing log file → unknown, never stalled."""
        result = runjob.classify_stall(
            status="running",
            pid=58714,
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=3600.0,
            log_path="/no/such/runjob-stall-log-missing.log",
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_UNKNOWN)

    def test_missing_log_field_fails_open(self):
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            age_seconds=3600.0,
            log_path=None,
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_UNKNOWN)

    def test_unparseable_age_fails_open(self):
        result = runjob.classify_stall(
            status="running",
            liveness=runjob.LIVENESS_ALIVE,
            launched_at="not-a-timestamp",
            log_bytes=60,
            cpu_seconds=0.0,
            cpu_delta=0.0,
        )
        self.assertEqual(result, runjob.STALL_UNKNOWN)

    def test_done_status_not_applicable(self):
        self.assertEqual(
            runjob.classify_stall(
                status="done",
                liveness=runjob.LIVENESS_ALIVE,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_NOT_APPLICABLE,
        )

    def test_dead_pid_not_applicable(self):
        self.assertEqual(
            runjob.classify_stall(
                status="running",
                liveness=runjob.LIVENESS_DEAD,
                age_seconds=3600.0,
                log_bytes=60,
                cpu_seconds=0.0,
                cpu_delta=0.0,
            ),
            runjob.STALL_NOT_APPLICABLE,
        )

    # --- cmd_log surfaces stalled; does not write ----------------------------

    def test_cmd_log_surfaces_stalled_distinct_from_running(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            log_path = home / "stall.log"
            # Banner-only log, matching the defect's on-disk shape.
            log_path.write_bytes(
                b"===== 2026-08-07T06:05:16+10:00 launch codex gpt-5.6-terra \n")
            row = {
                "event": "launch",
                "uid": "fin-file-alerts-codex-7086f499",
                "status": "running",
                "pid": 58714,
                "engine": "codex",
                "model": "gpt-5.6-terra",
                "project_name": "fin-file-alerts",
                "log": str(log_path),
                "ts": "2026-08-07T06:05:16+10:00",
            }
            ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=False, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch.object(runjob, "classify_stall",
                                   return_value=runjob.STALL_STALLED), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("stalled", text)
        self.assertIn("fin-file-alerts-codex-7086f499", text)
        self.assertIn("⚠", text)

    def test_cmd_log_stalled_does_not_mutate_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            row = {
                "event": "launch",
                "uid": "stall-uid",
                "status": "running",
                "pid": 4242,
                "engine": "codex",
                "model": "x",
                "project_name": "p",
                "ts": "2026-08-07T06:00:00+10:00",
            }
            before = json.dumps(row) + "\n"
            ledger.write_text(before, encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch.object(runjob, "classify_stall",
                                   return_value=runjob.STALL_STALLED), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                runjob.cmd_log(args)
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)

    def test_cmd_log_active_keeps_stalled_visible(self):
        """Stalled is still process-alive work-in-progress — --active shows it.

        Hiding it would re-create the defect (orchestrator cannot see the hang).
        """
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            row = {
                "event": "launch",
                "uid": "visible-stall",
                "status": "running",
                "pid": 4242,
                "engine": "codex",
                "model": "x",
                "project_name": "p",
                "ts": "2026-08-07T06:00:00+10:00",
            }
            ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch.object(runjob, "classify_stall",
                                   return_value=runjob.STALL_STALLED), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("visible-stall", text)
        self.assertIn("stalled", text)

    def test_cmd_log_batches_cpu_window_once_for_n_running_rows(self):
        """B3: cmd_log with N running candidates takes one shared window."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            lines = []
            for i in range(3):
                log_path = home / f"job{i}.log"
                log_path.write_bytes(b"x" * 60)
                lines.append(json.dumps({
                    "event": "launch",
                    "uid": f"batch-row-{i}",
                    "status": "running",
                    "pid": 1000 + i,
                    "engine": "codex",
                    "model": "x",
                    "project_name": "p",
                    "log": str(log_path),
                    "ts": "2026-01-01T00:00:00+10:00",
                }))
            ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            shared = {
                1000: (0.0, 0.0),
                1001: (0.0, 0.0),
                1002: (0.0, 0.0),
            }
            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE), \
                 mock.patch.object(
                     runjob, "process_tree_cpu_window_samples",
                     return_value=shared) as win_spy, \
                 mock.patch.object(
                     runjob, "process_tree_cpu_delta_seconds") as delta_spy, \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()
        self.assertEqual(rc, 0)
        win_spy.assert_called_once()
        # Shared inject means the per-row live sampler must not run.
        delta_spy.assert_not_called()
        for i in range(3):
            self.assertIn(f"batch-row-{i}", text)
            # hang shape injected → stalled
            self.assertIn("stalled", text)

    def test_cmd_log_prefilter_agrees_with_classify_stall(self):
        """Every row needing CPU must use the one shared listing window.

        ``stall_cpu_sample_pids`` mirrors ``classify_stall``'s cheap
        conjuncts. If the prefilter becomes narrower, or the classifier
        becomes broader, a missing shared sample silently falls back to the
        serial two-second sampler once per affected row. Exercise both age
        and log boundaries, plus an unknown-liveness row: sampling that row is
        benign over-inclusion because cmd_log never classifies it as stalled.
        """
        fixed_now = datetime.fromisoformat("2026-08-12T12:00:00+10:00")
        cases = [
            # uid suffix, pid, log bytes, age seconds, liveness
            ("young", 1100, 40, 5, runjob.LIVENESS_ALIVE),
            ("age-edge", 1101, 60, 121, runjob.LIVENESS_ALIVE),
            ("small", 1102, 95, 130, runjob.LIVENESS_ALIVE),
            ("log-edge", 1103, 96, 3600, runjob.LIVENESS_ALIVE),
            ("log-over", 1104, 97, 3600, runjob.LIVENESS_ALIVE),
            ("log-mid", 1105, 200, 5000, runjob.LIVENESS_ALIVE),
            ("log-large", 1106, 200_000, 5000, runjob.LIVENESS_ALIVE),
            ("unknown", 1107, 60, 3600, runjob.LIVENESS_UNKNOWN),
        ]
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ledger = home / "jobs.jsonl"
            rows = []
            liveness_by_pid = {}
            for suffix, pid, log_bytes, age, liveness in cases:
                log_path = home / f"{suffix}.log"
                log_path.write_bytes(b"x" * log_bytes)
                launched = (fixed_now - timedelta(seconds=age)).isoformat()
                rows.append(json.dumps({
                    "event": "launch",
                    "uid": f"agreement-{suffix}",
                    "status": "running",
                    "pid": pid,
                    "engine": "codex",
                    "model": "x",
                    "project_name": "p",
                    "log": str(log_path),
                    "ts": launched,
                }))
                liveness_by_pid[pid] = liveness
            ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                uid=None, active=True, include_abandoned=False,
                follow=False, limit=40, outcome=False)
            ps_table = ({}, {pid: 0.0 for _, pid, _, _, _ in cases})

            def fake_liveness(pid, launched_at=None):
                return liveness_by_pid[pid]

            with mock.patch.object(runjob, "JOBS_LOG", ledger), \
                 mock.patch.object(runjob, "HOME", home), \
                 mock.patch.object(runjob, "now", return_value=fixed_now), \
                 mock.patch.object(runjob, "process_liveness",
                                   side_effect=fake_liveness), \
                 mock.patch.object(runjob, "_read_ps_cputime_table",
                                   side_effect=[ps_table, ps_table]), \
                 mock.patch.object(runjob.time, "sleep") as slept, \
                 mock.patch.object(
                     runjob, "process_tree_cpu_delta_seconds",
                     return_value=None) as serial, \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = runjob.cmd_log(args)
            text = out.getvalue()

        self.assertEqual(rc, 0)
        serial.assert_not_called()
        self.assertLessEqual(
            slept.call_count, 1,
            "one listing must never pay more than one CPU sample window")
        for suffix in ("age-edge", "small", "log-edge"):
            self.assertRegex(
                text, rf"(?m)^. agreement-{suffix}\s+.*\bstalled\b")
        self.assertRegex(text, r"(?m)^. agreement-unknown\s+.*\brunning\?\s")

    def test_stall_cpu_sample_pids_skips_large_log(self):
        """Candidate collector must not enqueue rows the short-circuit skips."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            small = home / "small.log"
            large = home / "large.log"
            small.write_bytes(b"x" * 60)
            large.write_bytes(b"x" * 200_000)
            rows = [
                {
                    "status": "running", "pid": 101,
                    "log": str(small),
                    "ts": "2026-01-01T00:00:00+10:00",
                },
                {
                    "status": "running", "pid": 102,
                    "log": str(large),
                    "ts": "2026-01-01T00:00:00+10:00",
                },
            ]
            with mock.patch.object(runjob, "process_liveness",
                                   return_value=runjob.LIVENESS_ALIVE):
                pids = runjob.stall_cpu_sample_pids(rows)
        self.assertEqual(pids, [101])


if __name__ == "__main__":
    unittest.main()
