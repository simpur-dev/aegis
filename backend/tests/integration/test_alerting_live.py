"""真实 Alertmanager / Prometheus 的在线验证（默认跳过）。

本机取证起法（与 deploy/docker-compose.yml 的 observability profile 同形）：

    docker network create aegis-alerting
    docker run -d --name alertmanager --network aegis-alerting -p 127.0.0.1:9093:9093 \\
        -v $PWD/../deploy/observability/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro \\
        prom/alertmanager:v0.27.0 --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager
    docker run -d --name prometheus --network aegis-alerting -p 127.0.0.1:9090:9090 \\
        -v $PWD/../deploy/prometheus.yml:/etc/prometheus/prometheus.yml:ro \\
        -v $PWD/../deploy/observability/alerts.yml:/etc/prometheus/rules/alerts.yml:ro \\
        prom/prometheus:v2.55.0

    AEGIS_TEST_ALERTMANAGER_URL=http://127.0.0.1:9093 AEGIS_TEST_PROMETHEUS_URL=http://127.0.0.1:9090 \\
        pytest tests/integration/test_alerting_live.py

只验"只有真服务才能证明的东西"，静态一致性测试（tests/unit/test_alerting_topology.py）做不到：
1. 九条规则在真 Prometheus 里被加载且求值健康（表达式写错只在运行期暴露）；
2. Prometheus 真的把 Alertmanager 发现成了活动分发目标（网络可达 + 配置生效）；
3. critical/warning 按路由树分别落到 duty-escalation / platform-oncall；
4. 抑制规则真的把同实例同团队的 warning 压成 suppressed —— 静态写错 equal 键时它恒不生效，
   只有跑起来才看得出来。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

AM_URL = os.getenv("AEGIS_TEST_ALERTMANAGER_URL", "").rstrip("/")
PROM_URL = os.getenv("AEGIS_TEST_PROMETHEUS_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(not AM_URL, reason="未设置 AEGIS_TEST_ALERTMANAGER_URL，跳过真实 Alertmanager 测试")

RULES_FILE = Path(__file__).resolve().parents[3] / "deploy" / "observability" / "alerts.yml"
CRITICAL_RECEIVER = "duty-escalation"
FALLBACK_RECEIVER = "platform-oncall"
# 告警窗口足够短，测试留下的样本会自行过期，不给下一轮或验收现场留噪音
ALERT_TTL = timedelta(minutes=10)


def rules_by_severity(severity: str) -> list[dict[str, Any]]:
    doc = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    rules = [rule for group in doc["groups"] for rule in group["rules"]]
    return [rule for rule in rules if rule["labels"]["severity"] == severity]


def payload(rule: dict[str, Any], instance: str) -> list[dict[str, Any]]:
    start = datetime.now(UTC)
    return [
        {
            "labels": {**rule["labels"], "alertname": rule["alert"], "instance": instance, "job": "aegis-backend"},
            "annotations": dict(rule.get("annotations") or {}),
            "startsAt": (start - timedelta(minutes=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "endsAt": (start + ALERT_TTL).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
    ]


async def receiver_of(client: httpx.AsyncClient, alertname: str) -> str:
    groups = (await client.get("/api/v2/alerts/groups")).json()
    for group in groups:
        if any(alert["labels"]["alertname"] == alertname for alert in group.get("alerts", [])):
            return str(group["receiver"]["name"])
    raise AssertionError(f"Alertmanager 未把 {alertname} 归入任何分组：路由树或标签不匹配")


async def wait_until_suppressed(client: httpx.AsyncClient, alertname: str, timeout: float = 15.0) -> list[str]:
    """抑制是通知期计算的，POST 之后要等一拍才在 status.inhibitedBy 上体现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alerts = (await client.get("/api/v2/alerts", params={"active": "true", "inhibited": "true"})).json()
        for alert in alerts:
            if alert["labels"]["alertname"] == alertname:
                inhibited = alert["status"]["inhibitedBy"]
                if inhibited and alert["status"]["state"] == "suppressed":
                    return inhibited
        await asyncio.sleep(0.5)
    raise AssertionError(f"{timeout}s 内 warning 未被抑制：inhibit_rules 的 equal 键与真实标签不匹配")


class TestLiveAlertingChain:
    async def test_critical_and_warning_route_to_different_receivers(self) -> None:
        instance = f"aegis-itest-{uuid.uuid4().hex[:8]}:8000"
        critical = rules_by_severity("critical")[0]
        warning = rules_by_severity("warning")[0]

        async with httpx.AsyncClient(base_url=AM_URL, timeout=10.0) as client:
            for rule in (critical, warning):
                assert (await client.post("/api/v2/alerts", json=payload(rule, instance))).status_code == 200

            assert await receiver_of(client, critical["alert"]) == CRITICAL_RECEIVER
            assert await receiver_of(client, warning["alert"]) == FALLBACK_RECEIVER

    async def test_same_instance_critical_inhibits_the_warning(self) -> None:
        instance = f"aegis-itest-{uuid.uuid4().hex[:8]}:8000"
        critical = rules_by_severity("critical")[0]
        warning = rules_by_severity("warning")[0]

        async with httpx.AsyncClient(base_url=AM_URL, timeout=10.0) as client:
            for rule in (critical, warning):
                await client.post("/api/v2/alerts", json=payload(rule, instance))

            inhibited = await wait_until_suppressed(client, warning["alert"])
            assert inhibited, "抑制指纹为空说明命中的是静默而非抑制"

    @pytest.mark.skipif(not PROM_URL, reason="未设置 AEGIS_TEST_PROMETHEUS_URL，跳过 Prometheus 规则加载验证")
    async def test_prometheus_loads_every_rule_and_discovers_alertmanager(self) -> None:
        expected = {rule["alert"] for rule in yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))["groups"][0]["rules"]}
        async with httpx.AsyncClient(base_url=PROM_URL, timeout=10.0) as client:
            loaded = await client.get("/api/v1/rules")
            assert loaded.status_code == 200, "rule_files 未加载：Prometheus 里根本没有这些规则"
            data = loaded.json()["data"]
            groups = data["groups"] if isinstance(data, dict) else data
            names = {rule["name"] for group in groups for rule in group["rules"]}
            unhealthy = {rule["name"]: rule.get("health") for group in groups for rule in group["rules"] if rule.get("health") != "ok"}

            assert expected <= names, f"Prometheus 缺少的规则: {sorted(expected - names)}"
            assert not unhealthy, f"规则求值不健康（表达式或指标名有问题）: {unhealthy}"

            managers = (await client.get("/api/v1/alertmanagers")).json()["data"]
            active = [entry["url"] for entry in managers["activeAlertmanagers"]]
            assert any("alertmanager:9093" in url for url in active), f"未发现 Alertmanager 目标: {active}"
