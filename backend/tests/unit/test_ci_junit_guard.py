"""守卫脚本自己的反例：它必须能在"全体 skip"时红。

这条用例是这个仓里最容易写成空跑的那类——拿一份"正常"的 junit 断言 rc=0，永远测不出守卫失效。
所以真判据在红的那半边。
"""

from __future__ import annotations

from pathlib import Path

from scripts.ci_junit_guard import counts, main

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 每条集成/门禁 job 自己那份 junit 产物：守卫核对的就是它
GUARDED_JOBS = {
    "gate": "reports/junit-gate.xml",
    "integration-nats": "reports/junit-nats.xml",
    "integration-postgres": "reports/junit-postgres.xml",
}


def write_xml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


class TestCiWiring:
    """守卫接在 CI 里的哪几步，也要有本地用例盯着——否则"加了守卫"这件事本身会悄悄退化。"""

    def _workflow(self) -> dict:
        import yaml

        return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    def test_三条job都跑守卫且核对的是自己那份产物(self) -> None:
        jobs = self._workflow()["jobs"]
        for job, artifact in GUARDED_JOBS.items():
            steps = [str(step.get("run") or "") for step in jobs[job]["steps"]]
            guarded = [run for run in steps if "scripts/ci_junit_guard.py" in run]
            assert len(guarded) == 1, f"{job} 应当恰好一步计数守卫，实得 {len(guarded)} 步"
            assert artifact in guarded[0], f"{job} 的守卫核对的不是 {artifact}：{guarded[0]}"

    def test_每条被守卫的pytest步骤都产出junit(self) -> None:
        """守卫读的是产物；产物没生成时守卫只会报"文件不存在"，那不算测过东西。"""
        jobs = self._workflow()["jobs"]
        for job, artifact in GUARDED_JOBS.items():
            pytest_steps = [str(step.get("run") or "") for step in jobs[job]["steps"] if "uv run pytest" in str(step.get("run") or "")]
            assert pytest_steps, job
            assert all(artifact in run for run in pytest_steps), f"{job} 有 pytest 步骤没写 --junitxml={artifact}"


class TestGuardJudges:
    def test_全体skip必须红(self, tmp_path: Path) -> None:
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="36" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 1, "环境变量名打错时 skipif 会跳过整批用例，job 不许照绿"

    def test_零收集必须红(self, tmp_path: Path) -> None:
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="0" skipped="0" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 1

    def test_真跑过就绿(self, tmp_path: Path) -> None:
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="8" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0

    def test_产物缺失必须红而不是无声通过(self, tmp_path: Path) -> None:
        assert main(["guard", str(tmp_path / "nope.xml")]) == 1

    def test_畸形产物读不出计数时算不合格(self, tmp_path: Path) -> None:
        target = write_xml(tmp_path / "j.xml", "<testsuite")
        assert main(["guard", str(target)]) == 1

    def test_testsuites_包裹结构也认得(self, tmp_path: Path) -> None:
        target = write_xml(
            tmp_path / "j.xml",
            '<testsuites><testsuite name="pytest" tests="4" skipped="1" failures="0" errors="0"/></testsuites>',
        )
        assert counts(target) == (4, 1, 0, 0)
        assert main(["guard", str(target)]) == 0
