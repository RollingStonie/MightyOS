# MightyOS Brain Repo

Fleet coordination layer for the Hermes agentic OS.

**What lives here:**
- `fleet/registry.yaml` — who each agent is, what tools they use, what they produce
- `fleet/pipeline-contracts.md` — inter-agent interfaces
- `fleet/targets.yaml` — expected daily outputs
- `fleet/status/` — agent liveness files (each agent writes their own)
- `fleet/bootstrap/` — device recovery scripts
- `agents/*/AGENTS.md` — per-agent rules files

**All agents:** `git pull ~/MightyOS` at session start, then `cat fleet/registry.yaml`.
