"""Unit tests for the test-only dev-mock runtime adapter.

Covers the four adapter subcommands the planners exercise (``apply``,
``rollback``, ``validate``-equivalent ``preflight``, plus a no-mutation
``validate`` shape) and one end-to-end smoke test that drives Lucy ``apply``
with the dev-mock adapter as the approved runtime adapter.

The planners' ``run_adapter`` invokes the adapter as
``[adapter, operation, plan_file, root]`` and reads a JSON attestation from
stdout; these tests pin that contract so a refactor of either side is caught
early.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADAPTER = REPO / "tests/fixtures/dev_mock_adapter.py"
CLI = REPO / "fleet/bootstrap/v2/lucy_bootstrap.py"
MANIFEST = REPO / "fleet/bootstrap/manifests/lucy.json"
REGISTRY = REPO / "fleet/registry.yaml"
POLICY = REPO / "fleet/bootstrap/v2/registry-policy.json"
WATCHER_SOURCE = REPO / "tests/fixtures/a008-agent_watcher.py"


def _sha256_bytes(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_minimal_plan(resources: list[dict], *, manifest_sha256: str = "deadbeef") -> dict:
    """Assemble a plan shape that the adapter will accept.

    Mirrors the ``resources`` shape the planner emits (``path`` / ``mode`` /
    ``owner`` / ``run_as`` / ``launch_domain`` / ``content`` / ``content_sha256``)
    plus the wrapper-level ``binding`` and ``watcher_source`` the attestation
    check keys off.
    """
    return {
        "schema_version": 2,
        "agent": "lucy",
        "manifest_sha256": manifest_sha256,
        "binding": {"address": "127.0.0.1", "port": 8109, "tailscale_acl": "tag:test", "tailscale_tag": "tag:test"},
        "watcher_source": {
            "path": "opt/mightyos/a008/tools/watcher/agent_watcher.py",
            "sha256": _sha256_bytes("# watcher placeholder"),
            "mode": "0644",
            "owner": "root:wheel",
        },
        "resources": resources,
    }


def _invoke_adapter(operation: str, plan: dict, root: Path) -> subprocess.CompletedProcess:
    """Run the adapter with a temp plan file, mirroring ``run_adapter``."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(plan, handle)
        plan_file = Path(handle.name)
    try:
        return subprocess.run(
            [sys.executable, str(ADAPTER), operation, str(plan_file), str(root)],
            text=True, capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    finally:
        plan_file.unlink(missing_ok=True)


class DevMockAdapterTests(unittest.TestCase):
    def test_apply_writes_all_planned_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            content_a = "alpha-content"
            content_b = "beta-content"
            content_c = "gamma-content"
            resources = [
                {"path": "etc/a.conf", "mode": "0644", "owner": "root:wheel", "run_as": "root", "launch_domain": "system", "content": content_a, "content_sha256": _sha256_bytes(content_a)},
                {"path": "etc/b.conf", "mode": "0644", "owner": "root:wheel", "run_as": "root", "launch_domain": "system", "content": content_b, "content_sha256": _sha256_bytes(content_b)},
                {"path": "etc/c.conf", "mode": "0755", "owner": "root:wheel", "run_as": "root", "launch_domain": "system", "content": content_c, "content_sha256": _sha256_bytes(content_c)},
            ]
            plan = _build_minimal_plan(resources)
            result = _invoke_adapter("apply", plan, root)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            attestation = json.loads(result.stdout)
            self.assertEqual({r["path"] for r in attestation["resources"]}, {"etc/a.conf", "etc/b.conf", "etc/c.conf"})
            for path, expected in [("etc/a.conf", content_a), ("etc/b.conf", content_b), ("etc/c.conf", content_c)]:
                self.assertEqual((root / path).read_text(), expected)
            # Apply attestation must contain the stripped subset only.
            for resource in attestation["resources"]:
                self.assertEqual(
                    set(resource.keys()),
                    {"path", "content_sha256", "mode", "owner", "launch_domain", "run_as"},
                )
            # Mode bits must round-trip exactly.
            self.assertEqual((root / "etc/a.conf").stat().st_mode & 0o777, 0o644)
            self.assertEqual((root / "etc/c.conf").stat().st_mode & 0o777, 0o755)

    def test_rollback_removes_all_receipted_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contents = ["one", "two", "three"]
            resources = [
                {"path": f"var/log/{name}.log", "mode": "0644", "owner": "root:wheel", "run_as": "root", "launch_domain": "system", "content": text, "content_sha256": _sha256_bytes(text)}
                for name, text in zip(("alpha", "beta", "gamma"), contents)
            ]
            plan = _build_minimal_plan(resources)
            apply = _invoke_adapter("apply", plan, root)
            self.assertEqual(apply.returncode, 0, msg=apply.stderr)
            for name in ("alpha", "beta", "gamma"):
                self.assertTrue((root / f"var/log/{name}.log").is_file())
            # The planner passes a slim plan to rollback — only the stripped
            # subset is in receipt. The adapter must accept that shape.
            slim_resources = [{k: r[k] for k in ("path", "content_sha256", "mode", "owner", "launch_domain", "run_as")} for r in resources]
            slim_plan = {
                "agent": plan["agent"],
                "manifest_sha256": plan["manifest_sha256"],
                "binding": plan["binding"],
                "watcher_source": plan["watcher_source"],
                "resources": slim_resources,
            }
            rollback = _invoke_adapter("rollback", slim_plan, root)
            self.assertEqual(rollback.returncode, 0, msg=rollback.stderr)
            attestation = json.loads(rollback.stdout)
            self.assertEqual([r["path"] for r in attestation["resources"]], [r["path"] for r in slim_resources])
            for name in ("alpha", "beta", "gamma"):
                self.assertFalse((root / f"var/log/{name}.log").exists())

    def test_validate_does_not_mutate_filesystem(self):
        """``validate`` is the dry-run subcommand the adapter supports in
        addition to the planner's mutating/rollback operations. It must not
        create any files under ``root`` and must report the would-apply
        count from the plan it received."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contents = ["alpha", "beta", "gamma"]
            resources = [
                {"path": f"var/lib/{name}.txt", "mode": "0644", "owner": "root:wheel", "run_as": "root", "launch_domain": "system", "content": text, "content_sha256": _sha256_bytes(text)}
                for name, text in zip(("alpha", "beta", "gamma"), contents)
            ]
            plan = _build_minimal_plan(resources)
            result = _invoke_adapter("validate", plan, root)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            attestation = json.loads(result.stdout)
            self.assertEqual(attestation["status"], "validated")
            self.assertEqual(attestation["would_apply"], 3)
            # Nothing materialised on disk.
            for name in ("alpha", "beta", "gamma"):
                self.assertFalse((root / f"var/lib/{name}.txt").exists())

    def test_apply_rejects_plan_with_no_resources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = _build_minimal_plan([])
            result = _invoke_adapter("apply", plan, root)
            self.assertEqual(result.returncode, 2)
            err = json.loads(result.stderr)
            self.assertIn("no resources", err["error"])

    def test_smoke_lucy_apply_with_dev_mock_adapter_succeeds(self):
        """End-to-end: drive the real Lucy ``apply`` CLI with the dev-mock
        adapter as the approved runtime adapter. Confirms apply succeeds,
        the planned LaunchDaemon lands under root, and the receipt is valid
        JSON. Runs with ``LUCY_BOOTSTRAP_TEST_FAKE_ROOT=1`` so non-root test
        runs satisfy the LaunchDaemon-ownership check.
        """
        # Pre-bless the adapter: copy the shipped policy, insert the
        # dev-mock adapter's name + sha256 into the approved_adapters
        # list for ``lucy`` so ``approved_adapter`` accepts it.
        adapter_sha256 = hashlib.sha256(ADAPTER.read_bytes()).hexdigest()
        policy = json.loads(POLICY.read_text())
        policy["agents"]["lucy"]["approved_adapters"] = [
            {"id": ADAPTER.name, "version": "dev-mock-v1", "sha256": adapter_sha256},
        ]
        # Re-seed the watcher_source expected by the shipped lucy.json
        # manifest with bytes from the actual fixture so the preflight
        # verifier doesn't reject a stale digest.
        manifest = json.loads(MANIFEST.read_text())
        manifest["watcher_source"]["sha256"] = hashlib.sha256(WATCHER_SOURCE.read_bytes()).hexdigest()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "approved-policy.json").write_text(json.dumps(policy))
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            env = {**os.environ, "LUCY_BOOTSTRAP_TEST_FAKE_ROOT": "1", "LC_ALL": "C"}
            cli = subprocess.run(
                [
                    sys.executable, str(CLI),
                    "apply",
                    "--root", str(root),
                    "--manifest", str(manifest_path),
                    "--registry", str(REGISTRY),
                    "--policy", str(root / "approved-policy.json"),
                    "--approved-runtime-adapter", str(ADAPTER),
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(cli.returncode, 0, msg=f"stderr={cli.stderr}\nstdout={cli.stdout}")
            self.assertIn("APPLIED:", cli.stdout)
            # Receipt file lands under .mightyos/lucy-bootstrap-v2/receipt.json.
            receipt_path = root / ".mightyos/lucy-bootstrap-v2/receipt.json"
            self.assertTrue(receipt_path.is_file(), msg=f"missing receipt at {receipt_path}")
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(receipt["agent"], "lucy")
            self.assertEqual(receipt["status"], "applied")
            # The Lucy's planner-built plan includes the watcher LaunchDaemon
            # plist under Library/LaunchDaemons; this is the file Kenneth's
            # task description calls out by name.
            expected_plist = root / "Library/LaunchDaemons/com.mightyos.lucy.watcher.plist"
            self.assertTrue(expected_plist.is_file(), msg=f"missing plist at {expected_plist}")


if __name__ == "__main__":
    unittest.main()
