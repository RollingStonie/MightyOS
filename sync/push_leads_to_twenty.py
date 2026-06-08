#!/usr/bin/env python3
"""
Push scraped leads from Mike into Twenty CRM's `lead` object.
Self-contained (stdlib only — no `requests`) so it runs on a bare Python install.

Usage:
  python push_leads_to_twenty.py leads.json --brand airchy
  python push_leads_to_twenty.py leads.csv  --brand mightytechie

Field mapping (flexible — accepts common scraper key names):
  businessName  <- businessName | business_name | company | name
  phone         <- phone
  location      <- location | address
  source        <- source (default "google-maps")
  scrapedAt     <- scrapedAt (default: now)
  contactEmail  <- email | contactEmail
  contactWebsite <- website | contactWebsite

Contract: fleet/pipeline-contracts.md. Endpoint POST {url}/graphql, mutation createLead.
Twenty SELECT values are UPPER_CASE; status defaults to NEW.
"""
import sys, json, csv, os, argparse, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

VALID_BRANDS = {"AIRCHY", "MIGHTYTECHIE", "OTHER"}


def _secrets() -> dict:
    for p in [Path.home() / ".hermes" / "secrets" / "twenty_api_key.json",
              Path("/home/jomo/.hermes/secrets/twenty_api_key.json")]:
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
        raise RuntimeError("No Twenty token (env TWENTY_TOKEN or ~/.hermes/secrets/twenty_api_key.json)")
    return tok


def create_lead(lead: dict, brand: str, token: str) -> str:
    """Create one lead via GraphQL createLead. Returns the new lead id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    variables = {"input": {
        "businessName": lead.get("businessName") or lead.get("business_name")
                        or lead.get("company") or lead.get("name", ""),
        "phone": lead.get("phone", ""),
        "location": lead.get("location") or lead.get("address", ""),
        "source": lead.get("source", "google-maps"),
        "scrapedAt": lead.get("scrapedAt", now),
        "status": "NEW",
        "brand": brand,
        "contactEmail": lead.get("email") or lead.get("contactEmail", ""),
        "contactWebsite": lead.get("website") or lead.get("contactWebsite", ""),
    }}
    query = ("mutation CreateLead($input: LeadCreateInput!) "
             "{ createLead(data: $input) { id } }")
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(f"{_url()}/graphql", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twenty HTTP {e.code}: {e.read().decode()[:200]}")
    if resp.get("errors"):
        raise RuntimeError(f"Twenty GraphQL error: {resp['errors']}")
    return resp["data"]["createLead"]["id"]


def load_leads(path: str) -> list:
    if path.endswith(".csv"):
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    return json.loads(Path(path).read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="leads .json (list of objects) or .csv")
    ap.add_argument("--brand", required=True, help="airchy | mightytechie | other")
    ap.add_argument("--delay", type=float, default=0.7, help="seconds between requests (default 0.7 for rate limit)")
    ap.add_argument("--skip-names", help="JSON file with list of businessNames already in CRM to skip")
    args = ap.parse_args()

    brand = args.brand.strip().upper()
    if brand not in VALID_BRANDS:
        sys.exit(f"--brand must be one of {sorted(VALID_BRANDS)} (got {args.brand})")

    token = _token()
    raw = load_leads(args.file)
    # skip garbage rows: no name, or name contains terminal escape sequences (LLM meta-output)
    leads = [r for r in raw if r.get("name") and "\x1b" not in r.get("name", "")
             and not r["name"].startswith("Here are") and not r["name"].startswith("the Google")]
    skipped = len(raw) - len(leads)
    if skipped:
        print(f"[push] skipped {skipped} garbage rows, processing {len(leads)}")

    existing: set = set()
    if args.skip_names:
        existing = set(json.loads(Path(args.skip_names).read_text()))
        before = len(leads)
        leads = [r for r in leads if (r.get("businessName") or r.get("name", "")) not in existing]
        print(f"[push] skipped {before - len(leads)} already-in-CRM, pushing {len(leads)}")

    ok = fail = 0
    for lead in leads:
        try:
            lid = create_lead(lead, brand, token)
            ok += 1
            print(f"  [OK] {lead.get('businessName') or lead.get('company') or lead.get('name','?')} -> {lid}")
        except Exception as e:
            fail += 1
            print(f"  [FAIL] {lead.get('businessName') or lead.get('name','?')}: {e}")
        time.sleep(args.delay)
    print(f"[push] done — {ok} created, {fail} failed ({len(leads)} total)")


if __name__ == "__main__":
    main()
