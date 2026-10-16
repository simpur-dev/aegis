"""质量表门禁脚本的口径用例：数字必须从真实产出路径算出来，未评测的列不许冒充。

`current_quality()` 的 STU合法率不是手填的分数，而是走 plan 无 LLM 本地剧本【真跑】一批 task_units、
再交给 `ContractRegistry.stu_errors` 校验——所以这里的断言都在验"脚本读的确实是那条腿"。
首过率同样不是填的：它从网关用量台账（每次真调用的 attempt/validation 落笔）算，无台账即 None。
触发F1/定级一致率/文案裁判分需金标集与裁判模型（数据/密钥门），脚本给 None；
None 既不能被判成"达标"，也不能被判成"退步"（把 None 当 0.0 比的那版代码会误判，见基线相关用例）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.quality_gate import _COLUMNS, _load_baseline, _load_usage, current_quality, main, plan_playbook_stu_units

from aegis.contracts import ContractRegistry
from aegis.services.llm.quality_eval import QualityMetrics, regression_gate, structured_first_pass_rate, stu_legality_rate

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE = REPO_ROOT / "backend" / "baselines" / "quality_baseline.json"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


async def test_剧本产出一批真实STU而不是空表() -> None:
    """变异点：脚本若根本没跑到 plan（返回空列表），合法率会退化成"0 条也算 1.0"的空转。"""
    contracts = ContractRegistry()
    units = await plan_playbook_stu_units(contracts)
    assert len(units) >= 3, "本地剧本应产出可数的处置单元，否则这一列没有样本"
    assert all(unit["task_unit_id"] and unit["created_by"] == "plan.gate" for unit in units)
    assert contracts.stu_errors(units) == [], "合法率 1.0 必须建立在有样本且契约零违规之上"
    rate, bad = stu_legality_rate(units, contracts)
    assert (rate, bad) == (1.0, 0)


async def test_合法率会被真违规拉下来() -> None:
    """反向验证这一列不是写死的 1.0：把一条 STU 的 priority 改成契约不接受的值，必须掉数并报出条数。"""
    contracts = ContractRegistry()
    units = await plan_playbook_stu_units(contracts)
    units[0]["priority"] = 99
    rate, bad = stu_legality_rate(units, contracts)
    assert bad == 1 and rate < 1.0


async def test_当前表只有STU合法率有数其余如实留白() -> None:
    table = (await current_quality(ContractRegistry())).as_dict()
    assert table["stu_legality"] is not None
    assert 0.0 <= table["stu_legality"] <= 1.0
    for unmeasured in ("trigger_f1", "level_agreement", "copy_judge_score", "structured_first_pass"):
        assert table[unmeasured] is None, f"{unmeasured} 需金标集/裁判模型/真调用台账，未评测必须是 None 而不是 0"


def test_首过率只算真答了的结构化调用首答() -> None:
    """口径三件事一次钉住：修复轮不另计一次调用、传输失败不进分母、非结构化调用不参与。"""
    rows = [
        {"structured": True, "attempt": 0, "validation": "ok"},
        {"structured": True, "attempt": 0, "validation": "malformed"},
        {"structured": True, "attempt": 1, "validation": "ok"},  # 上一行那次调用的修复轮：不该被当成"首过了"
        {"structured": True, "attempt": 0, "validation": "transport_error"},  # 网关故障，不是格式问题
        {"structured": False, "attempt": 0, "validation": "ok"},  # 普通 chat，不参与结构化首过率
    ]
    rate, breakdown = structured_first_pass_rate(rows)
    assert rate == 0.5, "两次真答的首答里一次过 → 0.5；把修复轮或传输失败混进来的版本都不是这个数"
    assert breakdown == {"首过": 1, "需修复": 1, "传输失败(不参与)": 1}


def test_没有结构化调用时首过率是None不是满分() -> None:
    """变异点：把"没数"算成 1.0 的那版代码，会让这一列在根本没跑模型时冒充满分。"""
    assert structured_first_pass_rate([])[0] is None
    only_plain_chat = [{"structured": False, "attempt": 0, "validation": "ok"}]
    assert structured_first_pass_rate(only_plain_chat)[0] is None


def test_空台账或错档形要响亮报错(tmp_path: Path) -> None:
    """拿错文件/空文件/旧形状档都不能让这一列静默变 None——那等于悄悄关掉闸门。"""
    empty = tmp_path / "usage.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="空或不是"):
        _load_usage(str(empty))
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"quality_table": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="空或不是"):
        _load_usage(str(wrong))
    # 旧版网关导的档：非空、每行也是对象，但没有 structured/attempt 这两格——
    # 不拦的话整份档被读成"没有结构化调用"，门禁 exit 0 却什么都没把关。
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps([{"task": "assess", "validation": "ok"}, {"task": "plan", "validation": "ok"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="第 0 行"):
        _load_usage(str(legacy))
    scalars = tmp_path / "scalars.json"
    scalars.write_text('["just", "strings"]', encoding="utf-8")
    with pytest.raises(ValueError, match="第 0 行"):
        _load_usage(str(scalars))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text('{"usage": [{"structured": true, "attempt": 0, "validation": "ok"}]}', encoding="utf-8")
    assert _load_usage(str(wrapped))[0]["validation"] == "ok"


def test_真台账算出的首过率回退必须退码非零(capsys: Any, tmp_path: Path) -> None:
    """这一列必须真的能红：给一份首答只有一半过关的台账，对着 0.9 的基线判退步。"""
    ledger = tmp_path / "usage.json"
    rows = [{"structured": True, "attempt": a, "validation": v} for a, v in ((0, "ok"), (0, "malformed"), (1, "ok"))]
    ledger.write_text(json.dumps(rows), encoding="utf-8")
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({**QualityMetrics().as_dict(), "structured_first_pass": 0.9}), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--usage", str(ledger)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["quality_table"]["structured_first_pass"] == 0.5
    rows_out = report["gate"]["regressions"]
    assert [row["metric"] for row in rows_out] == ["structured_first_pass"]
    assert rows_out[0]["delta"] == -0.4
    assert report["first_pass_ledger"]["需修复"] == 1


def test_无基线时只记录不判退并退出零(capsys: Any) -> None:
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline_present"] is False
    assert report["gate"]["passed"] is True
    assert sorted(report["gate"]["unmeasured"]) == ["copy_judge_score", "level_agreement", "structured_first_pass", "trigger_f1"]


def test_STU合法率回退必须退出码非零(capsys: Any, tmp_path: Path) -> None:
    """CI 只认退出码：这一列真退步了就得给 1，并把退步列与差值写进结果。"""
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({**QualityMetrics().as_dict(), "stu_legality": 1.05}), encoding="utf-8")
    assert main(["--baseline", str(baseline)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["gate"]["passed"] is False
    rows = report["gate"]["regressions"]
    assert [row["metric"] for row in rows] == ["stu_legality"]
    assert rows[0]["current"] == 1.0 and rows[0]["baseline"] == 1.05 and rows[0]["delta"] == -0.05


def test_基线有数而当前未评测不算退步(capsys: Any, tmp_path: Path) -> None:
    """变异点：把 None 当成 0.0 去比基线的那版代码，会在"这一列根本没跑"时谎报退步、把 CI 卡死。"""
    baseline = tmp_path / "base.json"
    baseline.write_text(
        json.dumps(
            {
                "trigger_f1": 0.82,
                "level_agreement": 0.9,
                "stu_legality": 1.0,
                "copy_judge_score": 4.1,
                "structured_first_pass": 0.95,
            }
        ),
        encoding="utf-8",
    )
    assert main(["--baseline", str(baseline)]) == 0, "未评测的列不参与退步判定"
    report = json.loads(capsys.readouterr().out)
    assert report["gate"]["regressions"] == []
    assert sorted(report["gate"]["unmeasured"]) == [
        "copy_judge_score",
        "level_agreement",
        "structured_first_pass",
        "trigger_f1",
    ]


def test_结果落盘与打印同一份(capsys: Any, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    assert main(["--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(capsys.readouterr().out)


def test_基线档能写回来也被读回去(tmp_path: Path, capsys: Any) -> None:
    """写档与读档必须同形：形状对不上时门禁会读成整表 None、变成永远绿的空闸。"""
    base = tmp_path / "base.json"
    assert main(["--write-baseline", str(base)]) == 0
    capsys.readouterr()
    raw = json.loads(base.read_text(encoding="utf-8"))
    assert set(raw) == set(_COLUMNS), "基线档顶层就是这几列，不多不少"
    parsed = _load_baseline(str(base))
    assert parsed.stu_legality == 1.0
    assert parsed.trigger_f1 is None and parsed.level_agreement is None and parsed.copy_judge_score is None
    assert main(["--baseline", str(base)]) == 0


def test_报告档当基线读要直接报错(tmp_path: Path, capsys: Any) -> None:
    """--json 的报告把各列嵌在 quality_table 下；拿它当基线读会全读成 None，必须拒绝。"""
    report = tmp_path / "report.json"
    assert main(["--json", str(report)]) == 0
    capsys.readouterr()
    with pytest.raises(ValueError, match="缺列"):
        _load_baseline(str(report))


def test_基线与写基线同路径要拒绝(tmp_path: Path) -> None:
    """先写后读会让当前值当上自己的基线，门禁永远绿——这条脚gun要当场拦下。"""
    same = tmp_path / "base.json"
    with pytest.raises(SystemExit) as boom:
        main(["--baseline", str(same), "--write-baseline", str(same)])
    assert boom.value.code == 2


def test_刷新基线不许把有数的列静默写成None(capsys: Any, tmp_path: Path) -> None:
    """`--write-baseline` 没带 `--usage` 时首过率算不出→None；直接覆盖就等于悄悄关掉这一列的闸门。"""
    base = tmp_path / "base.json"
    base.write_text(json.dumps({**QualityMetrics().as_dict(), "structured_first_pass": 0.86}), encoding="utf-8")
    assert main(["--write-baseline", str(base)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["refused"] is True
    assert report["would_blank_columns"] == ["structured_first_pass"]
    assert json.loads(base.read_text(encoding="utf-8"))["structured_first_pass"] == 0.86, "拒绝必须真的没动原档"
    # 显式承认才放行：这条分支存在的意义是"确有理由地清空"，不是给静默通道开个后门。
    assert main(["--write-baseline", str(base), "--allow-blank"]) == 0
    assert json.loads(base.read_text(encoding="utf-8"))["structured_first_pass"] is None


def test_带台账刷出来的基线能把首过率钉进去(tmp_path: Path, capsys: Any) -> None:
    """这一列的值必须来自真档重算：同一份台账读两遍要得同一个数，否则口径是软的。"""
    ledger = tmp_path / "usage.json"
    ledger.write_text(
        json.dumps([{"structured": True, "attempt": 0, "validation": "ok"}, {"structured": True, "attempt": 0, "validation": "malformed"}]),
        encoding="utf-8",
    )
    base = tmp_path / "base.json"
    assert main(["--write-baseline", str(base), "--usage", str(ledger)]) == 0
    capsys.readouterr()
    assert json.loads(base.read_text(encoding="utf-8"))["structured_first_pass"] == 0.5
    assert main(["--baseline", str(base), "--usage", str(ledger)]) == 0, "同一份台账复算不应被判成退步"


def test_随库基线的首过率列是实测值不是留白() -> None:
    """§8 承诺"首过率持续盯"：基线里这一列必须是真跑出来的数，写成 null 就等于没盯。"""
    raw = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert raw["structured_first_pass"] is not None, "首过率已用真 DeepSeek 台账实测，基线不得留 null"
    assert 0.0 <= raw["structured_first_pass"] <= 1.0


async def test_随库基线档过得了门禁() -> None:
    """CI 用的就是这份基线：形状合法（各列齐全，缺列会被读成未评测），且当前实测不被它判成退步。"""
    baseline = _load_baseline(str(BASELINE))
    current = await current_quality(ContractRegistry())
    result = regression_gate(current, baseline)
    assert baseline.stu_legality == 1.0
    assert result.passed is True and result.regressions == []
    assert "structured_first_pass" in result.unmeasured, "CI 无台账时这一列算未评测：不参与判定，也不算退步"


def test_CI的gate任务必须跑这条门禁并带基线() -> None:
    """门禁脚本没人跑就等于没有：CI 里必须存在"带 --baseline 跑 quality_gate"的那一步。"""
    steps = [step for step in _ci_steps() if "quality_gate" in step.get("run", "")]
    assert len(steps) == 1
    assert "--baseline baselines/quality_baseline.json" in steps[0]["run"]


def _ci_steps() -> list[dict[str, Any]]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["gate"]["steps"]
