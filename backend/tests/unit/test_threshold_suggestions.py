"""阈值建议闭环的装配级判据（§3.5 后半截）。

这里**不连库**：SQL 语句的形状由纯函数产出后直接断言（与 `test_rules_api` 里对
`rulebook.build_query` 的判法同一套路），而"库里的行真的变了没有"由
`tests/integration/test_suggestions_live.py` 在真 Postgres 上量。两层分开是因为
把事务语义写进一条 mock 用例，得到的永远是"mock 说我提交了"这种自我实现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis.domain.messages import ThresholdSuggestion, new_suggestion_id
from aegis.persistence.suggestions import (
    PENDING,
    SUGGESTION_COLUMNS,
    SuggestionArgumentError,
    apply_threshold,
    build_decide_statements,
    build_insert,
    build_reject,
    build_select,
    check_status,
    parse_conditions,
)

TRACE = "trc_" + "7" * 16
DDL = (Path(__file__).resolve().parents[2] / "src" / "aegis" / "persistence" / "sql" / "008_threshold_suggestions.sql").read_text(
    encoding="utf-8"
)


def _suggestion(**kw: object) -> ThresholdSuggestion:
    payload: dict[str, object] = {
        "suggestion_id": new_suggestion_id(),
        "trace_id": TRACE,
        "suggestion": "rain_10min 阈值偏高，两次空报",
    }
    payload.update(kw)
    return ThresholdSuggestion(**payload)  # type: ignore[arg-type]


def _active_rule(**kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "rule_id": "R-DEBRIS-RAIN-1",
        "version": 1,
        "status": "active",
        "hazard_type": "debris_flow",
        "description": "短时强降雨激发泥石流",
        "mode": "all",
        "triggered_level": 2,
        "weight": 1.0,
        "conditions": [{"metric": "rain_10min", "op": ">=", "threshold": 30.0, "agg": "max", "window_seconds": 3600}],
        "calibration_basis": "公开规范量级，未经现场标定",
    }
    row.update(kw)
    return row


class Test建议入账的建形:
    def test_行模型与建表语句逐列对得上(self) -> None:
        """单一真源那条：`domain` 的字段与 `008` 的列名漂移一次，这条门禁就该红。

        与 `test_agent_memory_store.py` 对 007 的判法同一套路——不这么钉住的话，
        改列名的人只会看到"写入时报没有这一列"，而那发生在生产库上。
        """
        body = DDL.split("CREATE TABLE IF NOT EXISTS threshold_suggestions (", 1)[1].split(");", 1)[0]
        columns = set(re.findall(r"^\s{4}([a-z_]+)\s+[a-z]", body, flags=re.MULTILINE))
        assert columns == set(SUGGESTION_COLUMNS), columns ^ set(SUGGESTION_COLUMNS)
        assert set(ThresholdSuggestion.model_fields) <= set(SUGGESTION_COLUMNS)
        # 状态与裁决侧的字面量也要在库里存在，否则模型写出来的值会被 CHECK 拒
        assert "'pending_human_review'" in DDL and "'confirmed'" in DDL and "'rejected'" in DDL

    def test_空建议进不了行模型(self) -> None:
        with pytest.raises(ValidationError):
            _suggestion(suggestion="   ")

    def test_效果分缺席就是None不折成0(self) -> None:
        # 0.0 是一个结论（"完全没效果"），None 是一个事实（没测到）。混起来就是把没测说成测过。
        assert _suggestion(effect_score=None).effect_score is None
        assert _suggestion(effect_score=None).as_row()["status"] == PENDING

    def test_效果分越界就拒(self) -> None:
        for bad in (1.5, -0.1):
            with pytest.raises(ValidationError):
                _suggestion(effect_score=bad)

    def test_写入语句逐列绑定不拼字符串(self) -> None:
        row = _suggestion(region_code="540121").as_row()
        sql, params = build_insert(row)
        assert sql.count("$") == len(params)
        assert "threshold_suggestions" in sql
        assert TRACE in params
        assert not any(";" in str(value) for value in params)

    def test_状态过滤器与库的CHECK同集合(self) -> None:
        assert check_status("Confirmed") == "confirmed"
        with pytest.raises(SuggestionArgumentError):
            # 拼错的状态不该返回空集（看起来像"库里没有建议"），要当场拒
            check_status("pending_review")

    def test_读清单的limit与状态都进参数(self) -> None:
        sql, params = build_select(status=PENDING, limit=7)
        assert params == [PENDING, 7]
        assert "ORDER BY created_at DESC" in sql
        sql_all, params_all = build_select(status="all", limit=7)
        assert " WHERE " not in sql_all and params_all == [7]


class Test改阈值这件事的纯算术:
    def test_指标不在规则里就拒并列出可用指标(self) -> None:
        with pytest.raises(SuggestionArgumentError) as exc:
            apply_threshold(_active_rule()["conditions"], metric="位移", threshold=1.0)  # type: ignore[arg-type]
        assert exc.value.detail["available"] == ["rain_10min"]

    def test_同名指标出现两次就拒(self) -> None:
        # 改哪一条不该由数组顺序决定：静默选第一条就是替值班员做了他没做过的选择
        doubled = [
            {"metric": "rain_10min", "op": ">=", "threshold": 30.0, "agg": "max", "window_seconds": 3600},
            {"metric": "rain_10min", "op": ">=", "threshold": 45.0, "agg": "max", "window_seconds": 7200},
        ]
        with pytest.raises(SuggestionArgumentError):
            apply_threshold(doubled, metric="rain_10min", threshold=25.0)

    def test_负阈值与非数都拒(self) -> None:
        conditions = _active_rule()["conditions"]
        with pytest.raises(SuggestionArgumentError):
            apply_threshold(conditions, metric="rain_10min", threshold=-1.0)  # type: ignore[arg-type]
        with pytest.raises(SuggestionArgumentError):
            apply_threshold(conditions, metric="rain_10min", threshold="三十")  # type: ignore[arg-type]

    def test_只换阈值不动算子窗口与聚合(self) -> None:
        new, before = apply_threshold(_active_rule()["conditions"], metric="rain_10min", threshold=25.0)  # type: ignore[arg-type]
        assert before["threshold"] == 30.0
        assert new[0]["threshold"] == 25.0
        assert new[0]["op"] == ">=" and new[0]["agg"] == "max" and new[0]["window_seconds"] == 3600

    def test_条件载荷形状不对就说清是哪一列(self) -> None:
        with pytest.raises(SuggestionArgumentError):
            parse_conditions("不是 JSON")
        with pytest.raises(SuggestionArgumentError):
            parse_conditions([{"metric": "rain"}, "字符串行"])


class Test确认那一笔的语句:
    def test_新版本号是当前版本加一且三句话都带建议号或签字人(self) -> None:
        active = _active_rule(version=3)
        new_conditions, before = apply_threshold(active["conditions"], metric="rain_10min", threshold=25.0)  # type: ignore[arg-type]
        statements = build_decide_statements(
            suggestion_id="sug_aaaabbbbcccc",
            decided_by="王工",
            note="现场复核 6 月两次空报",
            active=active,
            new_conditions=new_conditions,
            before=before,
            threshold=25.0,
        )
        retire_sql, retire_params = statements[0]
        insert_sql, insert_params = statements[1]
        decide_sql, decide_params = statements[2]
        assert "status = 'retired'" in retire_sql and retire_params[1] == "4", "拼接文案里的那一格由驱动认成 text"
        assert "'active'" in insert_sql and insert_params[1] == 4
        assert insert_params[9] == "王工", "新版本的 reviewer 是签字人，不是平台也不是模型"
        assert "sug_aaaabbbbcccc" in insert_params[8], "标定依据栏要能回指到那条建议"
        assert decide_params[1] == "confirmed"
        assert "AND status = 'pending_human_review'" in decide_sql, "一条建议只允许被裁决一次"

    def test_before与after两格都写进参数(self) -> None:
        active = _active_rule()
        new_conditions, before = apply_threshold(active["conditions"], metric="rain_10min", threshold=25.0)  # type: ignore[arg-type]
        statements = build_decide_statements(
            suggestion_id="sug_1" + "0" * 11,
            decided_by="王工",
            note="",
            active=active,
            new_conditions=new_conditions,
            before=before,
            threshold=25.0,
        )
        payload = statements[2][1]
        # jsonb 那两格传的是 Python 对象（编解码器负责序列化）：先 dumps 会变成 JSON 字符串标量
        assert payload[5] == {"metric": "rain_10min", "threshold": 30.0, "version": 1}
        assert payload[6] == {"metric": "rain_10min", "threshold": 25.0, "from_version": 1}
        assert isinstance(statements[1][1][7], list), "conditions 传对象数组，不传自己拼好的 JSON 文本"

    def test_驳回不署名就拒(self) -> None:
        with pytest.raises(SuggestionArgumentError):
            build_reject(suggestion_id="sug_x", decided_by="  ", note="不认")
