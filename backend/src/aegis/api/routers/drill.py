"""写侧三入口：演练触发（/drill/run）、人工上报（/reports）、推演沙盒（/simulation/run）。

整张 API 表里会改变平台状态的写接口就这一族（案例入库那条在 knowledge 面）；上报的路由只做出口翻译，判级与链路全在容器一处。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.messages import TelemetryReading


class DrillRequest(BaseModel):
    scenario: str = Field(default="surge", pattern="^(surge|normal)$")
    ticks: int = Field(default=2, ge=1, le=50)
    region_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{6,24}$")


class SimulationRequest(BaseModel):
    """泥石流次生灾害推演入参：情景由调用方给（画布/演练），不臆造锚定遥测的默认。"""

    rain_base_mm: float = Field(default=40.0, ge=0, le=500)
    rain_spread_mm: float = Field(default=25.0, ge=0, le=200)
    scenarios: int = Field(default=8, ge=1, le=64)
    rounds: int = Field(default=5, ge=1, le=24)
    playbook: str = Field(default="转移", pattern="^(转移|封路|不行动)$")
    token_budget: int = Field(default=200_000, ge=1, le=10_000_000)
    seed: int = Field(default=20261011)


class ReportIn(BaseModel):
    """群防群治/巡查上报（Web 表单）。文本由平台结构化后进规则引擎。"""

    reporter: str = Field(min_length=2, max_length=64)
    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    hazard_hint: str | None = Field(default=None, max_length=64)
    note: str = Field(min_length=4, max_length=2_000)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("reporter", "region_code", "hazard_hint", "note", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        """先裁首尾空白再判长度。

        不裁的话 `"    "`（4 个空格）能过 `min_length=4`，一路走到解析腿才被
        `semantic_parser.parse` 的 `ValueError("待解析文本为空")` 拦下——那是个裸 ValueError，
        FastAPI 不认，出口就成了 **HTTP 500 + Internal Server Error**。真机在表单里敲四个空格
        就是这么撞的：现场看到的是一句"服务器错误"，而事实是"你没写正文"。
        """
        return value.strip() if isinstance(value, str) else value


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/drill/run", tags=["drill"])
    async def run_drill(
        payload: DrillRequest,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """演练：注入一轮模拟监测数据并跑通全链路，返回链路结果与指标快照。"""
        if ctn.simulator is None:
            raise HTTPException(status_code=503, detail="模拟器未启用")
        ctn.simulator.scenario = payload.scenario
        report = await ctn.ingest.ingest_once()
        readings = ctn.store.telemetry.query(region_code=payload.region_code, limit=1_000)
        if not readings:
            return {"ingest": report.as_dict(), "chains": [], "note": "本轮无可用读数"}

        grouped: dict[str, list[TelemetryReading]] = {}
        for reading in readings:
            grouped.setdefault(reading.region_code, []).append(reading)
        results = await ctn.chain.process_many(grouped)
        return {
            "ingest": report.as_dict(),
            "chains": [r.as_dict() for r in results],
            "regions": len(grouped),
        }

    @router.post("/api/v1/reports", tags=["ingest"], responses={503: {"description": "解析服务未装配"}})
    async def submit_report(
        payload: ReportIn,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """群防群治人工上报：文本 → 三路融合解析 → **与监测事件同一条链路**。

        路由只做出口翻译。解析、定级、预警生成、触达与量测全在
        `PlatformContainer.submit_report` 一处——助手页"帮我上报"这里也调同一个入口，
        两条路径给出不同等级就是第二个"预警准确率"口径。

        上报坐标不改判据，只进证据链：`location` 原样回显，并作为一条 evidence 挂在触发命中上。
        """
        if ctn.parser is None:
            raise HTTPException(status_code=503, detail={"code": "E_PARSER_UNAVAILABLE", "message": "任务解析服务未装配"})
        location = None if payload.lat is None or payload.lon is None else (payload.lon, payload.lat)
        outcome = await ctn.submit_report(
            note=payload.note,
            region_code=payload.region_code,
            reporter=payload.reporter,
            hazard_hint=payload.hazard_hint,
            location=location,
        )
        return {"report": {"reporter": payload.reporter, "region_code": payload.region_code, "location": location}, **outcome}

    @router.post("/api/v1/simulation/run", tags=["simulation"])
    async def simulation_run(payload: SimulationRequest, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """泥石流次生灾害推演沙盒（§3.6/§4.4）：确定性数值模型 → 概率矩阵/预案对比/时间线。

        演示/演练口径，不进任何 SLA 热路径；情景参数由调用方给定（画布/演练），概率/缓解系数为未实测初值，
        只保证模型单调与可复现，不声称现场达标。
        角色档写在 `roles.policy` 里（`rule` 或 `llm`）：默认是 `rule`，开 LLM 要
        `AEGIS_SIMULATION_ROLES_LLM=true` 且 LLM 腿在位——一次 T=5 的推演要点 20 次模型，
        这钱该由部署方按演练需求决定，不由平台替谁花。
        """
        from aegis.services.simulation import SimConfig, make_sandbox

        cfg = SimConfig(
            scenarios=payload.scenarios,
            rounds=payload.rounds,
            rain_base_mm=payload.rain_base_mm,
            rain_spread_mm=payload.rain_spread_mm,
            playbook=payload.playbook,
            token_budget=payload.token_budget,
            seed=payload.seed,
        )
        out = (await make_sandbox(ctn.settings, llm_gateway=ctn.llm_gateway).run(cfg)).as_dict()
        out["mode"] = "demo_sandbox"
        out["note"] = "概率/暴露/缓解系数为未实测初值；情景参数由调用方给定，不声称现场达标。"
        return out

    return router
