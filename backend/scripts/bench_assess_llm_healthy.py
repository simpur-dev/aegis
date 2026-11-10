"""研判段（ext 实现）的**对照实验台**：同一负载下量四种配置的时延。

【为什么要有它】
`bench_assess_ext.py` 量的是"LLM 不可用"这一档（真实上游返回 5xx）。
但那不是生产常态——生产常态是 **LLM 可用**，此时熔断永远不打开，
能省下来的只有"客户端复用"（不再每次新建 httpx 连接池 → 不再每次 TLS 握手）。
所以这里起一个**本地假 OpenAI 兼容端点**（真 HTTP、真握手、真 JSON），
把健康路径也量出来，避免"只量了坏路径就宣布优化成功"。

【四种配置】
  A 现状      : 每次新建 client + SDK 默认盲重试 2 次
  B 关盲重试  : 每次新建 client + max_retries=0
  C 客户端复用 : 复用 client + max_retries=0
  D 复用+熔断  : C 再加熔断（本��更的最终形态）

【纪律】
只改一个变量，其余完全相同；同一份请求、同一批 loop，报中位数与 p90。
假端点用 `http.server` 起在本地线程上，不出网、不花钱、不依赖真实凭据。

用法（后端目录）：
    python scripts/bench_assess_llm_healthy.py --loops 40
"""

from __future__ import annotations

import argparse
import http.server
import importlib
import json
import statistics
import threading
import time
from collections.abc import Callable
from typing import Any

from aegis.domain.assess import AssessEvidence, AssessRequest, FactorObservation
from aegis.domain.enums import HazardType, RiskLevel

SITES = 8
FACTORS = (
    "catchment_area_km2",
    "channel_gradient",
    "sediment_supply_index",
    "rainfall_mm_h",
    "snow_depth_cm",
    "rain_10min",
)


# ============================================================
# 本地假 OpenAI 兼容端点
# ============================================================


