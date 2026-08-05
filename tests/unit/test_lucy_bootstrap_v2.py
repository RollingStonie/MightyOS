import json
import os
import stat
import subprocess
import sys
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
        command = ["python3", str(CLI), "--root", str(root), "--manifest", str(MANIFEST), "--registry", str(REGISTRY), "--policy", str(POLICY), *args]
        if '--approved-runtime-adapter' in args:
            adapter = Path(args[args.index('--approved-runtime-adapter') + 1])
            policy = json.loads(POLICY.read_text())
            policy['approved_adapters'] = [{'id': adapter.name, 'version': 'test-v1', 'sha256': __import__('hashlib').sha256(adapter.read_bytes()).hexdigest()}]
            path = root.parent / 'approved-policy.json'; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(policy))
            command[command.index('--policy') + 1] = str(path)
        return subprocess.run(command, text=True, capture_output=True)

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
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(resource.get('content', 'managed')); path.chmod(int(resource['mode'], 8))
    elif operation in {'rollback', 'offboard'}:
        path.unlink(missing_ok=True)
if operation == 'apply':
    print(json.dumps({'binding': plan['binding'], 'resources': [{key: r[key] for key in ('path', 'content_sha256', 'mode', 'owner', 'launch_domain', 'run_as')} for r in plan['resources']]}))
else:
    print(json.dumps({'binding': plan['binding'], 'resources': plan['resources']}))
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
            plan = json.loads(self.run_cli(root, 'plan').stdout)
            evidence['binding'] = plan['binding']
            evidence['launchd'] = [{key: r[key] for key in ('path', 'content_sha256', 'mode', 'owner', 'launch_domain', 'run_as')} for r in plan['resources']]
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

    def test_watcher_wrapper_enforces_exact_loopback_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw), 'plan')
            plan = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper = next(item['content'] for item in plan['resources'] if item['path'].endswith('lucy-watcher-loopback.py'))
        watcher = next(item['content'] for item in plan['resources'] if item['path'].endswith('watcher.plist'))
        self.assertIn("_expected = ('127.0.0.1', 8109)", wrapper)
        self.assertIn('_a008_root = "/opt/mightyos/a008"', wrapper)
        self.assertIn('sys.path.insert(0, _a008_root)', wrapper)
        self.assertIn('super().__init__(_expected', wrapper)
        self.assertIn('/opt/mightyos/libexec/lucy-watcher-loopback.py', watcher)

    def test_watcher_wrapper_executes_import_setup_without_opening_listener(self):
        import http.server
        import runpy
        with tempfile.TemporaryDirectory() as raw:
            plan = json.loads(self.run_cli(Path(raw), 'plan').stdout)
            wrapper = next(item['content'] for item in plan['resources'] if item['path'].endswith('lucy-watcher-loopback.py'))
        original_server, original_run_path = http.server.ThreadingHTTPServer, runpy.run_path
        observed = {}
        def fake_run_path(path, run_name):
            observed['path'], observed['run_name'], observed['root'] = path, run_name, sys.path[0]
            return {}
        try:
            runpy.run_path = fake_run_path
            exec(compile(wrapper, '<lucy-wrapper>', 'exec'), {})
            self.assertEqual(observed, {'path': '/opt/mightyos/a008/tools/watcher/agent_watcher.py', 'run_name': '__main__', 'root': '/opt/mightyos/a008'})
            with self.assertRaises(RuntimeError):
                http.server.ThreadingHTTPServer(('0.0.0.0', 9999), object)
        finally:
            http.server.ThreadingHTTPServer, runpy.run_path = original_server, original_run_path

    def test_watcher_binding_mismatch_is_rejected_before_plan(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text()); bad['network']['bind_address'] = '0.0.0.0'
            path = Path(raw) / 'public.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'plan', '--manifest', str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('bind localhost', result.stderr)

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
        self.assertIn('receipt contains invalid resource facts', result.stderr)

    def test_manifest_cannot_change_reviewed_launchd_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text()); bad['launchd']['labels'][0] = '../../etc/payload'
            path = Path(raw) / 'bad.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('launchd labels', result.stderr)

    def test_closed_manifest_rejects_scope_acl_grant_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as raw:
            for field, value in [('secrets', {'required_names': ['INFISICAL_MACHINE_IDENTITY_TOKEN'], 'allowed_scopes': ['/too-broad']}), ('network', {**json.loads(MANIFEST.read_text())['network'], 'tailscale_tag': 'tag:any'}), ('required_grants', ['dev'])]:
                bad = json.loads(MANIFEST.read_text()); bad[field] = value
                path = Path(raw) / f'{field}.json'; path.write_text(json.dumps(bad))
                result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
            bad = json.loads(MANIFEST.read_text()); bad['unexpected'] = True
            path = Path(raw) / 'unknown.json'; path.write_text(json.dumps(bad))
            self.assertEqual(subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path)], text=True, capture_output=True).returncode, 2)

    def test_adapter_must_be_allowlisted_and_resource_drift_blocks_idempotency(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            denied = subprocess.run(['python3', str(CLI), 'apply', '--root', str(root), '--approved-runtime-adapter', str(adapter)], text=True, capture_output=True)
            self.assertEqual(denied.returncode, 2); self.assertIn('not allowlisted', denied.stderr)
            self.assertEqual(self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter)).returncode, 0)
            target = root / 'Library/LaunchDaemons/com.mightyos.lucy.watcher.plist'; target.write_text('drift')
            drift = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter))
        self.assertEqual(drift.returncode, 2)

    def test_owner_uid_is_removed_and_adapter_failure_is_redacted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = Path(raw) / 'loud-adapter.py'
            adapter.write_text('#!/bin/sh\necho SECRET_SENTINEL >&2\nexit 1\n'); adapter.chmod(0o755)
            uid = self.run_cli(root, 'plan', '--owner-uid', '501')
            failed = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter))
        self.assertEqual(uid.returncode, 2)
        self.assertEqual(failed.returncode, 2)
        self.assertNotIn('SECRET_SENTINEL', failed.stderr)
        self.assertIn('output redacted', failed.stderr)


if __name__ == '__main__':
    unittest.main()
