"""并发曲线脚本的读数层：用**真 locust 输出**钉住解析、双流合并与"零流量必须判失败"。

`tests/fixtures/locust_summary_locust-2.46.6.txt` 是本机对真后端跑出来的一份原样输出
（2026-10-01，2 并发 8s），不是手写的理想表格——当年那场"CI 全绿但一个请求都没发出去"
的事故，根源就是按想象中的表格写了读数代码。所以这里断言的数字全部来自那份原样输出。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
import scripts.load_curve as load_curve
from scripts.load_curve import (
    LOCAL_PROFILE,
    _spawn_server,
    _stop_server,
    _wait_until_ready,
    merge_streams,
    parse_summary,
    point_from,
    profile_for,
    read_log_tail,
    server_command,
    server_env,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "locust_summary_locust-2.46.6.txt"

ENDPOINT_ROWS = {
    "agents",
    "drill_run",
    "integrations",
    "latency",
    "node_types",
    "readyz",
    "stations",
    "telemetry",
    "warnings",
}


@pytest.fixture(scope="module")
def raw() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(raw: str) -> dict[str, Any]:
    return parse_summary(merge_streams(None, raw))


class TestMergeStreams:
    def test_台账在哪条流上都要读得到_漏一条就是当年那场假绿(self, raw: str) -> None:
        assert "Aggregated" in raw
        assert parse_summary(merge_streams(raw, ""))["stats"], "台账在 stdout 这一路"
        assert parse_summary(merge_streams("", raw))["stats"], "locust 实际把台账写在 stderr 这一路"
        assert parse_summary(merge_streams("", None))["stats"] == {}, "只交给它一条空流就必须是空表，而不是一个看起来达标的空结果"

    def test_缺失的流不参与拼接(self, raw: str) -> None:
        assert merge_streams(None, raw) == raw
        assert merge_streams(raw, None, "") == raw
        assert merge_streams(None, None) == ""
        assert merge_streams("abc", None, "", "def") == "abc\ndef"


class TestParseSummary:
    def test_九个端点与聚合行全部读出来(self, parsed: dict[str, Any]) -> None:
        assert set(parsed["stats"]) == ENDPOINT_ROWS | {"Aggregated"}

    def test_聚合行的数字就是那份真流量(self, parsed: dict[str, Any]) -> None:
        aggregated = parsed["stats"]["Aggregated"]
        assert aggregated == {
            "requests": 17.0,
            "fails": 0.0,
            "fail_pct": 0.0,
            "avg_ms": 31.0,
            "median_ms": 5.0,
            "max_ms": 179.0,
            "rps": 2.39,
        }

    def test_演练端点单独成行_因为它走的是另一套预算(self, parsed: dict[str, Any]) -> None:
        drill = parsed["stats"]["drill_run"]
        assert (drill["requests"], drill["avg_ms"], drill["max_ms"], drill["median_ms"]) == (3.0, 138.0, 179.0, 130.0)

    def test_分位数表按十一个档位读_并保留请求数(self, parsed: dict[str, Any]) -> None:
        aggregated = parsed["percentiles"]["Aggregated"]
        assert len(aggregated) == 11
        assert (aggregated["p50"], aggregated["p95"], aggregated["p100"]) == (5.0, 180.0, 180.0)
        assert parsed["percentiles"]["drill_run"]["p95"] == 180.0

    def test_分位数表里每个端点都在_否则结论里的_p95_会是空的(self, parsed: dict[str, Any]) -> None:
        assert set(parsed["percentiles"]) == ENDPOINT_ROWS | {"Aggregated"}

    def test_日志行与装饰行都不产出行(self, raw: str) -> None:
        noise = ("[", "---", "Type ")
        only_logs = "\n".join(line for line in raw.splitlines() if line.startswith(noise))
        assert parse_summary(only_logs)["stats"] == {}
        assert parse_summary("")["stats"] == {}

    def test_四位以上毫秒与带失败率的行也能读(self) -> None:
        # 真表格有大量对齐填充，这里用等价的紧凑写法：正则认的是字段次序，不是列宽。
        stats_row = "GET slow_endpoint 12 3(25.00%) | 1130 900 4820 1100 | 1.20 0.30"
        percentile_row = "GET slow_endpoint 1100 1120 1130 1150 4820 4820 4820 4820 4820 4820 4820 12"
        header = "Response time percentiles (approximated)"
        result = parse_summary(f"{stats_row}\n{header}\n{percentile_row}\n")
        row = result["stats"]["slow_endpoint"]
        assert (row["requests"], row["fails"], row["fail_pct"], row["max_ms"], row["rps"]) == (12.0, 3.0, 25.0, 4820.0, 1.2)
        assert result["percentiles"]["slow_endpoint"]["p90"] == 4820.0


class TestPointFrom:
    def test_真解析结果规整成一个并发点(self, parsed: dict[str, Any]) -> None:
        point = point_from(parsed, users=2, exit_code=0)
        assert point["users"] == 2
        assert point["requests"] == 17
        assert point["success_rate"] == 1.0
        assert point["locust_exit"] == 0
        assert point["aggregated_ms"] == {"avg": 31.0, "median": 5.0, "max": 179.0}

    def test_聚合行不进端点明细_但每个端点都带上了_p95(self, parsed: dict[str, Any]) -> None:
        point = point_from(parsed, users=2, exit_code=0)
        assert "Aggregated" not in point["by_endpoint"]
        assert set(point["by_endpoint"]) == ENDPOINT_ROWS
        assert point["by_endpoint"]["drill_run"]["p95"] == 180.0
        assert point["by_endpoint"]["readyz"]["requests"] == 1

    def test_失败率折算成成功率(self) -> None:
        fake = {"stats": {"Aggregated": {"requests": 8.0, "fails": 1.0, "fail_pct": 12.5}}, "percentiles": {}}
        assert point_from(fake, users=5, exit_code=0)["success_rate"] == 0.875

    def test_零流量直接判失败_不出具一份看着像量过的空表(self) -> None:
        with pytest.raises(RuntimeError, match="一个请求都没发出去"):
            point_from({"stats": {}, "percentiles": {}}, users=30, exit_code=0)

    def test_只有表头没有数据行也算零流量(self) -> None:
        header_only = parse_summary("Type     Name   # reqs      # fails\n--------|------\n")
        with pytest.raises(RuntimeError, match="users=100"):
            point_from(header_only, users=100, exit_code=0)


class TestServerProfile:
    """形态层：曲线必须知道自己是在哪种服务形态上量的，否则"容量"两个字没有可比性。"""

    def test_local_形态把总线与存储都写进子进程环境(self) -> None:
        env = server_env(8123, LOCAL_PROFILE)
        assert env["AEGIS_BUS_BACKEND"] == "memory"
        assert env["AEGIS_STORE_BACKEND"] == "memory"
        assert env["AEGIS_HTTP_PORT"] == "8123"
        assert env["AEGIS_DELIVERY_MODE"] == "mock"

    def test_形态写全每一项_不留继承来的隐式口径(self) -> None:
        # 调用方环境里残留 AEGIS_STORE_BACKEND=postgres 时，local 形态仍必须显式写 memory。
        env = server_env(8123, profile_for("local", {"AEGIS_STORE_BACKEND": "postgres"}))
        assert env["AEGIS_STORE_BACKEND"] == "memory"

    def test_deployed_缺_DSN_直接判失败而不是静默退回内存视图(self) -> None:
        with pytest.raises(ValueError, match="AEGIS_PG_DSN"):
            profile_for("deployed", {})

    def test_deployed_把真总线与真库都带上并默认开迁移(self) -> None:
        profile = profile_for(
            "deployed",
            {"AEGIS_PG_DSN": "postgresql://aegis:pw@127.0.0.1:5432/aegis"},
        )
        env = server_env(8123, profile)
        assert profile.bus_backend == "nats"
        assert profile.store_backend == "postgres"
        assert env["AEGIS_PG_DSN"] == "postgresql://aegis:pw@127.0.0.1:5432/aegis"
        assert env["AEGIS_NATS_URL"] == "nats://127.0.0.1:4222"
        assert env["AEGIS_PG_APPLY_MIGRATIONS_ON_START"] == "true"

    def test_未知形态响亮拒绝(self) -> None:
        with pytest.raises(ValueError, match="未知 --profile"):
            profile_for("prod-ish", {"AEGIS_PG_DSN": "x"})


class TestServerLog:
    """起服务那一层：判"提前退出"却把子进程输出丢进 DEVNULL，等于把现场擦掉再报"没现场"。

    2026-10-02 实测代价：`--profile deployed` 起不来，报的只有
    `ValueError: nats: invalid consumer name: 'd_perceive_perceive.mock01'`（在服务进程自己的
    stderr 里），脚本这边只给出退出码，只能手工再跑一遍 `python -m aegis.main` 才拿到那句话。
    """

    @staticmethod
    def _child_printing(*, out: str, err: str, code: int) -> str:
        return f"import sys;print({out!r});print({err!r}, file=sys.stderr, flush=True);sys.exit({code})"

    def test_日志不存在时给空列表而不是抛(self, tmp_path: Path) -> None:
        assert read_log_tail(tmp_path / "never-written.log") == []

    def test_只留最后若干行且丢掉空行(self, tmp_path: Path) -> None:
        log = tmp_path / "server.log"
        # 每第三行是空行/纯空白：日志里穿插的空行不该占掉尾巴的名额。
        body = "".join(f"line-{index}\n" if index % 3 else "\n   \n" for index in range(40))
        log.write_text(body, encoding="utf-8")
        non_blank = [f"line-{index}" for index in range(40) if index % 3]
        assert read_log_tail(log, lines=5) == non_blank[-5:]

    def test_子进程的_stdout_与_stderr_落到同一份日志(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 同一份句柄：uvicorn 的信息走行、traceback 走另一行，分成两个文件就没法按因果读。
        monkeypatch.setattr(
            load_curve,
            "server_command",
            lambda: [sys.executable, "-u", "-c", self._child_printing(out="OUT-1", err="ERR-1", code=0)],
        )
        log = tmp_path / "server.log"
        with log.open("wb") as sink:
            process = _spawn_server(8123, env=server_env(8123, LOCAL_PROFILE), log_sink=sink)
            # 这条量的是"正常退出"路径，所以必须等它自己跑完再收摊（_stop_server 是 terminate，
            # 抢在它 flush 之前就把子进程杀了，文件会是空的——那不是这条要问的东西）。
            assert process.wait(timeout=60) == 0
        text = log.read_text(encoding="utf-8")
        assert "OUT-1" in text
        assert "ERR-1" in text

    def test_进程挂住时尾巴已经在文件里了(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`-u` 买到的就是这一刻：服务"卡住不返回 /healthz"时是被 terminate 收摊的。

        重定向到文件时解释器默认对 stdout 做块缓冲，没有 `-u` 就会现场全丢在 8KB 缓冲区里，
        而"提前退出"那条路径反而会因为正常 flush 而有内容——正好把最需要看日志的那种失败
        变成空文件。所以这条按真行为量：子进程不 flush 地打印后睡死，父进程必须已经读得到。
        """
        assert "-u" in server_command()
        monkeypatch.setattr(
            load_curve,
            "server_command",
            lambda: [sys.executable, "-u", "-c", "print('HANGING-BEFORE-READY');import time;time.sleep(120)"],
        )
        log = tmp_path / "server.log"
        with log.open("wb") as sink:
            process = _spawn_server(8123, env=server_env(8123, LOCAL_PROFILE), log_sink=sink)
            try:
                seen = ""
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    seen = log.read_text(encoding="utf-8", errors="replace")
                    if "HANGING-BEFORE-READY" in seen:
                        break
                    time.sleep(0.1)
                assert "HANGING-BEFORE-READY" in seen
                # 同一条读法在真实报错里用的就是它：读不出东西的那条路径要单独判，别在这里猜。
                assert read_log_tail(log, lines=1) == ["HANGING-BEFORE-READY"]
            finally:
                _stop_server(process)

    def test_提前退出的报错里带着真正的死因(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            load_curve,
            "server_command",
            lambda: [
                sys.executable,
                "-u",
                "-c",
                self._child_printing(
                    out="INFO Starting server",
                    err="ValueError: nats: invalid consumer name",
                    code=3,
                ),
            ],
        )
        log = tmp_path / "server.log"
        with log.open("wb") as sink:
            process = _spawn_server(8123, env=server_env(8123, LOCAL_PROFILE), log_sink=sink)
            try:
                with pytest.raises(RuntimeError) as raised:
                    _wait_until_ready("http://127.0.0.1:9", process, timeout=30.0, log_path=log)
            finally:
                _stop_server(process)
        message = str(raised.value)
        assert "退出码 3" in message
        assert "invalid consumer name" in message
        assert "INFO Starting server" in message

    def test_就绪就返回启动耗时(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Ready:
            returncode = None

            def poll(self) -> None:
                return None

        monkeypatch.setattr(load_curve.httpx, "get", lambda url, timeout: _Ok() if url.endswith("/healthz") else _Bad())
        seconds = _wait_until_ready("http://127.0.0.1:8123", Ready(), timeout=5.0)  # type: ignore[arg-type]
        assert seconds >= 0.0

    def test_超时也是判失败而不是当成通过(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Alive:
            returncode = None

            def poll(self) -> None:
                return None

        monkeypatch.setattr(load_curve.httpx, "get", lambda url, timeout: _Bad())
        with pytest.raises(RuntimeError, match="没有就绪"):
            _wait_until_ready("http://127.0.0.1:8123", Alive(), timeout=0.3, log_path=Path("missing.log"))  # type: ignore[arg-type]


class _Ok:
    status_code = 200


class _Bad:
    status_code = 503

    def raise_for_status(self) -> None:
        raise RuntimeError("not ready")
