"""并发曲线量测：按并发梯度跑 Locust，产出可引用的 P50/P95/RPS/失败率曲线。

为什么要一个脚本而不是手跑一次 locust：考核问的是"在多少并发下仍然 ≤2s/≤3s、协同成功率 ≥90%"，
单个并发点的数字答不了这个问题；曲线上**第一个越阈值的级别**才是这套实现能对外承诺的容量口径。

服务由本脚本自己拉起（真端口、真 uvicorn 进程），跑完一定收摊：
留一个半死的后台服务，下一次量测拿到的就是一串假数据。
每个并发点都断言"请求数 > 0"——压测档案曾经因为 `float(timedelta)` 静默零流量而整场绿着
（见 tests/unit/test_load_profile.py），这条断言就是那道闸。

用法：
    uv run python -m scripts.load_curve                                  # 1/10/30/60 并发，各 25s
    uv run python -m scripts.load_curve --levels 100,200 --duration 60s --out reports/load_curve.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from aegis.config import Settings
from aegis.observability.load_policy import drill_budget_ms, read_budget_ms

STATS_ROW = re.compile(
    r"^(?:(?P<type>[A-Z]+)\s+)?(?P<name>\S+)\s+(?P<requests>\d+)\s+(?P<fails>\d+)\((?P<fail_pct>[\d.]+)%\)\s*\|"
    r"\s*(?P<avg>[\d.]+)\s+(?P<min>[\d.]+)\s+(?P<max>[\d.]+)\s+(?P<median>[\d.]+)\s*\|\s*(?P<rps>[\d.]+)"
)
# 分位数表的列顺序由 locust 固定：50 66 75 80 90 95 98 99 99.9 99.99 100
PERCENTILE_KEYS = ("p50", "p66", "p75", "p80", "p90", "p95", "p98", "p99", "p99_9", "p99_99", "p100")
PERCENTILE_ROW = re.compile(r"^(?:(?P<type>[A-Z]+)\s+)?(?P<name>[\w/]+)\s+(?P<values>(?:[\d.]+\s+){11})(?P<requests>\d+)\s*$")


def _spawn_server(port: int, *, env: dict[str, str]) -> subprocess.Popen[bytes]:
    child_env = {
        **os.environ,
        **env,
        "AEGIS_HTTP_HOST": "127.0.0.1",
        "AEGIS_HTTP_PORT": str(port),
        "AEGIS_ENV": "dev",
        "AEGIS_LOG_LEVEL": "WARNING",
        "AEGIS_BUS_BACKEND": "memory",
        "AEGIS_DELIVERY_MODE": "mock",
        # 演练端点要求模拟器在场；这条开关今晚才真正被配置面拧动
        "AEGIS_SIMULATOR_ENABLED": "true",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "aegis.main"],
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until_ready(base: str, process: subprocess.Popen[bytes], *, timeout: float) -> float:
    """返回"起进程到 /healthz 可答"的秒数；进程中途退出就直接判失败。"""
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务进程提前退出，退出码 {process.returncode}")
        try:
            if httpx.get(f"{base}/healthz", timeout=2.0).status_code == 200:
                return time.perf_counter() - started
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"服务在 {timeout}s 内没有就绪：{base}/healthz")


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def parse_summary(stdout: str) -> dict[str, Any]:
    """把 locust 汇总表与分位数表读成结构化事实（只认表体，不认装饰行）。"""
    stats: dict[str, dict[str, float]] = {}
    percentiles: dict[str, dict[str, float]] = {}
    in_percentiles = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Response time percentiles"):
            in_percentiles = True
            continue
        if not stripped or stripped.startswith("---") or stripped.startswith("Type "):
            continue
        if in_percentiles:
            match = PERCENTILE_ROW.match(stripped)
            if not match:
                continue
            values = [float(item) for item in match.group("values").split()]
            percentiles[match.group("name")] = dict(zip(PERCENTILE_KEYS, values, strict=True))
            continue
        match = STATS_ROW.match(stripped)
        if not match:
            continue
        stats[match.group("name")] = {
            "requests": float(match.group("requests")),
            "fails": float(match.group("fails")),
            "fail_pct": float(match.group("fail_pct")),
            "avg_ms": float(match.group("avg")),
            "median_ms": float(match.group("median")),
            "max_ms": float(match.group("max")),
            "rps": float(match.group("rps")),
        }
    return {"stats": stats, "percentiles": percentiles}


def merge_streams(*chunks: str | None) -> str:
    """合并 locust 子进程的 stdout 与 stderr。

    汇总表与分位数表在 stderr 上（stdout 只有零星日志）：只读 stdout 会把
    "跑出了一场真流量"读成"零请求"，这正是本脚本曾经整场绿着什么都没测到的成因。
    """
    return "\n".join(chunk for chunk in chunks if chunk)


def point_from(parsed: dict[str, Any], *, users: int, exit_code: int) -> dict[str, Any]:
    """一个并发点的结论。请求数为零直接判失败，不出具一份"看起来量过了"的空表。"""
    aggregated = parsed["stats"].get("Aggregated", {})
    requests = int(aggregated.get("requests", 0))
    if requests <= 0:
        raise RuntimeError(f"users={users} 一个请求都没发出去：这份结果不能当成量测")
    return {
        "users": users,
        "requests": requests,
        "fail_pct": aggregated.get("fail_pct", 0.0),
        "success_rate": round(1.0 - aggregated.get("fail_pct", 0.0) / 100.0, 4),
        "rps": aggregated.get("rps", 0.0),
        "aggregated_ms": {
            "avg": aggregated.get("avg_ms", 0.0),
            "median": aggregated.get("median_ms", 0.0),
            "max": aggregated.get("max_ms", 0.0),
        },
        "percentiles_ms": parsed["percentiles"].get("Aggregated", {}),
        "by_endpoint": {
            name: {
                "requests": int(spec.get("requests", 0)),
                "fail_pct": spec.get("fail_pct", 0.0),
                "p95": parsed["percentiles"].get(name, {}).get("p95"),
                "median": spec.get("median_ms", 0.0),
            }
            for name, spec in parsed["stats"].items()
            if name != "Aggregated"
        },
        "locust_exit": exit_code,
    }


def run_point(*, base: str, users: int, duration: str, spawn_rate: int, csv_prefix: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "tests/load/locustfile.py",
        "--headless",
        "--host",
        base,
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "-t",
        duration,
        "--csv",
        str(csv_prefix),
        "--only-summary",
        "--exit-code",
        "0",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=1_200, check=False)
    report_text = merge_streams(completed.stdout, completed.stderr)
    try:
        return point_from(parse_summary(report_text), users=users, exit_code=completed.returncode)
    except RuntimeError as exc:
        # 判失败之外还要给出现场：这份输出是唯一能区分"没打到流量"和"解析没认出台账"的东西。
        tail = [line for line in report_text.splitlines() if line.strip()][-5:]
        raise RuntimeError(f"{exc}；输出尾部：{tail}") from exc


def run_curve(
    *,
    port: int,
    levels: list[int],
    duration: str,
    spawn_rate: int,
    settings: Settings,
) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    read_budget = read_budget_ms(settings)
    drill_budget = drill_budget_ms(settings)
    process = _spawn_server(port, env={})
    try:
        boot_seconds = _wait_until_ready(base, process, timeout=60.0)
        points: list[dict[str, Any]] = []
        for users in levels:
            csv_prefix = Path("reports") / f"load_curve_u{users}"
            csv_prefix.parent.mkdir(parents=True, exist_ok=True)
            point = run_point(base=base, users=users, duration=duration, spawn_rate=spawn_rate, csv_prefix=csv_prefix)
            breaches: list[str] = []
            for name, row in point["by_endpoint"].items():
                budget = drill_budget if name == "drill_run" else read_budget
                p95 = row.get("p95")
                if row["fail_pct"] > 0.0:
                    breaches.append(f"{name} 失败率 {row['fail_pct']}%")
                elif isinstance(p95, (int, float)) and p95 > budget:
                    breaches.append(f"{name} P95={p95:.0f}ms > {budget:.0f}ms")
            point["breaches"] = breaches
            points.append(point)
            print(
                f"users={users:>4}  requests={point['requests']:>7}  RPS={point['rps']:>6.2f}  "
                f"失败率={point['fail_pct']:>5.2f}%  P50={point['percentiles_ms'].get('p50', 0):>7.1f}ms  "
                f"P95={point['percentiles_ms'].get('p95', 0):>7.1f}ms  越阈值={len(breaches)}",
                flush=True,
            )
        clean = [point for point in points if not point["breaches"]]
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "budgets_ms": {"read": read_budget, "drill": drill_budget},
            "server": {"boot_seconds": round(boot_seconds, 2), "bus": settings.bus_backend, "port": port},
            "duration_per_point": duration,
            "levels": levels,
            "points": points,
            "max_clean_level": max((point["users"] for point in clean), default=None),
            "first_breaching_level": next((point["users"] for point in points if point["breaches"]), None),
        }
    finally:
        _stop_server(process)


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 并发曲线量测（Locust）")
    parser.add_argument("--levels", default="1,10,30,60", help="逗号分隔的并发级别")
    parser.add_argument("--duration", default="25s", help="每个级别跑多久")
    parser.add_argument("--spawn-rate", type=int, default=10, help="每秒拉起多少用户")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--out", type=Path, default=None, help="同时把曲线写入 JSON 文件")
    args = parser.parse_args()

    levels = [int(item) for item in str(args.levels).split(",") if item.strip()]
    if not levels or any(level < 1 for level in levels):
        parser.error("--levels 至少给出一个正整数")
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock", simulator_enabled=True)
    curve = run_curve(port=args.port, levels=levels, duration=str(args.duration), spawn_rate=args.spawn_rate, settings=settings)
    print(json.dumps(curve, ensure_ascii=False, indent=2))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(curve, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[已写入] {args.out}", file=sys.stderr)
    # 有级别越阈值就非零退出：这条命令因此可以直接进 CI 当容量门禁，
    # 而不只是"跑出一份没人看的 JSON"。
    return 1 if curve["first_breaching_level"] is not None else 0


if __name__ == "__main__":
    sys.exit(main())
