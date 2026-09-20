# Architecture Notes

## Control plane

The gateway owns authentication, logical model routing and response proxying.
The supervisor owns Docker lifecycle and GPU exclusivity. It is reachable only on the internal `ai-stack-net` network.

This separation deliberately keeps `/var/run/docker.sock` out of the network-facing gateway container.

## Agent Lab trust boundary

Agent Lab is a separate CPU-only control-plane service for LangGraph experiments. It can call the authenticated gateway, but experimental workers never receive write access to the live checkout. The container sees the host repository's `.git` directory read-only and clones a private bare mirror under `data/agent-lab`; every run gets a detached worktree from that mirror.

The Agent Lab controller has a read-only root filesystem, drops all Linux capabilities, has no Docker socket, and does not mount `.env`, model directories, the host home directory, or the live worktree. Candidate commits are stored only in private refs under the Agent Lab mirror until a future promotion layer explicitly accepts them.

Evaluation is split again at the code-execution boundary. `agent-lab-sandbox` uses the same versioned image but runs with `network_mode: none`, receives no AI credentials, mounts run workspaces read-only, and communicates with the controller through a file queue. The daemon keeps the queue root-only and demotes each test process to an unprivileged UID. The sandbox retains only SETUID/SETGID capabilities needed for that demotion.

The v0.1 model interface is intentionally narrow: repository context is bounded, generated changes are exact-text replacements, paths must remain inside the run worktree, secret/environment paths are blocked, and a generic passing baseline is not accepted as proof of task completion. The immutable-controller/promotion boundary will remain outside future self-modifying agent and harness code.

## GPU lifecycle

For a request targeting a GPU service:

1. Gateway serializes the heavyweight job.
2. Gateway asks supervisor to ensure the requested service.
3. Supervisor stops any other running GPU service.
4. Supervisor starts the target container.
5. Supervisor polls the service-specific readiness endpoint.
6. Gateway forwards the request.
7. The service remains warm until another GPU service is requested or `stop-all` is called.

This avoids repeated reloads during bursts while guaranteeing that two large runtimes do not compete for 12 GB VRAM.

## Build isolation

Gateway, supervisor, STT and VLM each build from their own narrow directory.
ComfyUI builds from its repository with a dedicated `.dockerignore`.
WanGP only sends its Dockerfile, requirements and entrypoint into the build context.

Model files are runtime mounts, never Docker build inputs.

## API behavior

Chat model IDs are logical aliases:
- `local-fast`
- `local-reasoning`

The gateway supports buffered OpenAI-compatible chat responses and streaming pass-through.
Other modalities retain dedicated endpoints:
- `/v1/audio/transcriptions`
- `/v1/audio/speech`
- `/v1/vision/analyze`
- `/v1/comfy/*`
- `/v1/wangp/*`

## Failure behavior

Supervisor startup failures include the target container's recent logs.
Each service has an explicit cold-start timeout in `config\models.json`.
The control plane can report status even when no GPU backend is running.