class _Handler(http.server.BaseHTTPRequestHandler):
    """只实现 chat/completions 的一条最小路径：认 tool_calls，回一个合法工具调用。

    刻意做成"每次请求都要真握手、真解析 body、真回 JSON"——这样客户端复用
    省下的连接建立开销才会真实地体现在时延里。

    **必须用 HTTP/1.1**：`BaseHTTPRequestHandler` 默认 HTTP/1.0，每个响应后就断连，
    keep-alive 根本不生效——那样量出来的"客户端复用"永远没有收益（量的是噪声）。
    HTTP/1.1 + 正确的 Content-Length 才会让 httpx 的连接池真正复用连接。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _arguments_for(self, tool: dict[str, Any]) -> str:
        """按请求里给的 JSON Schema 造一份合法参数。

        不这么做的话，不同节点（研判说明 / 研判验证）各自的 schema 字段不同，
        假端点回固定字段会被 pydantic 拒掉——那测的就不是"LLM 正常返回"这条路了。

        布尔与枚举字段按"让链路走完"的方向取值：研判验证节点的 `is_valid`
        必须为真，否则输出节点判 `uncertain`，`run_risk_agent` 直接抛
        `AssessRejected`——那样量到的就不是"LLM 正常返回"这条路了。
        """
        schema = (tool.get("function") or {}).get("parameters") or {}
        properties: dict[str, Any] = schema.get("properties") or {}
        required: list[str] = list(schema.get("required") or [])
        values: dict[str, Any] = {}
        for name, spec in properties.items():
            kind = (spec or {}).get("type")
            if kind == "boolean":
                values[name] = name in ("is_valid", "evidence_sufficient", "consistency_ok")
            elif kind == "integer" or kind == "number":
                values[name] = 0
            elif kind == "array":
                values[name] = ["本地假端点"]
            elif name == "recommendation":
                values[name] = "proceed"
            else:
                values[name] = "本地假端点返回的占位说明（仅用于时延测量）。"
        for name in required:
            values.setdefault(name, "本地假端点返回的占位说明（仅用于时延测量）。")
        return json.dumps(values, ensure_ascii=False)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        tool = (request.get("tools") or [{}])[0]
        name = (tool.get("function") or {}).get("name") or "answer"
        body = json.dumps(
            {
                "id": "chatcmpl-bench",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model", "bench"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_bench",
                                    "type": "function",
                                    "function": {"name": name, "arguments": self._arguments_for(tool)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server() -> tuple[str, http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/v1", server


# ============================================================
# 负载
# ============================================================


def _request() -> AssessRequest:
    observations = [
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
        for site in range(SITES)
        for index, factor in enumerate(FACTORS)
    ]
    evidence = AssessEvidence(
        observations=observations,
        region_code="540121",
        window="1h",
        generated_at="2026-11-10T00:00:00Z",
    )
    return AssessRequest(
        region_code="540121",
        window="1h",
        hazard_type=HazardType.DEBRIS_FLOW,
        candidate_hazards=[HazardType.DEBRIS_FLOW],
        rule_level=RiskLevel.ORANGE,
        evidence=evidence,
    )


def _measure(label: str, call: Callable[[], Any], loops: int) -> tuple[str, float, float]:
    call()  # 预热（首次建图/编译不计入稳态）
    timings: list[float] = []
    for _ in range(loops):
        started = time.perf_counter()
        call()
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    median = statistics.median(timings)
    p90 = timings[int(len(timings) * 0.9)]
    print(f"{label:<44} 中位 {median:8.2f} ms   p90 {p90:8.2f} ms")
    return label, median, p90


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", type=int, default=40)
    args = parser.parse_args()

    base_url, server = _start_server()
    print(f"本地假端点：{base_url}")

    from aegis.agents_ext import llm as ext_llm
    from aegis.agents_ext.risk_agent import main as risk_main

    # ★ 必须拿**模块**，不是 `nodes/__init__.py` 导出的同名函数：
    #   `from ...nodes import risk_assess_node` 拿到的是函数对象，
    #   给它挂属性不会改到节点函数读的那个模块全局。
    #   节点里的 `get_risk_llm` 是 import 时绑定到**节点模块**的名字，
    #   所以要 patch 节点模块的全局。
    assess_module = importlib.import_module("aegis.agents_ext.risk_agent.nodes.risk_assess_node")
    validate_module = importlib.import_module("aegis.agents_ext.risk_agent.nodes.risk_validate_node")

    request = _request()
    evidence = request.evidence

    original_factory = ext_llm.get_llm
    cache: dict[str, Any] = {}

    def fresh_client() -> Any:
        """A/B 档：每次调用都新建 client（复刻改造前的行为）。"""
        client = ext_llm.ZhipuCompatChatOpenAI(
            model="glm-4-flash",
            temperature=0,
            timeout=60,
            api_key="bench-key",
            base_url=base_url,
            max_retries=2,  # SDK 默认：盲重试 2 次
        )
        return client

    def fresh_client_no_retry() -> Any:
        client = ext_llm.ZhipuCompatChatOpenAI(
            model="glm-4-flash",
            temperature=0,
            timeout=60,
            api_key="bench-key",
            base_url=base_url,
            max_retries=0,
        )
        return client

    def reused_client() -> Any:
        client = cache.get("client")
        if client is None:
            client = ext_llm.ZhipuCompatChatOpenAI(
                model="glm-4-flash",
                temperature=0,
                timeout=60,
                api_key="bench-key",
                base_url=base_url,
                max_retries=0,
            )
            cache["client"] = client
        return client

    def production_shape() -> Any:
        """改造后的线上形态：复用 client + 熔断（端点指向本地假上游，保持"健康"这一档）。

        注意这里不能用 `ext_llm.get_llm`：它读的是配置面里的**真实**智谱端点，
        那一档属于"上游坏了"，量的是熔断打开后的降级时延，
        由 `bench_assess_ext.py` 负责。这里要量的是"上游健康时"的时延。
        """
        return reused_client()

    def patch(factory: Callable[[], Any]) -> None:
        assess_module.get_risk_llm = factory  # type: ignore[attr-defined]
        validate_module.get_risk_llm = factory  # type: ignore[attr-defined]

    def run() -> Any:
        return risk_main.run_risk_agent(request, evidence)

    results = []
    try:
        patch(fresh_client)
        results.append(_measure("A 现状（每次新建 client + 盲重试 2）", run, args.loops))

        patch(fresh_client_no_retry)
        results.append(_measure("B 只关盲重试（每次新建 client）", run, args.loops))

        patch(reused_client)
        results.append(_measure("C 关重试 + 客户端复用", run, args.loops))

        patch(production_shape)
        results.append(_measure("D 改造后（复用 + 熔断，上游健康）", run, args.loops))
    finally:
        patch(original_factory)
        server.shutdown()

    baseline = results[0][1]
    print()
    for label, median, _p90 in results:
        print(f"{label:<44} 相对 A 加速 {baseline / median:6.2f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
