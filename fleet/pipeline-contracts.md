# Fleet Pipeline Contracts

Canonical interface between agents. Edit this file before changing any output schema.
Downstream agents read this file, not the upstream agent's source code.

> **Verified live 2026-06-06.** Twenty CRM `lead` object created with 8 custom fields.
> Helper: `sync/twenty_client.py` (post_lead / get_new_leads / update_lead_status / get_daily_stats).

---

## Twenty CRM connection

- **Endpoint:** `POST http://100.66.96.51:3000/graphql` (Tailscale IP — `localhost` does NOT work; the
  container binds only the Tailscale interface).
- **Auth:** `Authorization: Bearer <token>` — token in `~/.hermes/secrets/twenty_api_key.json`
  (key `key`) or env `TWENTY_TOKEN`. Workspace: **Kenneth HQ** (`f4d730ee-...`).
- **REST alternative:** `http://100.66.96.51:3000/rest/leads` (same auth) if GraphQL is inconvenient.
- **SELECT values are UPPER_CASE** (Twenty enforces this). In GraphQL filters the enum is **unquoted**:
  `status: { eq: NEW }`.

### `lead` object fields
| Field | Type | Notes |
|-------|------|-------|
| businessName | TEXT | |
| phone | TEXT | |
| location | TEXT | |
| source | TEXT | default `google-maps` |
| scrapedAt | DATE_TIME | ISO8601 |
| status | SELECT | `NEW` `CONTACTED` `ENROLLED` `REPLIED` `CONVERTED` `DEAD` (default `NEW`) |
| brand | SELECT | `AIRCHY` `MIGHTYTECHIE` `OTHER` |
| enrolledAt | DATE_TIME | set by Mighty when Day-0 sent |

> Brands are a **field on one workspace**, not separate Twenty workspaces — separate workspaces would
> each need their own onboarding + API key + routing. One workspace + `brand` filter is the CRM pattern.

---

## Mike → Twenty CRM

**Action:** POST via GraphQL mutation `createLead` per lead — real-time, not batched.

**Mutation:**
```graphql
mutation CreateLead($input: LeadCreateInput!) {
  createLead(data: $input) { id }
}
```

**Input:** `{ businessName, phone, location, source: "google-maps", scrapedAt, status: "NEW", brand }`

**On failure:** retry 3×, append to `~/.hermes/retry_queue/leads.jsonl`, flush on reconnect.

---

## Mighty ← Twenty CRM (hourly cron — `sync/lead_ingest.py`)

**Action:** Query NEW leads, send Day 0 email via SES, mark ENROLLED.

**Query:**
```graphql
query GetNewLeads($first: Int) {
  leads(filter: { status: { eq: NEW } }, first: $first) {
    edges { node { id businessName phone location scrapedAt } }
  }
}
```

**After send:** `updateLead(id, { status: "ENROLLED", enrolledAt: now })`

> ⚠️ Enrollment send is gated on SES credentials (not yet provisioned in `~/.hermes/.env`).
> Until then `lead_ingest` reads leads and the SES send returns False, so leads stay `NEW`.

---

## Reply → Claire IMAP → triage.py → Twenty CRM

**Action:** When triage identifies a reply matching an outreach lead, update the contact.

**Mutation:**
```graphql
mutation UpdateLead($id: UUID!, $input: LeadUpdateInput!) {
  updateLead(id: $id, data: $input) { id status }
}
```

**Input:** `{ status: "REPLIED" }`
> `lastReplyAt` / `replySnippet` fields are NOT yet on the `lead` object — add via metadata API before using.
