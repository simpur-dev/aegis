"""批次 0 冻结的旧定义 fixture：新文档模型必须一直读得懂它们。

这些 JSON 是 `WorkflowDef.model_dump(mode="json")` 的原样冻结（GET /definitions/{id} 的形状），
产生于 `schema_version`/`layout`/`start`/`end` 引入之前。它们不是"随便一份样例"：
签名与计数在 manifest 里钉死，模板语义变化（改节点/改连线）必须先改这里并重跑对拍。
"""

from __future__ import annotations

import json
from pathlib import Path

from aegis.workflow.model import WorkflowDef
from aegis.workflow.templates import BUILTIN_TEMPLATES, REPORT_REVIEW_TEMPLATE, STANDARD_CHAIN_TEMPLATE_NAME

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflow" / "legacy"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def load(entry: dict) -> WorkflowDef:
    return WorkflowDef.model_validate(json.loads((FIXTURES / entry["file"]).read_text(encoding="utf-8")))


class TestLegacyFixtures:
    def test_all_fixtures_validate(self) -> None:
        assert len(MANIFEST["entries"]) == 8
        for entry in MANIFEST["entries"]:
            load(entry)

    def test_manifest_signatures_still_match(self) -> None:
        """签名漂移是模板语义变化，不能让 fixture 静默变成"另一张图"。"""
        for entry in MANIFEST["entries"]:
            definition = load(entry)
            assert definition.workflow_id == entry["workflow_id"]
            assert definition.signature() == entry["signature"]
            assert len(definition.nodes) == entry["node_count"]
            assert len(definition.edges) == entry["edge_count"]

    def test_five_builtin_templates_are_covered(self) -> None:
        by_name = {load(entry).name for entry in MANIFEST["entries"] if entry["kind"] == "builtin"}
        assert by_name == {template["name"] for template in BUILTIN_TEMPLATES}

    def test_standard_chain_is_chain_stage_only(self) -> None:
        entry = next(item for item in MANIFEST["entries"] if item["kind"] == "standard_chain")
        definition = load(entry)
        assert definition.name == STANDARD_CHAIN_TEMPLATE_NAME
        assert [node.node_id for node in definition.nodes] == ["perceive", "assess", "plan", "execute", "feedback"]
        assert {node.type for node in definition.nodes} == {"chain_stage"}

    def test_report_review_keeps_three_choices_and_no_republish(self) -> None:
        entry = next(item for item in MANIFEST["entries"] if item["kind"] == "report_review")
        definition = load(entry)
        assert definition.name == REPORT_REVIEW_TEMPLATE["name"]
        review = definition.node("review")
        assert review is not None
        assert review.config["options"] == ["approve", "adjust", "reject"]
        assert all(node.type != "warning_publish" for node in definition.nodes)

    def test_all_fixtures_predate_start_end(self) -> None:
        """旧定义没有 Start 也必须合法：运行输入回退 JSON，这是兼容性的底线。"""
        for entry in MANIFEST["entries"]:
            definition = load(entry)
            types = {node.type for node in definition.nodes}
            assert "start" not in types
            assert "end" not in types

    def test_explicit_legacy_fixture_has_no_start(self) -> None:
        entry = next(item for item in MANIFEST["entries"] if item["kind"] == "legacy_no_start")
        definition = load(entry)
        assert definition.node("start") is None
        assert definition.roots() == ["fetch_rain"]
