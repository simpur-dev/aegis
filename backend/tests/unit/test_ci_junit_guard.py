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


class TestStepSummary:
    """summary 通道：只服务 Actions 页面渲染（check-run API 匿名读回是空串，见 TestAnnotationChannel）。"""

    def test_判负时也要把计数写进summary(self, tmp_path: Path, monkeypatch) -> None:
        sink = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(sink))
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="36" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 1
        body = sink.read_text(encoding="utf-8")
        assert "| 36 | 36 | 0 | 0 | 0 |" in body, "红的时候正是最需要看见这五个数的时候"

    def test_通过时也写(self, tmp_path: Path, monkeypatch) -> None:
        sink = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(sink))
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="8" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0
        assert "| 36 | 8 | 28 | 0 | 0 |" in sink.read_text(encoding="utf-8")

    def test_没有这个环境变量时不落盘(self, tmp_path: Path, monkeypatch) -> None:
        """本机跑守卫不该在谁的工作区旁写文件。"""
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="4" skipped="1" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0
        assert list(tmp_path.glob("summary*")) == []

    def test_已有正文时只追加不覆盖(self, tmp_path: Path, monkeypatch) -> None:
        sink = tmp_path / "summary.md"
        sink.write_text("既有内容\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(sink))
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="4" skipped="0" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0
        body = sink.read_text(encoding="utf-8")
        assert body.startswith("既有内容"), "summary 是各步骤共用的，盖掉别人的那段就是毁证据"
        assert "| 4 | 0 | 4 | 0 | 0 |" in body, body

    def test_两次调用只留一个表头(self, tmp_path: Path, monkeypatch) -> None:
        sink = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(sink))
        for name, attrs in (("gate.xml", 'tests="10" skipped="1"'), ("nats.xml", 'tests="4" skipped="0"')):
            target = write_xml(tmp_path / name, f'<testsuite name="pytest" {attrs} failures="0" errors="0"/>')
            assert main(["guard", str(target)]) == 0
        body = sink.read_text(encoding="utf-8")
        assert body.count("### 计数守卫") == 1, body
        assert body.count("| 10 | 1 | 9 | 0 | 0 |") == 1
        assert body.count("| 4 | 0 | 4 | 0 | 0 |") == 1


class TestAnnotationChannel:
    """计数得发到**不要令牌就能读**的通道上：annotation 可以，step summary 不行（运行 186 实测）。"""

    def test_在CI里发notice级annotation(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="8" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0
        out = capsys.readouterr().out
        assert "::notice title=计数守卫::tests=36 skipped=8 ran=28" in out, out

    def test_判负时发error级(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="36" skipped="36" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 1
        assert "::error title=计数守卫::tests=36 skipped=36 ran=0" in capsys.readouterr().out

    def test_本机不发workflow命令(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """不在 Actions 上时不许往 stdout 塞 `::` 指令——那会污染本机日志且没人消费。"""
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        target = write_xml(tmp_path / "j.xml", '<testsuite name="pytest" tests="4" skipped="0" failures="0" errors="0"/>')
        assert main(["guard", str(target)]) == 0
        out = capsys.readouterr().out
        assert "::notice" not in out and "::error" not in out, out


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

    def test_setup_uv的输入名必须是它认得的那个(self) -> None:
        """`enable-caching` 不是 setup-uv@v4 的输入（合法名 `enable-cache`）。

        写错时 Actions 只留一条 warning annotation、缓存静默不生效——"我开了缓存"与"缓存真的在"
        差的正是这一类拼写，2026-10-15 的运行 184 里三条 job 全中。
        """
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "enable-caching" not in text
        assert text.count("enable-cache: true") == 3, "gate / integration-nats / integration-postgres 各一条"


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
