"""assess.v1 契约的严格性与边界：字段、枚举、上限、溯源。"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from aegis.domain.assess import (
    AssessErrorCode,
    AssessEvidence,
    AssessRequest,
    AssessResponse,
    FactorObservation,
    UnmappedMetric,
)
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit
from aegis.domain.provenance import DataProvenance
from aegis.domain.site import SiteAttribute, SiteProfile

CODE_PATTERN = re.compile(r"^E[A-Z0-9_]{3,20}$")


def _hit(rule_id: str = "R-DEBRIS-RAIN-1", hazard: HazardType = HazardType.DEBRIS_FLOW) -> TriggerHit:
    return TriggerHit(rule_id=rule_id, hazard_type=hazard.value, region_code="540121", score=0.8)


def _observation(factor: str = "rainfall_mm_h") -> FactorObservation:
    return FactorObservation(
        factor=factor,
        unit="mm/h",
        samples=3,
        min_value=1.0,
        max_value=3.0,
        mean_value=2.0,
        observed_at="2026-11-09T08:00:00+00:00",
        station_ids=["RG-540121-01"],
        sources=["platform.telemetry:simulator"],
        derivation="rain_10min(max)×6",
    )


def _evidence() -> AssessEvidence:
    return AssessEvidence(
        region_code="540121",
        window="60m",
        generated_at="2026-11-09T08:05:00+00:00",
        observations=[_observation()],
        provenance=DataProvenance(data_kind="mock", sources=["platform.telemetry:simulator"], is_mock=True),
    )


def _request(**overrides) -> AssessRequest:
    payload = {
        "region_code": "540121",
        "window": "60m",
        "hazard_type": HazardType.DEBRIS_FLOW,
        "candidate_hazards": [HazardType.DEBRIS_FLOW],
        "hits": [_hit()],
        "evidence": _evidence(),
    }
    payload.update(overrides)
    return AssessRequest(**payload)


def _response(**overrides) -> AssessResponse:
    payload = {
        "hazard_type": HazardType.DEBRIS_FLOW,
        "region_code": "540121",
        "risk_level": RiskLevel.ORANGE,
        "confidence": 0.5,
        "confidence_basis": "coverage=0.80;validation=pass",
        "rationale": "测试结论",
        "assessed_by": "assess.test",
        "source": "test",
        "data_provenance": DataProvenance(data_kind="mock", method="factor_engine_mock", is_mock=True),
    }
    payload.update(overrides)
    return AssessResponse(**payload)


def test_roundtrip_request_and_response() -> None:
    request = _request()
    assert AssessRequest.model_validate(request.model_dump(mode="json")) == request
    response = _response(assessment={"engine_version": "v-test", "P": 0.4, "T": 0.5, "R": 20.0})
    assert AssessResponse.model_validate(response.model_dump(mode="json")) == response


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        _request(unknown_field=1)
    with pytest.raises(ValidationError):
        _response(unknown_field=1)
    with pytest.raises(ValidationError):
        FactorObservation(
            factor="rainfall_mm_h",
            samples=1,
            max_value=1.0,
            observed_at="2026-11-09T08:00:00+00:00",
            unknown_field=1,
        )


def test_confidence_bounds() -> None:
    for value in (-0.01, 1.01):
        with pytest.raises(ValidationError):
            _response(confidence=value)
    assert _response(confidence=0.0).confidence == 0.0
    assert _response(confidence=1.0).confidence == 1.0


def test_confidence_basis_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        _response(confidence_basis="")


def test_risk_level_and_hazard_enums() -> None:
    for level in (0, 6):
        with pytest.raises(ValidationError):
            _response(risk_level=level)
    with pytest.raises(ValidationError):
        _response(hazard_type="earthquake")


def test_region_and_window_patterns() -> None:
    with pytest.raises(ValidationError):
        _request(region_code="54")
    for window in ("0", "60", "1d", "m"):
        with pytest.raises(ValidationError):
            _request(window=window)
    assert _request(window="30m").window == "30m"
    assert _request(window="2h").window == "2h"


def test_hits_and_refs_limits() -> None:
    hits = [_hit(f"R-X-{i}") for i in range(64)]
    assert len(_request(hits=hits).hits) == 64
    with pytest.raises(ValidationError):
        _request(hits=[_hit(f"R-X-{i}") for i in range(65)])
    refs = [f"evidence:540121:{i}" for i in range(32)]
    assert _request(data_refs=refs).data_refs == refs
    with pytest.raises(ValidationError):
        _request(data_refs=[f"evidence:{i}" for i in range(33)])


def test_observations_limit() -> None:
    observations = [_observation(f"factor_{i}") for i in range(48)]
    evidence = _evidence().model_copy(update={"observations": observations})
    assert len(evidence.observations) == 48
    with pytest.raises(ValidationError):
        AssessEvidence(**{**evidence.model_dump(), "observations": [*observations, _observation("factor_48")]})


def test_factor_observation_rejects_non_finite_and_bad_ranges() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            FactorObservation(
                factor="rainfall_mm_h",
                samples=1,
                max_value=value,
                observed_at="2026-11-09T08:00:00+00:00",
            )
    with pytest.raises(ValidationError):
        FactorObservation(
            factor="rainfall_mm_h",
            samples=1,
            min_value=2.0,
            max_value=1.0,
            observed_at="2026-11-09T08:00:00+00:00",
        )
    with pytest.raises(ValidationError):
        FactorObservation(factor="rainfall_mm_h", samples=1, observed_at="2026-11-09T08:00:00+00:00")
    with pytest.raises(ValidationError):
        FactorObservation(factor="rainfall_mm_h", samples=0, max_value=1.0, observed_at="2026-11-09T08:00:00+00:00")


def test_factor_observation_accepts_class_only() -> None:
    observation = FactorObservation(
        factor="aspect",
        samples=1,
        class_value="N",
        observed_at="2026-11-09T08:00:00+00:00",
        quality="site",
        derivation="site_profile",
    )
    assert observation.class_value == "N"


def test_hazard_must_be_in_candidates() -> None:
    with pytest.raises(ValidationError):
        _request(hazard_type=HazardType.LANDSLIDE, candidate_hazards=[HazardType.DEBRIS_FLOW])


def test_hazard_can_be_absent_when_candidates_empty() -> None:
    request = _request(hazard_type=None, candidate_hazards=[])
    assert request.hazard_type is None


def test_unmapped_metric_requires_positive_samples() -> None:
    with pytest.raises(ValidationError):
        UnmappedMetric(metric="new_snow_cm", samples=0, reason="区间未知")
    assert UnmappedMetric(metric="new_snow_cm", samples=2, reason="区间未知").samples == 2


def test_site_profile_rejects_duplicate_and_bad_values() -> None:
    with pytest.raises(ValidationError):
        SiteProfile(
            region_code="540121",
            attributes=[
                SiteAttribute(key="slope_angle_deg", value=30.0),
                SiteAttribute(key="slope_angle_deg", value=31.0),
            ],
        )
    with pytest.raises(ValidationError):
        SiteAttribute(key="slope_angle_deg", value=float("nan"))
    with pytest.raises(ValidationError):
        SiteAttribute(key="slope_angle_deg", value="x" * 33)


def test_site_profile_value_of() -> None:
    profile = SiteProfile(
        region_code="540121",
        attributes=[
            SiteAttribute(key="slope_angle_deg", value=30.0, unit="deg"),
            SiteAttribute(key="aspect", value="N"),
        ],
    )
    assert profile.value_of("slope_angle_deg") == 30.0
    assert profile.value_of("aspect") == "N"
    assert profile.value_of("missing") is None


def test_response_to_verdict_carries_provenance_and_basis() -> None:
    response = _response(assessment={"engine_version": "v-test"}, human_review_suggested=True)
    verdict = response.to_verdict(hits=[_hit()])
    assert verdict.hazard_type is HazardType.DEBRIS_FLOW
    assert verdict.risk_level is RiskLevel.ORANGE
    assert verdict.human_review_suggested is True
    assert verdict.data_provenance.method == "factor_engine_mock"
    assert verdict.confidence_basis == "coverage=0.80;validation=pass"
    assert verdict.assessment == {"engine_version": "v-test"}
    payload = verdict.as_payload()
    assert payload["data_provenance"]["is_mock"] is True
    assert payload["confidence_basis"] == "coverage=0.80;validation=pass"


def test_error_codes_match_schema_pattern() -> None:
    for code in AssessErrorCode:
        assert CODE_PATTERN.match(code.value), code.value
        assert len(code.value) <= 21, code.value


def test_provenance_from_payload_is_forgiving() -> None:
    assert DataProvenance.from_payload(None).data_kind == "unknown"
    assert DataProvenance.from_payload({"data_kind": "observed"}).data_kind == "observed"
    assert DataProvenance.from_payload({"bogus": 1}).data_kind == "unknown"
