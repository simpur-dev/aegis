"""Locust 压测档案：把考核指标的阈值直接写成压测断言，产出可第三方复核的实测证据。

阈值与判定口径全部住在 `aegis.observability.load_policy`（与 SLA 告警规则同源），本文件只负责
"打哪些端点、按什么频率打、结果怎么上报"。放在产品包里有两个理由：一是压测口径不能和平台
量测口径漂移成两套；二是 locust 一 import 就做 gevent monkey-patch，测试进程里没法 import 本
文件——判定逻辑留在这里才能被单测真正覆盖。

用法：
    uv run python -m scripts.load_curve                     # 并发梯度曲线（自己起服务）
    AEGIS_DELIVERY_MODE=mock uv run locust -f tests/load/locustfile.py \\
        --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8000
"""

from __future__ import annotations

from typing import Any

from locust import HttpUser, between, task

from aegis.observability.load_policy import drill_budget_ms, elapsed_ms, over_budget, read_budget_ms

READ_SLA_MS = read_budget_ms()
DRILL_BUDGET_MS = drill_budget_ms()


READ_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/telemetry?limit=50",
    "/api/v1/warnings?limit=20",
    "/api/v1/agents",
    "/api/v1/metrics/latency",
    "/api/v1/integrations",
    "/api/v1/stations?limit=200",
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

    @task(2)
    def stations_ledger(self) -> None:
        """一张图的点位数据源：大屏每次刷新都要它，不量就等于没测。"""
        self._get("/api/v1/stations?limit=200", "stations")

    @task(1)
    def readiness(self) -> None:
        """就绪探针在负载下必须仍然是 200：它在高并发下先劣化，说明容量口径到顶了。"""
        self._get("/readyz", "readyz")

    def _get(self, path: str, name: str) -> None:
        with self.client.get(path, name=name, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            breach = over_budget(name, elapsed_ms(response), READ_SLA_MS)
            if breach is not None:
                response.failure(breach)
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
            # 一次演练覆盖感知→研判→规划→执行→反馈，预算口径见 load_policy.drill_budget_ms。
            breach = over_budget("drill", elapsed_ms(response), DRILL_BUDGET_MS)
            if breach is not None:
                response.failure(breach)
            else:
                response.success()
