#!/usr/bin/env python3
"""
Twenty CRM GraphQL API client.
Used by Mike (post leads) and Mighty (get new leads, update status).

Config via env or ~/.hermes/secrets/twenty_api_key.json:
  TWENTY_URL   - base URL, e.g. http://100.66.96.51:3000
  TWENTY_TOKEN - Bearer token from Twenty Settings -> API
"""
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone


def _secrets() -> dict:
    path = os.path.expanduser("~/.hermes/secrets/twenty_api_key.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _url() -> str:
    s = _secrets()
    return os.environ.get("TWENTY_URL") or s.get("url") or s.get("base_url") or "http://localhost:3000"


def _token() -> str:
    s = _secrets()
    tok = os.environ.get("TWENTY_TOKEN") or s.get("token") or s.get("key")
    if not tok:
        raise RuntimeError(
            "TWENTY_TOKEN not set and not found in ~/.hermes/secrets/twenty_api_key.json"
        )
    return tok


def _graphql(query: str, variables: dict = None) -> dict:
    """Execute a GraphQL request against Twenty CRM. Returns parsed JSON."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_url()}/graphql",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_token()}",
            "User-Agent": "HermesFleet/1.0",
        },
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twenty API HTTP {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Twenty API unreachable ({_url()}): {e.reason}")
    if "errors" in resp:
        raise RuntimeError(f"Twenty GraphQL error: {resp['errors']}")
    return resp


def post_lead(lead: dict) -> dict:
    """Create a new lead in Twenty CRM. Returns {'id': ...}."""
    mutation = """
    mutation CreateLead($input: LeadCreateInput!) {
      createLead(data: $input) { id }
    }
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    variables = {
        "input": {
            "businessName": lead.get("businessName", ""),
            "phone": lead.get("phone", ""),
            "location": lead.get("location", ""),
            "source": lead.get("source", "google-maps"),
            "scrapedAt": lead.get("scrapedAt", now),
            "status": "NEW",
            "contactEmail": lead.get("contactEmail", "") or lead.get("email", ""),
            "contactWebsite": lead.get("contactWebsite", "") or lead.get("website", ""),
        }
    }
    resp = _graphql(mutation, variables)
    return resp["data"]["createLead"]


def get_new_leads(limit: int = 100) -> list:
    """Fetch leads with status=new. Returns list of node dicts."""
    query = """
    query GetNewLeads($first: Int) {
      leads(filter: { status: { eq: NEW } }, first: $first) {
        edges { node { id businessName phone location scrapedAt } }
      }
    }
    """
    resp = _graphql(query, {"first": limit})
    return [edge["node"] for edge in resp["data"]["leads"]["edges"]]


def update_lead_status(lead_id: str, status: str, extra: dict = None) -> dict:
    """Update a lead's status (and optional extra fields). Returns updated node."""
    mutation = """
    mutation UpdateLead($id: UUID!, $input: LeadUpdateInput!) {
      updateLead(id: $id, data: $input) { id status }
    }
    """
    input_data = {"status": status}
    if extra:
        input_data.update(extra)
    resp = _graphql(mutation, variables={"id": lead_id, "input": input_data})
    return resp["data"]["updateLead"]


def get_daily_stats() -> dict:
    """Count leads by status added today. Returns {new, enrolled, replied, converted, total}."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    # Filters on scrapedAt (when Mike scraped), not a server-assigned createdAt.
    # Assumes Mike's clock is within a few minutes of UTC.
    query = """
    query DailyStats($since: DateTime) {
      leads(filter: { scrapedAt: { gte: $since } }) {
        edges { node { status } }
      }
    }
    """
    resp = _graphql(query, {"since": today})
    counts = {"new": 0, "enrolled": 0, "replied": 0, "converted": 0, "dead": 0}
    for edge in resp["data"]["leads"]["edges"]:
        s = (edge["node"]["status"] or "").lower()  # Twenty SELECT values are UPPER_CASE
        if s in counts:
            counts[s] += 1
        # unknown status values are silently ignored to keep dict shape predictable
    counts["total"] = sum(counts.values())
    return counts
