"""研判段（ext 实现）的基准脚本——性能测量的唯一入口。

为什么要有它：链路上"研判"是唯一有真实 CPU 计算的一段（因子引擎 + 置信度 + 证据整理），
而演练脚本（`scripts/drill.py`）走的是平台降级轨（`assess=local`），测不到它。
所以这里直接按统一契约构造请求，反复调 `run_risk_agent`，用 cProfile 定位热点。

用法（后端目录）：
    python scripts/bench_assess_ext.py --loops 50              # 计时
    python scripts/bench_assess_ext.py --loops 50 --profile     # cProfile 剖析
    python scripts/bench_assess_ext.py --loops 50 --sites 200   # 放大站点数（看复杂度）

**它不是生产路径的一部分**：只用标准库 + 本仓模块，不引新依赖，不改生产代码。
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import statistics
import time

from aegis.domain.assess import AssessEvidence, AssessRequest, FactorObservation
from aegis.domain.enums import HazardType, RiskLevel


def _observations(sites: int) -> list[FactorObservation]:
    """造一份**形状真实**的观测包：多站点 × 多因子（契约上限 48 条观测）。

    因子清单对着泥石流的 FACTOR_CONFIG（P 组孕灾 / T 组触发 / I 组影响），
    缺必需因子会被门控判 insufficient_data —— 那条路径也测，但要测**最重的成功路径**，
    得把必需因子都喂上。
    """
    factors = (
        "catchment_area_km2",  # P 组：孕灾（必需）
        "channel_gradient",  # P 组：孕灾（必需）
        "sediment_supply_index",  # P 组：孕灾
        "rainfall_mm_h",  # T 组：触发（必需）
        "snow_depth_cm",  # T 组：触发
        "rain_10min",  # T 组：触发（另一个触发口径）
    )
    out: list[FactorObservation] = []
    for site in range(sites):
        for index, factor in enumerate(factors):
            out.append(
                FactorObservation(
                    factor=factor,
                    unit="mm",
                    samples=12,
                    min_value=float(10 + index),
                    max_value=float(40 + index),
                    mean_value=float(20 + site % 37 + index * 3),
                    observed_at="2026-11-10T00:00:00Z",
                    station_ids=[f"ST{site:04d}"],
                    quality="ok",
                )
            )
    return out


def _request(sites: int) -> tuple[AssessRequest, AssessEvidence]:
    observations = _observations(sites)
    evidence = AssessEvidence(
        observations=observations,
        region_code="540121",
        window="1h",
        generated_at="2026-11-10T00:00:00Z",
    )
    request = AssessRequest(
        region_code="540121",
        window="1h",
        hazard_type=HazardType.DEBRIS_FLOW,
        candidate_hazards=[HazardType.DEBRIS_FLOW],
        rule_level=RiskLevel.ORANGE,
        evidence=evidence,
    )
    return request, evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", type=int, default=50)
    parser.add_argument("--sites", type=int, default=40, help="站点数（放大看复杂度）")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    from aegis.agents.hazards.risk_agent.main import run_risk_agent

    request, evidence = _request(args.sites)

    # 预热：首次调用要编译 LangGraph 图，那是一次性成本，不该计入稳态
    run_risk_agent(request, evidence)

    timings: list[float] = []
    profiler = cProfile.Profile() if args.profile else None
    if profiler is not None:
        profiler.enable()
    for _ in range(args.loops):
        started = time.perf_counter()
        run_risk_agent(request, evidence)
        timings.append((time.perf_counter() - started) * 1000)
    if profiler is not None:
        profiler.disable()

    timings.sort()
    print(f"loops={args.loops} sites={args.sites}")
    print(f"  中位数 {statistics.median(timings):.2f} ms")
    print(f"  p90     {timings[int(len(timings) * 0.9)]:.2f} ms")
    print(f"  最小    {timings[0]:.2f} ms")
    print(f"  最大    {timings[-1]:.2f} ms")
    print(f"  吞吐    {1000.0 / statistics.median(timings):.1f} 次/秒")

    if profiler is not None:
        buffer = io.StringIO()
        stats = pstats.Stats(profiler, stream=buffer).sort_stats("tottime")
        stats.print_stats(20)
        print(buffer.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
