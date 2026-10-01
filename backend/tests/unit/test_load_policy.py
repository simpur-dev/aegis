"""压测预算口径的行为测试（`aegis.observability.load_policy`）。

口径本身住在产品包里是有原因的：locust 一被 import 就做 gevent monkey-patch，
测试进程里没法 import 压测档案（`tests/perf/test_load_profile.py` 只能走 AST 做结构检查）。
所以"耗时换算 + 阈值判定"这两件会直接影响压测结论真假的事，在这里按行为覆盖：

- `float(timedelta)` 抛 TypeError，而 locust 会把任务异常记成 task error，
  汇总表仍是"0 请求、0.00% 失败、退出码 0"——静默零流量的压测比不跑更糟；
- 等于阈值必须判合格：考核口径写的是"≤"。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aegis.config import Settings
from aegis.observability.load_policy import drill_budget_ms, elapsed_ms, over_budget, read_budget_ms


class _Resp:
    def __init__(self, elapsed: object) -> None:
        self.elapsed = elapsed


class TestElapsedConversion:
    def test_timedelta_elapsed_becomes_milliseconds(self) -> None:
        assert elapsed_ms(_Resp(timedelta(milliseconds=1_500))) == pytest.approx(1500.0)

    def test_casting_timedelta_to_float_is_the_bug_we_guard(self) -> None:
        """先证明旧写法本身不可行，再证明新函数可行——否则这条守卫只是自说自话。"""
        with pytest.raises(TypeError):
            float(timedelta(seconds=1))  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("raw", "expected_ms"),
        [
            (timedelta(0), 0.0),
            (timedelta(milliseconds=1), 1.0),
            (timedelta(seconds=3), 3_000.0),
            (timedelta(minutes=1), 60_000.0),
        ],
    )
    def test_conversion_is_linear_across_the_range(self, raw: timedelta, expected_ms: float) -> None:
        assert elapsed_ms(_Resp(raw)) == pytest.approx(expected_ms)

    def test_seconds_as_plain_number_still_read_as_seconds(self) -> None:
        assert elapsed_ms(_Resp(0.25)) == pytest.approx(250.0)


class TestBudgetsFollowSettings:
    def test_read_budget_is_twice_schedule_with_a_floor(self) -> None:
        assert read_budget_ms(Settings(sla_schedule_ms=2_000)) == 4_000
        # 阈值调小不能让读请求预算跌破 1s：否则压测会比告警更严，两边各说一套
        assert read_budget_ms(Settings(sla_schedule_ms=100)) == 1_000

    def test_drill_budget_is_sync_plus_schedule(self) -> None:
        assert drill_budget_ms(Settings(sla_sync_ms=3_000, sla_schedule_ms=2_000)) == 5_000

    def test_shipped_defaults_keep_drill_looser_than_a_single_read(self) -> None:
        """演练一次要跑完五段：它的预算不能比单次读请求更紧，否则压测自己在制造假违约。"""
        defaults = Settings()
        assert drill_budget_ms(defaults) >= read_budget_ms(defaults)
        assert read_budget_ms() == max(float(defaults.sla_schedule_ms) * 2.0, 1_000.0)


class TestOverBudgetJudgement:
    def test_at_the_budget_counts_as_passing(self) -> None:
        """考核口径是"≤"：正好等于预算判合格，判超标会把边界样本算成违约。"""
        assert over_budget("telemetry", 4_000.0, 4_000.0) is None

    def test_beating_the_budget_names_both_numbers(self) -> None:
        message = over_budget("telemetry", 4_500.0, 4_000.0)
        assert message is not None
        assert "telemetry" in message and "4500" in message and "4000" in message

    @pytest.mark.parametrize("took_ms", [0.0, 999.0, 4_000.0])
    def test_within_budget_stays_quiet(self, took_ms: float) -> None:
        assert over_budget("agents", took_ms, 4_000.0) is None

    @pytest.mark.parametrize("took_ms", [4_000.1, 30_000.0])
    def test_over_budget_always_reports(self, took_ms: float) -> None:
        assert over_budget("drill", took_ms, 4_000.0) is not None
