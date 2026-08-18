#!/usr/bin/env python3
"""Test-only dev-mock runtime adapter for Lucy + Luna bootstrap v2 planners.

Mirrors the contract the planners enforce in ``run_adapter`` (see
``fleet/bootstrap/v2/lucy_bootstrap.py`` and ``luna_bootstrap.py``):

  $ adapter <operation> <plan.json> <root>

  - ``preflight`` and ``apply``: write the planned resources under ``root``,
    chmod them per the plan's ``mode`` field, and print the planner-required
    attestation JSON (binding + watcher_source + stripped resources) on
    stdout. Exit 0 on success.
  - ``rollback`` and ``offboard``: receive the receipt-scoped slim plan, remove
    the listed files (ignoring missing ones), and echo the same plan back so
    the planner's attestation check accepts the result. Exit 0 on success.
  - ``validate``: receive any plan, dry-run it without touching the
    filesystem, and emit ``{status: "validated", would_apply: N}`` on
    stdout. Exit 0 on success.
  - Anything else: emit a JSON error message on stderr and exit 2.

The adapter is **dev/test-only**: it owns no privileged installer,
network client, or secret reader. Production runs use the real deployment
adapter whose name/digest must appear in ``registry-policy.json`` under
``agents.<name>.approved_adapters``. The planners fail-closed when no approved
adapter is supplied, so this file only ever runs inside the unit-test harness
that points ``--approved-runtime-adapter`` at it and pre-blesses the SHA-256.

Pure stdlib (json/os/sys/hashlib/argparse/pathlib) — no PyYAML, no click.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# Path to the A008 agent_watcher fixture that ships with the test suite.
# Production adapters own the real watcher bytes; the dev-mock adapter re-uses
# the test fixture so end-to-end smoke runs in this repo don't need a separate
# blessed-watcher artifact.
_WATCHER_FIXTURE = Path(__file__).resolve().parent / "a008-agent_watcher.py"

MUTATING_OPERATIONS = {"preflight", "apply"}
ROLLBACK_OPERATIONS = {"rollback", "offboard"}
VALIDATE_OPERATIONS = {"validate"}
STRIPPED_KEYS = ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")


def _emit_attestation(plan: dict) -> None:
    """Print the JSON object the planner's ``run_adapter`` requires on stdout.

    For mutating operations the planner passes the full plan; the attestation
    must contain only the stripped resource subset so the planner's equality
    check (plan[resources] == adapter[resources]) accepts it.

    For rollback operations the planner passes a slim plan whose ``resources``
    already only carry the stripped keys, so we echo it back unchanged.
    """
    attestation = {
        "binding": plan["binding"],
        "watcher_source": plan["watcher_source"],
        "resources": plan["resources"],
    }
    json.dump(attestation, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def _strip_resource(resource: dict) -> dict:
    """Reduce a plan resource to the exact subset the planner expects."""
    return {key: resource[key] for key in STRIPPED_KEYS if key in resource}


def _apply_operation(plan: dict, root: Path, *, is_preflight: bool) -> None:
    """Materialise the planned resources under ``root``.

    For each resource: ensure the parent directory exists, write ``content``
    if present (the planner always includes it on apply; preflight plans may
    or may not — we tolerate both), chmod to ``mode`` (octal string).
    Best-effort chown to 0:0 — non-root test runs swallow the permission error.

    The watcher_source entry is treated identically: only materialised when
    a ``content`` field is present. When ``content`` is absent, fall back to
    the A008 watcher fixture that ships alongside this adapter so the
    post-apply ``verify_watcher_source`` digest check finds the right bytes.

    Preflight only materialises the watcher source — never the resources —
    so the planner's intermediate ``assert_managed`` check between preflight
    and apply does not see ``unmanaged resource`` errors on a fresh root.
    Apply materialises both watcher source and resources.
    """
    _write_entry(root, _resolve_watcher_source(plan["watcher_source"]))

    if not is_preflight:
        for resource in plan["resources"]:
            _write_entry(root, resource)

    # Mutating ops require the stripped attestation; preflight does too in
    # this contract (the planner's run_adapter check fires on both).
    attestation_plan = {
        "binding": plan["binding"],
        "watcher_source": plan["watcher_source"],
        "resources": [_strip_resource(r) for r in plan["resources"]],
    }
    _emit_attestation(attestation_plan)


def _resolve_watcher_source(entry: dict) -> dict:
    """Materialise watcher_source content when the plan omits it.

    The planner-built apply plan stores only path/sha256/mode/owner for the
    watcher source (it expects the deployment adapter to ship the actual
    bytes). The dev-mock adapter falls back to the in-repo test fixture so
    smoke tests don't need a separate blessed artifact; in production the
    real adapter provides its own watcher source.
    """
    if "content" in entry:
        return entry
    if not _WATCHER_FIXTURE.is_file():
        return entry
    return {**entry, "content": _WATCHER_FIXTURE.read_text()}


def _write_entry(root: Path, entry: dict) -> None:
    """Materialise a single plan entry (resource or watcher_source).

    No-op when ``content`` is absent — the plan is using the entry only as
    metadata (the existing fixture adapter has the same behaviour; rollback
    plans carry stripped resources with no ``content`` field, and watcher
    sources in tests where the file isn't supposed to be re-written).
    """
    target = root / entry["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if "content" not in entry:
        return
    target.write_text(entry["content"])
    target.chmod(int(entry["mode"], 8))
    _try_chown(target)


def _rollback_operation(plan: dict, root: Path) -> None:
    """Remove every receipt-scoped resource. Missing files are not an error.

    Rollback plans carry only the stripped resource subset already (no
    ``content``), so the slim plan's ``resources`` are exactly what we echo
    back to satisfy the planner's attestation check.
    """
    for resource in plan["resources"]:
        target = root / resource["path"]
        target.unlink(missing_ok=True)
    _emit_attestation(plan)


def _validate_operation(plan: dict, root: Path) -> None:
    """Dry-run the plan without touching the filesystem.

    Reports the number of resources the plan would materialise. ``root`` is
    accepted only to mirror the planner's uniform [operation, plan, root]
    invocation shape — validate never reads or writes under it.
    """
    json.dump({"status": "validated", "would_apply": len(plan["resources"])}, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def _try_chown(path: Path) -> None:
    """Best-effort chown to root:wheel — non-root test runs swallow the error."""
    try:
        os.chown(path, 0, 0)
    except (PermissionError, OSError):
        pass


def _die(message: str) -> "None":
    """Print a structured error and exit 2 (matches BootstrapError semantics)."""
    json.dump({"error": message}, sys.stderr, sort_keys=True)
    sys.stderr.write("\n")
    sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev-mock runtime adapter (test-only)")
    parser.add_argument("operation", choices=sorted(MUTATING_OPERATIONS | ROLLBACK_OPERATIONS | VALIDATE_OPERATIONS))
    parser.add_argument("plan_file", type=Path)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)

    try:
        plan = json.loads(args.plan_file.read_text())
    except (OSError, json.JSONDecodeError) as error:
        _die(f"cannot read plan: {error}")

    if not isinstance(plan, dict):
        _die("plan must be a JSON object")
    if "binding" not in plan or "watcher_source" not in plan or "resources" not in plan:
        _die("plan missing binding / watcher_source / resources")
    if not isinstance(plan["resources"], list):
        _die("plan.resources must be a list")
    if args.operation in MUTATING_OPERATIONS and len(plan["resources"]) == 0:
        _die("apply/preflight refused: plan has no resources")
    if args.operation in ROLLBACK_OPERATIONS and len(plan["resources"]) == 0:
        _die("rollback/offboard refused: plan has no resources")

    root = args.root.resolve()

    if args.operation in MUTATING_OPERATIONS:
        _apply_operation(plan, root, is_preflight=(args.operation == "preflight"))
    elif args.operation in VALIDATE_OPERATIONS:
        _validate_operation(plan, root)
    else:
        _rollback_operation(plan, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
