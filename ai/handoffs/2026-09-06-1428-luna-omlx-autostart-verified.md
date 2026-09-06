# Luna oMLX auto-start verified

Scope: this conversation only — Luna's local oMLX service and the `claude-ccr` route that depends on it.

## Phase 1 — Git and environment audit

- **Observed (2026-09-06 14:27 WIB):** `/Users/luna/AG_Mission/MightyOS` was on `main`, aligned with `origin/main`, with one worktree and no staged or unstaged files before this handoff was created.
- **Observed:** no project `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md` exists in this checkout.
- **Changed:** added `/Users/luna/Library/LaunchAgents/com.mightyos.luna.omlx-multimodel.plist`. It runs `/Users/luna/AG_Mission/omlx/.venv/bin/omlx serve` as Luna, using the existing `/Users/luna/.omlx/settings.json` multi-model configuration. `RunAtLoad` and `KeepAlive` are enabled; output goes to `/Users/luna/.omlx/logs/launchd.log`.
- **Observed:** the older system services `com.mightyos.luna.omlx` (single standard model on 8000) and `com.mightyos.luna.omlx-uncensored` (single uncensored model on 8001) remain disabled. They were not modified because CCR uses one provider at `127.0.0.1:8000` for all five model IDs.
- **Observed:** deploy mode was `prod`. This handoff is the only repository artifact from the change.

Rollback:

```bash
launchctl bootout gui/501/com.mightyos.luna.omlx-multimodel
launchctl disable gui/501/com.mightyos.luna.omlx-multimodel
rm /Users/luna/Library/LaunchAgents/com.mightyos.luna.omlx-multimodel.plist
```

The `rm` line is a documented rollback only; it was not run.

## Phase 2 — Plan checkpoint

- **Observed:** `ai/plans/` did not exist before this handoff. There is no scoped plan to update.
- **Observed:** no scoped ADR or spec exists for this runtime repair, so the scoped Phase 2b sweep found no relevant backlog item.

## Phase 3 — Task matrix

| Task | Status | Evidence |
|---|---|---|
| Diagnose blank/interrupted `claude-ccr` turns | ✅ Verified | CCR usage DB showed repeated HTTP 502 responses with zero tokens; port 8000 had no listener |
| Restore oMLX | ✅ Verified | `/health` returned `healthy` with five discovered models |
| Install login/crash auto-start | ✅ Verified | launchd job `com.mightyos.luna.omlx-multimodel` is running with `RunAtLoad` and `KeepAlive` |
| Verify recovery | ✅ Verified | controlled TERM changed PID 34163 to 34349 and health returned without manual restart |
| Verify CCR end to end | ✅ Verified | authenticated `/v1/messages` request through port 3456 returned a Qwen message with no error |

Decision chain: CCR itself was healthy, but its sole oMLX upstream was offline. The existing two disabled system daemons expose incompatible single-model/dual-port topology, so a user LaunchAgent was chosen to preserve the known-working five-model server on port 8000 without sudo or provider changes.

## Phase 4 — Next reasonable tasks

1. **Observe one real login/reboot** — confirm the job appears under `gui/501` and `/health` becomes healthy after Luna logs in. A controlled process restart was verified; a physical reboot was not performed.
2. **Investigate the 09:52 service exit** — the prior server stopped while the Claude session had grown to about 148k tokens. Review oMLX termination/crash evidence before changing cache or memory settings.
3. **Optionally remove obsolete disabled daemons** — only after confirming no other client relies on ports 8000/8001. They are harmless while disabled and were intentionally preserved.

## Session insights

### Insight: CCR's UI was healthy while every model request failed upstream

**What:** The repeated welcome screen and interrupted prompts were symptoms of oMLX being offline, not a broken `claude-ccr` shell function or Claude Code installation.

**Why we have it:** Claude Code opened and accepted input but never produced a response, so the launcher, gateway, provider, and model server were checked separately.

**How we got it:** `type -a` and the live process chain confirmed the launcher; `curl http://127.0.0.1:3456/health` confirmed CCR; `usage.sqlite` showed repeated 502s in roughly 0.18 seconds with zero tokens; direct port-8000 checks returned connection refused.

**Real conclusion for next agent:** Check port 8000 and the oMLX launch job before changing CCR profiles or Claude settings.

### Insight: the legacy dual-daemon topology does not match CCR's provider topology

**What:** CCR maps all five configured local models to one provider URL on port 8000, while the old daemons isolate the standard and uncensored models on ports 8000 and 8001.

**Why we have it:** Simply re-enabling both old daemons would make the selected uncensored model unreachable through CCR's current provider.

**How we got it:** Read `/Library/LaunchDaemons/com.mightyos.luna.omlx*.plist`, the CCR `app_config` row in `config.sqlite`, and `/Users/luna/.omlx/settings.json`.

**Real conclusion for next agent:** Preserve the multi-model port-8000 service unless CCR is deliberately redesigned for multiple providers.

## Phase 5 — GitNexus pre-flight

- **Observed:** no `.gitnexus` index exists in the MightyOS checkout. GitNexus checks were unavailable and skipped.

## Phase 6 — Fix-first checklist

- [x] Service health: healthy, five models discovered.
- [x] launchd recovery: controlled termination respawned the service with a new PID.
- [x] CCR route: real authenticated message request passed.
- [x] Uncommitted WIP: none before creating this handoff; only this handoff should be committed.
- [x] Schema/typecheck/test suite: not applicable; no application code or database schema changed.
- [ ] Physical reboot/login proof: intentionally not performed during an active session.

## Phase 7 — Resume prompt

```text
You are resuming the Luna oMLX auto-start verification. The user-level multi-model LaunchAgent has been installed and controlled crash recovery passed.

READ FIRST:
- /Users/luna/AG_Mission/MightyOS/ai/handoffs/2026-09-06-1428-luna-omlx-autostart-verified.md
- /Users/luna/Library/LaunchAgents/com.mightyos.luna.omlx-multimodel.plist

RULES:
- Do not enable or delete the two legacy system oMLX daemons without explicit approval.
- Do not expose the CCR token value in logs or output.
- Preserve the five-model server topology on port 8000.

TASK:
After Luna's next real login or reboot, verify that launchd started oMLX automatically.

VERIFY:
- launchctl print gui/501/com.mightyos.luna.omlx-multimodel
- curl --max-time 5 http://127.0.0.1:8000/health
- Send one tiny authenticated request through CCR port 3456 using the existing token file without printing it.

OUT OF SCOPE — DO NOT TOUCH:
- Other fleet services, agents, projects, plans, or registry entries
- oMLX model/cache/memory configuration
- CCR provider/profile configuration
- The disabled legacy oMLX daemons

WHEN DONE:
Report the launchd state, PID, health result, CCR request result, and any unexpected restart loop.

DISPATCH NOTES:
- Reuse `/Users/luna/AG_Mission/MightyOS`; it is the only worktree.
- Use a separate read-only verifier for the approval pass.
- Confirm `git status --short` is clean before unrelated work.
```

## Handoff destinations

- **Changed:** canonical handoff saved in this repository.
- **Observed:** the configured `/Users/kenneth/.../17-AI-Sessions` Obsidian path is unavailable on Luna, so no sibling copy was created.
- **Observed:** no `.plane-session.json` exists, so no Plane session update was required.
