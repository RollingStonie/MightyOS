import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "fleet/bootstrap/v2/lucy_bootstrap.py"
MANIFEST = REPO / "fleet/bootstrap/manifests/lucy.json"
REGISTRY = REPO / "fleet/registry.yaml"
POLICY = REPO / "fleet/bootstrap/v2/registry-policy.json"


class LucyBootstrapV2Tests(unittest.TestCase):
    def run_cli(self, root, *args):
        return subprocess.run(["python3", str(CLI), "--root", str(root), "--manifest", str(MANIFEST), "--registry", str(REGISTRY), "--policy", str(POLICY), *args], text=True, capture_output=True)

    def fake_adapter(self, directory):
        adapter = directory / "fake-mutator.py"
        adapter.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
operation, plan_path, root = sys.argv[1:]
plan = json.loads(pathlib.Path(plan_path).read_text())
root = pathlib.Path(root)
for resource in plan['resources']:
    path = root / resource['path']
    if operation == 'apply':
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(resource.get('content', 'managed'))
    elif operation in {'rollback', 'offboard'}:
        path.unlink(missing_ok=True)
""")
        adapter.chmod(adapter.stat().st_mode | stat.S_IXUSR)
        return adapter

    def test_plan_is_default_and_secret_free(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"mode": "plan"', result.stdout)
        self.assertNotIn('sk-', result.stdout.lower())
        self.assertNotIn('token=', result.stdout.lower())

    def test_validate_rejects_messaging_and_trading_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            raw = Path(raw)
            bad = json.loads(MANIFEST.read_text()); bad['modules'].append('trading-execute')
            path = raw / 'bad.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unapproved modules', result.stderr)

    def test_apply_fails_closed_without_adapter(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw), 'apply', '--owner-uid', '501')
        self.assertEqual(result.returncode, 2)
        self.assertIn('apply denied', result.stderr)

    def test_apply_is_idempotent_and_receipt_has_no_secret_value(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            first = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter))
            second = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter))
            receipt = (root / '.mightyos/lucy-bootstrap-v2/receipt.json').read_text()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('ALREADY_APPLIED', second.stdout)
        self.assertNotIn('INFISICAL_MACHINE_IDENTITY_TOKEN', receipt)
        self.assertNotIn('secret', receipt.lower())

    def test_unmanaged_resource_blocks_apply(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; target = root / 'Library/LaunchDaemons/com.mightyos.lucy.watcher.plist'
            target.parent.mkdir(parents=True); target.write_text('unmanaged')
            result = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(self.fake_adapter(Path(raw))))
        self.assertEqual(result.returncode, 2)
        self.assertIn('unmanaged resource', result.stderr)

    def test_health_and_receipt_scoped_offboard(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            self.assertEqual(self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter)).returncode, 0)
            evidence = {
                'manifest_sha256': __import__('hashlib').sha256(json.dumps(json.loads(MANIFEST.read_text()), sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'tailscale_scoped': True, 'local_health': True, 'reboot_survived': True, 'probation_72h': True,
            }
            evidence_path = root / '.mightyos/lucy-bootstrap-v2/lifecycle-evidence.json'
            evidence_path.write_text(json.dumps(evidence))
            health = self.run_cli(root, 'health')
            offboard = self.run_cli(root, 'offboard', '--approved-runtime-adapter', str(adapter))
            removed = not (root / 'Library/LaunchDaemons/com.mightyos.lucy.watcher.plist').exists()
        self.assertEqual(health.returncode, 0, health.stderr)
        self.assertEqual(offboard.returncode, 0, offboard.stderr)
        self.assertTrue(removed)

    def test_health_rejects_missing_or_incomplete_lifecycle_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            self.assertEqual(self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter)).returncode, 0)
            absent = self.run_cli(root, 'health')
            evidence_path = root / '.mightyos/lucy-bootstrap-v2/lifecycle-evidence.json'
            evidence_path.write_text(json.dumps({'manifest_sha256': 'wrong', 'local_health': True}))
            incomplete = self.run_cli(root, 'health')
        self.assertEqual(absent.returncode, 2)
        self.assertEqual(incomplete.returncode, 2)

    def test_launchdaemon_plan_uses_system_domain_dedicated_account_and_no_autostart(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw), 'plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('<key>UserName</key><string>lucy-compute</string>', result.stdout)
        self.assertIn('<key>RunAtLoad</key><false/>', result.stdout)
        self.assertIn('"launch_domain": "system"', result.stdout)
        self.assertIn('"owner": "root:wheel"', result.stdout)

    def test_secret_environment_and_stale_policy_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            env = {**os.environ, 'INFISICAL_MACHINE_IDENTITY_TOKEN': 'demonstration-only'}
            secret = subprocess.run(['python3', str(CLI), 'plan', '--root', raw], text=True, capture_output=True, env=env)
            stale = json.loads(POLICY.read_text()); stale['source_sha256'] = '0' * 64
            policy = Path(raw) / 'stale.json'; policy.write_text(json.dumps(stale))
            projection = subprocess.run(['python3', str(CLI), 'validate', '--policy', str(policy)], text=True, capture_output=True)
        self.assertEqual(secret.returncode, 2)
        self.assertIn('credentials must not enter', secret.stderr)
        self.assertEqual(projection.returncode, 2)
        self.assertIn('policy projection is stale', projection.stderr)

    def test_tampered_receipt_cannot_expand_offboard_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            self.assertEqual(self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter)).returncode, 0)
            receipt_path = root / '.mightyos/lucy-bootstrap-v2/receipt.json'
            receipt = json.loads(receipt_path.read_text()); receipt['resources'].append('../../unrelated')
            receipt_path.write_text(json.dumps(receipt))
            result = self.run_cli(root, 'offboard', '--approved-runtime-adapter', str(adapter))
        self.assertEqual(result.returncode, 2)
        self.assertIn('receipt resources do not match', result.stderr)

    def test_manifest_cannot_change_reviewed_launchd_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text()); bad['launchd']['labels'][0] = '../../etc/payload'
            path = Path(raw) / 'bad.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('launchd labels', result.stderr)


if __name__ == '__main__':
    unittest.main()
