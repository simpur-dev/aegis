"""内置案例库的"性质"必须对外可辨：预案模板不能冒充真实事件复盘。

为什么单独钉这件事：召回命中会进 `ChainResult` 的依据链与 `/api/v1/cases/recall`，
读者只能看到 title / actions / confidence / estimated_delay_hours。这些数字全部是编制判断值，
一旦出处不在同一份切片里，接口输出就被读成"从 16 起真实灾害事件里统计出来的经验"——
这正是最难被发现、也最难收回的夸大。所以三处都要有证据：
1. 数据文件本身逐条带 `source_note`（不是只有一份总说明）；
2. `plan_brief()` 把出处带进命中切片（漏一条就等于抹掉出处）；
3. `/api/v1/integrations` 的 knowledge 行说明整个库是什么性质。
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.messages import parse_iso
from aegis.integrations import build_knowledge
from aegis.knowledge.cases import (
    DATASET_PROVENANCE,
    DEFAULT_CASES_PATH,
    HazardCase,
    load_builtin_cases,
)
from aegis.knowledge.provider import CaseMatch

# 一条出处至少要说明"从哪来"，"NexusMind"（骨架移植）与"平台"（自编降级兜底）是两种真实来源
_ATTRIBUTION_TOKENS = ("NexusMind", "平台")
_MIN_NOTE_CHARS = 20


@pytest.fixture(scope="module")
def builtin_cases() -> list[HazardCase]:
    return load_builtin_cases()


class TestDatasetAttribution:
    def test_json_and_model_agree_on_size(self, builtin_cases: list[HazardCase]) -> None:
        """条数取自数据文件本身，不写死：文档里引用这个数字时才有可追溯的来源。"""
        raw = json.loads(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        assert len(builtin_cases) == len(raw)
        assert len(builtin_cases) >= 12

    def test_every_case_states_where_it_came_from(self, builtin_cases: list[HazardCase]) -> None:
        missing = [c.case_id for c in builtin_cases if len(c.source_note.strip()) < _MIN_NOTE_CHARS]
        assert not missing, f"未标注出处的案例: {missing}"

    def test_notes_name_a_real_source_not_a_placeholder(self, builtin_cases: list[HazardCase]) -> None:
        unattributed = [c.case_id for c in builtin_cases if not any(token in c.source_note for token in _ATTRIBUTION_TOKENS)]
        assert not unattributed, f"出处未指向可核对来源的案例: {unattributed}"

    def test_dataset_note_declares_itself_as_authored_playbooks(self) -> None:
        """这句声明是唯一能阻止"模板被当成复盘"的话，改文案时不许把它删软。"""
        assert "不是真实" in DATASET_PROVENANCE
        assert "编制判断值" in DATASET_PROVENANCE

    def test_valid_at_never_claims_an_event_in_the_future(self, builtin_cases: list[HazardCase]) -> None:
        """observed_at 是"经验生效时刻"：它落在未来就等于宣称我们预判了尚未发生的灾害。"""
        now = datetime.now(UTC)
        ahead = [c.case_id for c in builtin_cases if parse_iso(c.observed_at) > now]
        assert not ahead, f"生效时刻在未来: {ahead}"

    def test_confidences_are_not_an_uniform_placeholder_value(self, builtin_cases: list[HazardCase]) -> None:
        """全部相同说明 confidence 是装饰值；它参与预案权衡排序，必须有区分度。"""
        assert len({c.confidence for c in builtin_cases}) >= 8

    def test_every_case_id_is_unique(self, builtin_cases: list[HazardCase]) -> None:
        ids = [c.case_id for c in builtin_cases]
        assert len(set(ids)) == len(ids)


class TestProvenanceTravelsWithTheBrief:
    def test_plan_brief_keeps_the_note_for_every_case(self, builtin_cases: list[HazardCase]) -> None:
        stripped = [c.case_id for c in builtin_cases if c.plan_brief().get("source_note") != c.source_note]
        assert not stripped, f"命中切片丢了出处的案例: {stripped}"

    def test_memory_recall_brief_carries_provenance(self, builtin_cases: list[HazardCase]) -> None:
        case = builtin_cases[0]
        brief = CaseMatch(
            case_id=case.case_id,
            score=1.5,
            source="in_memory",
            case=case,
        ).planning_brief()
        assert brief["source_note"] == case.source_note

    def test_graph_only_hit_invents_no_provenance(self) -> None:
        """图谱侧只回节点摘要时连出处都不许编——没有案例本体就没有可声明的来源。"""
        brief = CaseMatch(case_id="case_graph_only", score=0.9, source="graphiti").planning_brief()
        assert "source_note" not in brief
        assert "confidence" not in brief

    def test_empty_note_stays_empty_instead_of_getting_a_placeholder(self, builtin_cases: list[HazardCase]) -> None:
        """出处缺失时切片要如实留空：填一句"内置案例"就是把缺口洗成看起来有出处的条目。"""
        bare = builtin_cases[0].model_copy(update={"source_note": ""})
        assert bare.plan_brief()["source_note"] == ""


class TestAssemblyDisclosesDatasetNature:
    def test_knowledge_row_reports_provenance_and_count(self) -> None:
        provider, state = build_knowledge(Settings(env="test", bus_backend="memory"))
        assert provider is not None
        assert state.name == "knowledge"
        assert state.detail["dataset"] == DATASET_PROVENANCE
        assert state.detail["fallback_cases"] == len(load_builtin_cases())

    def test_container_integration_status_exposes_it(self) -> None:
        container = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False))
        row = {state.name: state for state in container.integration_status()}["knowledge"]
        assert "预案模板" in row.detail["dataset"]

    async def test_http_surfaces_provenance_on_both_ends(self, settings: Settings) -> None:
        """状态接口与召回接口都要能答"这条经验是什么性质"——只有一处披露等于没披露。"""
        container = create_container(settings)
        await container.start()
        try:
            app = create_app(container.settings, container=container)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                integrations = (await client.get("/api/v1/integrations")).json()
                knowledge = {row["name"]: row for row in integrations["items"]}["knowledge"]
                assert knowledge["detail"]["dataset"] == DATASET_PROVENANCE

                recall = (await client.get("/api/v1/cases/recall", params={"q": "泥位超阈值 沟道转移"})).json()
                assert recall["count"] >= 1
                for item in recall["items"]:
                    assert item["source_note"]
        finally:
            await container.shutdown()


def test_dataset_file_is_bundled_with_the_package() -> None:
    """库随包发布（数据目录就在模块旁边）：装完依赖就能召回，不假设现场先挂了卷或先跑过迁移。"""
    assert DEFAULT_CASES_PATH.is_file()
    assert DEFAULT_CASES_PATH.parts[-4:] == ("aegis", "knowledge", "data", "hazard_cases.json")


def test_provenance_text_is_short_enough_for_a_status_row() -> None:
    """状态行会进前端表格与日志：说明要够用，但不能长到被截断成半句话。"""
    assert 40 <= len(DATASET_PROVENANCE) <= 200
    assert "\n" not in DATASET_PROVENANCE


def test_bundled_dataset_is_not_swallowed_by_gitignore() -> None:
    """`data/` 这种通配规则会连源码目录里的数据一起吞掉：本地在盘上照样全绿，新克隆却没有兜底案例。

    仓库根在 backend 上一级；忽略规则只有 git 自己能算得准（手工匹配会漏掉"先排除目录再反排除文件"
    这类顺序语义），所以非 git 检出（打包安装态）如实跳过，而不是假装通过。
    """
    repo_root = Path(__file__).resolve().parents[3]
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(DEFAULT_CASES_PATH)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if probe.returncode not in (0, 1):
        pytest.skip(f"无法判定 git 忽略规则: {probe.stderr.strip()}")
    assert probe.returncode == 1, "内置案例库被 .gitignore 匹配：必须显式反排除，否则仓库里没有降级兜底数据"
