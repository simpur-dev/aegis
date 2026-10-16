"""阈值建议闭环在真 Postgres 上的证据：确认真的长出一个新版本，且四步全成或全不成。

单测证明的是 SQL 长什么样，这一层证明的是只有真库能回答的四件事：
① `FOR UPDATE` 的行锁与部分唯一索引一起生效——同一条建议第二次裁决必须失败；
② 确认那一笔是**原子**的：指标不在规则里时，不能出现"阈值没改、版本先长出来"的半成品；
③ 台账里的 before/after/new_version 与 `trigger_rules` 里的实际行**互相对得上**
   （只断"接口回了 200"证不了这个，而那正是 §3.5 要留的痕）；
④ 服务层的 `reload_engine` 真把新阈值装进了内存规则集——库改了而判据没换，等于没落地。

运行方式与其它 live 用例相同：`AEGIS_TEST_PG_DSN=… uv run pytest tests/integration/test_suggestions_live.py`。
本文件只动自己种的规则 `R-LIVEGATE-1` 与自己那批 `trace_id`，不 TRUNCATE、不碰内置 9 条种子。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aegis.domain.messages import ThresholdSuggestion, new_suggestion_id
from aegis.persistence.postgres import PostgresStore
from aegis.persistence.suggestions import SuggestionArgumentError, SuggestionConflictError
from aegis.services.threshold_suggestions import ThresholdSuggestionService
from aegis.services.trigger_rules import RuleEngine, rulebook_from_rows

DSN = os.getenv("AEGIS_PG_DSN") or os.getenv("AEGIS_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 AEGIS_TEST_PG_DSN / AEGIS_PG_DSN，跳过真实 Postgres 测试")

RULE = "R-LIVEGATE-1"
TRACE = "trc_" + "a" * 16
METRIC = "rain_10min"


def _seed_rule() -> str:
    """种一条只属于本文件的规则：v1 active，阈值 30.0，单指标单条件。"""
    return (
        "INSERT INTO trigger_rules (rule_id, version, status, hazard_type, description, mode, triggered_level, weight,"
        " conditions, calibration_basis, reviewer, activated_at) VALUES"
        " ('R-LIVEGATE-1', 1, 'active', 'debris_flow', '门禁专用：短时强降雨激发泥石流', 'all', 2, 1.0,"
        ' \'[{"metric":"rain_10min","op":">=","threshold":30.0,"agg":"max","window_seconds":3600}]\'::jsonb,'
        " '门禁种子，非现场标定', 'pytest', now())"
        " ON CONFLICT (rule_id, version) DO NOTHING"
    )


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    pg = PostgresStore(dsn=DSN, batch_rows=50)
    await pg.connect(start_buffer=False)
    await pg.migrate()
    pool = pg.require_pool()
    await pool.execute("DELETE FROM threshold_suggestions WHERE trace_id = $1", TRACE)
    await pool.execute("DELETE FROM trigger_rules WHERE rule_id = $1", RULE)
    await pool.execute(_seed_rule())
    yield pg
    await pool.execute("DELETE FROM threshold_suggestions WHERE trace_id = $1", TRACE)
    await pool.execute("DELETE FROM trigger_rules WHERE rule_id = $1", RULE)
    await pg.close()


async def _put(store: PostgresStore, *, suggestion: str = "rain_10min 阈值偏高，两次空报") -> dict[str, Any]:
    row = ThresholdSuggestion(
        suggestion_id=new_suggestion_id(),
        trace_id=TRACE,
        warning_id="wrn_livegate_1",
        region_code="540199",
        hazard_type="debris_flow",
        suggestion=suggestion,
        attribution="民众侧触达滞后，蓝色预警后 2h 才有人撤离",
        lag_stage="响应",
        effect_score=0.42,
    ).as_row()
    return await store.put_threshold_suggestion(row)


async def _versions(store: PostgresStore) -> list[tuple[int, str]]:
    rows = await store.trigger_rules(status="all", limit=50)
    return sorted((int(item["version"]), str(item["status"])) for item in rows if item["rule_id"] == RULE)


async def _active_threshold(store: PostgresStore) -> float:
    rows = await store.trigger_rules(status="active", limit=50)
    active = next(item for item in rows if item["rule_id"] == RULE)
    return float(active["conditions"][0]["threshold"])


class Test入账与读回:
    async def test_写入后从库读回的是库里那一行(self, store: PostgresStore) -> None:
        written = await _put(store)
        assert written["status"] == "pending_human_review"
        assert written["effect_score"] == pytest.approx(0.42)
        assert written["decided_by"] == "" and written["decided_at"] is None
        listed = await store.threshold_suggestions(status="pending_human_review", limit=50)
        assert written["suggestion_id"] in [item["suggestion_id"] for item in listed]

    async def test_待确认那行的裁决侧是空格而不是零值(self, store: PostgresStore) -> None:
        """读端要能分辨"还没人裁决"（None）与"裁决成了 0"——后者会是一个假结论。"""
        written = await _put(store)
        assert written["before_value"] is None and written["after_value"] is None and written["new_version"] is None
        assert written["rule_id"] == "" and written["decision_note"] == ""


class Test确认落地:
    async def test_确认长出v2并退役v1台账同时销账(self, store: PostgresStore) -> None:
        written = await _put(store)
        decided = await store.confirm_threshold_suggestion(
            suggestion_id=written["suggestion_id"],
            rule_id=RULE,
            metric=METRIC,
            threshold=25.0,
            decided_by="王工",
            note="现场复核 6 月两次空报",
            expected_version=1,
        )
        assert decided["status"] == "confirmed"
        assert decided["decided_by"] == "王工" and decided["decided_at"] is not None
        assert decided["new_version"] == 2
        assert float(decided["before_value"]["threshold"]) == 30.0
        assert float(decided["after_value"]["threshold"]) == 25.0
        # 台账说的与库里的必须对得上：这是"闭环留痕"能被第三方复核的那一步
        assert await _active_threshold(store) == 25.0
        assert await _versions(store) == [(1, "retired"), (2, "active")]

    async def test_新版本带着签字人与回指建议的依据(self, store: PostgresStore) -> None:
        written = await _put(store)
        await store.confirm_threshold_suggestion(
            suggestion_id=written["suggestion_id"],
            rule_id=RULE,
            metric=METRIC,
            threshold=25.0,
            decided_by="王工",
            expected_version=1,
        )
        rows = await store.trigger_rules(status="all", limit=50)
        v2 = next(item for item in rows if item["rule_id"] == RULE and int(item["version"]) == 2)
        assert v2["reviewer"] == "王工", "reviewer 是签字人，不是平台也不是模型"
        assert written["suggestion_id"] in v2["calibration_basis"], "依据栏要能回指到那条建议"
        assert v2["triggered_level"] == 2 and v2["mode"] == "all", "除阈值以外的一字不改"

    async def test_第二次裁决失败且阈值不回退(self, store: PostgresStore) -> None:
        written = await _put(store)
        args = {
            "suggestion_id": written["suggestion_id"],
            "rule_id": RULE,
            "metric": METRIC,
            "threshold": 25.0,
            "decided_by": "王工",
            "expected_version": 1,
        }
        await store.confirm_threshold_suggestion(**args)
        with pytest.raises(SuggestionConflictError):
            await store.confirm_threshold_suggestion(**{**args, "expected_version": 2})
        assert await _active_threshold(store) == 25.0
        assert await _versions(store) == [(1, "retired"), (2, "active")], "第二次裁决不该再多长一个版本"

    async def test_版本对不上就不落地(self, store: PostgresStore) -> None:
        """提交者看到的现场已经不是现场：宁可拒了这一票，也不覆盖别人的判断。"""
        written = await _put(store)
        with pytest.raises(SuggestionConflictError):
            await store.confirm_threshold_suggestion(
                suggestion_id=written["suggestion_id"],
                rule_id=RULE,
                metric=METRIC,
                threshold=25.0,
                decided_by="王工",
                expected_version=7,
            )
        assert await _versions(store) == [(1, "active")], "冲突那一笔必须一个字都没改"
        again = await store.threshold_suggestions(status="pending_human_review", limit=50)
        assert written["suggestion_id"] in [item["suggestion_id"] for item in again], "被拒的确认不该顺手把建议销账"

    async def test_指标不在规则里时不留半成品(self, store: PostgresStore) -> None:
        """这条打的是"原子性"：拒绝之后既不能多出版本，建议也该还挂着待确认。"""
        written = await _put(store)
        with pytest.raises(SuggestionArgumentError):
            await store.confirm_threshold_suggestion(
                suggestion_id=written["suggestion_id"],
                rule_id=RULE,
                metric="位移速率",
                threshold=1.0,
                decided_by="王工",
                expected_version=1,
            )
        assert await _versions(store) == [(1, "active")]
        assert (await store.threshold_suggestions(status="pending_human_review", limit=50)) != []

    async def test_驳回留痕但阈值一个字不改(self, store: PostgresStore) -> None:
        written = await _put(store)
        decided = await store.reject_threshold_suggestion(suggestion_id=written["suggestion_id"], decided_by="李工", note="样本太少")
        assert decided["status"] == "rejected" and decided["decided_by"] == "李工"
        assert decided["new_version"] is None and decided["after_value"] is None
        assert await _versions(store) == [(1, "active")]
        assert await _active_threshold(store) == 30.0


class Test服务层把引擎也接上:
    async def test_确认之后装载的规则集里阈值就是新值(self, store: PostgresStore) -> None:
        engine = RuleEngine()
        service = ThresholdSuggestionService(store=store, rule_engine=engine)
        assert service.available is True
        state = await service.record(
            trace_id=TRACE,
            attribution={
                "threshold_suggestion": "rain_10min 阈值偏高，两次空报",
                "attribution": "民众侧触达滞后",
                "lag_stage": "响应",
                "effect_score": 0.42,
            },
            warning_id="wrn_livegate_1",
            region_code="540199",
            hazard_type="debris_flow",
        )
        assert state.startswith("stored:")
        suggestion_id = state.split(":", 1)[1]
        result = await service.confirm(suggestion_id, rule_id=RULE, metric=METRIC, threshold=25.0, decided_by="王工", expected_version=1)
        assert result["engine_loaded"] is True, result
        # 内置种子仍在那台引擎里，这里要比的是这一条规则：装载集里它应当是 v2/25.0
        rules, rejected = rulebook_from_rows(await store.trigger_rules(status="active", limit=200))
        assert rejected == [], rejected
        gate = next(rule for rule in rules if rule.rule_id == RULE)
        assert gate.version == 2 and gate.conditions[0].threshold == 25.0
        assert result["engine"]["versions"][RULE] == 2, "describe 里的版本号要说得出装载的是哪一版"

    async def test_台账收下建议后清单能读出来(self, store: PostgresStore) -> None:
        service = ThresholdSuggestionService(store=store, rule_engine=RuleEngine())
        before = len(await service.list())
        await _put(store)
        assert len(await service.list()) == before + 1
