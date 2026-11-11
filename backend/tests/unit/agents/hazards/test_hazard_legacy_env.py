"""旧名兜底必须**看得见**：命中 AEGIS_EXT_* 时每个变量记一条 deprecation 日志。

【为什么要有这一组】
改名（8d65f40）之后，"等现网迁移完再删旧名"的前提是**能观察到还有谁在用旧名**。
`_env` 那条路有日志，但路径类（CHECKPOINT_DB/FEEDBACK_MEMORY）与数据 API
（DATA_API_URL/TOKEN）四处兜底此前是静默的——用旧名的部署一声不吭，
"观察一个发布周期"就观察了个寂寞。这一组钉住：四处都 warn、每变量一次、新名优先。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import aegis.agents.hazards.config as hazard_config
import aegis.agents.hazards.risk_agent.evidence_pull as pull_module

CONFIG_LOGGER = "aegis.agents.hazards.config"
PULL_LOGGER = "aegis.agents.hazards.evidence_pull"


@pytest.fixture(autouse=True)
def _clean_warned():
    hazard_config._warned.clear()
    pull_module._warned.clear()
    yield
    hazard_config._warned.clear()
    pull_module._warned.clear()


def _warnings(caplog: pytest.LogCaptureFixture, logger: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING and "已改名" in r.getMessage()]


# ============================================================
# 路径类变量（_runtime_path）
# ============================================================


def test_路径变量命中旧名记一次日志(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    legacy = tmp_path / "legacy.sqlite"
    monkeypatch.delenv("AEGIS_HAZARD_CHECKPOINT_DB", raising=False)
    monkeypatch.setenv("AEGIS_EXT_CHECKPOINT_DB", str(legacy))
    with caplog.at_level(logging.WARNING):
        first = hazard_config._runtime_path("AEGIS_HAZARD_CHECKPOINT_DB", "manager_checkpoints.sqlite")
        second = hazard_config._runtime_path("AEGIS_HAZARD_CHECKPOINT_DB", "manager_checkpoints.sqlite")
    assert first == legacy.resolve(), "旧名仍可读（兼容行为不变）"
    assert second == first
    records = _warnings(caplog, CONFIG_LOGGER)
    assert len(records) == 1, "每个变量只记一次，不刷屏"
    message = records[0].getMessage()
    assert "AEGIS_EXT_CHECKPOINT_DB" in message and "AEGIS_HAZARD_CHECKPOINT_DB" in message


def test_路径变量新名优先且不记日志(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("AEGIS_HAZARD_FEEDBACK_MEMORY", str(tmp_path / "new.json"))
    monkeypatch.setenv("AEGIS_EXT_FEEDBACK_MEMORY", str(tmp_path / "old.json"))
    with caplog.at_level(logging.WARNING):
        path = hazard_config._runtime_path("AEGIS_HAZARD_FEEDBACK_MEMORY", "feedback_memory.json")
    assert path == (tmp_path / "new.json").resolve()
    assert _warnings(caplog, CONFIG_LOGGER) == []


# ============================================================
# 数据 API 变量（evidence_pull）
# ============================================================


def _fake_urlopen(monkeypatch: pytest.MonkeyPatch, seen: list[Any]) -> None:
    def fake(request: Any, timeout: float = 0) -> Any:
        seen.append(request)
        raise OSError("测试不拨号：请求形状已捕获")

    monkeypatch.setattr(pull_module, "urlopen", fake)


def test_数据API命中旧名各记一次且旧值真被使用(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("AEGIS_HAZARD_DATA_API_URL", raising=False)
    monkeypatch.delenv("AEGIS_HAZARD_DATA_API_TOKEN", raising=False)
    monkeypatch.setenv("AEGIS_EXT_DATA_API_URL", "http://legacy.test")
    monkeypatch.setenv("AEGIS_EXT_DATA_API_TOKEN", "tok-legacy")
    seen: list[Any] = []
    _fake_urlopen(monkeypatch, seen)
    with caplog.at_level(logging.WARNING):
        assert pull_module.pull_evidence("540121", "1h") is None  # 网络失败 → None（口径不变）
        assert pull_module.pull_evidence("540121", "1h") is None
    assert len(seen) == 2
    assert seen[0].full_url.startswith("http://legacy.test/"), "旧名配置必须真的生效，不是只打日志"
    assert seen[0].get_header("Authorization") == "Bearer tok-legacy"
    records = _warnings(caplog, PULL_LOGGER)
    assert len(records) == 2, "URL 与 TOKEN 各一条，第二次调用不重复"
    joined = " ".join(r.getMessage() for r in records)
    assert "AEGIS_EXT_DATA_API_URL" in joined and "AEGIS_EXT_DATA_API_TOKEN" in joined


def test_数据API新名优先且不记日志(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("AEGIS_HAZARD_DATA_API_URL", "http://new.test")
    monkeypatch.setenv("AEGIS_EXT_DATA_API_URL", "http://legacy.test")
    seen: list[Any] = []
    _fake_urlopen(monkeypatch, seen)
    with caplog.at_level(logging.WARNING):
        pull_module.pull_evidence("540121", "1h")
    assert seen[0].full_url.startswith("http://new.test/")
    assert _warnings(caplog, PULL_LOGGER) == []
