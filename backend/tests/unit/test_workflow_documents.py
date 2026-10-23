"""工作流文档模型：Start/End 约束、版式边界、哈希稳定性与旧定义兼容。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis.workflow.documents import (
    DOCUMENT_SCHEMA_VERSION,
    MAX_DOCUMENT_BYTES,
    MAX_NODE_CONFIG_BYTES,
    MAX_STICKY_NOTES,
    StartVariable,
    StickyNote,
    Viewport,
    WorkflowDocument,
    WorkflowDraft,
    WorkflowLayout,
    definition_from_document,
    document_from_definition,
    document_hash,
)
from aegis.workflow.model import WorkflowDef

LINEAGE = "wf_" + "a" * 12
WF_ID = "wf_" + "b" * 12


def node(node_id: str, node_type: str = "notify", config: dict | None = None) -> dict:
    return {"node_id": node_id, "type": node_type, "config": config or {}}


def edge(source: str, target: str, condition: str = "") -> dict:
    return {"source": source, "target": target, "condition": condition}


def document(nodes: list[dict], edges: list[dict] | None = None, **overrides: object) -> WorkflowDocument:
    payload = {
        "lineage_id": LINEAGE,
        "name": "泥石流处置",
        "nodes": nodes,
        "edges": edges or [],
        **overrides,
    }
    return WorkflowDocument(**payload)


class TestStartEnd:
    def test_起点终点齐全时校验通过(self) -> None:
        doc = document(
            [node("start", "start", {"variables": [{"name": "rain", "type": "number"}]}), node("end", "end")],
            [edge("start", "end")],
        )
        assert [item.name for item in doc.start_variables()] == ["rain"]

    def test_起点至多一个(self) -> None:
        with pytest.raises(ValidationError, match="start 节点至多一个"):
            document([node("s1", "start"), node("s2", "start")])

    def test_起点不允许入边(self) -> None:
        with pytest.raises(ValidationError, match="不允许有入边"):
            document([node("a"), node("start", "start")], [edge("a", "start")])

    def test_有起点时它必须是唯一根(self) -> None:
        with pytest.raises(ValidationError, match="唯一根"):
            document([node("start", "start"), node("island")])

    def test_终点不允许出边(self) -> None:
        with pytest.raises(ValidationError, match="不允许有出边"):
            document([node("end", "end"), node("a")], [edge("end", "a")])

    def test_终点至多一个(self) -> None:
        with pytest.raises(ValidationError, match="end 节点至多一个"):
            document([node("e1", "end"), node("e2", "end")])

    def test_旧定义没有起点仍然合法(self) -> None:
        """2026-10 之前保存的定义没有 Start；读取时不能在模型层判死。"""
        doc = document([node("a"), node("b")], [edge("a", "b")])
        assert all(item.type != "start" for item in doc.nodes)

    def test_起点变量名重复被拒(self) -> None:
        with pytest.raises(ValidationError, match="变量名重复"):
            document([node("start", "start", {"variables": [{"name": "x"}, {"name": "x"}]})])

    @pytest.mark.parametrize(
        "variable, reason",
        [
            ({"name": "x", "type": "string", "default": 1}, "必须是字符串"),
            ({"name": "x", "type": "number", "default": "不是数字"}, "必须是数字"),
            ({"name": "x", "type": "boolean", "default": 1}, "必须是布尔值"),
            ({"name": "x", "type": "json", "default": object()}, "不是合法 JSON"),
            ({"name": "__proto__"}, "不允许"),
            ({"name": "constructor"}, "不允许"),
        ],
    )
    def test_起点变量默认值与名字被校验(self, variable: dict, reason: str) -> None:
        with pytest.raises(ValidationError, match=reason):
            document([node("start", "start", {"variables": [variable]})])

    def test_起点变量必须是数组(self) -> None:
        with pytest.raises(ValidationError, match="必须是数组"):
            document([node("start", "start", {"variables": "rain"})])

    def test_变量数超过上限(self) -> None:
        variables = [{"name": f"v{index}"} for index in range(33)]
        with pytest.raises(ValidationError, match="不得超过 32"):
            document([node("start", "start", {"variables": variables})])

    def test_StartVariable_拒绝未知字段(self) -> None:
        with pytest.raises(ValidationError):
            StartVariable.model_validate({"name": "x", "type": "string", "extra": 1})


class TestLayout:
    def test_未知节点的坐标被剪枝而视口与备注保留(self) -> None:
        doc = document(
            [node("a")],
            layout={
                "positions": {"a": {"x": 10, "y": 20}, "ghost": {"x": 1, "y": 2}},
                "viewport": {"x": 5, "y": 6, "zoom": 1.5},
                "notes": [{"note_id": "note1", "x": 0, "y": 0, "text": "现场记录"}],
            },
        )
        assert set(doc.layout.positions) == {"a"}
        assert doc.layout.viewport is not None and doc.layout.viewport.zoom == 1.5
        assert doc.layout.notes[0].text == "现场记录"

    def test_传入布局对象时同样剪枝(self) -> None:
        layout = WorkflowLayout(
            positions={"a": {"x": 1, "y": 2}, "gone": {"x": 3, "y": 4}},
            viewport=Viewport(x=0, y=0, zoom=2),
        )
        doc = document([node("a")], layout=layout)
        assert set(doc.layout.positions) == {"a"}
        assert doc.layout.viewport == Viewport(x=0, y=0, zoom=2)

    @pytest.mark.parametrize("zoom", [0.0, 0.05, 4.5])
    def test_视口缩放边界(self, zoom: float) -> None:
        with pytest.raises(ValidationError):
            Viewport(x=0, y=0, zoom=zoom)

    def test_备注数量上限(self) -> None:
        notes = [{"note_id": f"n{index}", "x": index, "y": 0} for index in range(MAX_STICKY_NOTES + 1)]
        with pytest.raises(ValidationError):
            WorkflowLayout(notes=notes)

    def test_备注_ID_重复被拒(self) -> None:
        with pytest.raises(ValidationError, match="note_id 重复"):
            WorkflowLayout(notes=[{"note_id": "n1", "x": 0, "y": 0}, {"note_id": "n1", "x": 1, "y": 1}])

    @pytest.mark.parametrize("field, value", [("x", 1_000_001), ("y", -1_000_001), ("width", 119), ("height", 801)])
    def test_备注坐标与尺寸边界(self, field: str, value: float) -> None:
        payload = {"note_id": "n1", "x": 0, "y": 0, field: value}
        with pytest.raises(ValidationError):
            StickyNote(**payload)

    def test_颜色只认四个_token(self) -> None:
        with pytest.raises(ValidationError):
            StickyNote(note_id="n1", x=0, y=0, color="red")

    @pytest.mark.parametrize("key", ["", "x" * 33])
    def test_坐标键必须是合法节点_ID_形状(self, key: str) -> None:
        with pytest.raises(ValidationError, match="node_id 不合法"):
            WorkflowLayout(positions={key: {"x": 0, "y": 0}})


class TestGraphValidation:
    def test_节点_ID_重复被拒(self) -> None:
        with pytest.raises(ValidationError, match="节点 ID 重复"):
            document([node("a"), node("a")])

    @pytest.mark.parametrize(
        ("bad_edge", "reason"),
        [({"source": "ghost", "target": "a"}, "源节点不存在"), ({"source": "a", "target": "ghost"}, "目标节点不存在")],
    )
    def test_悬空边被拒(self, bad_edge: dict, reason: str) -> None:
        with pytest.raises(ValidationError, match=reason):
            document([node("a")], [bad_edge])

    def test_自环被拒(self) -> None:
        with pytest.raises(ValidationError, match="不允许自环"):
            document([node("a")], [edge("a", "a")])

    def test_有环被拒(self) -> None:
        with pytest.raises(ValidationError, match="存在环"):
            document([node("a"), node("b")], [edge("a", "b"), edge("b", "a")])


class TestDocumentLimits:
    def test_schema_version_未知时给出专用错误类型(self) -> None:
        with pytest.raises(ValidationError) as caught:
            document([node("a")], schema_version=DOCUMENT_SCHEMA_VERSION + 1)
        assert caught.value.errors()[0]["type"] == "invalid_schema_version"

    def test_节点数超过上限(self) -> None:
        with pytest.raises(ValidationError):
            document([node(f"n{index}") for index in range(201)])

    def test_单节点配置超过_64KB(self) -> None:
        with pytest.raises(ValidationError, match="config 超过"):
            document([node("a", config={"blob": "x" * (MAX_NODE_CONFIG_BYTES + 1)})])

    def test_整份文档超过_2MB(self) -> None:
        nodes = [node(f"n{index}", config={"blob": "x" * 60_000}) for index in range(40)]
        with pytest.raises(ValidationError, match="文档序列化后超过"):
            document(nodes)

    def test_文档是只读快照(self) -> None:
        doc = document([node("a")])
        with pytest.raises(ValidationError):
            doc.name = "改个名"  # type: ignore[misc]


class TestCanonicalHash:
    def test_键序不影响哈希(self) -> None:
        first = document([node("a", config={"a": 1, "b": {"z": 2, "y": 3}})])
        second = document([node("a", config={"b": {"y": 3, "z": 2}, "a": 1})])
        assert document_hash(first) == document_hash(second)

    def test_名称说明版式都计入哈希(self) -> None:
        base = document([node("a")])
        assert document_hash(base) != document_hash(base.model_copy(update={"description": "改一版"}))
        moved = base.model_copy(update={"layout": WorkflowLayout(positions={"a": {"x": 100, "y": 0}})})
        assert document_hash(base) != document_hash(moved)

    def test_节点顺序计入哈希(self) -> None:
        first = document([node("a"), node("b")])
        second = document([node("b"), node("a")])
        assert document_hash(first) != document_hash(second)

    def test_哈希前缀固定(self) -> None:
        assert document_hash(document([node("a")])).startswith("sha256:")


class TestDefinitionBridge:
    def _definition(self, *, nodes: list[dict] | None = None) -> WorkflowDef:
        return WorkflowDef(
            workflow_id=WF_ID,
            name="旧流程",
            description="旧定义没有版式",
            nodes=nodes or [{"node_id": "a", "type": "notify", "config": {"text": "广播"}}],
            edges=[],
        )

    def test_旧定义投影成文档时版式为空(self) -> None:
        doc = document_from_definition(self._definition())
        assert doc.layout.positions == {}
        assert doc.lineage_id == WF_ID
        assert doc.nodes[0].node_id == "a"

    def test_版式不进入_WorkflowDef(self) -> None:
        doc = document(
            [node("a")],
            layout={"positions": {"a": {"x": 1, "y": 2}}},
        )
        definition = definition_from_document(doc, workflow_id=WF_ID)
        assert "layout" not in definition.model_dump()
        restored = document_from_definition(definition)
        assert restored.layout.positions == {}
        assert definition.content_hash == document_hash(doc)

    def test_定义与文档往返保持节点连线(self) -> None:
        doc = document([node("a"), node("b")], [edge("a", "b", "hit")])
        definition = definition_from_document(doc, workflow_id=WF_ID, created_at="2026-01-01T00:00:00.000Z")
        assert definition.created_at == "2026-01-01T00:00:00.000Z"
        assert definition.schema_version == DOCUMENT_SCHEMA_VERSION
        assert [item.node_id for item in definition.nodes] == ["a", "b"]
        assert definition.edges[0].condition == "hit"
        assert definition.lineage_id == LINEAGE

    def test_草稿的_document_lineage_必须与草稿一致(self) -> None:
        doc = document([node("a")])
        with pytest.raises(ValidationError, match="不一致"):
            WorkflowDraft(lineage_id="wf_" + "c" * 12, revision=1, document=doc)


def test_单节点配置必须可_json_序列化() -> None:
    with pytest.raises(ValidationError, match="可 JSON 序列化"):
        document([node("a", config={"bad": object()})])


def test_备注不参与图校验() -> None:
    doc = document(
        [node("a")],
        layout={"notes": [{"note_id": "n1", "x": 0, "y": 0, "text": "和节点无关"}]},
    )
    assert doc.nodes[0].node_id == "a"


def test_文档大小上限是_2MB() -> None:
    assert MAX_DOCUMENT_BYTES == 2 * 1024 * 1024
