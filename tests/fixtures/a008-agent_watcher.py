#!/usr/bin/env python3
"""
tools/watcher/agent_watcher.py — Multi-PC Agent Watcher daemon (N2.8, Plane AOS8-8).

Architecture copied from ctrl's multi-daemon pattern (deep-ingest verdict
2026-07-12, ai/specs/2026-07-12-agentic-os-n2-autonomy-spec.md Decision 2/3):
one thin stdlib daemon per machine, watching JSONL transcripts + git repos,
serving live-session presence over the Tailnet. GET only, no mutations, no
secrets in any response.

  GET /agents-live  — Claude Code / Hermes / Codex sessions on this machine
  GET /repos        — git health for every ACTIVE registry/projects.yaml entry
  GET /health       — {ok, uptime}

Reads (never writes) ~/.claude/projects/*/*.jsonl mtimes + a bounded 64KB
tail parse (best-effort, never full contents), ~/.hermes/cron/output/*
mtimes, and ~/.codex/sessions (best-effort, omitted if absent).

Binds 0.0.0.0 intentionally: Tailnet-only exposure (100.64.0.0/10 CGNAT
range, not internet-routable) is the accepted estate convention for this
class of daemon — see tools/plists/README.md.

Usage:
  python3 -m tools.watcher.agent_watcher
"""
import http.server
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent          # A008_Agentic OS/
AG_MISSION_ROOT = REPO_ROOT.parents[1]                              # AG_Mission/
REGISTRY_PROJECTS = REPO_ROOT / "registry" / "projects.yaml"

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
HERMES_CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

PORT = 8109
from tools.nightshift.config import MACHINE_ID  # noqa: E402

ACTIVE_MINUTES = 15       # Claude/Codex: mtime under this = "active"
IDLE_MAX_MINUTES = 60     # beyond this the session is dropped, not just "idle"
HERMES_ACTIVE_MINUTES = 20
TAIL_BYTES = 64 * 1024    # never read more of a transcript than this

_START_TIME = time.time()


# ── pure: project-dir decoding ────────────────────────────────────────────

def sanitize_path(path_str):
    """Reverse-engineers the Claude Code project-dir encoding: every
    non-alphanumeric character in an absolute path becomes '-'. Forward-only
    (lossy), but that's all we need to *compare* a candidate path against an
    already-encoded directory name."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path_str))


def decode_project_dir(dirname, projects=None, ag_mission_root=None):
    """Best-effort short project label from a Claude-Code-encoded project dir
    name. Exact match against a registered project's absolute path
    (AG_MISSION_ROOT/path, sanitized the same way) wins; otherwise the last
    non-empty '-'-token is returned as a readable fallback."""
    if projects and ag_mission_root:
        for p in projects:
            if not p.get("path"):
                continue
            full = str(Path(ag_mission_root) / p["path"])
            if sanitize_path(full) == dirname:
                return p["id"]
    tokens = [t for t in dirname.split("-") if t]
    return tokens[-1] if tokens else dirname


# ── pure: session-state classification ────────────────────────────────────

def classify_session_state(age_minutes, active_minutes=ACTIVE_MINUTES,
                           idle_max_minutes=IDLE_MAX_MINUTES):
    """'active' | 'idle' | None (drop — too stale to report as a live session)."""
    if age_minutes < active_minutes:
        return "active"
    if age_minutes < idle_max_minutes:
        return "idle"
    return None


# ── pure: bounded tail read + best-effort jsonl parsing ───────────────────

def tail_bytes(path, n=TAIL_BYTES):
    """Last n bytes of a file, best-effort. Empty string on any failure —
    never raises. read(n) (not read()) so this holds even if the file grows
    between stat() and read() — a live-appended transcript."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > n:
                f.seek(size - n)
            return f.read(n).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def parse_last_jsonl_entry(text):
    """Last parseable JSON object in a (possibly truncated) tail of a jsonl
    file. {} if none parse — a tail read can cut the first line mid-object,
    which is fine, this is best-effort only."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


CURRENT_TASK_MAX_CHARS = 80


def extract_current_task(text):
    """Best-effort 'what is this agent doing right now' — the most recent
    TaskCreate tool_use call's activeForm (present-tense framing, e.g.
    'Building roster panel'), falling back to its subject. None if no
    TaskCreate call appears anywhere in the tail (most sessions, most of the
    time — task tracking is opt-in, not every turn uses it).

    Deliberately does NOT correlate TaskUpdate calls (which carry only a
    taskId + status, no text) back to their originating TaskCreate — that
    task's TaskCreate may be outside the bounded 64KB tail on a long
    session. 'Most recently created task' is a reasonable live-glance proxy
    for the pixel-office label, not a precise current-task-list
    reconstruction — this is a fun visualization, not an audit trail."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "TaskCreate":
                continue
            task_input = block.get("input") or {}
            label = task_input.get("activeForm") or task_input.get("subject")
            if label:
                return str(label)[:CURRENT_TASK_MAX_CHARS]
    return None


