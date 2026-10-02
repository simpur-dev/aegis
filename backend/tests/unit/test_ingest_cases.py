"""批量回填命令 `scripts/ingest_cases.py` 的行为与边界。

这个命令存在的理由只有一个：让"16 条内置预案真的进了图谱"成为一句可复核的话。
所以这里的断言全部盯着两件最容易糊过去的事——
1. **落点**：降级到兜底腿、契约外异常中止，都必须从退出码和逐条结果里读出来，
   不能被"total=16"这一行盖掉；
2. **回读**：写入返回成功只说明驱动收了字节。查不回来的那批要被单独点名（退出码 4），
   否则新部署的图谱"灌了 16 条却一条都召回不到"看起来跟成功一样。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from scripts.ingest_cases import (
    EXIT_DEGRADED,
    EXIT_INVALID,
    EXIT_NOT_RECALLABLE,
    EXIT_OK,
    EXIT_REFUSED,
    exit_code,
    ingest,
    main,
    select_cases,
    summarize,
    verify,
)

from aegis import integrations
from aegis.integrations import IntegrationState
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.memory_store import InMemoryKnowledgeProvider
from aegis.knowledge.provider import CaseMatch, LearnOutcome, RecallSource

ALL_IDS = [case.case_id for case in load_builtin_cases()]
FIRST = ALL_IDS[0]
SECOND = ALL_IDS[1]
THIRD = ALL_IDS[2]


def outcome(case_id: str, *, driver: str = "graphiti", degraded: bool = False) -> LearnOutcome:
    return LearnOutcome(case_id=case_id, driver=driver, degraded=degraded, reason="learn_failed" if degraded else "")


class BatchStub:
    """图谱侧替身：`learn_case` 报落点、`recall` 定查不查得回来、`close` 数次数。

    `learn` 故意炸掉：批量路径必须走报告事实的 `learn_case`，走 `learn` 就等于把降级吞了。
    """

    driver: ClassVar[RecallSource] = "graphiti"

    def __init__(
        self,
        *,
        degrade: tuple[str, ...] = (),
        boom_at: str | None = None,
        recallable: bool = True,
    ) -> None:
        self._degrade = set(degrade)
        self._boom_at = boom_at
        self._recallable = recallable
        self.written: list[str] = []
        self.closed = 0

    async def learn(self, case: HazardCase) -> None:
        raise AssertionError(f"批量回填不该调用 learn()（case={case.case_id}）：它不报告落点")

    async def learn_case(self, case: HazardCase) -> LearnOutcome:
        if case.case_id == self._boom_at:
            raise RuntimeError("LLM 鉴权失败")
        self.written.append(case.case_id)
        if case.case_id in self._degrade:
            return LearnOutcome(
                case_id=case.case_id,
                driver="in_memory",
                degraded=True,
                reason="learn_failed",
                detail="Neo4j 不可达",
            )
        return LearnOutcome(case_id=case.case_id, driver=self.driver)

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        del hazard_type, region_code, limit, budget_ms
        if not self._recallable:
            return []
        by_title = {case.title: case for case in load_builtin_cases()}
        found = by_title.get(query)
        return [] if found is None else [CaseMatch(case_id=found.case_id, score=1.0, source="graphiti")]

    async def close(self) -> None:
        self.closed += 1


def patch_knowledge(monkeypatch: pytest.MonkeyPatch, provider: Any, *, driver: str = "graphiti") -> None:
    state = IntegrationState(name="knowledge", enabled=True, driver=driver)
    monkeypatch.setattr(integrations, "build_knowledge", lambda *_args, **_kwargs: (provider, state))


def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any], str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out[captured.out.index("{") :]), captured.err


class TestSelectCases:
    def test_不给条件就是整库内置模板(self) -> None:
        cases = select_cases(load_builtin_cases(), [])
        assert [case.case_id for case in cases] == ALL_IDS
        assert len(cases) >= 10  # 考核指标：预案库够编排用

    def test_挑子集按请求顺序(self) -> None:
        cases = select_cases(load_builtin_cases(), [SECOND, FIRST])
        assert [case.case_id for case in cases] == [SECOND, FIRST]

    def test_重复的id只灌一次(self) -> None:
        """同一批里重复写两次不报错但会虚报条数——运维看到的 17 条其实是 16 条。"""
        cases = select_cases(load_builtin_cases(), [FIRST, FIRST, SECOND])
        assert [case.case_id for case in cases] == [FIRST, SECOND]

    def test_拼错的id是错误而不是少一条(self) -> None:
        with pytest.raises(ValueError, match="未知 case_id") as exc:
            select_cases(load_builtin_cases(), ["case_df_gully_evacuatee"])
        assert "case_df_gully_evacuatee" in str(exc.value)


class TestIngest:
    async def test_逐条落到报告落点的那个入口(self) -> None:
        stub = BatchStub()
        outcomes, aborted = await ingest(stub, load_builtin_cases()[:3])
        assert aborted == ""
        assert stub.written == [FIRST, SECOND, THIRD]
        assert [item.case_id for item in outcomes] == [FIRST, SECOND, THIRD]
        assert all(item.driver == "graphiti" and not item.degraded for item in outcomes)

    async def test_降级是数据不是异常(self) -> None:
        stub = BatchStub(degrade=(SECOND,))
        outcomes, aborted = await ingest(stub, load_builtin_cases()[:3])
        assert aborted == ""
        assert [item.degraded for item in outcomes] == [False, True, False]

    async def test_契约外异常整批中止但保留已落地的前缀(self) -> None:
        """异常替图谱编一个落点，比中止更糟：交回真实前缀，断在哪一条看得见。"""
        stub = BatchStub(boom_at=THIRD)
        outcomes, aborted = await ingest(stub, load_builtin_cases()[:4])
        assert stub.written == [FIRST, SECOND]
        assert len(outcomes) == 2
        assert aborted.startswith("RuntimeError")
        assert "LLM 鉴权失败" in aborted


class TestVerify:
    async def test_查得回来的报告命中的腿(self) -> None:
        checked = await verify(BatchStub(), load_builtin_cases()[:2])
        assert checked == {FIRST: ["graphiti"], SECOND: ["graphiti"]}

    async def test_查不回来是空列表而不是缺键(self) -> None:
        """缺键会让"没查"和"查不到"读成同一件事。"""
        checked = await verify(BatchStub(recallable=False), load_builtin_cases()[:2])
        assert checked == {FIRST: [], SECOND: []}


class TestSummarize:
    def test_landed不把降级算进去(self) -> None:
        summary = summarize([outcome(FIRST), outcome(SECOND, driver="in_memory", degraded=True)], aborted="")
        assert (summary["total"], summary["landed"], summary["degraded"]) == (2, 1, 1)
        assert summary["by_driver"] == {"graphiti": 1, "in_memory": 1}

    def test_回读账按缺项与已核两栏(self) -> None:
        summary = summarize([outcome(FIRST)], aborted="", recall={FIRST: ["graphiti"], SECOND: []})
        assert (summary["recall_checked"], summary["recall_missing"]) == (1, [SECOND])

    def test_没回读时recall_checked是None(self) -> None:
        """`--no-verify` 不是"全部查不到"，把两件事糊在一起就会误报故障。"""
        assert summarize([outcome(FIRST)], aborted="")["recall_checked"] is None

    def test_中止原因原样带上(self) -> None:
        assert summarize([], aborted="RuntimeError: 鉴权失败")["aborted"] == "RuntimeError: 鉴权失败"


class TestExitCode:
    def test_全部落在主腿是零(self) -> None:
        assert exit_code(summarize([outcome(FIRST)], aborted="", recall={FIRST: ["graphiti"]})) == EXIT_OK

    def test_中止优先于其他一切(self) -> None:
        summary = summarize([outcome(FIRST), outcome(SECOND, driver="in_memory", degraded=True)], aborted="RuntimeError: x")
        assert exit_code(summary) == EXIT_INVALID

    def test_有降级报降级(self) -> None:
        assert exit_code(summarize([outcome(FIRST), outcome(SECOND, driver="in_memory", degraded=True)], aborted="")) == EXIT_DEGRADED

    def test_写得进去却召回不到单独一档(self) -> None:
        assert exit_code(summarize([outcome(FIRST)], aborted="", recall={FIRST: []})) == EXIT_NOT_RECALLABLE

    def test_降级比召回不到先报(self) -> None:
        """同批两个问题时报更靠近写路径的那个：先修好写入，回读问题才有意义。"""
        summary = summarize([outcome(FIRST, driver="in_memory", degraded=True)], aborted="", recall={FIRST: []})
        assert exit_code(summary) == EXIT_DEGRADED


class TestCli:
    def test_dry_run不碰提供者(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        stub = BatchStub()
        patch_knowledge(monkeypatch, stub)
        code, body, _err = run_cli(monkeypatch, capsys, ["--dry-run"])
        assert code == EXIT_OK
        assert (body["status"], body["rows"], body["source"]) == ("validated", len(ALL_IDS), "builtin")
        assert stub.written == [] and stub.closed == 0

    def test_内存形态默认拒绝(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        memory = InMemoryKnowledgeProvider(cases=[])
        patch_knowledge(monkeypatch, memory, driver="in_memory")
        code, body, err = run_cli(monkeypatch, capsys, [])
        assert code == EXIT_REFUSED
        assert body["status"] == "refused" and "只活到本进程结束" in body["reason"]
        assert "退出码 2" in err and "只活到本进程结束" in err  # 拒绝的原因要当场看得见，不只躺在 JSON 里
        assert len(memory) == 0

    def test_显式允许内存时结果写明durable假(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        memory = InMemoryKnowledgeProvider(cases=[])
        patch_knowledge(monkeypatch, memory, driver="in_memory")
        code, body, err = run_cli(monkeypatch, capsys, ["--allow-memory", "--case-id", FIRST, "--case-id", SECOND])
        assert code == EXIT_OK
        assert (body["durable"], body["by_driver"]) == (False, {"in_memory": 2})
        # 真走一遍内存打分器：自己的标题要能把自己查回来，否则"回读"这句话说的是空气
        assert body["recall_checked"] == 2
        assert body["recalled_from"] == {FIRST: ["in_memory"], SECOND: ["in_memory"]}
        assert "只活在本进程里" in err

    def test_图谱形态整库回填并关停连接(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        stub = BatchStub()
        patch_knowledge(monkeypatch, stub)
        code, body, _err = run_cli(monkeypatch, capsys, [])
        assert code == EXIT_OK
        assert (body["status"], body["target_driver"], body["durable"]) == ("ingested", "graphiti", True)
        assert (body["total"], body["landed"], body["degraded"]) == (len(ALL_IDS), len(ALL_IDS), 0)
        assert body["recall_checked"] == len(ALL_IDS)
        assert stub.closed == 1

    def test_有降级时退出码不是零(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        patch_knowledge(monkeypatch, BatchStub(degrade=(SECOND,)))
        code, body, err = run_cli(monkeypatch, capsys, ["--case-id", FIRST, "--case-id", SECOND])
        assert code == EXIT_DEGRADED
        assert body["degraded"] == 1
        # 目标是图谱不等于落在图谱：有一条掉进内存时 durable 必须翻假
        assert body["durable"] is False
        assert [item["reason"] for item in body["outcomes"]] == ["", "learn_failed"]
        assert f"退出码 {EXIT_DEGRADED}" in err and "1 条没进图谱" in err

    def test_契约外异常中止时仍报已落地条数(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        stub = BatchStub(boom_at=THIRD)
        patch_knowledge(monkeypatch, stub)
        code, body, _err = run_cli(monkeypatch, capsys, [])
        assert code == EXIT_INVALID
        assert (body["status"], body["total"], body["aborted"]) == ("aborted", 2, "RuntimeError: LLM 鉴权失败")
        assert stub.closed == 1  # 中止也要把连接关掉，否则留下"命令退了、驱动还开着"

    def test_召回不到单独退出码(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        patch_knowledge(monkeypatch, BatchStub(recallable=False))
        code, body, _err = run_cli(monkeypatch, capsys, ["--case-id", FIRST])
        assert code == EXIT_NOT_RECALLABLE
        assert body["recall_missing"] == [FIRST]

    def test_跳过回读时不给回读结论(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        patch_knowledge(monkeypatch, BatchStub())
        code, body, _err = run_cli(monkeypatch, capsys, ["--no-verify", "--case-id", FIRST])
        assert code == EXIT_OK
        assert (body["recall_checked"], body["recalled_from"]) == (None, {})

    def test_拼错的case_id整批不导入(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        stub = BatchStub()
        patch_knowledge(monkeypatch, stub)
        code, body, _err = run_cli(monkeypatch, capsys, ["--case-id", "case_不存在"])
        assert code == EXIT_INVALID
        assert body["status"] == "invalid" and "未知 case_id" in body["reason"]
        assert stub.written == []

    def test_外部json数组走同一条路(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        source = tmp_path / "cases.json"
        rows = [case.model_dump(mode="json") for case in load_builtin_cases()[:2]]
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        stub = BatchStub()
        patch_knowledge(monkeypatch, stub)
        code, body, _err = run_cli(monkeypatch, capsys, ["--source", str(source)])
        assert code == EXIT_OK
        assert body["total"] == 2
        assert stub.written == [FIRST, SECOND]

    def test_坏文件当作输入问题报告而不是堆栈(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        source = tmp_path / "cases.json"
        source.write_text("{不是 JSON", encoding="utf-8")
        code, body, err = run_cli(monkeypatch, capsys, ["--source", str(source)])
        assert code == EXIT_INVALID
        assert body["status"] == "invalid"
        assert "案例库" in body["reason"] and "Traceback" not in err

    def test_知识层未装配时拒绝而不是抛NoneType(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        patch_knowledge(monkeypatch, None)
        code, body, _err = run_cli(monkeypatch, capsys, [])
        assert code == EXIT_REFUSED
        assert body["status"] == "refused"


def test_命令在装配面上只走build_knowledge() -> None:
    """脚本里不许自己 new 提供者：它造出的链和生产用的链不是同一条时，"回填成功"只对本进程成立。"""
    from scripts import ingest_cases

    source = Path(ingest_cases.__file__).read_text(encoding="utf-8")
    assert "build_knowledge(" in source
    for forbidden in ("GraphitiKnowledgeProvider(", "InMemoryKnowledgeProvider("):
        assert forbidden not in source
