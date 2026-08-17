import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "fleet/bootstrap/v2/lucy_bootstrap.py"
MANIFEST = REPO / "fleet/bootstrap/manifests/lucy.json"
REGISTRY = REPO / "fleet/registry.yaml"
POLICY = REPO / "fleet/bootstrap/v2/registry-policy.json"
WATCHER_SOURCE = REPO / 'tests/fixtures/a008-agent_watcher.py'
SPEC = importlib.util.spec_from_file_location('lucy_bootstrap', CLI)
BOOTSTRAP = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(BOOTSTRAP)


class LucyBootstrapV2Tests(unittest.TestCase):
    def run_cli(self, root, *args):
        command = ["python3", str(CLI), "--root", str(root), "--manifest", str(MANIFEST), "--registry", str(REGISTRY), "--policy", str(POLICY), *args]
        if '--approved-runtime-adapter' in args:
            adapter = Path(args[args.index('--approved-runtime-adapter') + 1])
            policy = json.loads(POLICY.read_text())
            policy['agents']['lucy']['approved_adapters'] = [{'id': adapter.name, 'version': 'test-v1', 'sha256': __import__('hashlib').sha256(adapter.read_bytes()).hexdigest()}]
            path = root.parent / 'approved-policy.json'; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(policy))
            command[command.index('--policy') + 1] = str(path)
        return subprocess.run(command, text=True, capture_output=True, env={**os.environ, 'LUCY_BOOTSTRAP_TEST_FAKE_ROOT': '1'})

    def fake_adapter(self, directory):
        adapter = directory / "fake-mutator.py"
        payload = WATCHER_SOURCE.read_bytes().hex()
        adapter.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
operation, plan_path, root = sys.argv[1:]
plan = json.loads(pathlib.Path(plan_path).read_text())
root = pathlib.Path(root)
source = root / plan['watcher_source']['path']
def _try_chown(p):
    try:
        os.chown(p, 0, 0)
    except (PermissionError, OSError):
        pass
if operation == 'preflight':
    source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(bytes.fromhex('""" + payload + """')); source.chmod(int(plan['watcher_source']['mode'], 8)); _try_chown(source)
for resource in plan['resources']:
    path = root / resource['path']
    if operation == 'apply':
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(resource.get('content', 'managed')); path.chmod(int(resource['mode'], 8)); _try_chown(path)
    elif operation in {'rollback', 'offboard'}:
        path.unlink(missing_ok=True)
if operation == 'apply' or operation == 'preflight':
    print(json.dumps({'binding': plan['binding'], 'watcher_source': plan['watcher_source'], 'resources': [{key: r[key] for key in ('path', 'content_sha256', 'mode', 'owner', 'launch_domain', 'run_as')} for r in plan['resources']]}))
