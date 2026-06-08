#!/usr/bin/env python3
"""
Lead watcher — polls Mike's scraper CSV output and pushes new rows to Twenty CRM.

Runs continuously alongside the scraper. Detects new rows by tracking last processed
row index in a state file. Rate-limited to respect Twenty's 100 req/60s limit.

Usage (on Mike):
  python lead_watcher.py
  python lead_watcher.py --start-row 3920   # skip already-backfilled rows

Config via env or ~/.hermes/secrets/twenty_api_key.json:
  TWENTY_URL   - base URL (default http://100.66.96.51:3000)
  TWENTY_TOKEN - Bearer token
  LEAD_BRAND   - brand value, default AIRCHY
  POLL_SECS    - poll interval in seconds, default 60
"""
import sys, json, csv, os, time, argparse, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 for stdout/stderr on Windows (avoids cp1252 charmap errors)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- paths ----------------------------------------------------------------

HERE = Path(__file__).parent.resolve()

# Watched files: [path, default_start_row]
# start_row is used only when state has no entry for this file yet
WATCH_FILES = [
    Path(r"C:\Users\Jomo\work\scrapy-tools\output\uk_badminton_leads_10k.csv"),
    Path(r"C:\Users\Jomo\work\scrapy-tools\output\uk_badminton_leads_extra.csv"),
]

STATE_FILE = HERE / ".lead_watcher_state.json"

# --- config ---------------------------------------------------------------

def _secrets() -> dict:
    for p in [Path.home() / ".hermes" / "secrets" / "twenty_api_key.json",
              HERE / "twenty_api_key.json"]:
        if p.exists():
            return json.loads(p.read_text())
    return {}

def _url() -> str:
    s = _secrets()
    return os.environ.get("TWENTY_URL") or s.get("url") or s.get("base_url") or "http://100.66.96.51:3000"

def _token() -> str:
    s = _secrets()
    tok = os.environ.get("TWENTY_TOKEN") or s.get("token") or s.get("key")
    if not tok:
        raise RuntimeError("No Twenty token — set TWENTY_TOKEN env or ~/.hermes/secrets/twenty_api_key.json")
    return tok

BRAND = os.environ.get("LEAD_BRAND", "AIRCHY").upper()
POLL_SECS = int(os.environ.get("POLL_SECS", "60"))
DELAY_BETWEEN = 1.5  # seconds between Twenty requests (100 req/60s limit → safe at ~40/min)

# --- Twenty API -----------------------------------------------------------

def create_lead(lead: dict, token: str, url: str) -> str:
    """POST one lead to Twenty CRM. Returns new lead id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    variables = {"input": {
        "businessName": lead.get("businessName") or lead.get("business_name")
                        or lead.get("company") or lead.get("name", ""),
        "phone": lead.get("phone", ""),
        "location": lead.get("location") or lead.get("address", ""),
        "source": lead.get("source", "google-maps"),
        "scrapedAt": lead.get("scrapedAt", now),
        "status": "NEW",
        "brand": BRAND,
        "contactEmail": lead.get("email") or lead.get("contactEmail", ""),
        "contactWebsite": lead.get("website") or lead.get("contactWebsite", ""),
    }}
    query = ("mutation CreateLead($input: LeadCreateInput!) "
             "{ createLead(data: $input) { id } }")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(f"{url}/graphql", data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "HermesFleet/1.0",
    })
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")
    if resp.get("errors"):
        raise RuntimeError(f"GraphQL: {resp['errors']}")
    return resp["data"]["createLead"]["id"]

# --- state ----------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# --- CSV reading ----------------------------------------------------------

def read_csv_rows(path: Path) -> list:
    """Read all rows from a CSV. Returns list of dicts."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))

_GARBAGE_PREFIXES = (
    "Here are", "the Google", "There are no", "There is no",
    "I'm sorry", "I don't", "No badminton", "Unfortunately",
)

def is_garbage(row: dict) -> bool:
    name = row.get("name") or row.get("businessName") or ""
    if not name:
        return True
    if "\x1b" in name:
        return True
    if any(name.startswith(p) for p in _GARBAGE_PREFIXES):
        return True
    return False

# --- main loop ------------------------------------------------------------

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[lead_watcher] {ts}  {msg}", flush=True)

def process_file(path: Path, state: dict, token: str, url: str) -> int:
    """Push new rows from path to Twenty. Returns count of new rows pushed."""
    key = str(path)
    if not path.exists():
        return 0

    rows = read_csv_rows(path)
    last_row = state.get(key, 0)

    if last_row >= len(rows):
        return 0  # no new rows

    new_rows = rows[last_row:]
    good = [r for r in new_rows if not is_garbage(r)]
    skipped_garbage = len(new_rows) - len(good)

    if skipped_garbage:
        log(f"{path.name}: {skipped_garbage} garbage rows skipped")

    pushed = 0
    for lead in good:
        name = lead.get("businessName") or lead.get("name") or lead.get("company", "?")
        try:
            lid = create_lead(lead, token, url)
            log(f"  [OK] {name[:50]} → {lid}")
            pushed += 1
        except Exception as e:
            log(f"  [FAIL] {name[:50]}: {e}")
        time.sleep(DELAY_BETWEEN)

    state[key] = len(rows)
    return pushed

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-row", type=int, default=None,
                    help="Row index to start from for uk_badminton_leads_10k.csv "
                         "(overrides state file; default 3920 if no state)")
    args = ap.parse_args()

    token = _token()
    url = _url()

    state = load_state()

    # Initialize start row for main file if not in state
    main_key = str(WATCH_FILES[0])
    if main_key not in state:
        if args.start_row is not None:
            state[main_key] = args.start_row
            log(f"Starting from row {args.start_row} (--start-row arg)")
        else:
            # Default: start from row 3920 (rows 1-3919 were already backfilled)
            state[main_key] = 3920
            log("No state found — defaulting to row 3920 (skipping already-backfilled rows)")

    log(f"Watching {len(WATCH_FILES)} file(s), brand={BRAND}, poll={POLL_SECS}s")
    for f in WATCH_FILES:
        log(f"  {f}  (start_row={state.get(str(f), 0)})")

    while True:
        total_pushed = 0
        for path in WATCH_FILES:
            try:
                n = process_file(path, state, token, url)
                if n:
                    log(f"{path.name}: pushed {n} new leads")
                    total_pushed += n
            except Exception as e:
                log(f"ERROR processing {path.name}: {e}")

        if total_pushed == 0:
            log("No new rows — sleeping")

        save_state(state)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
