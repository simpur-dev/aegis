"""质量四表门禁脚本的口径用例：数字必须从真实产出路径算出来，未评测的列不许冒充。

`current_quality()` 的 STU合法率不是手填的分数，而是走 plan 无 LLM 本地剧本【真跑】一批 task_units、
再交给 `ContractRegistry.stu_errors` 校验——所以这里的断言都在验"脚本读的确实是那条腿"。
四列里只有 STU合法率能离线实算，另三列需金标集与裁判模型（数据/密钥门），脚本给 None；
None 既不能被判成"达标"，也不能被判成"退步"（把 None 当 0.0 比的那版代码会误判，见基线相关用例）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.quality_gate import _COLUMNS, _load_baseline, current_quality, main, plan_playbook_stu_units

from aegis.bus.gateway import ContractRegistry
from aegis.services.quality_eval import QualityMetrics, regression_gate, stu_legality_rate

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


async def test_当前四表只有STU合法率有数其余如实留白() -> None:
    table = (await current_quality(ContractRegistry())).as_dict()
    assert table["stu_legality"] is not None
    assert 0.0 <= table["stu_legality"] <= 1.0
    for unmeasured in ("trigger_f1", "level_agreement", "copy_judge_score"):
        assert table[unmeasured] is None, f"{unmeasured} 需金标集与裁判模型，未评测必须是 None 而不是 0"


def test_无基线时只记录不判退并退出零(capsys: Any) -> None:
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline_present"] is False
    assert report["gate"]["passed"] is True
    assert sorted(report["gate"]["unmeasured"]) == ["copy_judge_score", "level_agreement", "trigger_f1"]


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
        json.dumps({"trigger_f1": 0.82, "level_agreement": 0.9, "stu_legality": 1.0, "copy_judge_score": 4.1}),
        encoding="utf-8",
    )
    assert main(["--baseline", str(baseline)]) == 0, "未评测的三列不参与退步判定"
    report = json.loads(capsys.readouterr().out)
    assert report["gate"]["regressions"] == []
    assert sorted(report["gate"]["unmeasured"]) == ["copy_judge_score", "level_agreement", "trigger_f1"]


def test_结果落盘与打印同一份(capsys: Any, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    assert main(["--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(capsys.readouterr().out)


def test_基线档能写回来也被读回去(tmp_path: Path, capsys: Any) -> None:
    """写档与读档必须同形：形状对不上时门禁会读成四列 None、变成永远绿的空闸。"""
    base = tmp_path / "base.json"
    assert main(["--write-baseline", str(base)]) == 0
    capsys.readouterr()
    raw = json.loads(base.read_text(encoding="utf-8"))
    assert set(raw) == set(_COLUMNS), "基线档顶层就是四列，不多不少"
    parsed = _load_baseline(str(base))
    assert parsed.stu_legality == 1.0
    assert parsed.trigger_f1 is None and parsed.level_agreement is None and parsed.copy_judge_score is None
    assert main(["--baseline", str(base)]) == 0


def test_报告档当基线读要直接报错(tmp_path: Path, capsys: Any) -> None:
    """--json 的报告把四列嵌在 quality_table 下；拿它当基线读会全读成 None，必须拒绝。"""
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


async def test_随库基线档过得了门禁() -> None:
    """CI 用的就是这份基线：形状合法（四列齐全），且当前实测不被它判成退步。"""
    baseline = _load_baseline(str(BASELINE))
    current = await current_quality(ContractRegistry())
    result = regression_gate(current, baseline)
    assert baseline.stu_legality == 1.0
    assert result.passed is True and result.regressions == []


def test_CI的gate任务必须跑这条门禁并带基线() -> None:
    """门禁脚本没人跑就等于没有：CI 里必须存在"带 --baseline 跑 quality_gate"的那一步。"""
    steps = [step for step in _ci_steps() if "quality_gate" in step.get("run", "")]
    assert len(steps) == 1
    assert "--baseline baselines/quality_baseline.json" in steps[0]["run"]


def _ci_steps() -> list[dict[str, Any]]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["gate"]["steps"]
