# Realtime Voice Route and ICE Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unreachable TURN sessions when host-agent route verification fails and expose safe, per-session route and ICE-state diagnostics.

**Architecture:** The gateway remains responsible for selecting and validating the public ICE route; it returns a credential-free route summary alongside the session and fails with an actionable 503 when a TURN route cannot be verified. The dashboard attaches listeners to the active peer connection and retains a sanitized diagnostic snapshot through failures and cleanup.

**Tech Stack:** FastAPI/Python, gateway unittest suite, React/TypeScript, Vitest.

**Spec:** Current user request and `docs/superpowers/specs/2026-10-01-turn-loopback-proxy-design.md`.

## Global Constraints

- Never expose or log TURN credentials, session tickets, SDP, candidate addresses, or gateway secrets.
- Do not weaken host-agent authentication or change supervisor/GPU ownership.
- Preserve compatibility with Windows PowerShell 5.1 for repository scripts.
- Rebuild only affected gateway/dashboard services; check active GPU ownership first and leave GPU services untouched.

## Review Focus

- Host-agent timeout or error with TURN configured returns actionable 503 and no internal ICE URL.
- TURN disabled, missing, or invalid advertised hostname cannot produce a success response with internal coturn URLs.
- A response without TURN configuration remains usable and reports a direct route.
- Failed peer connections retain the last connection, ICE, and gathering state after cleanup.
- Diagnostics never include candidate IPs, server hostnames, credentials, or session tickets.

---

### Task 1: Fail closed on unverifiable gateway TURN route

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/gateway/test_realtime_voice.py`

**Interfaces:** Successful session responses include `ice_route` with sanitized route kind and transport/port; TURN session creation requires an online host with available route state, a present private route, a ready listener, Funnel explicitly disabled, `voice_turn_enabled=true`, and a valid DNS name.

- [x] Add tests for host-agent error, disabled route, malformed DNS name, verified TURN route metadata, and direct-only response metadata.
- [x] Run the gateway voice test module and confirm the new failure cases fail against current behavior.
- [x] Return an actionable 503 when TURN is configured but its route cannot be verified; only return the rewritten TURN URL after hostname validation.
- [x] Add credential-free structured route verification logs correlated by session ID and request ID.
- [x] Run the gateway voice test module and verify all cases pass.

### Task 2: Capture browser route and ICE state per session

**Files:**
- Modify: `dashboard/src/api.ts`
- Modify: `dashboard/src/realtimeVoice.ts`
- Modify: `dashboard/src/RealtimeVoicePanel.tsx`
- Modify: `dashboard/src/App.css`
- Modify: `dashboard/src/RealtimeVoicePanel.test.tsx`
- Modify: `dashboard/src/realtimeVoice.test.ts`

**Interfaces:** The API session type consumes `ice_route`; the voice client publishes sanitized `RTCPeerConnection` connection/ICE/gathering states and selected candidate types; the panel displays route and last state in its existing connection details.

- [x] Add failing tests for peer state event snapshots, snapshot retention when disconnect clears the selected path, route display on failure, and absence of private addresses/secrets.
- [x] Run the focused Vitest files and confirm those assertions fail for the missing behavior.
- [x] Attach peer state listeners as soon as the transport creates its peer connection; publish state and selected candidate types without addresses.
- [x] Keep the latest ICE diagnostics in panel state through retryable failures and show session ID plus sanitized route/state in connection details.
- [x] Run focused and full dashboard checks.

### Task 3: Verify and deliver

**Files:**
- No additional production files.

- [ ] Run Python syntax compilation for `gateway/app.py` and the complete relevant gateway voice tests.
- [x] Run dashboard lint, tests, and build.
- [ ] Run `docker compose config` and `git diff --check`.
- [ ] Check `scripts\ai.ps1 status`, rebuild/recreate only gateway and dashboard if safe, then run `scripts\doctor.ps1` and relevant smoke checks.
- [ ] Commit only implementation, tests, and this plan; push to `origin/main` as previously authorized.
