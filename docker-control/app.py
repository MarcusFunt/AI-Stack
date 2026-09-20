import json
import os
import secrets
from pathlib import Path

import docker
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/models.json"))
TOKEN = os.getenv("DOCKER_CONTROL_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DOCKER_CONTROL_TOKEN must be set")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
CONTAINERS = {
    service: spec["container"]
    for service, spec in CONFIG["services"].items()
    if spec.get("container")
}
client = docker.from_env()
app = FastAPI(title="Local AI Docker Control", version="0.1.0")


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    supplied = request.headers.get("x-docker-control-token", "")
    if not secrets.compare_digest(supplied, TOKEN):
        return JSONResponse({"detail": "invalid docker-control credential"}, status_code=401)
    return await call_next(request)


def get_container(service):
    name = CONTAINERS.get(service)
    if not name:
        raise HTTPException(404, f"unknown service: {service}")
    try:
        return client.containers.get(name)
    except docker.errors.NotFound as exc:
        raise HTTPException(503, f"{service} container has not been created") from exc


@app.get("/health")
def health():
    try:
        client.ping()
        return {"status": "ok", "version": app.version}
    except Exception as exc:
        return JSONResponse(
            {"status": "degraded", "version": app.version, "detail": str(exc)},
            status_code=503,
        )


@app.get("/info")
def info():
    payload = client.info()
    return {"server_version": payload.get("ServerVersion", "reachable")}


@app.get("/containers/{service}")
def container(service: str):
    target = get_container(service)
    target.reload()
    return {"service": service, "status": target.status}


@app.post("/containers/{service}/start")
def start(service: str):
    target = get_container(service)
    target.reload()
    if target.status != "running":
        target.start()
        target.reload()
    return {"service": service, "status": target.status}
@app.post("/containers/{service}/stop")
def stop(service: str, timeout: int = Query(default=30, ge=1, le=120)):
    target = get_container(service)
    target.reload()
    if target.status == "running":
        target.stop(timeout=timeout)
        target.reload()
    return {"service": service, "status": target.status}


@app.get("/containers/{service}/logs", response_class=PlainTextResponse)
def logs(service: str, tail: int = Query(default=100, ge=1, le=1000)):
    target = get_container(service)
    return target.logs(tail=tail).decode(errors="replace")