def extract_model_and_activity(entry):
    """Best-effort (model, last_activity_iso) from one Claude Code transcript
    line; both None if the shape doesn't match. Never raises."""
    model = None
    msg = entry.get("message") if isinstance(entry, dict) else None
    if isinstance(msg, dict):
        model = msg.get("model")
    ts = entry.get("timestamp") if isinstance(entry, dict) else None
    return model, ts


# ── source scanners (thin filesystem edges, all injectable) ──────────────

def scan_claude_sessions(projects_dir=CLAUDE_PROJECTS_DIR, projects=None,
                         ag_mission_root=AG_MISSION_ROOT, now=None):
    """One entry per ~/.claude/projects/<dir>/*.jsonl, using only the most
    recently modified transcript per project dir. Sessions older than
    IDLE_MAX_MINUTES are dropped entirely."""
    now = now or datetime.now(timezone.utc)
    p = Path(projects_dir)
    if not p.is_dir():
        return []
    sessions = []
    for project_dir in sorted(p.iterdir()):
        if not project_dir.is_dir():
            continue
        try:
            candidate_files = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        # Per-file stat guard: a file vanishing/rotating mid-scan (broken
        # symlink, concurrent rotation) drops only that file, never the
        # whole project's session.
        candidates = []
        for f in candidate_files:
            try:
                candidates.append((f, f.stat().st_mtime))
            except OSError:
                continue
        if not candidates:
            continue
        latest, latest_mtime = max(candidates, key=lambda pair: pair[1])
        mtime = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
        age_min = (now - mtime).total_seconds() / 60
        state = classify_session_state(age_min)
        if state is None:
            continue
        model, last_activity = None, mtime.isoformat()
        tail_text = tail_bytes(latest)
        entry = parse_last_jsonl_entry(tail_text)
        parsed_model, parsed_ts = extract_model_and_activity(entry)
        model = parsed_model or model
        last_activity = parsed_ts or last_activity
        current_task = extract_current_task(tail_text)
        project_label = decode_project_dir(project_dir.name, projects, ag_mission_root)
        sessions.append({"agent": "claude-code", "project": project_label,
                         "model": model, "last_activity": last_activity, "state": state,
                         "current_task": current_task})
    return sessions


def scan_hermes_session(cron_output_dir=HERMES_CRON_OUTPUT, now=None,
                        active_minutes=HERMES_ACTIVE_MINUTES):
    """The single most-recently-touched file across all Hermes cron job output
    dirs, if within active_minutes — Hermes runs one job at a time so this is
    "the current job" (or None if nothing fired recently)."""
    now = now or datetime.now(timezone.utc)
    p = Path(cron_output_dir)
    if not p.is_dir():
        return None
    newest_mtime, newest_job = None, None
    for job_dir in p.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            files = [f for f in job_dir.iterdir() if f.is_file()]
        except OSError:
            continue
        for f in files:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime, newest_job = mtime, job_dir.name
    if newest_mtime is None:
        return None
    age_min = (now - newest_mtime).total_seconds() / 60
    if age_min >= active_minutes:
        return None
    return {"agent": "hermes", "project": newest_job, "model": None,
            "last_activity": newest_mtime.isoformat(), "state": "active"}


