#!/usr/bin/env python3
"""Manifest-driven, fail-closed Lucy bootstrap planner.

This program deliberately contains no privileged installer, network client, or secret reader.
An approved, deployment-specific runtime adapter is the only component permitted to mutate a
target machine.  Without that adapter, `apply` always fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from typing import Any

# The rollback verifier lives in a sibling module that has no package
# ``__init__.py`` ancestor, so a plain ``from fleet...`` import would fail
# when this file is loaded as a script.  Load it via spec so both the
# production CLI invocation (``python3 lucy_bootstrap.py …``) and the unit
# tests (which also ``spec_from_file_location`` this file) see the same
# module reference.
_ROLLBACK_VERIFY_PATH = Path(__file__).resolve().parent / "rollback_verify.py"
_rollback_verify_spec = importlib.util.spec_from_file_location(
    "fleet_bootstrap_v2_rollback_verify", _ROLLBACK_VERIFY_PATH
)
_rollback_verify_module = importlib.util.module_from_spec(_rollback_verify_spec)
sys.modules.setdefault("fleet_bootstrap_v2_rollback_verify", _rollback_verify_module)
_rollback_verify_spec.loader.exec_module(_rollback_verify_module)
verify_rollback = _rollback_verify_module.verify_rollback

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = ROOT / "fleet/bootstrap/manifests/lucy.json"
DEFAULT_REGISTRY = ROOT / "fleet/registry.yaml"
DEFAULT_POLICY = ROOT / "fleet/bootstrap/v2/registry-policy.json"
ALLOWED_MODULES = {
    "tailscale-node", "a008-watcher", "background-worker", "trading-research",
    "hermes-profile", "git-clone-mirror", "a008-dev-instance",
    "hannah-user-account", "contenthub-creator", "contenthub-scanner",
    "contenthub-render-worker", "qmd-runtime",
    "ollama-qwen3.8", "meshcentral-agent",
}
FORBIDDEN_MODULES = {"messaging", "discord-bot", "slack-bot", "trading-execute", "caffeinate-wrapper"}
FORBIDDEN_GRANTS = {"trading.execute", "publish", "email.send", "crm.write"}
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
EXPECTED_ACCOUNT = "contenthub-prod"
EXPECTED_LABELS = [
    "com.mightyos.lucy.watcher",
    "com.mightyos.lucy.hermes-bot",
    "com.mightyos.contenthub.fastapi",
    "com.mightyos.contenthub.celery-worker",
    "com.mightyos.contenthub.celery-render-worker",
    "com.mightyos.qmd.runtime",
    "com.mightyos.lucy.ollama",
    "com.mightyos.lucy.meshcentral-agent",
]
PLANNER_OWNED_LABELS = {
    "com.mightyos.lucy.watcher",
    "com.mightyos.lucy.hermes-bot",
    "com.mightyos.lucy.ollama",
    "com.mightyos.lucy.meshcentral-agent",
}
ADAPTER_REQUIRED_SENTINEL_PREFIX = "ADAPTER REQUIRED"
EXPECTED_GRANTS = {
    "contenthub-creator", "contenthub-scanner", "contenthub-render-worker",
    "qmd-runtime", "hermes-profile", "a008-dev-instance", "hannah-ssh-access",
    "heavy-compute",
}
EXPECTED_DENIALS = {"publish", "email.send", "crm.write", "trading.execute", "portable-sleep-mode", "caffeinate-wrapper"}
EXPECTED_SECRET_NAMES = ["INFISICAL_MACHINE_IDENTITY_TOKEN", "DISCORD_BOT_TOKEN_LUCY"]
EXPECTED_SECRET_SCOPES = ["/lucy/runtime", "/hannah/ssh-keys", "/contenthub/runtime", "/lucy-runtime/meshcentral-server-url", "/lucy-runtime/meshcentral-cert-hash"]
EXPECTED_TAILSCALE_TAG = "tag:contenthub-prod"
EXPECTED_TAILSCALE_ACL = "tag:contenthub-prod"
EXPECTED_HANNAH_SSH_KEY_PATH = "/hannah/ssh-keys/id_ed25519.pub"
WATCHER_SOURCE = {"path": "opt/mightyos/a008/tools/watcher/agent_watcher.py", "sha256": "eb9c2b7a18eec0f066eddb2c0e3104243dd80af084b20c5e8e98748b573f5339", "mode": "0644", "owner": "root:wheel"}


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


def registry_agent_policy(path: Path, agent: str) -> dict[str, set[str]]:
    """Read one agent's simple YAML lists (grants, forbidden) from the registry.

    Kept stdlib-only so the planner can run without third-party YAML parsers. Only the
    requested agent's block is captured; callers MUST know the canonical name.
    """
    try:
        text = path.read_text()
    except OSError as error:
        raise BootstrapError(f"cannot read registry: {error}") from error
    match = re.search(rf"(?ms)^{re.escape(agent)}:\n(?P<body>.*?)(?=^[A-Za-z0-9_-]+:\n|\Z)", text)
    if not match:
        raise BootstrapError(f"{agent} is missing from fleet registry")
    body = match.group("body")
    def listed(name: str) -> set[str]:
        found = re.search(rf"(?m)^\s*{re.escape(name)}:\s*\[([^\]]*)\]", body)
        if not found:
            raise BootstrapError(f"{agent} registry lacks {name}")
        return {item.strip().strip("'\"") for item in found.group(1).split(",") if item.strip()}
    return {"grants": listed("grants"), "forbidden": listed("forbidden")}


def registry_lucy_policy(path: Path) -> dict[str, set[str]]:
    """Backward-compatible alias for Lucy registry read."""
    return registry_agent_policy(path, "lucy")


def _extract_agent_block(policy: dict[str, Any], agent: str) -> dict[str, Any]:
    agents = policy.get("agents")
    if not isinstance(agents, dict) or agent not in agents:
        raise BootstrapError(f"policy projection must contain an {agent} block under 'agents'")
    block = agents[agent]
    if not isinstance(block, dict):
        raise BootstrapError(f"policy projection {agent} block must be an object")
    return block


def validate_policy_projection(registry: Path, policy_path: Path, registry_policy: dict[str, set[str]], agent: str = "lucy") -> dict[str, Any]:
    policy = load_json(policy_path)
    source_hash = hashlib.sha256(registry.read_bytes()).hexdigest()
    if policy.get("source") != "fleet/registry.yaml" or policy.get("source_sha256") != source_hash:
        raise BootstrapError("registry policy projection is stale; regenerate and review it before use")
    block = _extract_agent_block(policy, agent)
    if agent == "lucy":
        if block.get("discord_identity") is not None:
            raise BootstrapError("Lucy policy projection must keep discord_identity null (canonical: compute host does not earn a bot identity by default)")
    elif agent == "luna":
        if block.get("discord_identity") is not None:
            raise BootstrapError("Luna policy projection must keep discord_identity null (canonical: portable machine does not earn a bot identity by default)")
    else:
        raise BootstrapError(f"unknown agent in policy projection: {agent}")
    for key in ("grants", "forbidden"):
        if set(block.get(key, [])) != registry_policy[key]:
            raise BootstrapError(f"registry policy projection parity failure for {agent}.{key}")
    if registry_policy["grants"] != EXPECTED_GRANTS or registry_policy["forbidden"] != EXPECTED_DENIALS:
        raise BootstrapError(f"{agent} registry grants and denials must exactly match the reviewed role policy")
    if not isinstance(block.get("approved_adapters"), list):
        raise BootstrapError("policy agent block must declare an approved_adapters allowlist")
    return policy


def reject_secret_channels() -> None:
    # Reject the legacy bootstrap channels.  We intentionally do not scan every inherited
    # process variable: developer shells often contain unrelated service keys, which must not
    # make a non-mutating plan unusable. Lucy secrets have no supported environment channel.
    prohibited = {"TS_KEY", "GH_TOKEN", "BOT_TOKEN", "FLEET_WEBHOOK", "DS_KEY", "HERMES_RAW", "LUCY_BOOTSTRAP_SECRET", "INFISICAL_MACHINE_IDENTITY_TOKEN"}
    detected = [key for key in os.environ if key in prohibited]
    if detected:
        raise BootstrapError("credentials must not enter bootstrap through environment: " + ", ".join(sorted(detected)))


def validate_manifest(manifest: dict[str, Any], policy: dict[str, set[str]]) -> None:
    expected_top = {"schema_version", "agent", "role", "service_account", "watcher_source", "required_grants", "denied_grants", "lifecycle", "modules", "network", "secrets", "hermes", "launchd", "qmd", "power"}
    if set(manifest) != expected_top:
        raise BootstrapError("manifest schema is closed; unknown or missing top-level fields")
    if manifest.get("schema_version") != 2 or manifest.get("agent") != "lucy":
        raise BootstrapError("only schema v2 Lucy manifests are accepted")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise BootstrapError("manifest modules must be a non-empty list")
    unknown = set(modules) - ALLOWED_MODULES
    forbidden = set(modules) & FORBIDDEN_MODULES
    if unknown or forbidden:
        raise BootstrapError(f"unapproved modules: {sorted(unknown | forbidden)}")
    if "caffeinate-wrapper" in set(modules):
        raise BootstrapError("Lucy is stationary always-on; caffeinate-wrapper must never be present")
    if policy["forbidden"] != EXPECTED_DENIALS:
        raise BootstrapError("registry forbidden grants must retain Lucy's safety denials")
    if policy["grants"] != EXPECTED_GRANTS or set(manifest["required_grants"]) != EXPECTED_GRANTS or set(manifest["denied_grants"]) != EXPECTED_DENIALS:
        raise BootstrapError("Lucy registry grants no longer match the role contract")
    if "trading.execute" in policy["grants"] or "trading.execute" not in policy["forbidden"]:
        raise BootstrapError("trading.execute must be forbidden, never granted")
    network = manifest.get("network", {})
    expected_network = {"watcher_port", "bind_address", "tailscale_acl", "tailscale_tag", "contenthub_web_ui", "hannah_ssh"}
    if set(network) != expected_network or network.get("bind_address") != "127.0.0.1":
        raise BootstrapError("Lucy network endpoints must bind localhost; Tailscale exposure needs a later contract")
    if network.get("watcher_port") != 8109:
        raise BootstrapError("Lucy watcher must use port 8109")
    if network.get("tailscale_acl") != EXPECTED_TAILSCALE_ACL or network.get("tailscale_tag") != EXPECTED_TAILSCALE_TAG:
        raise BootstrapError("Lucy requires the exact reviewed Tailscale tag and ACL")
    web_ui = network.get("contenthub_web_ui")
    if not isinstance(web_ui, dict) or not web_ui.get("enabled") or web_ui.get("external_access") != "tailscale-only":
        raise BootstrapError("Lucy contenthub_web_ui must be enabled and exposed only via Tailscale")
    hannah_ssh = network.get("hannah_ssh")
    if not isinstance(hannah_ssh, dict) or not hannah_ssh.get("enabled") or hannah_ssh.get("user") != "hannah" or hannah_ssh.get("firewall") != "tailscale-only":
        raise BootstrapError("Lucy hannah_ssh must be enabled for user 'hannah' with tailscale-only firewall")
    account = manifest.get("service_account")
    if account != EXPECTED_ACCOUNT:
        raise BootstrapError("a non-root dedicated service_account is required")
    if manifest.get("watcher_source") != WATCHER_SOURCE:
        raise BootstrapError("watcher source must match the reviewed immutable digest and root-owned mode contract")
    secrets = manifest.get("secrets")
    if not isinstance(secrets, dict) or secrets.get("required_names") != EXPECTED_SECRET_NAMES or secrets.get("allowed_scopes") != EXPECTED_SECRET_SCOPES:
        raise BootstrapError("Lucy requires exactly the reviewed Infisical secret names and scopes")
    if secrets.get("hannah_ssh_key_path") != EXPECTED_HANNAH_SSH_KEY_PATH:
        raise BootstrapError("Lucy hannah_ssh_key_path must match the reviewed Infisical-mounted path")
    names = secrets["required_names"]
    if not isinstance(names, list) or not all(isinstance(n, str) and SECRET_NAME.fullmatch(n) for n in names):
        raise BootstrapError("secret staging accepts names only (UPPER_SNAKE_CASE), never values")
    serialized = canonical_json(manifest).decode("utf-8")
    if re.search(r"(?i)(sk-[a-z0-9]|token[=:][^\"]|password[=:][^\"]|authkey[=:][^\"])", serialized):
        raise BootstrapError("manifest appears to contain a credential value")
    hermes = manifest.get("hermes")
    if not isinstance(hermes, dict) or hermes.get("enabled") is not True:
        raise BootstrapError("Hermes is now required for Lucy v2 and must be explicitly enabled")
    if hermes.get("profile_name") != "lucy":
        raise BootstrapError("Lucy must declare hermes profile_name == 'lucy'")
    if hermes.get("surface") != "discord":
        raise BootstrapError("Lucy Hermes surface must be 'discord'")
    if not isinstance(hermes.get("channel"), str) or not hermes["channel"].startswith("#"):
        raise BootstrapError("Lucy Hermes channel must be a #channel string")
    if "hermes-profile" not in set(modules):
        raise BootstrapError("Lucy manifest must include the hermes-profile module when Hermes is enabled")
    lifecycle = manifest.get("lifecycle", {})
    if lifecycle.get("promotion_path") != ["registered", "provisioned", "probation", "ready"]:
        raise BootstrapError("lifecycle promotion path must be registered → provisioned → probation → ready")
    required = set(lifecycle.get("required_evidence", []))
    if required != {"tailscale_scoped", "local_health", "reboot_survived", "probation_72h"}:
        raise BootstrapError("lifecycle evidence contract is incomplete")
    launchd = manifest.get("launchd", {})
    if launchd.get("kind") != "daemon":
        raise BootstrapError("Lucy services require reviewed LaunchDaemons")
    if launchd.get("labels") != EXPECTED_LABELS:
        raise BootstrapError("launchd labels must match the reviewed Lucy service set")
    if launchd.get("run_at_load") is not True:
        raise BootstrapError("Lucy is stationary always-on; LaunchDaemons must run_at_load == true")
    adapter_required = launchd.get("adapter_required")
    if not isinstance(adapter_required, dict):
        raise BootstrapError("launchd.adapter_required must be an object describing labels awaiting real adapters")
    unknown_adapters = set(adapter_required) - set(EXPECTED_LABELS)
    if unknown_adapters:
        raise BootstrapError(f"launchd.adapter_required references unknown labels: {sorted(unknown_adapters)}")
    uncovered = (set(EXPECTED_LABELS) - PLANNER_OWNED_LABELS) - set(adapter_required)
    if uncovered:
        raise BootstrapError(
            f"launchd labels without planner-owned commands must declare adapter_required: {sorted(uncovered)}"
        )
    for label, contract in adapter_required.items():
        if label in PLANNER_OWNED_LABELS:
            raise BootstrapError(f"launchd.adapter_required must not cover planner-owned label: {label}")
        if not isinstance(contract, str) or not contract.strip():
            raise BootstrapError(f"launchd.adapter_required[{label}] must be a non-empty contract description")
    qmd = manifest.get("qmd")
    if not isinstance(qmd, dict) or qmd.get("enabled") is not True or qmd.get("scope") != "contenthub-prod-pc" or qmd.get("primary_user") != "hannah":
        raise BootstrapError("Lucy qmd block must be enabled, scoped to contenthub-prod-pc, with primary_user 'hannah'")
    if not isinstance(qmd.get("users"), list) or "hannah" not in qmd["users"]:
        raise BootstrapError("Lucy qmd.users must include 'hannah'")
    qmd_modes = {"launchdaemon-as-contenthub-prod", "launchagent", "undecided"}
    qmd_mode = qmd.get("qmd_execution_mode")
    if qmd_mode not in qmd_modes:
        raise BootstrapError(f"Lucy qmd.qmd_execution_mode must be one of {sorted(qmd_modes)} (Open Q1 resolution); got {qmd_mode!r}")
    if qmd_mode == "undecided" and qmd.get("qmd_execution_reason") is None:
        raise BootstrapError("Lucy qmd.qmd_execution_reason must be set when qmd_execution_mode is 'undecided'")
    power = manifest.get("power")
    if not isinstance(power, dict) or power.get("always_on_when_powered") is not True or power.get("caffeinate_required_when_docked") is not False:
        raise BootstrapError("Lucy power contract must declare always_on_when_powered == true and caffeinate_required_when_docked == false")


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


def watcher_loopback_wrapper(bind_address: str, port: int) -> str:
    """Immutable wrapper for the current A008 watcher, which otherwise hard-codes 0.0.0.0."""
    return f'''#!/usr/bin/env python3
import http.server
import os
import runpy
import sys

_expected = ({bind_address!r}, {port})
_a008_root = "/opt/mightyos/a008"
if _a008_root not in sys.path:
    sys.path.insert(0, _a008_root)
_base = http.server.ThreadingHTTPServer
class _LoopbackOnlyServer(_base):
    def __init__(self, requested, handler, *args, **kwargs):
        if requested != ("0.0.0.0", {port}):
            raise RuntimeError("unexpected A008 watcher bind request")
        super().__init__(_expected, handler, *args, **kwargs)
http.server.ThreadingHTTPServer = _LoopbackOnlyServer
os.environ["A008_WATCHER_PORT"] = str({port})
runpy.run_path("/opt/mightyos/a008/tools/watcher/agent_watcher.py", run_name="__main__")
'''


def build_plan(manifest: dict[str, Any], root: Path, owner_uid: str | None) -> dict[str, Any]:
    account = manifest["service_account"]
    if owner_uid is not None:
        raise BootstrapError("owner UID is not accepted: Lucy uses a system LaunchDaemon with explicit UserName")
    labels = manifest["launchd"]["labels"]
    adapter_required = manifest["launchd"].get("adapter_required", {})
    wrapper = watcher_loopback_wrapper(manifest["network"]["bind_address"], manifest["network"]["watcher_port"])
    label_commands = {
        "com.mightyos.lucy.watcher": ["/usr/bin/env", "python3", "/opt/mightyos/libexec/lucy-watcher-loopback.py"],
        "com.mightyos.lucy.hermes-bot": ["/usr/bin/env", "python3", "-m", "hermes.runtime", "--profile", "lucy"],
        # Ollama serves the local qwen3.8 model for ContentHub heavy-compute / authoring flows.
        # Program path is the canonical Homebrew install; the runtime adapter is responsible for
        # confirming the binary exists on the target host before bootstrapping.
        "com.mightyos.lucy.ollama": ["/opt/homebrew/bin/ollama", "serve"],
        # MeshCentral agent connects the box to the fleet MeshCentral server. Server URL and
        # cert-hash come from Infisical at runtime; the bootstrap manifest only declares the
        # scope allowlist, never the values.
        "com.mightyos.lucy.meshcentral-agent": [
            "/usr/bin/env", "sh", "-c",
            '/usr/bin/env INFISICAL_MESH_URL="$INFISICAL_MESH_URL" INFISICAL_MESH_HASH="$INFISICAL_MESH_HASH" exec /opt/mightyos/libexec/meshagent --server-url="$INFISICAL_MESH_URL" --cert-hash="$INFISICAL_MESH_HASH"',
        ],
    }
    # ContentHub / QMD labels are owned by their own adapters; the planner emits a fail-loud
    # sentinel ProgramArguments so a missing adapter is visible in launchd logs the moment
    # the daemon tries to start, instead of silently exiting success via /usr/bin/env true.
    def command_for(label: str) -> list[str]:
        if label in label_commands:
            return label_commands[label]
        contract = adapter_required.get(label)
        if contract:
            return [
                "/bin/sh", "-c",
                f'/bin/echo "{ADAPTER_REQUIRED_SENTINEL_PREFIX}: {label} — {contract}" >&2; exit 1',
            ]
        raise BootstrapError(f"label {label} has no planner-owned command and no adapter_required contract")
    services = [(label, command_for(label)) for label in labels]
    resources = [{
        "path": "opt/mightyos/libexec/lucy-watcher-loopback.py", "mode": "0755", "owner": "root:wheel", "run_as": account,
        "launch_domain": "system", "content": wrapper,
    }]
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
        "modules": manifest["modules"], "resources": [{**resource, "content_sha256": hashlib.sha256(resource["content"].encode()).hexdigest()} for resource in resources],
        "binding": {"address": manifest["network"]["bind_address"], "port": 8109, "tailscale_acl": EXPECTED_TAILSCALE_ACL, "tailscale_tag": EXPECTED_TAILSCALE_TAG},
        "watcher_source": manifest["watcher_source"],
        "lifecycle_required_evidence": manifest["lifecycle"]["required_evidence"],
        "hermes_enabled": True,
        "hermes_profile": "lucy",
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
    if receipt.get("watcher_source") != plan["watcher_source"]:
        raise BootstrapError("receipt watcher source facts do not match the reviewed artifact")
    if not isinstance(paths, list) or [item.get("path") for item in paths if isinstance(item, dict)] != expected_paths:
        raise BootstrapError("receipt resources do not match the reviewed manifest plan")
    for item in paths:
        if not isinstance(item, dict):
            raise BootstrapError("receipt contains invalid resource facts")
        candidate = Path(item["path"])
        if candidate.is_absolute() or ".." in candidate.parts:
            raise BootstrapError("receipt contains an unsafe resource path")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".pending-", delete=False) as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def verify_watcher_source(plan: dict[str, Any], root: Path, *, stat_fn=os.stat, enforce_owner: bool = True) -> None:
    source = plan["watcher_source"]
    path = root / source["path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"] or format(stat_fn(path).st_mode & 0o777, "04o") != source["mode"]:
        raise BootstrapError("watcher source digest or non-writable mode drifted")
    if not enforce_owner:
        return
    source_stat = stat_fn(path)
    if source_stat.st_uid != 0 or source_stat.st_gid != 0:
        raise BootstrapError("watcher source must be owned by root:wheel")
    current = path.parent
    root = root.resolve()
    while current != root:
        mode = stat_fn(current).st_mode & 0o777
        if mode & 0o022:
            raise BootstrapError("watcher source parent path is group/world writable")
        current = current.parent


def verify_runtime_watcher_source(plan: dict[str, Any], root: Path) -> None:
    # The escape hatch is constrained to the unittest temporary-root harness. A real root
    # (including any deployment root outside the system temp directory) always uses os.stat.
    resolved = root.resolve()
    fake_root = os.environ.get("LUCY_BOOTSTRAP_TEST_FAKE_ROOT") == "1" and resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
    verify_watcher_source(plan, root, enforce_owner=not fake_root)


def _is_launchdaemon_path(path: Path) -> bool:
    parts = path.parts
    if "Library" not in parts or "LaunchDaemons" not in parts:
        return False
    return parts.index("LaunchDaemons") == parts.index("Library") + 1


def verify_launchdaemon_ownership(path: Path, *, stat_fn=os.stat, enforce_owner: bool = True) -> None:
    """Reject LaunchDaemon files that are not owned by root:wheel.

    macOS refuses to load a LaunchDaemon whose owning UID/GID are not 0/0, even when the
    file content and mode are correct. Apply and idempotency checks must inspect ownership
    so a misconfigured adapter cannot report success while the daemon silently fails to
    load. Ownership checks apply only to /Library/LaunchDaemons/*; libexec helpers and
    the watcher source have their own ownership contracts.

    ``enforce_owner=False`` mirrors the ``verify_watcher_source`` test-only escape hatch:
    when the unittest harness sets ``LUCY_BOOTSTRAP_TEST_FAKE_ROOT=1`` against a temp
    directory the calling process cannot chown to root:wheel, so the check is relaxed.
    Production runs always see ``enforce_owner=True``.
    """
    if not _is_launchdaemon_path(path):
        return
    if not enforce_owner:
        return
    stat = stat_fn(path)
    if stat.st_uid != 0 or stat.st_gid != 0:
        raise BootstrapError(
            f"LaunchDaemon file {path} must be owned by root:wheel (uid=0,gid=0); got uid={stat.st_uid}, gid={stat.st_gid}"
        )


def _resource_filesystem_facts(root: Path, plan: dict[str, Any]) -> None:
    """Validate every managed resource: existence, hash, mode, and (LaunchDaemon) ownership.

    Centralised so post-apply, idempotency, and health checks all use the same fail-closed
    rules. LaunchDaemon ownership is enforced for every managed file under Library/LaunchDaemons;
    libexec helpers and the watcher source have their own contracts. Ownership enforcement
    is relaxed under the same ``LUCY_BOOTSTRAP_TEST_FAKE_ROOT`` escape hatch that the
    watcher-source verifier honours, so the unit-test fake adapter can run without root.
    """
    resolved = root.resolve()
    fake_root = os.environ.get("LUCY_BOOTSTRAP_TEST_FAKE_ROOT") == "1" and resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
    for resource in plan["resources"]:
        path = root / resource["path"]
        if not path.is_file():
            raise BootstrapError(f"managed resource missing on disk: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != resource["content_sha256"]:
            raise BootstrapError(f"managed resource content drifted: {path}")
        if format(path.stat().st_mode & 0o777, "04o") != resource["mode"]:
            raise BootstrapError(f"managed resource mode drifted: {path}")
        verify_launchdaemon_ownership(path, enforce_owner=not fake_root)


def write_receipt_atomically(root: Path, plan: dict[str, Any], adapter: Path) -> Path:
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2, "agent": "lucy", "status": "applied",
        "manifest_sha256": plan["manifest_sha256"], "root": plan["root"],
        "service_account": plan["service_account"], "resources": [{key: r[key] for key in ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")} for r in plan["resources"]], "binding": plan["binding"], "watcher_source": plan["watcher_source"],
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
        raise BootstrapError(f"runtime adapter {operation} failed (output redacted)")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise BootstrapError("runtime adapter returned invalid attestation") from error
    if result.get("binding") != plan["binding"] or result.get("watcher_source") != plan["watcher_source"] or result.get("resources") != [{key: r[key] for key in ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")} for r in plan["resources"]]:
        raise BootstrapError("runtime adapter attestation does not match the approved plan")


def approved_adapter(policy: dict[str, Any], adapter: Path, agent: str = "lucy") -> None:
    digest = hashlib.sha256(adapter.read_bytes()).hexdigest()
    block = _extract_agent_block(policy, agent)
    for item in block.get("approved_adapters", []):
        if isinstance(item, dict) and item.get("id") == adapter.name and isinstance(item.get("version"), str) and item.get("sha256") == digest:
            return
    raise BootstrapError("runtime adapter is not allowlisted with an approved version and digest")


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
    policy = validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    plan = build_plan(manifest, args.root, args.owner_uid)
    approved_adapter(policy, args.approved_runtime_adapter)
    receipt = load_receipt(args.root)
    if receipt and receipt.get("status") == "applied" and receipt.get("manifest_sha256") == plan["manifest_sha256"]:
        validate_receipt(receipt, plan, args.root)
        verify_runtime_watcher_source(plan, args.root)
        try:
            _resource_filesystem_facts(args.root, plan)
        except BootstrapError as error:
            raise BootstrapError(f"idempotency denied: receipt resources drifted: {error}") from error
        print("ALREADY_APPLIED: matching receipt exists; no mutation requested")
        return 0
    run_adapter(args.approved_runtime_adapter, "preflight", plan, args.root)
    verify_runtime_watcher_source(plan, args.root)
    assert_managed(plan, receipt, args.root)
    run_adapter(args.approved_runtime_adapter, "apply", plan, args.root)
    try:
        _resource_filesystem_facts(args.root, plan)
    except BootstrapError as error:
        raise BootstrapError(f"adapter did not create planned resources: {error}") from error
    print(f"APPLIED: receipt={write_receipt_atomically(args.root, plan, args.approved_runtime_adapter)}")
    return 0


def command_health(args: argparse.Namespace) -> int:
    reject_secret_channels()
    manifest = load_json(args.manifest)
    registry_policy = registry_lucy_policy(args.registry)
    policy = validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    receipt = load_receipt(args.root)
    if not receipt or receipt.get("status") != "applied":
        raise BootstrapError("health denied: no applied receipt")
    plan = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, plan, args.root)
    verify_runtime_watcher_source(plan, args.root)
    if receipt.get("binding") != plan["binding"] or receipt.get("resources") != [{key: r[key] for key in ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")} for r in plan["resources"]]:
        raise BootstrapError("health denied: receipt resource facts do not match the approved plan")
    try:
        _resource_filesystem_facts(args.root, plan)
    except BootstrapError as error:
        raise BootstrapError(f"health failed: missing managed resources: {error}") from error
    evidence = load_json(args.evidence)
    if evidence.get("manifest_sha256") != receipt["manifest_sha256"]:
        raise BootstrapError("health denied: lifecycle evidence is not bound to the applied manifest")
    expected_facts = [{key: r[key] for key in ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")} for r in plan["resources"]]
    if evidence.get("binding") != plan["binding"] or evidence.get("launchd") != expected_facts:
        raise BootstrapError("health denied: adapter binding or launchd evidence is not enforceable against the approved plan")
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
    policy = validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    canonical = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, canonical, args.root)
    approved_adapter(policy, args.approved_runtime_adapter)
    plan = {"agent": "lucy", "manifest_sha256": receipt["manifest_sha256"], "resources": receipt["resources"], "binding": receipt["binding"], "watcher_source": receipt["watcher_source"]}
    run_adapter(args.approved_runtime_adapter, "rollback", plan, args.root)
    all_removed, still_present = verify_rollback(receipt["resources"], str(args.root))
    if not all_removed:
        raise BootstrapError(
            "rollback verification failed: managed resources still present: "
            + ", ".join(sorted(still_present))
        )
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
    policy = validate_policy_projection(args.registry, args.policy, registry_policy)
    validate_manifest(manifest, registry_policy)
    canonical = build_plan(manifest, args.root, args.owner_uid)
    validate_receipt(receipt, canonical, args.root)
    approved_adapter(policy, args.approved_runtime_adapter)
    plan = {"agent": "lucy", "manifest_sha256": receipt["manifest_sha256"], "resources": receipt["resources"], "binding": receipt["binding"], "watcher_source": receipt["watcher_source"]}
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
    p.add_argument("--dry-run", action="store_true", help="Print the change list instead of mutating (for apply/rollback/offboard, behaves like plan)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.evidence is None:
        args.evidence = evidence_path(args.root)
    if args.dry_run and args.command in {"apply", "rollback", "offboard"}:
        args.command = "plan"
    try:
        return globals()[f"command_{args.command}"](args)
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