else:
    print(json.dumps({'binding': plan['binding'], 'watcher_source': plan['watcher_source'], 'resources': plan['resources']}))
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

    def test_dry_run_apply_produces_plan_output_without_mutating(self):
        """Warning #5: --dry-run on apply prints the plan JSON and exits 0 without any mutation."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'
            result = self.run_cli(root, 'apply', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['mode'], 'plan')
        self.assertNotIn('APPLIED', result.stdout)
        # Confirm no receipt directory was created under the fake root.
        self.assertFalse((Path(raw) / 'fake-root' / '.mightyos').exists())

    def test_dry_run_rollback_does_not_crash_or_mutate(self):
        """Warning #5: --dry-run on rollback without an applied receipt prints plan (no crash, no mutation)."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'
            # write a non-zero adapter file so run_cli's sha256 helper accepts it
            adapter_path = Path(raw) / 'unused.py'; adapter_path.write_text('#!/usr/bin/env python3\n')
            result = self.run_cli(root, 'rollback', '--dry-run', '--approved-runtime-adapter', str(adapter_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['mode'], 'plan')
        # Confirm no receipt directory was created.
        self.assertFalse((Path(raw) / 'fake-root' / '.mightyos').exists())

    def test_dry_run_apply_matches_explicit_plan_output(self):
        """Warning #5: --dry-run on apply produces identical plan output to the explicit 'plan' command."""
        with tempfile.TemporaryDirectory() as raw:
            plan_root = Path(raw) / 'plan-root'
            dry_root = Path(raw) / 'dry-root'
            plan_result = self.run_cli(plan_root, 'plan')
            dry_result = self.run_cli(dry_root, 'apply', '--dry-run')
        self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
        self.assertEqual(dry_result.returncode, 0, dry_result.stderr)
        plan_obj = json.loads(plan_result.stdout); dry_obj = json.loads(dry_result.stdout)
        # root path differs by construction; normalize.
        self.assertEqual(plan_obj['manifest_sha256'], dry_obj['manifest_sha256'])
        self.assertEqual(plan_obj['binding'], dry_obj['binding'])
        self.assertEqual(plan_obj['resources'], dry_obj['resources'])

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

    def test_launchdaemon_plan_uses_system_domain_dedicated_account_and_runs_at_load(self):
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw), 'plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('<key>UserName</key><string>contenthub-prod</string>', result.stdout)
        self.assertIn('<key>RunAtLoad</key><true/>', result.stdout)
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
        original_sys_path = sys.path.copy()
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
            sys.path[:] = original_sys_path

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

    def test_watcher_source_digest_and_mode_drift_block_apply_reuse_and_health(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'; adapter = self.fake_adapter(Path(raw))
            self.assertEqual(self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter)).returncode, 0)
            source = root / 'opt/mightyos/a008/tools/watcher/agent_watcher.py'
            source.write_text('drift'); source.chmod(0o666)
            reused = self.run_cli(root, 'apply', '--approved-runtime-adapter', str(adapter))
            health = self.run_cli(root, 'health')
        self.assertEqual(reused.returncode, 2)
        self.assertIn('watcher source digest', reused.stderr)
        self.assertEqual(health.returncode, 2)

    def test_watcher_source_rejects_wrong_owner_and_writable_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / 'opt/mightyos/a008/tools/watcher/agent_watcher.py'
            source.parent.mkdir(parents=True); source.write_bytes(WATCHER_SOURCE.read_bytes()); source.chmod(0o644)
            plan = {'watcher_source': json.loads(MANIFEST.read_text())['watcher_source']}
            real = os.stat
            class Facts:
                st_uid = 0; st_gid = 0; st_mode = 0o100644
            def wrong_owner(path):
                return type('S', (), {'st_uid': 501, 'st_gid': 0, 'st_mode': 0o100644})()
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'root:wheel'):
                BOOTSTRAP.verify_watcher_source(plan, root, stat_fn=wrong_owner)
            def writable_parent(path):
                if path == source: return type('S', (), {'st_uid': 0, 'st_gid': 0, 'st_mode': 0o100644})()
                return type('S', (), {'st_uid': 0, 'st_gid': 0, 'st_mode': 0o40775})()
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'parent path'):
                BOOTSTRAP.verify_watcher_source(plan, root, stat_fn=writable_parent)

    def test_hermes_is_now_enabled_and_policy_projects_null_discord_identity(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertTrue(manifest['hermes']['enabled'])
        self.assertEqual(manifest['hermes']['profile_name'], 'lucy')
        self.assertEqual(manifest['hermes']['surface'], 'discord')
        self.assertTrue(manifest['hermes']['channel'].startswith('#'))
        self.assertIn('hermes-profile', manifest['modules'])
        policy = json.loads(POLICY.read_text())
        # Canonical A008 registry sets Lucy discord_identity=null (compute host
        # does not earn a bot identity by default). The policy projection must mirror
        # that, not the old "lucy-bot" string.
        self.assertIsNone(policy['agents']['lucy']['discord_identity'])
        with tempfile.TemporaryDirectory() as raw:
            result = self.run_cli(Path(raw), 'plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan['hermes_enabled'])
        self.assertEqual(plan['hermes_profile'], 'lucy')

    def test_lucy_role_is_hannah_contenthub_pc_with_new_account_and_tag(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(manifest['role'], 'hannah-contenthub-pc')
        self.assertEqual(manifest['service_account'], 'contenthub-prod')
        self.assertEqual(manifest['network']['tailscale_tag'], 'tag:contenthub-prod')
        self.assertEqual(manifest['network']['tailscale_acl'], 'tag:contenthub-prod')
        self.assertEqual(BOOTSTRAP.EXPECTED_ACCOUNT, 'contenthub-prod')
        self.assertEqual(BOOTSTRAP.EXPECTED_TAILSCALE_TAG, 'tag:contenthub-prod')

    def test_lucy_required_grants_match_new_role_contract(self):
        manifest = json.loads(MANIFEST.read_text())
        policy = json.loads(POLICY.read_text())
        # Canonical A008 grants for Lucy (registry/fleet-agents.yaml). The bootstrap
        # planner hardcodes the same set in EXPECTED_GRANTS so a future registry edit
        # without a planner update must be caught by parity validation.
        expected = {"contenthub-creator", "contenthub-scanner", "contenthub-render-worker", "qmd-runtime", "hermes-profile", "a008-dev-instance", "hannah-ssh-access", "heavy-compute"}
        self.assertEqual(set(manifest['required_grants']), expected)
        self.assertEqual(set(BOOTSTRAP.EXPECTED_GRANTS), expected)
        self.assertEqual(set(policy['agents']['lucy']['grants']), expected)
        self.assertEqual(set(manifest['denied_grants']), {"publish", "email.send", "crm.write", "trading.execute", "ollama-daemon", "portable-sleep-mode", "caffeinate-wrapper"})

    def test_lucy_hannah_contenthub_and_qmd_modules_present(self):
        manifest = json.loads(MANIFEST.read_text())
        for module in ('hermes-profile', 'a008-dev-instance', 'git-clone-mirror', 'hannah-user-account', 'contenthub-creator', 'contenthub-scanner', 'contenthub-render-worker', 'qmd-runtime'):
            self.assertIn(module, manifest['modules'])
            self.assertIn(module, BOOTSTRAP.ALLOWED_MODULES)
        self.assertNotIn('caffeinate-wrapper', manifest['modules'])
        self.assertIn('caffeinate-wrapper', BOOTSTRAP.FORBIDDEN_MODULES)

    def test_lucy_qmd_block_declares_hannah_as_primary(self):
        manifest = json.loads(MANIFEST.read_text())
        qmd = manifest['qmd']
        self.assertTrue(qmd['enabled'])
        self.assertEqual(qmd['scope'], 'contenthub-prod-pc')
        self.assertEqual(qmd['primary_user'], 'hannah')
        self.assertIn('hannah', qmd['users'])
        self.assertIn('kenneth', qmd['users'])

    def test_lucy_qmd_execution_mode_documents_headless_launchdaemon_decision(self):
        """Open Q1 (adversarial review): qmd runs as the contenthub-prod LaunchDaemon,
        not a per-user LaunchAgent. The manifest must pin this decision and the reason
        so future adapter writes cannot silently flip QMD to a GUI/keychain tool.
        """
        qmd = json.loads(MANIFEST.read_text())['qmd']
        self.assertEqual(qmd['qmd_execution_mode'], 'launchdaemon-as-contenthub-prod')
        self.assertIsInstance(qmd['qmd_execution_reason'], str)
        self.assertTrue(qmd['qmd_execution_reason'].strip(), 'qmd_execution_reason must be non-empty to document Open Q1')
        # 'users' stays as the auth/access roster, not session identity.
        self.assertEqual(set(qmd['users']), {'hannah', 'kenneth'})
        self.assertEqual(qmd['primary_user'], 'hannah')

    def test_lucy_validate_rejects_qmd_execution_mode_outside_documented_set(self):
        """An undocumented qmd_execution_mode must break the planner so the decision
        cannot drift to a value the team did not review (e.g. raw 'launchagent'
        without a per-user label, or an unknown mode invented by a future adapter).
        """
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            bad['qmd']['qmd_execution_mode'] = 'launchagent-as-hannah'
            path = Path(raw) / 'bad-mode.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('qmd_execution_mode', result.stderr)

    def test_lucy_validate_requires_reason_when_qmd_execution_mode_is_undecided(self):
        """'undecided' is temporarily accepted so a future adapter author can flip
        the mode without a new schema bump, but only if they also document the
        unresolved QMD runtime requirements in qmd_execution_reason.
        """
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            bad['qmd']['qmd_execution_mode'] = 'undecided'
            bad['qmd'].pop('qmd_execution_reason', None)
            path = Path(raw) / 'undecided.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('qmd_execution_reason', result.stderr)

    def test_lucy_network_exposes_hannah_ssh_and_contenthub_web_ui_only_via_tailscale(self):
        network = json.loads(MANIFEST.read_text())['network']
        ssh = network['hannah_ssh']
        self.assertTrue(ssh['enabled'])
        self.assertEqual(ssh['user'], 'hannah')
        self.assertEqual(ssh['ssh_key_source'], 'infisical')
        self.assertEqual(ssh['firewall'], 'tailscale-only')
        web_ui = network['contenthub_web_ui']
        self.assertTrue(web_ui['enabled'])
        self.assertEqual(web_ui['external_access'], 'tailscale-only')
        self.assertEqual(web_ui['auth'], 'plane-or-discord')

    def test_lucy_power_contract_is_always_on_with_no_caffeinate(self):
        manifest = json.loads(MANIFEST.read_text())
        power = manifest['power']
        self.assertTrue(power['always_on_when_powered'])
        self.assertFalse(power['caffeinate_required_when_docked'])
        self.assertEqual(manifest['launchd']['run_at_load'], True)

    def test_lucy_launchd_labels_cover_contenthub_and_qmd(self):
        manifest = json.loads(MANIFEST.read_text())
        labels = manifest['launchd']['labels']
        self.assertIn('com.mightyos.lucy.watcher', labels)
        self.assertIn('com.mightyos.lucy.hermes-bot', labels)
        self.assertIn('com.mightyos.contenthub.fastapi', labels)
        self.assertIn('com.mightyos.contenthub.celery-worker', labels)
        self.assertIn('com.mightyos.contenthub.celery-render-worker', labels)
        self.assertIn('com.mightyos.qmd.runtime', labels)
        self.assertEqual(labels, BOOTSTRAP.EXPECTED_LABELS)

    def test_lucy_hannah_ssh_key_path_is_infisical_mounted(self):
        secrets = json.loads(MANIFEST.read_text())['secrets']
        self.assertEqual(secrets['hannah_ssh_key_path'], '/hannah/ssh-keys/id_ed25519.pub')
        self.assertEqual(BOOTSTRAP.EXPECTED_HANNAH_SSH_KEY_PATH, '/hannah/ssh-keys/id_ed25519.pub')
        self.assertEqual(secrets['allowed_scopes'], ['/lucy/runtime', '/hannah/ssh-keys', '/contenthub/runtime'])

    def test_lucy_adapter_required_marker_covers_contenthub_and_qmd_labels(self):
        manifest = json.loads(MANIFEST.read_text())
        adapter_required = manifest['launchd']['adapter_required']
        for label in ('com.mightyos.contenthub.fastapi', 'com.mightyos.contenthub.celery-worker', 'com.mightyos.contenthub.celery-render-worker', 'com.mightyos.qmd.runtime'):
            self.assertIn(label, adapter_required, f"{label} must declare an adapter_required contract")
            self.assertTrue(adapter_required[label].strip(), f"{label} contract must be non-empty")
        # planner-owned labels must NOT appear in adapter_required
        self.assertNotIn('com.mightyos.lucy.watcher', adapter_required)
        self.assertNotIn('com.mightyos.lucy.hermes-bot', adapter_required)

    def test_lucy_plan_emits_fail_loud_sentinel_for_adapter_required_labels(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = json.loads(self.run_cli(Path(raw), 'plan').stdout)
        plists_by_label = {item['path']: item['content'] for item in plan['resources'] if item['path'].startswith('Library/LaunchDaemons/')}
        sentinel_prefix = BOOTSTRAP.ADAPTER_REQUIRED_SENTINEL_PREFIX
        for label in ('com.mightyos.contenthub.fastapi', 'com.mightyos.contenthub.celery-worker', 'com.mightyos.contenthub.celery-render-worker', 'com.mightyos.qmd.runtime'):
            plist = plists_by_label[f'Library/LaunchDaemons/{label}.plist']
            self.assertIn(sentinel_prefix, plist, f"{label} plist must carry the fail-loud sentinel")
            self.assertNotIn('/usr/bin/env', plist.split('ProgramArguments')[1].split('</array>')[0] if 'ProgramArguments' in plist else '', f"{label} must not silently fall back to /usr/bin/env true")
        # planner-owned labels retain their real commands
        watcher = plists_by_label['Library/LaunchDaemons/com.mightyos.lucy.watcher.plist']
        self.assertIn('lucy-watcher-loopback.py', watcher)
        self.assertNotIn(sentinel_prefix, watcher)
        hermes = plists_by_label['Library/LaunchDaemons/com.mightyos.lucy.hermes-bot.plist']
        self.assertIn('hermes.runtime', hermes)
        self.assertNotIn(sentinel_prefix, hermes)

    def test_lucy_validate_rejects_missing_adapter_required_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            del bad['launchd']['adapter_required']['com.mightyos.contenthub.fastapi']
            path = Path(raw) / 'missing-marker.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('adapter_required', result.stderr)

    def test_lucy_validate_rejects_adapter_required_marker_on_planner_owned_label(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            bad['launchd']['adapter_required']['com.mightyos.lucy.watcher'] = 'should not be here'
            path = Path(raw) / 'wrong-marker.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('planner-owned', result.stderr)

    def test_lucy_validate_rejects_empty_adapter_required_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            bad['launchd']['adapter_required']['com.mightyos.contenthub.fastapi'] = '   '
            path = Path(raw) / 'empty-contract.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('non-empty contract', result.stderr)

    def test_lucy_validate_rejects_adapter_required_referencing_unknown_label(self):
        with tempfile.TemporaryDirectory() as raw:
            bad = json.loads(MANIFEST.read_text())
            bad['launchd']['adapter_required']['com.example.unknown'] = 'rogue'
            path = Path(raw) / 'unknown-label.json'; path.write_text(json.dumps(bad))
            result = subprocess.run(['python3', str(CLI), 'validate', '--manifest', str(path), '--registry', str(REGISTRY), '--policy', str(POLICY)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown labels', result.stderr)

    def test_lucy_verify_launchdaemon_ownership_passes_when_root_owned(self):
        """Critical #3: a LaunchDaemon file owned by uid=0, gid=0 must pass."""
        plist = Path('/Library/LaunchDaemons/com.example.label.plist')
        self.assertTrue(BOOTSTRAP._is_launchdaemon_path(plist))
        def root_owned(path):
            return type('S', (), {'st_uid': 0, 'st_gid': 0, 'st_mode': 0o100644})()
        BOOTSTRAP.verify_launchdaemon_ownership(plist, stat_fn=root_owned)

    def test_lucy_verify_launchdaemon_ownership_fails_on_wrong_uid(self):
        """Critical #3: a LaunchDaemon file with uid=501 (invoking user) must be rejected."""
        plist = Path('/Library/LaunchDaemons/com.example.label.plist')
        def invoking_user(path):
            return type('S', (), {'st_uid': 501, 'st_gid': 0, 'st_mode': 0o100644})()
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'root:wheel'):
            BOOTSTRAP.verify_launchdaemon_ownership(plist, stat_fn=invoking_user)

    def test_lucy_verify_launchdaemon_ownership_fails_on_wrong_gid(self):
        """Critical #3: a LaunchDaemon file with gid=20 (wrong group) must be rejected."""
        plist = Path('/Library/LaunchDaemons/com.example.label.plist')
        def wrong_group(path):
            return type('S', (), {'st_uid': 0, 'st_gid': 20, 'st_mode': 0o100644})()
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'root:wheel'):
            BOOTSTRAP.verify_launchdaemon_ownership(plist, stat_fn=wrong_group)

    def test_lucy_verify_launchdaemon_ownership_ignores_non_launchdaemon_paths(self):
        """Non-LaunchDaemon paths must not be subject to root:wheel enforcement."""
        libexec = Path('/opt/mightyos/libexec/lucy-watcher-loopback.py')
        self.assertFalse(BOOTSTRAP._is_launchdaemon_path(libexec))
        def invoking_user(path):
            return type('S', (), {'st_uid': 501, 'st_gid': 20, 'st_mode': 0o100755})()
        BOOTSTRAP.verify_launchdaemon_ownership(libexec, stat_fn=invoking_user)

    def test_lucy_resource_filesystem_facts_rejects_wrong_ownership(self):
        """Critical #3: the post-apply/idempotency checker must reject wrong LaunchDaemon ownership."""
        plist_path = Path('Library/LaunchDaemons/com.mightyos.lucy.watcher.plist')
        plan_resource = {'path': str(plist_path), 'mode': '0644', 'content': 'managed', 'content_sha256': hashlib.sha256(b'managed').hexdigest()}
        plan = {'resources': [plan_resource]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / plist_path
            target.parent.mkdir(parents=True)
            target.write_bytes(b'managed')
            target.chmod(0o644)
            real_chown = BOOTSTRAP.verify_launchdaemon_ownership
            try:
                # Simulate a misconfigured adapter: ownership check sees uid=501.
                BOOTSTRAP.verify_launchdaemon_ownership = lambda path, **kwargs: real_chown(path, stat_fn=lambda _p: type('S', (), {'st_uid': 501, 'st_gid': 0, 'st_mode': 0o100644})(), enforce_owner=True)
                with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'root:wheel'):
                    BOOTSTRAP._resource_filesystem_facts(root, plan)
            finally:
                BOOTSTRAP.verify_launchdaemon_ownership = real_chown

    def test_lucy_resource_filesystem_facts_passes_when_root_owned(self):
        """Critical #3: when LaunchDaemon ownership is root:wheel, the post-apply check passes."""
        plist_path = Path('Library/LaunchDaemons/com.mightyos.lucy.watcher.plist')
        plan_resource = {'path': str(plist_path), 'mode': '0644', 'content': 'managed', 'content_sha256': hashlib.sha256(b'managed').hexdigest()}
        plan = {'resources': [plan_resource]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / plist_path
            target.parent.mkdir(parents=True)
            target.write_bytes(b'managed')
            target.chmod(0o644)
            real_chown = BOOTSTRAP.verify_launchdaemon_ownership
            try:
                BOOTSTRAP.verify_launchdaemon_ownership = lambda path, **kwargs: real_chown(path, stat_fn=lambda _p: type('S', (), {'st_uid': 0, 'st_gid': 0, 'st_mode': 0o100644})(), enforce_owner=True)
                BOOTSTRAP._resource_filesystem_facts(root, plan)  # must not raise
            finally:
                BOOTSTRAP.verify_launchdaemon_ownership = real_chown

    def test_lucy_idempotency_denies_when_launchdaemon_ownership_drifts(self):
        """Critical #3: the idempotency checker (``_resource_filesystem_facts``) must
        reject LaunchDaemon ownership drift so a re-apply cannot silently pass when an
        out-of-band change replaces a root-owned plist with a user-owned one.

        The same function backs post-apply, idempotency, and health checks; exercising it
        directly proves the idempotency path without racing the watcher-source ownership
        check (which fires first in the CLI and would mask this specific drift).
        """
        plist_path = Path('Library/LaunchDaemons/com.mightyos.lucy.watcher.plist')
        plan_resource = {'path': str(plist_path), 'mode': '0644', 'content': 'managed', 'content_sha256': hashlib.sha256(b'managed').hexdigest()}
        plan = {'resources': [plan_resource]}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'fake-root'
            root.mkdir()
            target = root / plist_path
            target.parent.mkdir(parents=True)
            target.write_bytes(b'managed')
            target.chmod(0o644)
            real_chown = BOOTSTRAP.verify_launchdaemon_ownership
            try:
                # invoke the verifier with enforce_owner=True (production semantics)
                # but the stat result claims uid=501 — the drift scenario.
                BOOTSTRAP.verify_launchdaemon_ownership = lambda path, **kwargs: real_chown(path, stat_fn=lambda _p: type('S', (), {'st_uid': 501, 'st_gid': 0, 'st_mode': 0o100644})(), enforce_owner=True)
                with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, 'root:wheel'):
                    BOOTSTRAP._resource_filesystem_facts(root, plan)
            finally:
                BOOTSTRAP.verify_launchdaemon_ownership = real_chown


if __name__ == '__main__':
    unittest.main()
