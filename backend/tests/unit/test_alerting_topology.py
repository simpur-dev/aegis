"""告警链路一致性：规则文件、抓取配置、分发路由与 compose 挂载四者必须对齐。

真实教训：`deploy/observability/alerts.yml` 早就写好、也单独过 promtool 校验，但 `prometheus.yml`
里既没有 `rule_files` 也没有 `alerting` 段——于是 Prometheus 只抓样本、从不求值那九条 SLA 规则，
Alertmanager 也永远收不到任何东西。"规则文件存在"与"告警链路在跑"是两件事，且失败形态是静默的，
只能靠机器钉住：任何人改动其中一处而漏掉另一处，这里必须变红。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from aegis.config import Settings
from aegis.container import create_container

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_DIR = REPO_ROOT / "deploy"
PROMETHEUS = COMPOSE_DIR / "prometheus.yml"
ALERT_RULES = COMPOSE_DIR / "observability" / "alerts.yml"
ALERTMANAGER = COMPOSE_DIR / "observability" / "alertmanager.yml"
COMPOSE = COMPOSE_DIR / "docker-compose.yml"

# 路由树的两个出口：critical 走值班升级，其余落平台值班（与 alertmanager.yml 同步演进）
CRITICAL_RECEIVER = "duty-escalation"
FALLBACK_RECEIVER = "platform-oncall"


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def alert_rules() -> list[dict[str, Any]]:
    doc = load(ALERT_RULES)
    return [rule for group in doc["groups"] for rule in group["rules"]]


def rule_severity(rule: dict[str, Any]) -> str:
    return str(rule["labels"]["severity"])


def container_paths_of(service: str, key: str = "volumes") -> dict[str, str]:
    """compose 里某个 service 的"容器内路径 -> 仓库内源文件"映射（匿名卷与纯路径条目忽略）。"""
    mapping: dict[str, str] = {}
    for entry in load(COMPOSE)["services"][service].get(key, []):
        if isinstance(entry, str) and entry.count(":") >= 2:
            source, target, _mode = entry.split(":")[:3]
            mapping[target] = source
    return mapping


def exported_metric_families() -> set[str]:
    """/metrics 实写的指标族名：`# TYPE <name> <kind>` 是导出器与规则文件之间唯一的公共面。"""
    ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=False)
    text = ctn.exporter.collect().decode("utf-8")
    return {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}


class TestRuleFileLabels:
    def test_rules_carry_every_grouping_label_the_routing_needs(self) -> None:
        """分组键来自 labels：规则少写一个 group_by 键，那组告警就分不出组，通知也就合不到一起。"""
        group_keys = set(load(ALERTMANAGER)["route"]["group_by"])

        for rule in alert_rules():
            missing = group_keys - set(rule["labels"])
            assert not missing, f"{rule['alert']} 缺少路由分组所需标签: {sorted(missing)}"

    def test_severities_are_the_two_the_route_tree_distinguishes(self) -> None:
        """路由只对 critical 分流，其它一律落根接收者：出现第三种取值等于没人处理它。"""
        assert {rule_severity(rule) for rule in alert_rules()} <= {"critical", "warning"}
        assert any(rule_severity(rule) == "critical" for rule in alert_rules())

    def test_exprs_only_reference_metrics_the_exporter_actually_writes(self) -> None:
        """规则里写一个导出器不产出的指标名，Prometheus 不会报错，只会让告警恒为空。"""
        families = exported_metric_families()
        referenced: set[str] = set()
        for rule in alert_rules():
            for name in re.findall(r"\baegis_[a-z0-9_]+", str(rule["expr"])):
                referenced.add(name.removesuffix("_bucket"))

        assert referenced, "规则里没抓到任何 aegis_* 指标，正则可能已随告警文件写法失效"
        absent = sorted(referenced - families)
        assert not absent, f"告警规则引用了 /metrics 不存在的指标族: {absent}"


class TestPrometheusConfiguration:
    def test_rules_are_loaded_and_an_alertmanager_is_targeted(self) -> None:
        doc = load(PROMETHEUS)
        assert doc.get("rule_files"), "prometheus.yml 没有 rule_files：SLA 规则永不求值"
        managers = doc["alerting"]["alertmanagers"]
        targets = [t for item in managers for t in item["static_configs"][0]["targets"]]
        assert "alertmanager:9093" in targets, f"未指向 compose 里的 alertmanager: {targets}"

    def test_rule_files_are_mounted_into_the_prometheus_container(self) -> None:
        """配置写了容器内路径、compose 没挂载，Prometheus 启动即报文件不存在。"""
        mounted = container_paths_of("prometheus")
        for entry in load(PROMETHEUS)["rule_files"]:
            assert entry in mounted, f"rule_files 的 {entry} 没有对应的 compose 挂载"
            source = COMPOSE_DIR / mounted[entry]
            assert source.is_file(), f"挂载源文件不存在: {source}"


class TestAlertmanagerRouting:
    def test_every_route_lands_on_a_declared_receiver(self) -> None:
        doc = load(ALERTMANAGER)
        declared = {receiver["name"] for receiver in doc["receivers"]}

        walk = [doc["route"]]
        while walk:
            route = walk.pop()
            assert route["receiver"] in declared, f"路由指向未定义的接收者: {route['receiver']}"
            walk.extend(route.get("routes", []))

    def test_critical_gets_its_own_route(self) -> None:
        """critical 必须真的被分流到升级接收者，否则 alertmanager.yml 与 alerts.yml 分级脱钩。"""
        sub_routes = load(ALERTMANAGER)["route"]["routes"]
        assert any(rule_severity_matches(route, "critical") and route["receiver"] == CRITICAL_RECEIVER for route in sub_routes)

    def test_inhibit_rules_cannot_be_permanently_inert(self) -> None:
        """抑制按 equal 列出的标签取值求交集：只要没有任何 critical 告警与 warning 告警共享同一取值，
        这条规则就永远不生效。写在 indicator 上就是这种死规则（两组告警的指标名天然不同），
        写在 instance/team 上才可能命中——team 是规则文件自己声明的，可以在这里静态证明。"""
        doc = load(ALERTMANAGER)
        rules = alert_rules()
        declared = set.intersection(*(set(rule["labels"]) for rule in rules))

        for inhibit in doc["inhibit_rules"]:
            keys = [key for key in inhibit["equal"] if key in declared]
            if not keys:
                continue  # 全靠抓取标签（instance/job）对齐：静态面证明不了，交给在线用例
            sources = {tuple(rule["labels"][key] for key in keys) for rule in rules if rule_severity(rule) == "critical"}
            targets = {tuple(rule["labels"][key] for key in keys) for rule in rules if rule_severity(rule) == "warning"}
            assert sources & targets, f"抑制规则 {inhibit['equal']} 在真实告警标签上永不可能匹配"


def rule_severity_matches(route: dict[str, Any], severity: str) -> bool:
    return any(f"severity = {severity}" in str(matcher) for matcher in route.get("matchers", []))
