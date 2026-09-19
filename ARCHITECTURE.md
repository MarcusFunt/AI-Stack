# Architecture Notes

## Control plane

The gateway owns authentication, logical model routing and response proxying.
The supervisor owns Docker lifecycle and GPU exclusivity. It is reachable only on the internal `ai-stack-net` network.

This separation deliberately keeps `/var/run/docker.sock` out of the network-facing gateway container.

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
