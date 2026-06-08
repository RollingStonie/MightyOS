#!/usr/bin/env python3
"""
Fleet heartbeat — every agent (Claire/Mike/Mighty/Brian/Lucy) runs this.
Broadcasts a live status card to Discord #fleet-status (edits ONE message, no spam)
so any agent can read the whole fleet's state. Stale card >15 min = agent offline.

Usage (cron every 5 min, or `--loop`):
  AGENT=Mike FLEET_WEBHOOK="https://discord.com/api/webhooks/..." python3 heartbeat.py
  python3 heartbeat.py --loop          # run forever, beat every BEAT_SECS

Config via env (or a sibling heartbeat.env file):
  AGENT          - agent name (Claire/Mike/Mighty/...)
  FLEET_WEBHOOK  - the shared #fleet-status webhook URL
  ROLE           - one-line role description (optional)
  BEAT_SECS      - loop interval, default 300

What it reports: status, current task, host (OS/CPU/RAM/GPU load), uptime.
Current task = contents of ~/.hermes/current_task.txt if present, else the top
running python/node process (so we learn what an unknown PC is already doing).
"""
import json, os, sys, time, socket, platform, subprocess, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".heartbeat_state.json")   # remembers our message id
TASK_FILE = os.path.expanduser("~/.hermes/current_task.txt")

def load_env():
    envf = os.path.join(HERE, "heartbeat.env")
    if os.path.exists(envf):
        for line in open(envf):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

def host_metrics():
    m = {"host": socket.gethostname(), "os": f"{platform.system()} {platform.release()}"}
    try:
        import psutil
        m["cpu"] = f"{psutil.cpu_percent(interval=0.5):.0f}%"
        vm = psutil.virtual_memory()
        m["ram"] = f"{vm.percent:.0f}% of {vm.total/1e9:.0f}GB"
        m["uptime"] = f"{(time.time()-psutil.boot_time())/3600:.1f}h"
    except Exception:
        m["cpu"] = m.get("cpu", "n/a (pip install psutil)")
        m["ram"] = m.get("ram", "n/a")
    # GPU best-effort (Mike's GTX 1650 / Lucy)
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            u, used, tot = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
            m["gpu"] = f"{u}% · {used}/{tot}MB"
    except Exception:
        pass
    return m

def current_task():
    if os.path.exists(TASK_FILE):
        t = open(TASK_FILE).read().strip()
        if t:
            return t
    # fallback: detect what this machine is actually running
    try:
        import psutil
        cands = []
        for p in psutil.process_iter(["name", "cmdline", "cpu_percent"]):
            try:
                cmd = " ".join(p.info["cmdline"] or [])
                nm = (p.info["name"] or "").lower()
                if any(k in nm for k in ("python", "node")) and any(
                        k in cmd.lower() for k in ("scrape", "scraper", "lead", "outreach",
                        "crawl", "bot", "hermes", "agent", "ollama", "llama", "pipeline")):
                    cands.append((p.info.get("cpu_percent") or 0, cmd[:120]))
            except Exception:
                pass
        if cands:
            cands.sort(reverse=True)
            return "auto-detected: " + cands[0][1]
    except Exception:
        pass
    return "idle (no current_task.txt set)"

# ── Status file (Brain repo) ─────────────────────────────────────────────────

STATUS_PUSH_INTERVAL = 1800  # 30 minutes

# Throttle persists in a sidecar file so one-shot scheduled runs (e.g. a Task
# Scheduler / cron entry every 5 min) honour the 30-min interval — an in-memory
# timestamp would reset every run and push on every beat.
PUSH_STATE = os.path.join(HERE, ".status_push.json")

def _last_push_ts() -> float:
    if os.path.exists(PUSH_STATE):
        try:
            return float(json.load(open(PUSH_STATE)).get("last_status_push", 0.0))
        except Exception:
            return 0.0
    return 0.0

def build_status_json(agent: str, model: str, current_task: str,
                      results_today: int = 0, errors_today: int = 0) -> dict:
    """Build the status dict written to Brain repo fleet/status/<agent>.json."""
    return {
        "agent": agent.lower(),
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "idle" if "idle" in (current_task or "").lower() else "online",
        "current_task": current_task,
        "model_in_use": model,
        "results_today": results_today,
        "errors_today": errors_today,
        "session_started": os.environ.get("SESSION_STARTED", "unknown"),
    }

def write_status_file(filepath: str, agent: str, model: str, current_task: str,
                      results_today: int = 0, errors_today: int = 0) -> None:
    """Write fleet/status/<agent>.json to Brain repo on disk."""
    status = build_status_json(agent, model, current_task, results_today, errors_today)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(status, f, indent=2)

def should_push_status(last_push_ts: float) -> bool:
    """Return True if 30+ minutes have passed since last git push."""
    return (time.time() - last_push_ts) >= STATUS_PUSH_INTERVAL

