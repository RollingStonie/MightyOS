#!/usr/bin/env python3
"""Manifest-driven, fail-closed Lucy bootstrap planner.

This program deliberately contains no privileged installer, network client, or secret reader.
An approved, deployment-specific runtime adapter is the only component permitted to mutate a
target machine.  Without that adapter, `apply` always fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = ROOT / "fleet/bootstrap/manifests/lucy.json"
DEFAULT_REGISTRY = ROOT / "fleet/registry.yaml"
DEFAULT_POLICY = ROOT / "fleet/bootstrap/v2/registry-policy.json"
ALLOWED_MODULES = {
    "tailscale-node", "a008-watcher", "local-llm-endpoint",
    "contenthub-render-worker", "background-worker", "trading-research",
}
FORBIDDEN_MODULES = {"messaging", "discord-bot", "slack-bot", "trading-execute", "hermes-worker"}
FORBIDDEN_GRANTS = {"trading.execute", "publish", "email.send", "crm.write", "messaging"}
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
EXPECTED_ACCOUNT = "lucy-compute"
EXPECTED_LABELS = ["com.mightyos.lucy.watcher", "com.mightyos.lucy.render-worker"]


class BootstrapError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BootstrapError(f"invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise BootstrapError("manifest must be a JSON object")
    return value


def registry_lucy_policy(path: Path) -> dict[str, set[str]]:
    """Read only Lucy's simple YAML lists; keep this tool standard-library-only."""
    try:
        text = path.read_text()
    except OSError as error:
        raise BootstrapError(f"cannot read registry: {error}") from error
    match = re.search(r"(?ms)^lucy:\n(?P<body>.*?)(?=^[A-Za-z0-9_-]+:\n|\Z)", text)
    if not match:
        raise BootstrapError("Lucy is missing from fleet registry")
    body = match.group("body")
    def listed(name: str) -> set[str]:
        found = re.search(rf"(?m)^\s*{re.escape(name)}:\s*\[([^\]]*)\]", body)
        if not found:
            raise BootstrapError(f"Lucy registry lacks {name}")
        return {item.strip().strip("'\"") for item in found.group(1).split(",") if item.strip()}
    return {"grants": listed("grants"), "forbidden": listed("forbidden")}


def validate_policy_projection(registry: Path, policy_path: Path, registry_policy: dict[str, set[str]]) -> None:
    policy = load_json(policy_path)
    source_hash = hashlib.sha256(registry.read_bytes()).hexdigest()
    if policy.get("source") != "fleet/registry.yaml" or policy.get("source_sha256") != source_hash:
        raise BootstrapError("registry policy projection is stale; regenerate and review it before use")
    if policy.get("agent") != "lucy" or policy.get("discord_identity", "not-null") is not None:
        raise BootstrapError("policy projection must keep Lucy without a messaging identity")
    for key in ("grants", "forbidden"):
        if set(policy.get(key, [])) != registry_policy[key]:
            raise BootstrapError(f"registry policy projection parity failure for {key}")


def reject_secret_channels() -> None:
    # Reject the legacy bootstrap channels.  We intentionally do not scan every inherited
    # process variable: developer shells often contain unrelated service keys, which must not
    # make a non-mutating plan unusable. Lucy secrets have no supported environment channel.
    prohibited = {"TS_KEY", "GH_TOKEN", "BOT_TOKEN", "FLEET_WEBHOOK", "DS_KEY", "HERMES_RAW", "LUCY_BOOTSTRAP_SECRET", "INFISICAL_MACHINE_IDENTITY_TOKEN"}
    detected = [key for key in os.environ if key in prohibited]
    if detected:
        raise BootstrapError("credentials must not enter bootstrap through environment: " + ", ".join(sorted(detected)))


