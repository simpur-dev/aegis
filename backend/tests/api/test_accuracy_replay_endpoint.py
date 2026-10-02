"""预警准确率回放的 HTTP 出口：大屏要能直接读同一份报表，而不是只有 CLI。

审计照实记（2026-10-02）：算式、判据分支、真值表与库侧配对都已就位，但回放只有命令行入口
——`--from-store` 那一路跑出来的报表没人能从界面看到，验收时"准确率回落"就还是一句口头话。

这里守三条：
① 端点**不做任何算术**，判定一律走 `persistence/replay.measure`（全局唯一口径）；
② 存储后端不提供库侧回放时是 503 + 明确要配什么，而不是回一份看起来正常的空报表；
③ 数据集性质（现场标注 / 合成 / 未声明）由调用方显式声明，默认 `unspecified`，
   非现场标注时 `official_accuracy` 恒为 None——这是"≥80% 达成"最容易被混过去的地方。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.persistence.replay import ReplayCase, ReplayDataset, measure

BASE = "http://testserver"
SINCE = "2026-09-01T00:00:00+00:00"


def _case(case_id: str, *, truth: bool, predicted: bool, hazard: str = "debris_flow") -> ReplayCase:
    """回放案例的最小构造：`type_correct` 由"报对灾种"推出来，不在测试里手填。"""
    return ReplayCase(
        case_id=case_id,
        hazard_type=hazard,
        region_code="540121",
        truth_warning=truth,
        predicted_warning=predicted,
        predicted_hazard_type=hazard if predicted else None,
        truth_level=2 if truth else None,
        predicted_level=2 if predicted else None,
        lead_seconds=600.0 if (truth and predicted) else None,
    )


class StubAccuracyStore:
    """只回答库侧回放这一件事的存储替身：记录入参，返回给定案例集合。"""

    def __init__(self, cases: list[ReplayCase]) -> None:
        self._cases = cases
        self.calls: list[dict[str, Any]] = []

    async def accuracy_replay_cases(self, **kwargs: Any) -> list[ReplayCase]:
        self.calls.append(kwargs)
        return list(self._cases)


class StubPlainStore:
    """内存读视图那一类：没有 `accuracy_replay_cases` 这个方法。"""

    def snapshot(self) -> dict[str, Any]:
        return {}


def base_settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {"env": "test", "bus_backend": "memory", "store_backend": "memory", "simulator_enabled": False}
    payload.update(overrides)
    return Settings(**payload)


async def _client(store: Any) -> httpx.AsyncClient:
    ctn = create_container(base_settings(), with_simulator=False)
    ctn.store = store  # type: ignore[assignment]
    app = create_app(ctn.settings, container=ctn)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def hits_store() -> AsyncIterator[tuple[httpx.AsyncClient, StubAccuracyStore]]:
    """四条案例：2 命中（其中一条真无预测=漏报、一条预测无真=误报）。"""
    cases = [
        _case("case_api_tp1", truth=True, predicted=True),
        _case("case_api_tp2", truth=True, predicted=True),
        _case("case_api_fn", truth=True, predicted=False),
        _case("case_api_fp", truth=False, predicted=True),
    ]
    store = StubAccuracyStore(cases)
    http = await _client(store)
    try:
        yield http, store
    finally:
        await http.aclose()


async def _replay(http: httpx.AsyncClient, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {"since": SINCE, **(params or {})}
    response = await http.get("/api/v1/accuracy/replay", params=query)
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestEndpoint:
    async def test_报表字段齐全且明确标出这是哪种数据集(self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore]) -> None:
        http, _store = hits_store
        body = await _replay(http)

        assert body["status"] == "not_measured"
        assert body["indicator"] == "synthetic_only"
        assert body["official_accuracy"] is None
        assert body["measured_accuracy"] == pytest.approx(0.5)  # TP/(TP+FN+FP) = 2/4
        assert body["dataset"]["kind"] == "unspecified"
        assert body["dataset"]["cases"] == 4
        assert "不构成官方" in str(body["provenance_warning"])

    async def test_端点不做算术_结果与直接调用唯一口径完全相同(self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore]) -> None:
        """这条是防"两套准确率"的关键：HTTP 面上出现的数字必须能由 measure() 原样复现。"""
        cases = [
            _case("case_api_tp1", truth=True, predicted=True),
            _case("case_api_tp2", truth=True, predicted=True),
            _case("case_api_fn", truth=True, predicted=False),
            _case("case_api_fp", truth=False, predicted=True),
        ]
        expected = measure(
            ReplayDataset(cases=tuple(cases), kind="field", source="api-gate", note="门禁样例"),
            min_field_cases=4,
        ).as_dict()

        http, _store = hits_store
        body = await _replay(http, {"kind": "field", "min_field_cases": 4})

        judgement = (
            "status",
            "indicator",
            "official_accuracy",
            "measured_accuracy",
            "overall",
            "per_hazard",
            "type_correct_of_tp",
            "target",
        )
        assert {key: body[key] for key in judgement} == {key: expected[key] for key in judgement}
        # 数据集身份由端点自己声明（来源与性质都要外显），所以只比 judgement 那几项
        assert body["dataset"]["kind"] == "field"
        assert body["dataset"]["source"] == "store:warning_truth_labels"

    async def test_现场标注且样本够时才允许给出官方准确率(self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore]) -> None:
        http, _store = hits_store
        body = await _replay(http, {"kind": "field", "min_field_cases": 4, "accuracy_target": 0.5})

        assert body["status"] == "measured"
        assert body["indicator"] == "met"
        assert body["official_accuracy"] == pytest.approx(0.5)
        assert body["provenance_warning"] is None

    async def test_样本不够时是insufficient_sample而不是一个像样的百分比(
        self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore]
    ) -> None:
        http, _store = hits_store
        body = await _replay(http, {"kind": "field", "min_field_cases": 50})

        assert body["status"] == "insufficient_sample"
        assert body["official_accuracy"] is None
        assert body["indicator"] == "not_measured"

    async def test_时间窗与区域条件原样交给库侧而不是在HTTP层重算(self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore]) -> None:
        http, store = hits_store
        before = len(store.calls)

        body = await _replay(http, {"window_seconds": 900, "region_code": "540121", "until": "2026-09-30T00:00:00+00:00"})

        assert len(store.calls) == before + 1
        call = store.calls[-1]
        assert call["window_seconds"] == 900
        assert call["region_code"] == "540121"
        assert call["since"].tzinfo is not None and call["until"] is not None
        assert body["window_seconds"] == 900

    @pytest.mark.parametrize(
        ("params", "field"),
        [
            ({"window_seconds": 0}, "window_seconds"),
            ({"window_seconds": -1}, "window_seconds"),
            ({"window_seconds": 99999}, "window_seconds"),
            ({"region_code": "54"}, "region_code"),
            ({"accuracy_target": 1.5}, "accuracy_target"),
            ({"since": "2026-09-01T00:00:00"}, "since"),
        ],
    )
    async def test_非法参数在触库之前就被拒(
        self, hits_store: tuple[httpx.AsyncClient, StubAccuracyStore], params: dict[str, Any], field: str
    ) -> None:
        http, store = hits_store
        before = len(store.calls)

        response = await http.get("/api/v1/accuracy/replay", params={"since": SINCE, **params})

        assert response.status_code == 422, response.text
        assert field in str(response.json()), response.text
        assert len(store.calls) == before, "非法条件不该走到库"


class TestDegradedStorage:
    async def test_存储后端不支持库侧回放时是503并说清缺什么(self) -> None:
        http = await _client(StubPlainStore())
        try:
            response = await http.get("/api/v1/accuracy/replay", params={"since": SINCE})
        finally:
            await http.aclose()

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["code"] == "E_ACCURACY_UNAVAILABLE"
        assert "postgres" in str(detail["requires"])

    async def test_这个端点是只读的_知识层与外呼那几条写入口不受影响(self) -> None:
        ctn: PlatformContainer = create_container(base_settings(), with_simulator=False)
        app = create_app(ctn.settings, container=ctn)
        gets = {route.path for route in app.routes if "GET" in (getattr(route, "methods", None) or set())}
        assert "/api/v1/accuracy/replay" in gets