def scan_codex_sessions(sessions_dir=CODEX_SESSIONS_DIR, now=None):
    """Best-effort — Codex session layout isn't guaranteed; any failure to
    read the directory just omits Codex sessions entirely."""
    now = now or datetime.now(timezone.utc)
    p = Path(sessions_dir)
    if not p.is_dir():
        return []
    try:
        files = list(p.rglob("*.jsonl"))
    except (OSError, RuntimeError):
        # RuntimeError: older Python raises this (not OSError) on a symlink
        # loop during rglob — cheap insurance, best-effort omission either way.
        return []
    sessions = []
    for f in files:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        age_min = (now - mtime).total_seconds() / 60
        state = classify_session_state(age_min)
        if state is None:
            continue
        sessions.append({"agent": "codex", "project": f.stem, "model": None,
                         "last_activity": mtime.isoformat(), "state": state})
    return sessions


# ── /agents-live assembly ──────────────────────────────────────────────────

def build_agents_live(machine_id, projects=None, ag_mission_root=AG_MISSION_ROOT, now=None,
                      claude_projects_dir=CLAUDE_PROJECTS_DIR,
                      hermes_cron_output=HERMES_CRON_OUTPUT,
                      codex_sessions_dir=CODEX_SESSIONS_DIR):
    now = now or datetime.now(timezone.utc)
    sessions = scan_claude_sessions(claude_projects_dir, projects, ag_mission_root, now)
    hermes = scan_hermes_session(hermes_cron_output, now)
    if hermes:
        sessions.append(hermes)
    sessions.extend(scan_codex_sessions(codex_sessions_dir, now))
    return {"machine": machine_id, "ts": now.isoformat(), "sessions": sessions}


# ── /repos assembly (reuses collect.py's git-sweep pattern) ───────────────

def load_registry_projects(registry_path=REGISTRY_PROJECTS):
    try:
        reg = yaml.safe_load(Path(registry_path).read_text()) or {}
    except Exception:
        return []
    return reg.get("projects", [])


def run_git(cmd, cwd=None, timeout=10):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def build_repos(projects, ag_mission_root, run_fn=run_git):
    """{id, branch, dirty_files, last_commit_age_days} per ACTIVE project with
    a real .git dir. Non-active / missing-path / non-git entries are skipped."""
    out = []
    for p in projects:
        if p.get("status") != "active" or not p.get("path"):
            continue
        path = Path(ag_mission_root) / p["path"]
        if not path.is_dir() or not (path / ".git").is_dir():
            continue
        branch = run_fn(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        dirty = run_fn(["git", "status", "--porcelain"], cwd=path)
        dirty_files = len(dirty.splitlines()) if dirty else 0
        last = run_fn(["git", "log", "-1", "--format=%ct"], cwd=path)
        age_days = None
        if last and last.isdigit():
            commit_dt = datetime.fromtimestamp(int(last), tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - commit_dt).days
        out.append({"id": p["id"], "branch": branch, "dirty_files": dirty_files,
                    "last_commit_age_days": age_days})
    return out


# ── /health ─────────────────────────────────────────────────────────────────

def build_health(start_time=None):
    start_time = _START_TIME if start_time is None else start_time
    return {"ok": True, "uptime": round(time.time() - start_time, 1)}


# ── HTTP layer (thin, untested per plan) ──────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def _write_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/agents-live":
                projects = load_registry_projects()
                self._write_json(build_agents_live(MACHINE_ID, projects, AG_MISSION_ROOT))
            elif self.path == "/repos":
                projects = load_registry_projects()
                self._write_json({"repos": build_repos(projects, AG_MISSION_ROOT)})
            elif self.path == "/health":
                self._write_json(build_health())
            else:
                self._write_json({"error": "not found"}, status=404)
        except Exception as e:
            # Generic message to the (unauthenticated, Tailnet-reachable)
            # caller — detail (which can contain local paths) goes to the
            # daemon's own stderr/log only, never the wire.
            print(f"agent_watcher: error handling {self.path}: {e}", file=sys.stderr)
            self._write_json({"error": "internal error"}, status=500)

    def log_message(self, format, *args):
        pass  # quiet — this is a background daemon, not an interactive server


def main():
    port = int(os.environ.get("A008_WATCHER_PORT", PORT))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"agent_watcher listening on 0.0.0.0:{port} (machine={MACHINE_ID})", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