def validate_manifest(manifest: dict[str, Any], policy: dict[str, set[str]]) -> None:
    if manifest.get("schema_version") != 2 or manifest.get("agent") != "lucy":
        raise BootstrapError("only schema v2 Lucy manifests are accepted")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise BootstrapError("manifest modules must be a non-empty list")
    unknown = set(modules) - ALLOWED_MODULES
    forbidden = set(modules) & FORBIDDEN_MODULES
    if unknown or forbidden:
        raise BootstrapError(f"unapproved modules: {sorted(unknown | forbidden)}")
    if policy["forbidden"] & FORBIDDEN_GRANTS != {"trading.execute", "publish", "email.send", "crm.write"}:
        raise BootstrapError("registry forbidden grants must retain Lucy's safety denials")
    module_grants = {"local-llm-endpoint", "contenthub-renders", "heavy-compute", "trading-research", "backtesting", "nightshift-worker"}
    if not module_grants <= policy["grants"]:
        raise BootstrapError("Lucy registry grants no longer match the role contract")
    if "trading.execute" in policy["grants"] or "trading.execute" not in policy["forbidden"]:
        raise BootstrapError("trading.execute must be forbidden, never granted")
    if manifest.get("network", {}).get("bind_address") not in {"127.0.0.1", "::1"}:
        raise BootstrapError("network endpoints must bind localhost; Tailscale exposure needs a later contract")
    if manifest.get("network", {}).get("watcher_port") != 8109:
        raise BootstrapError("Lucy watcher must use port 8109")
    account = manifest.get("service_account")
    if account != EXPECTED_ACCOUNT:
        raise BootstrapError("a non-root dedicated service_account is required")
    names = manifest.get("secrets", {}).get("required_names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and SECRET_NAME.fullmatch(n) for n in names):
        raise BootstrapError("secret staging accepts names only (UPPER_SNAKE_CASE), never values")
    serialized = canonical_json(manifest).decode("utf-8")
    if re.search(r"(?i)(sk-[a-z0-9]|token[=:][^\"]|password[=:][^\"]|authkey[=:][^\"])", serialized):
        raise BootstrapError("manifest appears to contain a credential value")
    hermes = manifest.get("hermes")
    if not isinstance(hermes, dict) or hermes.get("enabled") is not False:
        raise BootstrapError("Hermes is optional and must be explicitly disabled for Lucy v2")
    lifecycle = manifest.get("lifecycle", {})
    if lifecycle.get("promotion_path") != ["registered", "provisioned", "probation", "ready"]:
        raise BootstrapError("lifecycle promotion path must be registered → provisioned → probation → ready")
    required = set(lifecycle.get("required_evidence", []))
    if required != {"tailscale_scoped", "local_health", "reboot_survived", "probation_72h"}:
        raise BootstrapError("lifecycle evidence contract is incomplete")
    launchd = manifest.get("launchd", {})
    if launchd.get("kind") != "daemon" or launchd.get("run_at_load") is not False:
        raise BootstrapError("stationary services require reviewed LaunchDaemons and may not autostart before health proof")
    if launchd.get("labels") != EXPECTED_LABELS:
        raise BootstrapError("launchd labels must match the reviewed Lucy service set")


def launchd_plist(label: str, account: str, command: list[str], run_at_load: bool) -> str:
    args = "".join(f"<string>{xml_escape(part)}</string>" for part in command)
    flag = "<true/>" if run_at_load else "<false/>"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{xml_escape(label)}</string>
<key>UserName</key><string>{xml_escape(account)}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key>{flag}
<key>StandardOutPath</key><string>/var/log/mightyos/{label}.log</string>
<key>StandardErrorPath</key><string>/var/log/mightyos/{label}.log</string>
</dict></plist>\n'''


def build_plan(manifest: dict[str, Any], root: Path, owner_uid: str | None) -> dict[str, Any]:
    account = manifest["service_account"]
    if owner_uid is not None and (not owner_uid.isdigit() or owner_uid == "0"):
        raise BootstrapError("owner UID must be a non-root numeric UID")
    labels = manifest["launchd"]["labels"]
    services = [
        (labels[0], ["/usr/bin/env", "python3", "-m", "agentic_os.watcher", "--port", "8109"]),
        (labels[1], ["/usr/bin/env", "python3", "-m", "contenthub.render_worker"]),
    ]
    resources = []
    for label, command in services:
        resources.append({
            "path": f"Library/LaunchDaemons/{label}.plist",
            "mode": "0644", "owner": "root:wheel", "run_as": account,
            "launch_domain": "system",
            "content": launchd_plist(label, account, command, manifest["launchd"]["run_at_load"]),
        })
    return {
        "schema_version": 2, "agent": "lucy", "mode": "plan", "root": str(root.resolve()),
        "manifest_sha256": manifest_hash(manifest), "service_account": account,
        "secret_names": manifest["secrets"]["required_names"], "secret_scopes": manifest["secrets"]["allowed_scopes"],
        "modules": manifest["modules"], "resources": resources,
        "lifecycle_required_evidence": manifest["lifecycle"]["required_evidence"],
        "hermes_enabled": False,
    }


def state_dir(root: Path) -> Path:
    return root / ".mightyos/lucy-bootstrap-v2"


def receipt_path(root: Path) -> Path:
    return state_dir(root) / "receipt.json"


def evidence_path(root: Path) -> Path:
    return state_dir(root) / "lifecycle-evidence.json"


def load_receipt(root: Path) -> dict[str, Any] | None:
    path = receipt_path(root)
    if not path.exists():
        return None
    return load_json(path)


def assert_managed(plan: dict[str, Any], receipt: dict[str, Any] | None, root: Path) -> None:
    for resource in plan["resources"]:
        target = root / resource["path"]
        if target.exists() and (not receipt or receipt.get("manifest_sha256") != plan["manifest_sha256"]):
            raise BootstrapError(f"unmanaged resource exists: {target}")


def validate_receipt(receipt: dict[str, Any], plan: dict[str, Any], root: Path) -> None:
    expected_paths = [resource["path"] for resource in plan["resources"]]
    paths = receipt.get("resources")
    if receipt.get("schema_version") != 2 or receipt.get("agent") != "lucy":
        raise BootstrapError("receipt does not belong to Lucy bootstrap v2")
    if receipt.get("root") != str(root.resolve()) or receipt.get("manifest_sha256") != plan["manifest_sha256"]:
        raise BootstrapError("receipt is not bound to this root and manifest")
    if not isinstance(paths, list) or paths != expected_paths:
        raise BootstrapError("receipt resources do not match the reviewed manifest plan")
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise BootstrapError("receipt contains an unsafe resource path")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".pending-", delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_receipt_atomically(root: Path, plan: dict[str, Any], adapter: Path) -> Path:
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2, "agent": "lucy", "status": "applied",
        "manifest_sha256": plan["manifest_sha256"], "root": plan["root"],
        "service_account": plan["service_account"], "resources": [r["path"] for r in plan["resources"]],
        "adapter": adapter.name,
    }
    destination = receipt_path(root)
    atomic_json(destination, receipt)
    return destination


def run_adapter(adapter: Path, operation: str, plan: dict[str, Any], root: Path) -> None:
    if not adapter.is_file() or not os.access(adapter, os.X_OK):
        raise BootstrapError("approved runtime adapter must be an executable file")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(plan, handle)
        plan_file = Path(handle.name)
    try:
        completed = subprocess.run(
            [str(adapter), operation, str(plan_file), str(root)], text=True, capture_output=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
        )
    finally:
        plan_file.unlink(missing_ok=True)
    if completed.returncode:
        raise BootstrapError(f"runtime adapter {operation} failed: {completed.stderr.strip() or completed.stdout.strip()}")


def command_validate(args: argparse.Namespace) -> int:
    reject_secret_channels()
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(load_json(args.manifest), registry_policy)
    print("VALID: Lucy manifest and registry policy are compatible")
    return 0


def command_plan(args: argparse.Namespace) -> int:
    reject_secret_channels()
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    print(json.dumps(build_plan(manifest, args.root, args.owner_uid), indent=2, sort_keys=True))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    if args.approved_runtime_adapter is None:
        raise BootstrapError("apply denied: supply an explicit --approved-runtime-adapter; no live adapter is bundled")
    reject_secret_channels()
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    plan = build_plan(manifest, args.root, args.owner_uid)
    receipt = load_receipt(args.root)
    if receipt and receipt.get("status") == "applied" and receipt.get("manifest_sha256") == plan["manifest_sha256"]:
        print("ALREADY_APPLIED: matching receipt exists; no mutation requested")
        return 0
    assert_managed(plan, receipt, args.root)
    run_adapter(args.approved_runtime_adapter, "apply", plan, args.root)
    missing = [r["path"] for r in plan["resources"] if not (args.root / r["path"]).is_file()]
    if missing:
        raise BootstrapError(f"adapter did not create planned resources: {missing}")
    print(f"APPLIED: receipt={write_receipt_atomically(args.root, plan, args.approved_runtime_adapter)}")
    return 0


def command_health(args: argparse.Namespace) -> int:
    reject_secret_channels()
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    receipt = load_receipt(args.root)
    if not receipt or receipt.get("status") != "applied":
        raise BootstrapError("health denied: no applied receipt")
    plan = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, plan, args.root)
    missing = [path for path in receipt["resources"] if not (args.root / path).is_file()]
    if missing:
        raise BootstrapError(f"health failed: missing managed resources {missing}")
    evidence = load_json(args.evidence)
    if evidence.get("manifest_sha256") != receipt["manifest_sha256"]:
        raise BootstrapError("health denied: lifecycle evidence is not bound to the applied manifest")
    required = {"tailscale_scoped", "local_health", "reboot_survived", "probation_72h"}
    if {name for name in required if evidence.get(name) is True} != required:
        raise BootstrapError("health denied: lifecycle evidence is incomplete")
    print("HEALTH: receipt and planned local resources are present; network checks remain adapter-owned")
    return 0


def command_rollback(args: argparse.Namespace) -> int:
    reject_secret_channels()
    if args.approved_runtime_adapter is None:
        raise BootstrapError("rollback denied: supply an explicit --approved-runtime-adapter")
    receipt = load_receipt(args.root)
    if not receipt or receipt.get("status") != "applied":
        raise BootstrapError("rollback denied: no applied receipt")
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    canonical = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, canonical, args.root)
    plan = {"agent": "lucy", "manifest_sha256": receipt["manifest_sha256"], "resources": [{"path": p} for p in receipt["resources"]]}
    run_adapter(args.approved_runtime_adapter, "rollback", plan, args.root)
    receipt["status"] = "rolled_back"
    atomic_json(receipt_path(args.root), receipt)
    print("ROLLED_BACK: only receipt-scoped resources were requested from the adapter")
    return 0


def command_offboard(args: argparse.Namespace) -> int:
    reject_secret_channels()
    if args.approved_runtime_adapter is None:
        raise BootstrapError("offboard denied: supply an explicit --approved-runtime-adapter")
    receipt = load_receipt(args.root)
    if not receipt or receipt.get("status") not in {"applied", "rolled_back"}:
        raise BootstrapError("offboard denied: no receipt-scoped Lucy resources")
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    canonical = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, canonical, args.root)
    plan = {"agent": "lucy", "manifest_sha256": receipt["manifest_sha256"], "resources": [{"path": p} for p in receipt["resources"]]}
    run_adapter(args.approved_runtime_adapter, "offboard", plan, args.root)
    receipt["status"] = "offboarded"
    atomic_json(receipt_path(args.root), receipt)
    print("OFFBOARDED: adapter received only receipt-scoped resource paths")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Safe Lucy bootstrap v2 (plans by default)")
    p.add_argument("command", nargs="?", choices=["validate", "plan", "apply", "health", "rollback", "offboard"], default="plan")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--root", type=Path, default=Path("/"))
    p.add_argument("--evidence", type=Path)
    p.add_argument("--owner-uid")
    p.add_argument("--approved-runtime-adapter", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.evidence is None:
        args.evidence = evidence_path(args.root)
    try:
        return globals()[f"command_{args.command}"](args)
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
