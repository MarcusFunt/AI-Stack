# AI-Stack agent instructions

This repository is the control plane and local-model stack for Marcus Computer.

## Architecture invariants
- The gateway owns public API authentication, model aliases, routing, and request proxying.
- The supervisor alone owns Docker lifecycle and GPU exclusivity.
- Do not expose the Docker socket to the gateway, dashboard, MCP server, or other network-facing services.
- Heavy GPU workers are demand-loaded; only one heavyweight GPU service should run at a time.
- Keep network-facing ports loopback-only unless exposure is deliberately implemented through the existing Tailscale host-agent path.
- Model files are runtime mounts and must never be copied into Docker build contexts.

## Secrets and credentials
- Never read, print, modify, or commit .env or .env.* files.
- Consume credentials only through environment variables or existing authenticated control-plane APIs.
- Do not weaken gateway, MCP, supervisor, or host-agent authentication to make a test pass.

## Normal workflow
- Use scripts\ai.ps1 for stack lifecycle operations.
- Prefer the unified gateway at http://127.0.0.1:8090 over direct backend calls.
- Use the existing MCP service rather than duplicating local-AI helper tools.
- OpenCode's primary model is `ai-stack/local-fast`. For deeper reasoning, call the `local-ai` MCP server's `ask_local_ai` tool in reasoning mode; do not switch OpenCode's primary model to the 8k reasoning worker because OpenCode's own instruction/tool context is too large for that runtime context.
- Preserve compatibility with Windows PowerShell 5.1 for repository scripts.

## Verification
After relevant changes, run the smallest applicable checks, then broaden:
- docker compose config
- Python syntax compilation for changed Python services
- dashboard lint/build for dashboard changes
- scripts\doctor.ps1 for control-plane diagnostics
- scripts\smoke.ps1 for end-to-end service verification when appropriate
- git diff --check before considering work complete

Do not silently stop or restart an active GPU job. Check status first and preserve the lease/supervisor contract.
