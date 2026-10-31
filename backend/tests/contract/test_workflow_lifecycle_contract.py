"""工作流文档生命周期的 OpenAPI 契约：路径、请求必填与响应字段。

契约测试守的是"前端按文档写代码能跑"：路径少一条、`base_revision` 从必填变可选、
发布回执少了 `content_hash`，前端都不会在类型检查里发现，而是在真机上表现为 404/422
或"版本列表里没法判断是不是当前版"。
"""

from __future__ import annotations

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container

_settings = Settings(env="test", bus_backend="memory", delivery_mode="mock", simulator_enabled=False)
_container = create_container(_settings)
APP = create_app(_settings, container=_container)


def _schema() -> dict:
    return APP.openapi()


def _operation(path: str, method: str) -> dict:
    document = _schema()
    assert path in document["paths"], f"OpenAPI 缺少路径 {path}"
    assert method in document["paths"][path], f"{path} 缺少 {method.upper()}"
    return document["paths"][path][method]


def _resolve(schema: dict) -> dict:
    if "$ref" not in schema:
        return schema
    name = schema["$ref"].rsplit("/", 1)[-1]
    return _schema()["components"]["schemas"][name]


class TestPaths:
    def test_生命周期端点齐全(self) -> None:
        expected = {
            ("/api/v1/workflow/lineages", "post"),
            ("/api/v1/workflow/lineages/{lineage_id}", "patch"),
            ("/api/v1/workflow/lineages/{lineage_id}/draft", "get"),
            ("/api/v1/workflow/lineages/{lineage_id}/draft", "put"),
            ("/api/v1/workflow/lineages/{lineage_id}/publish", "post"),
            ("/api/v1/workflow/lineages/{lineage_id}/revert-to-draft", "post"),
            ("/api/v1/workflow/lineages/{lineage_id}/versions", "get"),
            ("/api/v1/workflow/versions/{workflow_id}", "get"),
        }
        missing = {(path, method) for path, method in expected if method not in _schema()["paths"].get(path, {})}
        assert not missing, f"草稿/版本端点未进 OpenAPI: {sorted(missing)}"

    def test_旧定义端点仍保留(self) -> None:
        paths = _schema()["paths"]
        for path in (
            "/api/v1/workflow/definitions",
            "/api/v1/workflow/definitions/{workflow_id}",
            "/api/v1/workflow/definitions/{workflow_id}/revise",
        ):
            assert path in paths, f"旧画布客户端还在用的路径被删了: {path}"


class TestRequestContracts:
    def test_草稿写入必须带_base_revision_与文档(self) -> None:
        body = _resolve(
            _operation("/api/v1/workflow/lineages/{lineage_id}/draft", "put")["requestBody"]["content"]["application/json"]["schema"]
        )
        assert {"base_revision", "document"} <= set(body["required"])
        revision = _resolve(body["properties"]["base_revision"])
        assert revision["minimum"] == 0, "首次写入要求 base_revision=0，不能收紧成 ≥1"
        document = _resolve(body["properties"]["document"])
        assert {"lineage_id", "name", "nodes"} <= set(document["required"])
        assert document["properties"]["schema_version"]["default"] == 1, "新文档必须声明当前 schema 版本作为默认值"

    def test_发布必须带_base_revision_且不允许_0(self) -> None:
        body = _resolve(
            _operation("/api/v1/workflow/lineages/{lineage_id}/publish", "post")["requestBody"]["content"]["application/json"]["schema"]
        )
        assert body["required"] == ["base_revision"]
        assert _resolve(body["properties"]["base_revision"])["minimum"] == 1

    def test_恢复版本必须指定_workflow_id(self) -> None:
        body = _resolve(
            _operation("/api/v1/workflow/lineages/{lineage_id}/revert-to-draft", "post")["requestBody"]["content"]["application/json"][
                "schema"
            ]
        )
        assert body["required"] == ["workflow_id"]


class TestResponseContracts:
    def test_发布回执带版本号与内容哈希(self) -> None:
        schema = _operation("/api/v1/workflow/lineages/{lineage_id}/publish", "post")["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        properties = _resolve(schema)["properties"]
        assert {"workflow_id", "lineage_id", "version", "content_hash", "is_current"} <= set(properties)

    def test_版本文档返回完整文档而非计数(self) -> None:
        schema = _operation("/api/v1/workflow/versions/{workflow_id}", "get")["responses"]["200"]["content"]["application/json"]["schema"]
        resolved = _resolve(schema)
        assert {"schema_version", "lineage_id", "nodes", "edges", "layout"} <= set(resolved["properties"])

    def test_草稿响应带_revision_与_base_workflow_id(self) -> None:
        schema = _operation("/api/v1/workflow/lineages/{lineage_id}/draft", "get")["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert {"lineage_id", "base_workflow_id", "revision", "document", "updated_at"} <= set(_resolve(schema)["properties"])

    def test_只读端点声明了_404(self) -> None:
        for path, method in (
            ("/api/v1/workflow/lineages/{lineage_id}/versions", "get"),
            ("/api/v1/workflow/versions/{workflow_id}", "get"),
        ):
            assert "404" in _operation(path, method)["responses"], f"{path} 查不到时的事实没进契约"

    def test_版本文档的_layout_是可选的(self) -> None:
        """旧定义没有版式仍必须可读：缺 layout 时由服务端补空版式，契约不能把旧数据判成非法。"""
        schema = _operation("/api/v1/workflow/versions/{workflow_id}", "get")["responses"]["200"]["content"]["application/json"]["schema"]
        resolved = _resolve(schema)
        assert "layout" not in resolved.get("required", [])


class TestPreviewContracts:
    def test_试运行端点齐全(self) -> None:
        _operation("/api/v1/workflow/previews", "post")
        _operation("/api/v1/workflow/previews/{preview_id}", "get")

    def test_试运行请求可带文档或_lineage(self) -> None:
        body = _resolve(_operation("/api/v1/workflow/previews", "post")["requestBody"]["content"]["application/json"]["schema"])
        assert {"document", "lineage_id", "payload"} <= set(body["properties"])
        assert "required" not in body or body["required"] == [], "document/lineage_id 二选一由模型校验，不能写成两个都必填"

    def test_试运行响应与实例详情同构(self) -> None:
        for schema in (
            _operation("/api/v1/workflow/previews", "post")["responses"]["201"]["content"]["application/json"]["schema"],
            _operation("/api/v1/workflow/previews/{preview_id}", "get")["responses"]["200"]["content"]["application/json"]["schema"],
            _operation("/api/v1/workflow/instances/{instance_id}", "get")["responses"]["200"]["content"]["application/json"]["schema"],
        ):
            properties = _resolve(schema)["properties"]
            assert {
                "instance_id",
                "lineage_id",
                "preview",
                "preview_id",
                "created_at",
                "finished_at",
                "results",
                "nodes",
                "graph",
            } <= set(properties), f"实例/预览详情缺字段: {sorted(properties)}"

    def test_预览不存在时声明_404(self) -> None:
        assert "404" in _operation("/api/v1/workflow/previews/{preview_id}", "get")["responses"]
