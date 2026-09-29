"""Fail-closed resource admission for supervisor-managed workers."""

from dataclasses import asdict, dataclass
from typing import Optional


WORKLOAD_PROFILES = (
    "voice",
    "interactive",
    "reasoning",
    "vision",
    "image",
    "video",
    "benchmark",
)


@dataclass(frozen=True)
class ResourceAllocation:
    lease_id: str
    service: str
    profile: str = "interactive"
    gpu_vram_mb: Optional[int] = None
    system_ram_mb: Optional[int] = None
    exclusive_gpu: bool = True
    priority: int = 0


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str


def _nonnegative(value, label, *, nullable=True):
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer" + (" or null" if nullable else ""))


def validate_config(config):
    """Validate the legacy model registry plus optional scheduler metadata."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    services = config.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("services must be a non-empty object")
    for name, spec in services.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("each service must have a name and object config")
        if "gpu" in spec and not isinstance(spec["gpu"], bool):
            raise ValueError(f"services.{name}.gpu must be boolean")
        resources = spec.get("resources", {})
        if not isinstance(resources, dict):
            raise ValueError(f"services.{name}.resources must be an object")
        for field in ("gpu", "exclusive_gpu"):
            if field in resources and not isinstance(resources[field], bool):
                raise ValueError(f"services.{name}.resources.{field} must be boolean")
        if "gpu" in spec and resources.get("gpu", spec["gpu"]) != spec["gpu"]:
            raise ValueError(f"services.{name}.resources.gpu conflicts with legacy gpu flag")
        for field in ("gpu_vram_mb", "system_ram_mb"):
            if field in resources:
                _nonnegative(resources[field], f"services.{name}.resources.{field}")
        groups = spec.get("compatibility_groups", [])
        if not isinstance(groups, list) or any(not isinstance(group, str) or not group for group in groups):
            raise ValueError(f"services.{name}.compatibility_groups must be a list of names")
        if len(set(groups)) != len(groups):
            raise ValueError(f"services.{name}.compatibility_groups cannot contain duplicates")

    capacity = config.get("resource_capacity", {})
    if not isinstance(capacity, dict):
        raise ValueError("resource_capacity must be an object")
    for field in ("gpu_total_mb", "gpu_reserved_headroom_mb"):
        if field in capacity:
            _nonnegative(capacity[field], f"resource_capacity.{field}")

    raw_profiles = config.get("workload_profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("workload_profiles must be an object")
    for name, spec in raw_profiles.items():
        if name not in WORKLOAD_PROFILES or not isinstance(spec, dict):
            raise ValueError(f"invalid workload profile: {name}")
        priority = spec.get("priority", 0)
        _nonnegative(priority, f"workload_profiles.{name}.priority", nullable=False)
        residents = spec.get("prefer_resident", [])
        if not isinstance(residents, list) or any(service not in services for service in residents):
            raise ValueError(f"workload_profiles.{name}.prefer_resident must reference configured services")
        if len(set(residents)) != len(residents):
            raise ValueError(f"workload_profiles.{name}.prefer_resident cannot contain duplicates")

    models = config.get("models", [])
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    model_ids = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("each model must be an object")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in model_ids:
            raise ValueError(f"invalid or duplicate model id: {model_id}")
        model_ids.add(model_id)
        if model.get("service") not in services:
            raise ValueError(f"model {model_id} references an unknown service")
        profiles = model.get("profiles", [])
        if not isinstance(profiles, list) or any(profile not in WORKLOAD_PROFILES for profile in profiles):
            raise ValueError(f"model {model_id} profiles contain an unknown workload profile")
        capabilities = model.get("capabilities", [])
        if isinstance(capabilities, list):
            if any(not isinstance(capability, str) or not capability for capability in capabilities):
                raise ValueError(f"model {model_id} capabilities must be non-empty names")
        elif isinstance(capabilities, dict):
            if any(not isinstance(capability, str) or not isinstance(enabled, bool)
                   for capability, enabled in capabilities.items()):
                raise ValueError(f"model {model_id} capabilities must map names to booleans")
        else:
            raise ValueError(f"model {model_id} capabilities must be a list or object")
    aliases = config.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("aliases must be an object")
    for alias, model_id in aliases.items():
        if not isinstance(alias, str) or not alias or model_id not in model_ids:
            raise ValueError(f"alias {alias!r} references an unknown model")
    return config


class ResourceScheduler:
    """Decide whether GPU allocations are safe under configured evidence."""

    def __init__(self, config, mode="compatibility"):
        self.config = validate_config(config)
        if mode not in {"compatibility", "resource"}:
            raise ValueError("scheduler mode must be 'compatibility' or 'resource'")
        self.mode = mode

    @property
    def services(self):
        return self.config["services"]

    def _profile(self, name):
        if name not in WORKLOAD_PROFILES:
            raise ValueError(f"unknown workload profile: {name}")
        return self.config.get("workload_profiles", {}).get(name, {})

    def preferred_resident(self, profile):
        return list(self._profile(profile).get("prefer_resident", []))

    def resident_plan(self, profile, allocations=None):
        """Return preferred workers that can safely remain loaded together."""
        selected = []
        projected = list(allocations or [])
        for service in self.preferred_resident(profile):
            decision = self.admit(service, projected, profile=profile)
            if not decision.allowed:
                continue
            selected.append(service)
            if not any(item.service == service for item in projected):
                projected.append(self.allocation(f"profile-{profile}-{service}", service, profile))
        return selected

    def _requirements(self, service):
        spec = self.services[service]
        resources = spec.get("resources", {})
        gpu = resources.get("gpu", spec.get("gpu", False))
        return {
            "gpu": gpu,
            "gpu_vram_mb": resources.get("gpu_vram_mb"),
            "system_ram_mb": resources.get("system_ram_mb"),
            # A legacy GPU service remains exclusive unless explicitly relaxed.
            "exclusive_gpu": resources.get("exclusive_gpu", bool(gpu)),
            "compatibility_groups": spec.get("compatibility_groups", []),
        }

    def allocation(self, lease_id, service, profile="interactive"):
        profile_spec = self._profile(profile)
        requirements = self._requirements(service)
        return ResourceAllocation(
            lease_id=str(lease_id),
            service=service,
            profile=profile,
            gpu_vram_mb=requirements["gpu_vram_mb"],
            system_ram_mb=requirements["system_ram_mb"],
            exclusive_gpu=requirements["exclusive_gpu"],
            priority=profile_spec.get("priority", 0),
        )

    def admit(self, service, allocations, profile="interactive"):
        if service not in self.services:
            raise ValueError(f"unknown service: {service}")
        self._profile(profile)
        if any(allocation.service == service for allocation in allocations):
            return AdmissionDecision(True, "service_already_allocated")
        requested = self._requirements(service)
        by_service = {}
        for allocation in allocations:
            if allocation.service == service or allocation.service not in self.services:
                continue
            if self._requirements(allocation.service)["gpu"]:
                current = by_service.get(allocation.service)
                if current is None or allocation.priority > current.priority:
                    by_service[allocation.service] = allocation
        current_gpu = list(by_service.values())
        if not requested["gpu"]:
            return AdmissionDecision(True, "no_gpu_conflict")
        if not current_gpu and self.mode == "compatibility":
            return AdmissionDecision(True, "no_gpu_conflict")
        if self.mode == "compatibility":
            return AdmissionDecision(False, "exclusive_gpu_conflict")
        if current_gpu and (requested["exclusive_gpu"] or any(item.exclusive_gpu for item in current_gpu)):
            return AdmissionDecision(False, "exclusive_gpu_conflict")
        requested_groups = set(requested["compatibility_groups"])
        if current_gpu and (not requested_groups or any(
            not requested_groups.intersection(self._requirements(item.service)["compatibility_groups"])
            for item in current_gpu
        )):
            return AdmissionDecision(False, "incompatible_workload_groups")

        capacity = self.config.get("resource_capacity", {})
        total = capacity.get("gpu_total_mb")
        headroom = capacity.get("gpu_reserved_headroom_mb")
        requirements = [requested] + [self._requirements(item.service) for item in current_gpu]
        if total is None or headroom is None:
            return AdmissionDecision(False, "gpu_capacity_unknown")
        if any(item["gpu_vram_mb"] is None for item in requirements):
            return AdmissionDecision(False, "gpu_requirement_unknown")
        reserved = sum(item["gpu_vram_mb"] for item in requirements) + headroom
        if reserved > total:
            return AdmissionDecision(False, "gpu_capacity_exceeded")
        return AdmissionDecision(True, "compatible_capacity_fit")

    def state(self, allocations, loaded_services, pending_requests=None):
        capacity = self.config.get("resource_capacity", {})
        profiles = self.config.get("workload_profiles", {})
        return {
            "scheduler_mode": self.mode,
            "gpu_total_mb": capacity.get("gpu_total_mb"),
            "gpu_reserved_headroom_mb": capacity.get("gpu_reserved_headroom_mb"),
            "allocations": [asdict(item) for item in allocations],
            "loaded_services": sorted(set(loaded_services)),
            "pending_requests": list(pending_requests or []),
            "workload_profiles": {
                name: {
                    "priority": profiles.get(name, {}).get("priority", 0),
                    "prefer_resident": self.preferred_resident(name),
                    "eligible_resident": self.resident_plan(name, allocations),
                }
                for name in WORKLOAD_PROFILES
            },
        }
