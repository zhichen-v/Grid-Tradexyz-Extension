import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from core.lighter_submission_journal import (
    SubmissionJournal, read_pending, safe_error_fields,
)


class SubmissionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "evidence"
        self.journal = SubmissionJournal("https://api.rh.lighter.xyz", 42, 0, self.directory)

    def test_lazily_persists_pending_across_instances_without_inference_from_ack(self):
        self.assertFalse(self.directory.exists())
        self.journal.append("pre_send", "tx-1", client_order_id="99", nonce=4,
                            quantity=Decimal("0.00100"), price=841500)
        self.journal.append("acknowledged", "tx-1", response_code=200)
        restored = SubmissionJournal("https://api.rh.lighter.xyz", 42, 0, self.directory)
        pending = read_pending(restored.path)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["client_order_id"], "99")
        self.assertEqual(pending[0]["quantity"], "0.00100")
        self.assertEqual(pending[0]["last_event"], "acknowledged")
        self.journal.append("order_observed", "tx-1", order_id="562950000000001")
        self.assertEqual(read_pending(self.directory), [])
        self.assertEqual(len(self.journal.path.read_text().splitlines()), 3)
        if os.name == "posix":
            self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)

    def test_rejection_and_exact_identity_do_not_resolve_other_account_or_environment(self):
        self.journal.append("pre_send", "same-hash", client_order_id="99")
        other = SubmissionJournal("https://api.rh.lighter.xyz", 43, 0, self.directory)
        other.append("rejected", "same-hash")
        another_environment = SubmissionJournal("https://mainnet.zklighter.elliot.ai", 42, 0, self.directory)
        another_environment.append("order_observed", "same-hash")
        another_key = SubmissionJournal("https://api.rh.lighter.xyz", 42, 1, self.directory)
        another_key.append("order_observed", "same-hash")
        self.assertEqual(len(read_pending(self.directory)), 1)
        self.journal.append("rejected", "same-hash", response_code=21104)
        self.assertEqual(read_pending(self.directory), [])

    def test_new_run_preserves_previous_unresolved_evidence(self):
        self.journal.append("pre_send", "tx-1", client_order_id="99")
        with patch("core.lighter_submission_journal._RUN_ID", "next-run"):
            restarted = SubmissionJournal("https://api.rh.lighter.xyz", 42, 0, self.directory)
        restarted.append("pre_send", "tx-2", client_order_id="100")
        restarted.append("rejected", "tx-1")
        self.assertEqual({item["tx_hash"] for item in read_pending(self.directory)}, {"tx-1", "tx-2"})
        self.assertNotEqual(self.journal.path, restarted.path)

    def test_lookup_failure_cannot_overwrite_original_submission_http_evidence(self):
        self.journal.append("pre_send", "tx-1", client_order_id="99")
        self.journal.append("uncertain", "tx-1", http_status=502, error_type="ApiException",
                            trace_headers={"x-amz-cf-id": "original-trace"}, elapsed_ms=500)
        self.journal.append("tx_lookup_failed", "tx-1", http_status=404, error_type="NotFoundException",
                            trace_headers={"x-amz-cf-id": "lookup-trace"}, elapsed_ms=25)
        pending = read_pending(self.directory)[0]
        self.assertEqual(pending["http_status"], 502)
        self.assertEqual(pending["error_type"], "ApiException")
        self.assertEqual(pending["trace_headers"], {"x-amz-cf-id": "original-trace"})
        self.assertEqual(pending["elapsed_ms"], 500)
        self.assertEqual(pending["last_event"], "tx_lookup_failed")

    def test_redaction_uses_allowlists_and_never_stringifies_exception(self):
        class SecretError(Exception):
            status = 502
            body = "private-body"
            headers = {"X-Amz-Cf-Id": "trace\r\nid", "Authorization": "secret-token"}

            def __str__(self):
                raise AssertionError("Must not stringify exception")

        journal = SubmissionJournal("https://user:password@api.rh.lighter.xyz/path?auth=secret", 42, 0, self.directory)
        journal.append("pre_send", "tx-1", client_order_id="99", signature="private-signature",
                       tx_info="signed-private-payload", auth="secret-token", body="private-body",
                       reason="message with secret-token", config={"key": "private-key"})
        journal.append("uncertain", "tx-1", **safe_error_fields(SecretError()))
        raw = journal.path.read_text()
        for secret in ("password", "secret", "private", "signed", "Authorization"):
            self.assertNotIn(secret, raw)
        pending = read_pending(journal.path)[0]
        self.assertEqual(pending["http_status"], 502)
        self.assertEqual(pending["trace_headers"], {"x-amz-cf-id": "trace  id"})
        self.assertEqual(pending["base_url"], "https://api.rh.lighter.xyz")

    def test_write_and_fsync_failure_are_not_silently_accepted(self):
        self.journal.append("pre_send", "tx-1")
        with patch("core.lighter_submission_journal.os.write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.journal.append("pre_send", "tx-2")
        another = SubmissionJournal("https://api.rh.lighter.xyz", 42, 0, self.directory / "another")
        with patch("core.lighter_submission_journal.os.fsync", side_effect=OSError("disk failed")) as fsync:
            with self.assertRaises(OSError):
                another.append("pre_send", "tx-3")
        fsync.assert_called_once()

    def test_short_write_failure_blocks_other_instances_without_extending_torn_tail(self):
        self.journal.append("pre_send", "tx-1")
        original_write = os.write
        calls = 0

        def broken_write(descriptor, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(descriptor, data[:10])
            raise OSError("disk full")

        with patch("core.lighter_submission_journal.os.write", side_effect=broken_write):
            with self.assertRaises(OSError):
                self.journal.append("pre_send", "tx-2")
        torn_bytes = self.journal.path.read_bytes()
        same_path = SubmissionJournal("https://api.rh.lighter.xyz", 43, 0, self.directory)
        with patch("core.lighter_submission_journal.os.write") as writer:
            with self.assertRaisesRegex(OSError, "blocked after a prior write failure"):
                same_path.append("pre_send", "tx-3")
        writer.assert_not_called()
        self.assertEqual(self.journal.path.read_bytes(), torn_bytes)
        with self.assertWarns(RuntimeWarning):
            self.assertEqual([item["tx_hash"] for item in read_pending(self.directory)], ["tx-1"])

    def test_interrupted_final_write_warns_without_hiding_earlier_pending_record(self):
        self.journal.append("pre_send", "tx-1")
        with self.journal.path.open("ab") as handle:
            handle.write(b'{"event":"pre_send"')
        with self.assertWarns(RuntimeWarning):
            pending = read_pending(self.directory)
        self.assertEqual([item["tx_hash"] for item in pending], ["tx-1"])
        with self.journal.path.open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(ValueError, "Invalid submission journal record"):
            read_pending(self.directory)

    def test_missing_hash_and_nonfinite_numbers_fail_before_any_write(self):
        for tx_hash in (None, "", "invalid hash"):
            with self.assertRaises(ValueError):
                self.journal.append("pre_send", tx_hash)
        with self.assertRaises(ValueError):
            self.journal.append("pre_send", "tx-1", elapsed_ms=float("nan"))
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
