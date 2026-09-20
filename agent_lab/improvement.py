from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class ImprovementSandbox(Protocol):
    def python_validator(
        self,
        run_id: str,
        code: str,
        timeout_s: int = 120,
    ) -> dict[str, Any]: ...

    def python_unit(
        self,
        run_id: str,
        timeout_s: int,
        suite: str = "public",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ValidatorSpec:
    id: str
    code: str
    timeout_s: int = 120


@dataclass(frozen=True)
class ImprovementFeature:
    slug: str
    objective: str
    public_validator: ValidatorSpec
    hidden_validator: ValidatorSpec
    context_include: tuple[str, ...] = ("agent_lab",)
    context_exclude: tuple[str, ...] = ("agent_lab/tests",)
    edit_include: tuple[str, ...] = ("agent_lab/worker", "agent_lab/harnesses")
    edit_exclude: tuple[str, ...] = ("agent_lab/tests", "agent_eval", "scripts", "config")
    max_attempts: int = 2
    sealed_repeats: int = 2

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ImprovementFeature":
        slug = str(payload.get("slug", "")).strip()
        objective = str(payload.get("objective", "")).strip()
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError(f"invalid improvement slug: {slug!r}")
        if not objective:
            raise ValueError(f"feature {slug} has no objective")
        public_code = str(payload.get("public_validator", "")).strip()
        hidden_code = str(payload.get("hidden_validator", "")).strip()
        if not public_code or not hidden_code:
            raise ValueError(f"feature {slug} requires public and hidden validators")
        if public_code == hidden_code:
            raise ValueError(f"feature {slug} validators must be independent")
        max_attempts = int(payload.get("max_attempts", 2))
        sealed_repeats = int(payload.get("sealed_repeats", 2))
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 1 <= sealed_repeats <= 5:
            raise ValueError("sealed_repeats must be between 1 and 5")
        return cls(
            slug=slug,
            objective=objective,
            public_validator=ValidatorSpec(f"{slug}-public", public_code),
            hidden_validator=ValidatorSpec(f"{slug}-hidden", hidden_code),
            context_include=tuple(payload.get("context_include") or ["agent_lab"]),
            context_exclude=tuple(payload.get("context_exclude") or ["agent_lab/tests"]),
            edit_include=tuple(
                payload.get("edit_include")
                or ["agent_lab/worker", "agent_lab/harnesses"]
            ),
            edit_exclude=tuple(
                payload.get("edit_exclude")
                or ["agent_lab/tests", "agent_eval", "scripts", "config"]
            ),
            max_attempts=max_attempts,
            sealed_repeats=sealed_repeats,
        )


def load_feature_catalog(path: str | Path) -> tuple[ImprovementFeature, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("features") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("improvement catalog has no features")
    features = tuple(ImprovementFeature.from_dict(dict(item)) for item in rows)
    slugs = [item.slug for item in features]
    if len(slugs) != len(set(slugs)):
        raise ValueError("improvement feature slugs must be unique")
    return features


@dataclass(frozen=True)
class GateCheck:
    id: str
    status: str
    message: str
    evidence: dict[str, Any]
@dataclass(frozen=True)
class FeatureGateResult:
    eligible: bool
    checks: tuple[GateCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "checks": [
                {
                    "id": item.id,
                    "status": item.status,
                    "message": item.message,
                    "evidence": item.evidence,
                }
                for item in self.checks
            ],
        }


def _validator_check(
    sandbox: ImprovementSandbox,
    *,
    run_id: str,
    spec: ValidatorSpec,
    expect_pass: bool,
    check_id: str,
) -> GateCheck:
    expectation = "pass" if expect_pass else "fail"
    try:
        result = sandbox.python_validator(run_id, spec.code, spec.timeout_s)
    except Exception as exc:
        return GateCheck(
            id=check_id,
            status="block",
            message=f"validator {spec.id} infrastructure error: {exc}",
            evidence={"error": str(exc)},
        )
    returncode = int(result.get("returncode", 1))
    timed_out = returncode == 124
    passed = returncode == 0
    ok = (passed == expect_pass) and not timed_out
    return GateCheck(
        id=check_id,
        status="pass" if ok else "block",
        message=f"validator {spec.id} expected to {expectation}; returncode={returncode}",
        evidence=result,
    )
def evaluate_feature_baseline(
    sandbox: ImprovementSandbox,
    *,
    base_run_id: str,
    public_validator: ValidatorSpec,
    hidden_validator: ValidatorSpec,
) -> FeatureGateResult:
    checks = (
        _validator_check(
            sandbox,
            run_id=base_run_id,
            spec=public_validator,
            expect_pass=False,
            check_id="baseline-public-fails",
        ),
        _validator_check(
            sandbox,
            run_id=base_run_id,
            spec=hidden_validator,
            expect_pass=False,
            check_id="baseline-hidden-fails",
        ),
    )
    return FeatureGateResult(
        eligible=all(item.status == "pass" for item in checks),
        checks=checks,
    )


def evaluate_feature_candidate(
    sandbox: ImprovementSandbox,
    *,
    base_run_id: str,
    candidate_run_id: str,
    public_validator: ValidatorSpec,
    hidden_validator: ValidatorSpec,
    regression_timeout_s: int = 600,
) -> FeatureGateResult:
    checks = list(evaluate_feature_baseline(
        sandbox,
        base_run_id=base_run_id,
        public_validator=public_validator,
        hidden_validator=hidden_validator,
    ).checks)
    checks.append(_validator_check(
        sandbox,
        run_id=candidate_run_id,
        spec=public_validator,
        expect_pass=True,
        check_id="candidate-public-passes",
    ))
    checks.append(_validator_check(
        sandbox,
        run_id=candidate_run_id,
        spec=hidden_validator,
        expect_pass=True,
        check_id="candidate-hidden-passes",
    ))

    try:
        regression = sandbox.python_unit(
            candidate_run_id,
            regression_timeout_s,
            suite="public",
        )
        returncode = int(regression.get("returncode", 1))
        output = str(regression.get("output", ""))
        regression_ok = returncode == 0 and "Ran 0 tests" not in output
        checks.append(GateCheck(
            id="candidate-full-regression",
            status="pass" if regression_ok else "block",
            message=(
                "full repository unit suite passed"
                if regression_ok
                else f"full repository unit suite failed; returncode={returncode}"
            ),
            evidence=regression,
        ))
    except Exception as exc:
        checks.append(GateCheck(
            id="candidate-full-regression",
            status="block",
            message=f"full repository unit suite infrastructure error: {exc}",
            evidence={"error": str(exc)},
        ))

    eligible = all(item.status == "pass" for item in checks)
    return FeatureGateResult(eligible=eligible, checks=tuple(checks))
