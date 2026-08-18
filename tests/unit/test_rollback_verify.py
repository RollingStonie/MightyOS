"""Unit tests for ``fleet.bootstrap.v2.rollback_verify.verify_rollback``.

These tests pin the post-state contract the Lucy/Luna planners now enforce
after ``run_adapter(rollback)`` returns success: a buggy adapter that lies
about removing files must be caught, while a well-behaved adapter (every
receipt-scoped file gone) must still produce a clean ``(True, [])`` result.

Only the helper is tested here; integration with the planners is exercised
by the existing ``test_lucy_bootstrap_v2`` / ``test_luna_bootstrap_v2`` suites
to avoid duplicating setup across the three test modules.
"""
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "fleet/bootstrap/v2/rollback_verify.py"

# The helper module has no package ``__init__.py`` ancestor (``fleet/`` is a
# plain directory of scripts), so load it the same way the planners do:
# via ``spec_from_file_location``.
_SPEC = importlib.util.spec_from_file_location("rollback_verify_under_test", HELPER)
rollback_verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rollback_verify)
verify_rollback = rollback_verify.verify_rollback


class VerifyRollbackTests(unittest.TestCase):
    def test_verify_rollback_passes_when_all_files_removed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            # Receipt records three managed files; none of them exist on disk
            # after rollback.  This is the happy-path the planners must see.
            receipts = [
                {"path": "opt/mightyos/libexec/example.sh"},
                {"path": "Library/LaunchDaemons/com.example.watcher.plist"},
                {"path": "etc/mightyos/example.conf"},
            ]
            ok, still_present = verify_rollback(receipts, root_path)
            self.assertTrue(ok)
            self.assertEqual(still_present, [])

    def test_verify_rollback_fails_when_files_still_present(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            survivor = root_path / "opt/mightyos/libexec/lucy-watcher-loopback.py"
            survivor.parent.mkdir(parents=True, exist_ok=True)
            survivor.write_text("stub")
            gone = root_path / "Library/LaunchDaemons/com.example.watcher.plist"
            gone.parent.mkdir(parents=True, exist_ok=True)
            # Touch the gone file then remove it so the directory still
            # exists; this proves the verifier is looking at the file, not
            # the parent dir.
            gone.write_text("x")
            gone.unlink()
            receipts = [
                {"path": "opt/mightyos/libexec/lucy-watcher-loopback.py"},
                {"path": "Library/LaunchDaemons/com.example.watcher.plist"},
            ]
            ok, still_present = verify_rollback(receipts, root_path)
            self.assertFalse(ok)
            self.assertEqual(still_present, [str(survivor)])

    def test_verify_rollback_handles_modified_files_with_sha256_before(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            # Baseline content the receipt records as ``sha256_before``;
            # the post-apply digest is different, so receipt carries both.
            baseline_bytes = b"original-handler-bytes"
            baseline_digest = hashlib.sha256(baseline_bytes).hexdigest()
            mutated_digest = hashlib.sha256(b"new-handler-bytes").hexdigest()
            # Case A: rollback restored to baseline, file is gone.  Receipt
            # carries ``sha256_before``/``sha256_after``; planner-side
            # verification cannot observe the bytes because the file is
            # absent, so this must report ``(True, [])`` -- the contract is
            # "rollback removed the file", and the restore-digest check is
            # only meaningful if something remained on disk.
            receipts_restored = [
                {
                    "path": "opt/mightyos/libexec/example.sh",
                    "sha256_before": baseline_digest,
                    "sha256_after": mutated_digest,
                },
            ]
            ok, still_present = verify_rollback(receipts_restored, root_path)
            self.assertTrue(ok)
            self.assertEqual(still_present, [])

            # Case B: same receipt shape, but the file is still on disk and
            # the surviving bytes happen to match the original baseline
            # (i.e. the adapter restored the bytes but forgot to remove the
            # path).  The helper's job here is to surface the surviving
            # file so the planner refuses to flip status.  The
            # restore-digest check fires only after step 1 (file absent)
            # has already failed, so we still report the path -- not a
            # drift verdict -- which is the contract callers rely on.
            survivor = root_path / "opt/mightyos/libexec/example.sh"
            survivor.parent.mkdir(parents=True, exist_ok=True)
            survivor.write_bytes(baseline_bytes)
            ok, still_present = verify_rollback(receipts_restored, root_path)
            self.assertFalse(ok)
            self.assertEqual(still_present, [str(survivor)])

            # Case C: same receipt shape, surviving bytes equal the
            # post-apply (mutated) digest -- the adapter lied, restoring
            # nothing.  Step 1 still catches this; the helper must not
            # silently accept the mutated bytes just because they match
            # ``sha256_after``.
            survivor.write_bytes(b"new-handler-bytes")
            ok, still_present = verify_rollback(receipts_restored, root_path)
            self.assertFalse(ok)
            self.assertEqual(still_present, [str(survivor)])


if __name__ == "__main__":
    unittest.main()
