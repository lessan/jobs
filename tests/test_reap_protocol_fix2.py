"""Cycle-2 regressions copied from the 2026-08-06 fix1 refute."""

import random
import unittest

import runjob


BASE = (
    "STATUS: COMPLETE\nCAUSE_CLASS: NONE\nTESTS: 1 passed / 0 failed\n"
    "RESIDUAL: 0\nCOMMIT: OK + abc1234\n"
    "MODIFIED_PATHS: runjob.py,docs/report.md\n"
    "EVIDENCE_PATH: docs/report.md\n"
)


class CycleTwoRefuteRegressionTests(unittest.TestCase):
    """The three finding tables are executable acceptance cases, not examples."""

    def test_finding_one_small_windows_never_become_absent(self):
        # Verbatim 113-byte canonical frame from the fix1 refute's table.
        small = (
            "STATUS: COMPLETE\nCAUSE_CLASS: NONE\nTESTS: NONE\nRESIDUAL: 0\n"
            "COMMIT: NONE\nMODIFIED_PATHS: NONE\nEVIDENCE_PATH: NONE\n")
        raw = small.encode()
        self.assertEqual(len(raw), 113)
        for nbytes in (0, 1, 5, 10, 32):
            with self.subTest(nbytes=nbytes):
                got = runjob.parse_report_trailer(
                    raw[-nbytes:].decode("utf-8", errors="replace") if nbytes else "",
                    bytes_skipped=len(raw) - nbytes)
                self.assertEqual(got.state, runjob.TRAILER_WINDOW_CLIPPED)
                self.assertEqual(runjob.TRAILER_EXIT[got.state], 4)
        self.assertEqual(runjob.parse_report_trailer(small).state,
                         runjob.TRAILER_PRESENT)

    def test_finding_one_trailer_then_five_kib_output_is_clipped(self):
        raw = (BASE + "x" * 5000 + "\n").encode()
        got = runjob.parse_report_trailer(
            raw[-4096:].decode("utf-8", errors="replace"),
            bytes_skipped=len(raw) - 4096)
        self.assertEqual(got.state, runjob.TRAILER_WINDOW_CLIPPED)

    def test_finding_two_oversize_wrapped_and_truncated_are_loud(self):
        # Over the independent trailer cap (not merely over the 4 KiB window).
        over_len = runjob.MAX_REPORT_TRAILER_BYTES + 1024
        oversize = (BASE.rsplit("EVIDENCE_PATH:", 1)[0]
                    + "EVIDENCE_PATH: docs/" + "a" * over_len + ".md\n")
        raw = oversize.encode()
        self.assertGreater(len(raw), runjob.MAX_REPORT_TRAILER_BYTES)
        clipped = runjob.parse_report_trailer(
            raw[-runjob.DEFAULT_REPORT_BYTES:].decode("utf-8", errors="replace"),
            bytes_skipped=len(raw) - runjob.DEFAULT_REPORT_BYTES)
        self.assertEqual(clipped.state, runjob.TRAILER_WINDOW_CLIPPED)
        # Even if a caller supplies a larger bounded window that happens to
        # contain the entire oversize frame, skip > 0 still forbids malformed
        # or absent as a final bounded-read conclusion.
        prefixed = ("x\n" + oversize).encode()
        self.assertEqual(runjob.parse_report_trailer(
            prefixed[-len(raw):].decode("utf-8", errors="replace"),
            bytes_skipped=len(prefixed) - len(raw)).state,
            runjob.TRAILER_WINDOW_CLIPPED)
        # Whole-file oversize remains loud malformed.
        whole = runjob.parse_report_trailer(oversize)
        self.assertEqual(whole.state, runjob.TRAILER_MALFORMED)
        self.assertEqual(whole.reason, "trailer-too-large")

        wrapped = BASE + "".join(
            f"  continuation prose {i}\n" for i in range(8))
        self.assertEqual(runjob.parse_report_trailer(wrapped).state,
                         runjob.TRAILER_MALFORMED)
        self.assertEqual(runjob.parse_report_trailer("work completed\nSTAT").state,
                         runjob.TRAILER_MALFORMED)

    def test_finding_two_every_short_eof_key_run_is_malformed(self):
        lines = BASE.splitlines()
        for count in range(1, len(runjob.REPORT_TRAILER_KEYS)):
            with self.subTest(count=count):
                got = runjob.parse_report_trailer("\n".join(lines[:count]) + "\n")
                self.assertEqual(got.state, runjob.TRAILER_MALFORMED)

    def test_finding_three_normative_mutations_are_malformed(self):
        cases = {
            "cause-colon": BASE.replace("CAUSE_CLASS: NONE", "CAUSE_CLASS: tool:error"),
            "cause-slash": BASE.replace("CAUSE_CLASS: NONE", "CAUSE_CLASS: tool/error"),
            "modified-parent": BASE.replace("MODIFIED_PATHS: runjob.py,docs/report.md",
                                             "MODIFIED_PATHS: ../outside"),
            "evidence-parent": BASE.replace("EVIDENCE_PATH: docs/report.md",
                                             "EVIDENCE_PATH: ../../outside"),
            "c1-ansi": BASE.replace("COMMIT: OK + abc1234",
                                     "COMMIT: DENIED + bad\u009b31mred"),
            "two-markers": BASE + "===== exit:0\n===== exit:1\n",
            "nonliteral-marker": BASE + "=   exit:0\n",
        }
        for name, case in cases.items():
            with self.subTest(name=name):
                self.assertEqual(runjob.parse_report_trailer(case).state,
                                 runjob.TRAILER_MALFORMED)

    def test_comma_space_compatibility_is_normalized(self):
        fields = runjob.parse_report_trailer(BASE.replace(
            "MODIFIED_PATHS: runjob.py,docs/report.md",
            "MODIFIED_PATHS: a, b, c")).fields
        self.assertIsNotNone(fields)
        self.assertEqual(fields["MODIFIED_PATHS"], "a,b,c")

    def test_documented_former_code_only_rules_are_pinned(self):
        # CAUSE_CLASS no longer has an undocumented alphanumeric-first rule.
        self.assertEqual(runjob.parse_report_trailer(BASE.replace(
            "CAUSE_CLASS: NONE", "CAUSE_CLASS: .cache")).state,
            runjob.TRAILER_PRESENT)
        # The two retained restrictions are now explicit protocol grammar.
        self.assertEqual(runjob.parse_report_trailer(BASE.replace(
            "COMMIT: OK + abc1234", "COMMIT: OK + a")).state,
            runjob.TRAILER_MALFORMED)
        self.assertEqual(runjob.parse_report_trailer(BASE.replace(
            "EVIDENCE_PATH: docs/report.md", "EVIDENCE_PATH: docs/a b.md")).state,
            runjob.TRAILER_MALFORMED)

    def test_fuzz_absent_implies_whole_file_read(self):
        """Seed 20260806; varies trailer position, value length, wraps and cuts."""
        rng = random.Random(20260806)
        for case in range(250):
            prefix = "p" * rng.randrange(0, 6000) + ("\n" if rng.randrange(2) else "")
            body = BASE.replace("docs/report.md", "docs/" + "a" * rng.randrange(0, 5000) + ".md")
            mode = rng.randrange(4)
            if mode == 1:
                body += "  continuation prose\n" * rng.randrange(1, 10)
            elif mode == 2:
                body = body[:rng.randrange(0, len(body))]
            elif mode == 3:
                body += "ordinary output\n" * rng.randrange(1, 20)
            raw = (prefix + body).encode()
            nbytes = rng.randrange(0, len(raw) + 1)
            skipped = len(raw) - nbytes
            text = raw[-nbytes:].decode("utf-8", errors="replace") if nbytes else ""
            got = runjob.parse_report_trailer(text, bytes_skipped=skipped)
            with self.subTest(case=case, nbytes=nbytes, skipped=skipped):
                self.assertFalse(got.state == runjob.TRAILER_ABSENT and skipped > 0)
