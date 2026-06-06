# Fleet Pipeline Contracts

Canonical interface between agents. Edit this file before changing any output schema.
Downstream agents read this file, not the upstream agent's source code.

---

## Mike → Twenty CRM

**Action:** POST via GraphQL mutation `createLead` per lead — real-time, not batched.

**Mutation:**
```graphql
mutation CreateLead($input: LeadCreateInput!) {
  createLead(data: $input) { id }
}
```

**Input schema:**
| Field | Type | Example |
|-------|------|---------|
| businessName | string | "Shatin Badminton Club" |
| phone | string | "+852 2601 1234" |
| location | string | "Shatin, New Territories, HK" |
| source | string | "google-maps" |
| scrapedAt | ISO8601 | "2026-06-06T14:32:01Z" |
| status | select | "new" |

**On failure:** retry 3×, append to `~/.hermes/retry_queue/leads.jsonl`, flush on reconnect.

---

## Mighty ← Twenty CRM (hourly cron)

**Action:** Query new leads, send Day 0 email via SES, update status.

**Query:**
```graphql
query GetNewLeads {
  leads(filter: { status: { eq: "new" } }, first: 100) {
    edges { node { id businessName phone location scrapedAt } }
  }
}
```

**After send:** `updateLead(id, { status: "enrolled", enrolledAt: now })`

---

## Reply → Claire IMAP → triage.py → Twenty CRM

**Action:** When triage identifies a reply matching an outreach lead, update the contact.

**Mutation:**
```graphql
mutation UpdateLead($id: ID!, $input: LeadUpdateInput!) {
  updateLead(id: $id, data: $input) { id status }
}
```

**Input:** `{ status: "replied", lastReplyAt: ISO8601, replySnippet: string }`
