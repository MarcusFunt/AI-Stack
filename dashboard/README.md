# Local AI Dashboard

React/TypeScript control surface for the Local AI workstation. The dashboard talks only
to the authenticated FastAPI gateway through its nginx `/api` proxy; API credentials are
injected server-side and are not stored in browser code or local storage.

Main views:
- **Overview**: physical GPU/system telemetry, current job, scheduler and worker states.
- **Models**: installed models, measured throughput/VRAM/startup data, LLM benchmark action.
- **Jobs**: active and recent requests with phase, elapsed time and activity indicators.
- **Health**: control-plane topology, heartbeat and worker semantic state.
- **Setup**: GUI system doctor and optional MQTT/Home Assistant telemetry configuration.
- Chat, Speech, Robot Vision and Studio retain the interactive model tools.

Live state is delivered through `/api/events` WebSocket with periodic polling as a fallback.

Development:

```powershell
cd D:\AI-Stack\dashboard
npm ci
npm run build
```

Production is built and served by the dashboard Docker image. MQTT is telemetry-only by
default; no MQTT command topics are subscribed by the gateway.
