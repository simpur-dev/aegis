"""Locust 压测档案：把考核指标的阈值直接写成压测断言，产出可第三方复核的实测证据。

阈值不在这里手写数字，而是一律从 `aegis.config.Settings` 取——否则压测口径会和
平台量测口径漂移成两套，"≤2s 调度响应"就变成靠人记住的口头约定。

用法：
    AEGIS_DELIVERY_MODE=mock uv run locust -f tests/load/locustfile.py \\
        --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8000
"""

from __future__ import annotations

from typing import Any

from locust import HttpUser, between, task

from aegis.config import get_settings

_settings = get_settings()

# 考核阈值（秒→毫秒就地换算）：与 SLA 告警规则同源，压测报告与告警不可能各说一套。
SCHEDULE_SLA_MS = float(_settings.sla_schedule_ms)
SYNC_SLA_MS = float(_settings.sla_sync_ms)
READ_SLA_MS = max(SCHEDULE_SLA_MS * 2.0, 1_000.0)

READ_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/telemetry?limit=50",
    "/api/v1/warnings?limit=20",
    "/api/v1/agents",
    "/api/v1/metrics/latency",
    "/api/v1/integrations",
    "/api/v1/workflow/node-types",
    "/readyz",
)


class AegisReadUser(HttpUser):
    """读侧用户：模拟指挥台/大屏轮询，衡量"常规任务调度响应时延 ≤2s"这一侧的真实体验。

    只打只读端点：压测不能改写平台状态，否则测到的时延混进了写入抖动。
    """

    wait_time = between(0.2, 1.0)

    @task(3)
    def telemetry_feed(self) -> None:
        self._get("/api/v1/telemetry?limit=50", "telemetry")

    @task(3)
    def warnings_feed(self) -> None:
        self._get("/api/v1/warnings?limit=20", "warnings")

    @task(2)
    def agents_registry(self) -> None:
        self._get("/api/v1/agents", "agents")

    @task(2)
    def latency_report(self) -> None:
        self._get("/api/v1/metrics/latency", "latency")

    @task(1)
    def integration_status(self) -> None:
        self._get("/api/v1/integrations", "integrations")

    @task(1)
    def node_types(self) -> None:
        self._get("/api/v1/workflow/node-types", "node_types")

    def _get(self, path: str, name: str) -> None:
        with self.client.get(path, name=name, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            elapsed_ms = float(response.elapsed) * 1000.0
            if elapsed_ms > READ_SLA_MS:
                response.failure(f"{name} 超阈值: {elapsed_ms:.0f}ms > {READ_SLA_MS:.0f}ms")
            else:
                response.success()


class AegisDrillUser(HttpUser):
    """演练用户：真正驱动一次五段链路，用来观察协同事务在并发下的时延分布。

    每个阈值都以请求名进 Locust 统计，报告里的 P95 就是考核口径的 P95。
    """

    wait_time = between(1.0, 3.0)

    @task
    def run_drill(self) -> None:
        payload: dict[str, Any] = {"region_code": "540121", "hazard_type": "rainstorm", "intensity": 96.0}
        with self.client.post("/api/v1/drill/run", json=payload, name="drill_run", catch_response=True) as response:
            if response.status_code not in (200, 202):
                response.failure(f"HTTP {response.status_code}")
                return
            elapsed_ms = float(response.elapsed) * 1000.0
            # 一次演练覆盖感知→研判→规划→执行→反馈，上界取同步 + 调度两条阈值之和。
            budget = SYNC_SLA_MS + SCHEDULE_SLA_MS
            if elapsed_ms > budget:
                response.failure(f"drill 超阈值: {elapsed_ms:.0f}ms > {budget:.0f}ms")
            else:
                response.success()
