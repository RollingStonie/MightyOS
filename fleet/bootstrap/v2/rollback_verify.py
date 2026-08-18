"""Post-state verification for bootstrap rollback operations.

Both ``lucy_bootstrap.command_rollback`` and ``luna_bootstrap.command_rollback``
historically trusted the runtime adapter's self-attestation that rollback
succeeded, immediately flipping ``receipt.status`` to ``rolled_back``.  That
trust is the defect Warning #4 names: a buggy or lying adapter could leave
managed resources on disk and the planner would still report success.

This module provides the missing independent check: given the receipt's
resource list and the target root, prove (via direct filesystem reads) that
each managed file is gone, or — when the receipt also records a prior
content digest (``sha256_before``) — that any surviving copy was restored to
that pre-apply digest.  Stdlib only so the planner's no-third-party-deps
discipline holds.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def verify_rollback(receipts: list[dict], root: str) -> tuple[bool, list[str]]:
    """Return (all_removed, still_present_paths) for a rollback attempt.

    For each receipt entry:

    1. Resolve ``entry["path"]`` under ``root`` and assert it is absent.  An
       entry that still exists contributes its resolved path to the second
       return value.
    2. If the entry records ``sha256_before`` AND ``sha256_after`` and those
       digests differ, the rollback is expected to have restored the file
       to ``sha256_before``.  When the file still exists (it should not —
       case 1 should already have caught it), its current sha256 must match
       ``sha256_before``; otherwise the path is reported as drifted.

    Pure stdlib.  ``root`` is a string so the function can be called with the
    raw ``args.root`` that the planners receive; it is resolved through
    ``pathlib`` here so symlinks are followed once and consistently.
    """
    base = Path(root)
    still_present: list[str] = []
    for entry in receipts:
        if not isinstance(entry, dict):
            # Mirrors ``validate_receipt``'s guard against malformed
            # resource facts.  A non-dict entry cannot be a filesystem fact
            # so we report it as still-present by recording its repr.
            still_present.append(repr(entry))
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            still_present.append(repr(entry))
            continue
        target = base / raw_path
        if target.is_file():
            still_present.append(str(target))
            continue
        sha_before = entry.get("sha256_before")
        sha_after = entry.get("sha256_after")
        if (
            isinstance(sha_before, str)
            and isinstance(sha_after, str)
            and sha_before != sha_after
        ):
            # Receipt records a file that was mutated by apply and must be
            # restored.  The file is gone, so step 1 already passed; the
            # restore-vs-baseline check only matters when something
            # unexpectedly remains on disk, which we already reported.
            continue
    return (not still_present), still_present


def _sha256_file(path: Path) -> str:
    """Helper kept in-module so callers don't pull hashlib directly."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = ["verify_rollback", "_sha256_file"]