def git_push_status(brain_repo_path: str, agent: str) -> bool:
    """Commit and push the agent's status file to the Brain repo. Returns True on success."""
    status_file = os.path.join(brain_repo_path, "fleet", "status", f"{agent.lower()}.json")
    try:
        subprocess.run(["git", "-C", brain_repo_path, "add", status_file],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", brain_repo_path, "commit", "-m",
                        f"chore: {agent.lower()} status update {time.strftime('%H:%M')}"],
                       capture_output=True)  # ok if nothing to commit
        # Each agent writes only its own file, so a rebase against other agents'
        # pushes is conflict-free. Pull first to avoid non-fast-forward rejects.
        subprocess.run(["git", "-C", brain_repo_path, "pull", "--rebase", "--autostash"],
                       capture_output=True, timeout=30)
        subprocess.run(["git", "-C", brain_repo_path, "push"],
                       check=True, capture_output=True, timeout=30)
        return True
    except Exception as e:
        print(f"[heartbeat] status push failed: {e}")
        return False

def build_embed(agent, role, task, m):
    busy = "idle" not in task.lower()
    color = 0x2ecc71 if busy else 0x95a5a6   # green busy, grey idle
    fields = [
        {"name": "Status", "value": "🟢 working" if busy else "⚪ idle", "inline": True},
        {"name": "Host", "value": f"`{m['host']}` · {m['os']}", "inline": True},
        {"name": "Load", "value": f"CPU {m.get('cpu','?')} · RAM {m.get('ram','?')}"
            + (f" · GPU {m['gpu']}" if m.get("gpu") else ""), "inline": False},
        {"name": "Current task", "value": task[:1000], "inline": False},
    ]
    if m.get("uptime"):
        fields.append({"name": "Uptime", "value": m["uptime"], "inline": True})
    return {"title": f"🤖 {agent}", "description": role or "", "color": color, "fields": fields,
            "footer": {"text": "heartbeat"}, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}

def post_or_edit(webhook, agent, embed):
    state = {}
    if os.path.exists(STATE):
        try: state = json.load(open(STATE))
        except Exception: state = {}
    mid = state.get("message_id")
    payload = {"username": agent, "embeds": [embed]}
    data = json.dumps(payload).encode()
    # Discord 403s the default Python-urllib User-Agent — must send a real one.
    hdrs = {"Content-Type": "application/json",
            "User-Agent": "HermesFleet (https://github.com/RollingStonie, 1.0)"}
    try:
        if mid:
            url = f"{webhook}/messages/{mid}"
            req = urllib.request.Request(url, data=data, method="PATCH", headers=hdrs)
            urllib.request.urlopen(req, timeout=15)
            return "edited", mid
        raise FileNotFoundError
    except urllib.error.HTTPError as e:
        if mid and e.code == 404:
            pass  # message deleted — fall through to recreate
        elif mid:
            raise
        # create new (message lost or first run)
        req = urllib.request.Request(webhook + "?wait=true", data=data, headers=hdrs)
        resp = json.load(urllib.request.urlopen(req, timeout=15))
        json.dump({"message_id": resp["id"]}, open(STATE, "w"))
        return "created", resp["id"]
    except Exception:
        req = urllib.request.Request(webhook + "?wait=true", data=data, headers=hdrs)
        resp = json.load(urllib.request.urlopen(req, timeout=15))
        json.dump({"message_id": resp["id"]}, open(STATE, "w"))
        return "created", resp["id"]

def beat():
    agent = os.environ.get("AGENT", "Unknown")
    webhook = os.environ.get("FLEET_WEBHOOK")
    role = os.environ.get("ROLE", "")
    model = os.environ.get("MODEL", "unknown")
    brain_repo = os.environ.get("BRAIN_REPO_PATH", "")

    if not webhook:
        print("ERROR: FLEET_WEBHOOK not set"); sys.exit(1)

    task = current_task()
    embed = build_embed(agent, role, task, host_metrics())
    action, mid = post_or_edit(webhook, agent, embed)
    print(f"[{time.strftime('%H:%M:%S')}] {agent} heartbeat {action} (msg {mid})")

    # Write status file to Brain repo every 30 min (throttle persists via sidecar)
    if brain_repo and should_push_status(_last_push_ts()):
        results_file = os.path.expanduser("~/.hermes/results_today.txt")
        errors_file = os.path.expanduser("~/.hermes/errors_today.txt")
        results = int(open(results_file).read().strip()) if os.path.exists(results_file) else 0
        errors = int(open(errors_file).read().strip()) if os.path.exists(errors_file) else 0
        status_path = os.path.join(brain_repo, "fleet", "status", f"{agent.lower()}.json")
        write_status_file(status_path, agent, model, task, results, errors)
        if git_push_status(brain_repo, agent):
            json.dump({"last_status_push": time.time()}, open(PUSH_STATE, "w"))
            print(f"[{time.strftime('%H:%M:%S')}] {agent} status pushed to Brain repo")

if __name__ == "__main__":
    load_env()
    if "--loop" in sys.argv:
        interval = int(os.environ.get("BEAT_SECS", "300"))
        while True:
            try: beat()
            except Exception as e: print("beat error:", e)
            time.sleep(interval)
    else:
        beat()
