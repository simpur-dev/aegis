"""灾情文本 → 结构化任务的三路融合解析（规则 / 检索佐证 / LLM 裁决）。

对应《课题6_项目架构设计》§三 L4「任务智能解析(规则+检索+LLM三路)」与《课题6_完善计划》批次 C1。
三条腿的权责是固定的，不是「谁算得快听谁的」：

- **规则腿是唯一判据来源**：文本里的数量先换算成本平台口径的遥测量（`TelemetryReading`），
  再交给 `services/trigger_rules.py` 的同一张规则表与 `RiskEngine` 定级——阈值不在这里复制第二份
  （架构铁律 4：口径只有一处）。文本关键词与颜色词只是规则腿的另外两路线索（灾种、申报等级）。
- **检索佐证腿只加置信与出处**，不单独把等级抬起来；命中为空算常态（默认形态没有图谱与向量库）。
- **LLM 裁决腿只提建议**：逐字段过白名单（灾种取枚举、等级取 1..5、置信夹到 0..1），
  越界即弃用并留痕，不做静默兜底。
- **冲突消解顺序固定：规则 > 检索佐证 > LLM**，三腿的原始结论一律进 `legs`，
  报表与交底书要能回答「每一路各自说了什么」。

内核环不得 import 可选腿（架构铁律 3）：检索/知识/LLM 三侧能力一律以 Protocol 注入，构造点在 `container.py`。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, TriggerHit
from aegis.services.risk_engine import RiskEngine, RiskVerdict
from aegis.services.trigger_rules import RuleEngine

log = logging.getLogger("aegis.services.semantic_parser")

LEG_RULE = "rule"
LEG_RETRIEVAL = "retrieval"
LEG_LLM = "llm"
#: 规则腿里"阈值未命中、但上报人白纸黑字写了颜色/等级"的那一路：仍属规则腿（确定性词表），
#: 但它是申报值而非测量值，置信必须低于阈值命中，否则一条"我要红色预警"就能顶替一次读数。
LEG_RULE_DECLARED = "rule_declared"

#: 无阈值命中时的置信上限：只有文本线索、申报等级或 LLM 建议时不允许给出高置信结论。
CEILING_WITHOUT_RULE = 0.55
#: 低于该置信度就转人工核签（架构文档 §6.2 场景二：「低置信度转人工核签」）。
DEFAULT_REVIEW_CONFIDENCE = 0.6

_HAZARD_KEYWORDS: tuple[tuple[HazardType, tuple[str, ...]], ...] = (
    (HazardType.DEBRIS_FLOW, ("泥石流", "泥位", "泥流", "沟道", "沟口", "稀性固体径流")),
    (HazardType.LAKE_OUTBURST, ("冰湖", "冰碛", "溃决", "溃口", "湖水", "湖盆", "坝体", "渗流")),
    (HazardType.AVALANCHE, ("雪崩", "积雪", "新雪", "雪层", "吹雪", "雪水当量", "弱层")),
    (HazardType.ROCKFALL, ("崩塌", "危岩", "落石", "滚石", "石块", "塌方", "坍塌")),
    (HazardType.LANDSLIDE, ("滑坡", "滑塌", "溜塌", "滑动变形", "滑移", "坡体", "边坡", "鼓胀", "错动", "裂缝")),
    (HazardType.QUAKE_TRIGGERED, ("地震", "余震", "震感", "烈度")),
)

_LEVEL_KEYWORDS: tuple[tuple[RiskLevel, tuple[str, ...]], ...] = (
    (RiskLevel.RED, ("红色", "Ⅰ级", "I级", "一级", "特大")),
    (RiskLevel.ORANGE, ("橙色", "Ⅱ级", "II级", "二级", "重大")),
    (RiskLevel.YELLOW, ("黄色", "Ⅲ级", "III级", "三级", "较大")),
    (RiskLevel.BLUE, ("蓝色", "Ⅳ级", "IV级", "四级", "一般")),
)

#: 数字 + 单位：长单位排前面，否则「毫米」会被「米」抢走一次匹配。
_UNIT_ALTERNATIVES = (
    "米/秒",
    "米每秒",
    "m/s",
    "毫米",
    "厘米",
    "立方米",
    "公里",
    "摄氏度",
    "NTU",
    "ntu",
    "℃",
    "°C",
    "km",
    "mm",
    "cm",
    "米",
    "度",
    "次",
    "m",
)
_QUANTITY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(" + "|".join(_UNIT_ALTERNATIVES) + r")")
#: 区划码要带上下文，或者是一条独立的 6 位数字且后面不接单位：
#: 「海拔540121米」「东经540121」这类裸数字被当成区划码，会把整条上报送错区。
_REGION_CODED_RE = re.compile(r"(?:区划|区划码|行政编码|区域码|region_code)\D{0,4}(\d{6})(?!\d)")
_REGION_BARE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)(?!\s*(?:米|毫米|厘米|公里|万元|度))")
_STANDALONE_LEVEL_RE = re.compile(r"(?<![\dA-Za-z])([1-5])\s*级")
_THREAT_RE = re.compile(r"(\d+)\s*(人|户)")


@dataclass(frozen=True, slots=True)
class LegFinding:
    """一条腿的原始结论：冲突消解之后仍完整保留，取证时要能回答「各自说了什么」。"""

    leg: str
    hazard_type: HazardType | None = None
    risk_level: RiskLevel | None = None
    confidence: float = 0.0
    rationale: str = ""
    refs: tuple[str, ...] = ()
    used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "leg": self.leg,
            "hazard_type": None if self.hazard_type is None else self.hazard_type.value,
            "risk_level": None if self.risk_level is None else int(self.risk_level),
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale[:400],
            "refs": list(self.refs)[:12],
            "used": self.used,
        }


@dataclass(frozen=True, slots=True)
class EvidenceNote:
    """检索/知识侧回给解析器的一条佐证。字段由装配点适配器填充，解析器不认识 CaseMatch 与检索文档。"""

    source: str
    text: str = ""
    hazard_type: str | None = None
    typical_level: int | None = None
    refs: tuple[str, ...] = ()


class EvidenceProvider(Protocol):
    """佐证腿能力面。装配点用知识层 + 检索层实现它；缺位时解析器只走规则 + LLM。"""

    async def corroborate(self, *, query: str, hazard_type: str | None, region_code: str | None, limit: int) -> Sequence[EvidenceNote]: ...


class JsonLlm(Protocol):
    """LLM 能力面（`LlmGateway` 结构上即满足）：只暴露一次 JSON 问答，解析器不关心供应商。"""

    @property
    def available(self) -> bool: ...

    async def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ParsedDisaster:
    """三路融合的产物：既能直接进链路（`hits`/`to_verdict`），也能整份外显（`as_dict`）。"""

    text: str
    hazard_type: HazardType
    region_code: str
    risk_level: RiskLevel | None
    confidence: float
    decided_by: str
    entities: tuple[str, ...] = ()
    metrics: tuple[tuple[str, float, str], ...] = ()
    hits: tuple[TriggerHit, ...] = ()
    legs: tuple[LegFinding, ...] = ()
    conflicts: tuple[str, ...] = ()
    degradations: tuple[str, ...] = ()
    readings: tuple[TelemetryReading, ...] = ()
    evidence: tuple[EvidenceNote, ...] = ()
    needs_review: bool = False
    reported_people: int | None = None

    @property
    def acted(self) -> bool:
        return self.risk_level is not None and self.risk_level is not RiskLevel.NONE

    @property
    def measured_by_rule(self) -> bool:
        """等级是否来自阈值命中（而不是申报值、佐证或 LLM 建议）——评测集与报表要用这个口径。"""
        return self.decided_by == LEG_RULE

    def to_verdict(self, *, assessed_by: str = "platform.semantic_parser") -> RiskVerdict | None:
        if self.risk_level is None:
            return None
        rationale = "；".join(finding.rationale for finding in self.legs if finding.rationale) or "无文字依据"
        return RiskVerdict(
            hazard_type=self.hazard_type,
            region_code=self.region_code,
            risk_level=self.risk_level,
            confidence=self.confidence,
            rationale=rationale[:1_000],
            hits=list(self.hits),
            assessed_by=assessed_by,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:2_000],
            "hazard_type": self.hazard_type.value,
            "region_code": self.region_code,
            "risk_level": None if self.risk_level is None else int(self.risk_level),
            "confidence": round(self.confidence, 4),
            "decided_by": self.decided_by,
            "needs_review": self.needs_review,
            "entities": list(self.entities),
            "metrics": [{"metric": metric, "value": value, "unit": unit} for metric, value, unit in self.metrics],
            "reported_people": self.reported_people,
            "trigger_hits": [hit.model_dump() for hit in self.hits],
            "legs": [finding.as_dict() for finding in self.legs],
            "conflicts": list(self.conflicts),
            "degradations": list(self.degradations),
            "evidence": [
                {"source": note.source, "hazard_type": note.hazard_type, "typical_level": note.typical_level} for note in self.evidence
            ],
        }


@dataclass(frozen=True, slots=True)
class _RuleSignal:
    hazard_type: HazardType | None = None
    #: 阈值命中得到的等级（测量值）
    risk_level: RiskLevel | None = None
    #: 上报人写明的颜色/等级（申报值），只在阈值未命中时参与定级
    declared_level: RiskLevel | None = None
    confidence: float = 0.0
    rationale: str = ""
    hits: tuple[TriggerHit, ...] = ()
    readings: tuple[TelemetryReading, ...] = ()
    metrics: tuple[tuple[str, float, str], ...] = ()
    entities: tuple[str, ...] = ()
    reported_people: int | None = None


def extract_metrics(text: str) -> list[tuple[str, float, str]]:
    """从自由文本里取「数字 + 单位 + 最近的指标关键词」的读数。

    距离口径是这里的真正判据：只看窗口里有没有关键词，会让「裂缝宽度 25 毫米，位移 30 mm」
    把 30 也记成裂缝宽度（25 与 30 都在同一个 16 字窗口里）。找不到近邻关键词就不产出读数——
    宁缺毋滥，多一条假读数就是多一条假证据。同一指标出现多次取最大值：预警要的是最不利读数。
    """
    found: list[tuple[str, float, str]] = []
    for match in _QUANTITY_RE.finditer(text):
        try:
            raw = float(match.group(1))
        except ValueError:  # pragma: no cover - 正则已限定数字
            continue
        unit = match.group(2)
        # 距离以"数字本体"为准，不含单位：用整段匹配的末端会把紧跟在"毫米"后面的
        # 下一个指标的关键词算成这条数字的上下文（"裂缝宽度 25 毫米，位移 30 mm" 会被并成一条）。
        for pattern in _nearest_patterns(text, match.start(1), match.end(1)):
            value = pattern.convert(raw, unit)
            if value is not None:
                found.append((pattern.metric, round(value, 4), pattern.unit))
    deduped: dict[str, tuple[str, float, str]] = {}
    for metric, value, unit in found:
        if metric not in deduped or value > deduped[metric][1]:
            deduped[metric] = (metric, value, unit)
    return [deduped[metric] for metric in sorted(deduped)]


def _nearest_patterns(text: str, start: int, end: int) -> list[_MetricPattern]:
    """返回与该数字距离最近的全部指标（并列时同时保留，让"降雨量与雨量"这类同义不丢）。"""
    scored: list[tuple[int, _MetricPattern]] = []
    low, high = max(0, start - 60), min(len(text), end + 30)
    for keyword, pattern in _KEYWORD_INDEX:
        for found in re.finditer(re.escape(keyword), text[low:high]):
            k_start, k_end = low + found.start(), low + found.end()
            if k_end <= start:
                gap = start - k_end
            elif k_start >= end:
                gap = k_start - end
            else:
                continue  # 关键词与数字重叠：这条不算上下文（如"25毫米"里的数字被词表包住）
            if gap <= _METRIC_MAX_GAP:
                scored.append((gap, pattern))
    if not scored:
        return []
    nearest = min(gap for gap, _pattern in scored)
    picked: list[_MetricPattern] = []
    for gap, pattern in scored:
        # 用列表去重而不是 set：`_MetricPattern` 带 factors 字典，不可哈希。
        if gap == nearest and pattern not in picked:
            picked.append(pattern)
    return picked


def detect_hazard(text: str, *, hint: str | None = None) -> HazardType | None:
    """灾种线索：显式 hint 优先，其次取文本中最早出现的灾种词。

    「最早出现」是有意为之的稳定口径：上报人通常先说现象（「泥石流」）再补背景，
    按命中词数取胜会让「历史上曾因雪崩」盖过本次实际灾种。
    """
    if hint:
        candidate = hint.strip().lower()
        if candidate in {item.value for item in HazardType}:
            return HazardType(candidate)
    positions = [(text.find(keyword), hazard) for hazard, keywords in _HAZARD_KEYWORDS for keyword in keywords if keyword in text]
    if not positions:
        return None
    positions.sort(key=lambda item: (item[0], -len(item[1].value)))
    return positions[0][1]


def detect_level(text: str) -> RiskLevel | None:
    """文本里写明的等级（颜色词或「N 级」）。这是申报值，不是测量值。"""
    for level, keywords in _LEVEL_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return level
    match = _STANDALONE_LEVEL_RE.search(text)
    return RiskLevel(int(match.group(1))) if match else None


def extract_region(text: str, *, default: str | None = None) -> str | None:
    if match := _REGION_CODED_RE.search(text):
        return match.group(1)
    if match := _REGION_BARE_RE.search(text):
        return match.group(1)
    return default


@dataclass(frozen=True, slots=True)
class _MetricPattern:
    """一个平台指标在灾情文本里的表达方式：触发关键词 + 单位换算表 + 物理量程。"""

    metric: str
    unit: str
    keywords: tuple[str, ...]
    factors: dict[str, float]
    #: 量程上限：越界就是绑错了对象（"海拔 4200 米"离"沟道"只有 4 个字，
    #: 但泥位不可能 4200 米）。与 `observed_at` 落在未来即拒同一类纪律：假读数不进判据。
    maximum: float = 1e9

    def convert(self, raw: float, unit: str) -> float | None:
        factor = self.factors.get(unit)
        if factor is None:
            return None
        value = raw * factor
        return None if value > self.maximum else value


#: 指标名与 `trigger_rules.default_rulebook()` 用到的指标逐一对齐：口径只有一份，
#: 所以这里只写「文本 → 读数」的翻译表，不写任何阈值。
_METRIC_PATTERNS: tuple[_MetricPattern, ...] = (
    _MetricPattern("rain_10min", "mm", ("10分钟降雨", "短时强降雨", "雨强", "小时降雨", "强降雨"), {"毫米": 1.0, "mm": 1.0}, maximum=300.0),
    _MetricPattern(
        "rain_cumulative_24h",
        "mm",
        ("累计降雨", "累计雨量", "过程降雨", "24小时降雨", "日雨量", "降雨量", "雨量"),
        {"毫米": 1.0, "mm": 1.0, "厘米": 10.0},
        maximum=1_500.0,
    ),
    _MetricPattern(
        "debris_level", "m", ("泥位", "沟道", "泥石流堆积", "泥深"), {"米": 1.0, "m": 1.0, "厘米": 0.01, "cm": 0.01}, maximum=30.0
    ),
    _MetricPattern(
        "displacement_mm",
        "mm",
        ("位移", "滑距", "错距", "变形量"),
        {"毫米": 1.0, "mm": 1.0, "厘米": 10.0, "cm": 10.0, "米": 1_000.0, "m": 1_000.0},
        maximum=20_000.0,
    ),
    _MetricPattern(
        "crack_aperture_mm",
        "mm",
        ("裂缝", "张裂隙", "拉裂缝", "裂隙", "错台"),
        {"毫米": 1.0, "mm": 1.0, "厘米": 10.0, "cm": 10.0, "米": 1_000.0, "m": 1_000.0},
        maximum=5_000.0,
    ),
    _MetricPattern(
        "new_snow_cm",
        "cm",
        ("新雪", "积雪", "降雪", "雪深"),
        {"厘米": 1.0, "cm": 1.0, "米": 100.0, "m": 100.0, "毫米": 0.1, "mm": 0.1},
        maximum=500.0,
    ),
    _MetricPattern("snow_water_equivalent_mm", "mm", ("雪水当量", "含水量"), {"毫米": 1.0, "mm": 1.0, "厘米": 10.0}, maximum=2_000.0),
    _MetricPattern("lake_level_m", "m", ("湖水位", "水位", "湖面", "壅水"), {"米": 1.0, "m": 1.0, "厘米": 0.01, "cm": 0.01}, maximum=200.0),
    _MetricPattern(
        "dam_seepage_turbidity_ntu", "NTU", ("浊度", "渗流浑浊", "渗漏", "管涌"), {"NTU": 1.0, "ntu": 1.0, "度": 1.0}, maximum=10_000.0
    ),
    _MetricPattern("wind_speed_ms", "m/s", ("风速", "阵风", "大风"), {"米/秒": 1.0, "米每秒": 1.0, "m/s": 1.0}, maximum=120.0),
    _MetricPattern("air_temperature_c", "℃", ("气温", "温度", "升温"), {"℃": 1.0, "°C": 1.0, "摄氏度": 1.0, "度": 1.0}, maximum=60.0),
    _MetricPattern("freeze_thaw_cycles", "次", ("冻融", "冻融循环"), {"次": 1.0}, maximum=366.0),
)

#: 数字与关键词的最大绑定距离（字符）。
#: 12 而不是 6：真实上报常带时长插入语（"冰湖水位 6 小时抬升 0.8 米"），
#: 6 字窗口会把这条水位的归属丢掉；跨指标串味由"最近优先"规则挡住，不靠收窄窗口。
_METRIC_MAX_GAP = 12
_KEYWORD_INDEX: tuple[tuple[str, _MetricPattern], ...] = tuple(
    (keyword, pattern) for pattern in _METRIC_PATTERNS for keyword in pattern.keywords
)


class DisasterTextParser:
    """三路融合解析器。纯计算 + 注入能力，自己不做任何 I/O。"""

    def __init__(
        self,
        *,
        rule_engine: RuleEngine | None = None,
        risk_engine: RiskEngine | None = None,
        evidence: EvidenceProvider | None = None,
        llm: JsonLlm | None = None,
        review_confidence: float = DEFAULT_REVIEW_CONFIDENCE,
        evidence_limit: int = 5,
    ) -> None:
        self._rules = rule_engine or RuleEngine()
        self._risk = risk_engine or RiskEngine(self._rules)
        self._evidence = evidence
        self._llm = llm
        self._review_confidence = review_confidence
        self._evidence_limit = evidence_limit

    @property
    def evidence_enabled(self) -> bool:
        return self._evidence is not None

    @property
    def llm_enabled(self) -> bool:
        return self._llm is not None and self._llm.available

    async def parse(self, text: str, *, region_code: str | None = None, hazard_hint: str | None = None) -> ParsedDisaster:
        body = str(text or "").strip()
        if not body:
            raise ValueError("待解析文本为空")
        region = extract_region(body, default=region_code)
        rule_signal = self._rule_leg(body, region=region, hazard_hint=hazard_hint)
        notes, retrieval = await self._retrieval_leg(body, rule_signal, region=region)
        llm = await self._llm_leg(body, region=region)
        return self._fuse(body, region=region, signal=rule_signal, notes=notes, retrieval=retrieval, llm=llm)

    # ---------- 规则腿 ----------

    def _rule_leg(self, text: str, *, region: str | None, hazard_hint: str | None) -> _RuleSignal:
        hazard = detect_hazard(text, hint=hazard_hint)
        declared = detect_level(text)
        metrics = extract_metrics(text)
        threats = _THREAT_RE.findall(text)
        people = sum(int(count) for count, _unit in threats) if threats else None
        entities = tuple(f"{metric}={value}{unit}" for metric, value, unit in metrics)

        def signal(
            *,
            risk_level: RiskLevel | None = None,
            confidence: float = 0.0,
            rationale: str,
            hits: tuple[TriggerHit, ...] = (),
            readings: tuple[TelemetryReading, ...] = (),
        ) -> _RuleSignal:
            return _RuleSignal(
                hazard_type=hazard,
                risk_level=risk_level,
                declared_level=declared,
                confidence=confidence,
                rationale=rationale,
                hits=hits,
                readings=readings,
                metrics=tuple(metrics),
                entities=entities,
                reported_people=people,
            )

        if not region:
            return signal(rationale="文本未给区划码，规则腿不判定")
        if not metrics:
            return signal(rationale=f"无可量化读数（灾种线索 {hazard.cn if hazard else '未识别'}）")

        readings = tuple(
            TelemetryReading(
                station_id=f"RPT-{region}",
                metric=metric,
                value=value,
                unit=unit,
                region_code=region,
                source="report",
            )
            for metric, value, unit in metrics
        )
        hits = tuple(self._rules.evaluate(list(readings), region_code=region).hits)
        if not hits:
            return signal(rationale=f"{len(metrics)} 项读数均未达规则阈值", readings=readings)

        verdict = self._risk.assess(list(hits), region_code=region)
        if verdict is None:  # pragma: no cover - 有命中即有结论，仅防口径变更
            return signal(rationale="规则命中但定级未产出", hits=hits, readings=readings)
        # 阈值证据比关键词更强：两者指向不同灾种时以阈值证据为准，分歧本身另外留痕。
        return signal(
            risk_level=verdict.risk_level,
            confidence=verdict.confidence,
            rationale="；".join(f"[{hit.rule_id}] {e}" for hit in hits for e in hit.evidence_refs[:2]) or f"{len(hits)} 条规则命中",
            hits=hits,
            readings=readings,
        )

    # ---------- 检索佐证腿 ----------

    async def _retrieval_leg(self, text: str, signal: _RuleSignal, *, region: str | None) -> tuple[tuple[EvidenceNote, ...], LegFinding]:
        if self._evidence is None:
            return (), LegFinding(LEG_RETRIEVAL, rationale="佐证腿未装配（默认形态无图谱/向量库）")
        query = f"{signal.hazard_type.cn if signal.hazard_type else ''} {region or ''} {text[:180]}".strip()
        try:
            notes = tuple(
                await self._evidence.corroborate(
                    query=query,
                    hazard_type=None if signal.hazard_type is None else signal.hazard_type.value,
                    region_code=region,
                    limit=self._evidence_limit,
                )
            )
        except Exception as exc:  # 佐证是增强腿：任何异常都不许把上报入口拖下水
            log.warning("灾情解析佐证降级", extra={"err": type(exc).__name__})
            return (), LegFinding(LEG_RETRIEVAL, rationale=f"佐证降级 {type(exc).__name__}")
        hazards = sorted({note.hazard_type for note in notes if note.hazard_type})
        rationale = f"佐证 {len(notes)} 条" + (f"，灾种分布 {hazards}" if hazards else "")
        refs = tuple(dict.fromkeys(ref for note in notes for ref in note.refs))
        return notes, LegFinding(LEG_RETRIEVAL, rationale=rationale, refs=refs)

    # ---------- LLM 裁决腿 ----------

    async def _llm_leg(self, text: str, *, region: str | None) -> LegFinding:
        if self._llm is None or not self._llm.available:
            return LegFinding(LEG_LLM, rationale="LLM 未配置，裁决腿缺席")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是山地灾害灾情要素抽取器。只输出 JSON 对象，键为 "
                    "hazard_type(landslide|rockfall|debris_flow|avalanche|lake_outburst|quake_triggered|unknown)、"
                    "risk_level(1..5 整数或 null)、confidence(0..1)、entities(字符串数组)、reason(一句话依据)。"
                    "不得新增文本里没有的事实、地点或数字。"
                ),
            },
            {"role": "user", "content": f"区划码：{region or '未给出'}\n灾情文本：{text[:1_000]}"},
        ]
        try:
            raw = await self._llm.chat_json(messages)
        except Exception as exc:
            log.warning("LLM 裁决腿降级", extra={"err": type(exc).__name__})
            return LegFinding(LEG_LLM, rationale=f"LLM 裁决降级 {type(exc).__name__}")

        entities = tuple(str(item)[:80] for item in raw.get("entities", []) if isinstance(item, (str, int, float)) and str(item).strip())[
            :12
        ]
        reason = str(raw.get("reason", ""))[:200]
        return LegFinding(
            LEG_LLM,
            hazard_type=_coerce_hazard(raw.get("hazard_type")),
            risk_level=_coerce_level(raw.get("risk_level")),
            confidence=_coerce_confidence(raw.get("confidence")),
            rationale=f"LLM {reason}" if reason else "LLM 已给建议",
            refs=tuple(f"llm:{item}" for item in entities),
        )

    # ---------- 融合与冲突消解 ----------

    def _fuse(
        self,
        text: str,
        *,
        region: str | None,
        signal: _RuleSignal,
        notes: tuple[EvidenceNote, ...],
        retrieval: LegFinding,
        llm: LegFinding,
    ) -> ParsedDisaster:
        conflicts: list[str] = []
        degradations: list[str] = []

        hazard = signal.hazard_type or llm.hazard_type or HazardType.UNKNOWN
        if signal.hazard_type is not None and llm.hazard_type is not None and llm.hazard_type is not signal.hazard_type:
            conflicts.append(f"灾种分歧：规则 {signal.hazard_type.cn} vs LLM {llm.hazard_type.cn}，按「规则 > LLM」取前者")
        if hazard is HazardType.UNKNOWN and llm.hazard_type is not None:
            hazard = llm.hazard_type

        same_hazard = [note for note in notes if note.hazard_type and note.hazard_type == hazard.value]
        corroborated = LegFinding(
            LEG_RETRIEVAL,
            hazard_type=hazard,
            rationale=retrieval.rationale + f"；同灾种佐证 {len(same_hazard)} 条",
            refs=(*retrieval.refs, *(note.source for note in same_hazard)),
        )

        # 定级优先级 = 冲突消解顺序：阈值命中 > 申报等级 > 检索典型等级 > LLM 建议。
        # 后三档都属"未测量"，置信一律压在 CEILING_WITHOUT_RULE 之下并转人工核签。
        if signal.risk_level is not None:
            level, decided_by = signal.risk_level, LEG_RULE
            confidence = min(0.99, signal.confidence + (0.05 if same_hazard else 0.0))
        elif signal.declared_level is not None:
            level, decided_by = signal.declared_level, LEG_RULE_DECLARED
            confidence = 0.5 + (0.05 if same_hazard else 0.0)
            degradations.append("等级来自上报人申报值而非阈值命中，转人工核签")
        elif (typical := _typical_level(same_hazard)) is not None:
            level, decided_by = RiskLevel(typical), LEG_RETRIEVAL
            confidence = 0.4 + 0.05 * len(same_hazard)
            degradations.append("等级来自历史案例典型等级，转人工核签")
        elif llm.risk_level is not None:
            level, decided_by = llm.risk_level, LEG_LLM
            confidence = max(0.2, llm.confidence) * 0.7
            degradations.append("等级仅由 LLM 建议得出，转人工核签")
        else:
            level, decided_by = None, "none"
            degradations.append("三路均未产出等级")

        if level is not None:
            level = RiskLevel(level)
            if llm.risk_level is not None and llm.risk_level is not level:
                conflicts.append(f"等级分歧：采纳 {decided_by} 的 {int(level)} 级 vs LLM {int(llm.risk_level)} 级")
        if decided_by == LEG_RULE:
            confidence = min(0.99, confidence)
        elif level is not None:
            confidence = min(CEILING_WITHOUT_RULE, confidence)
        else:
            confidence = 0.0

        # 「这条腿被采纳」在四条腿上必须是同一个意思：它的等级进了结论。
        # 原先检索腿标的是"有没有同灾种佐证"，LLM 腿压根没标过——于是页面上会出现
        # "采纳 检索佐证 ｜ 等级=未给出等级"（真机 wfi_1bacfe566685 那张回执里就是这样），
        # 而等级真的来自 LLM 建议时那条腿仍显示"未采纳"。读的人只能猜标签到底在说什么。
        corroborated = replace(corroborated, used=decided_by == LEG_RETRIEVAL)
        llm = replace(llm, used=decided_by == LEG_LLM)

        rule_finding = LegFinding(
            LEG_RULE,
            hazard_type=signal.hazard_type,
            risk_level=signal.risk_level,
            confidence=signal.confidence,
            rationale=signal.rationale,
            refs=tuple(hit.rule_id for hit in signal.hits),
            used=decided_by == LEG_RULE,
        )
        declared_finding = LegFinding(
            LEG_RULE_DECLARED,
            hazard_type=signal.hazard_type,
            risk_level=signal.declared_level,
            # 被采纳的那条腿要带着被采纳时的置信：原先这里永远是 0%，
            # 于是页面上写着"采纳 申报等级 ｜ 等级=红色 ｜ 置信=0%"，读的人只能当它是坏数。
            confidence=confidence if decided_by == LEG_RULE_DECLARED else 0.0,
            rationale="上报文本写明的颜色/等级（申报值）" if signal.declared_level is not None else "上报文本未写明等级",
            used=decided_by == LEG_RULE_DECLARED,
        )
        corroborated = replace(
            corroborated,
            confidence=confidence if decided_by == LEG_RETRIEVAL else corroborated.confidence,
        )

        return ParsedDisaster(
            text=text,
            hazard_type=hazard,
            region_code=region or "",
            risk_level=level,
            confidence=round(confidence, 4),
            decided_by=decided_by,
            entities=signal.entities,
            metrics=signal.metrics,
            hits=signal.hits,
            legs=(rule_finding, declared_finding, corroborated, llm),
            conflicts=tuple(conflicts),
            degradations=tuple(degradations),
            readings=signal.readings,
            evidence=notes,
            needs_review=level is None or decided_by != LEG_RULE or confidence < self._review_confidence,
            reported_people=signal.reported_people,
        )


def _typical_level(notes: Sequence[EvidenceNote]) -> int | None:
    levels = [note.typical_level for note in notes if note.typical_level is not None]
    # 取最不利（数字最小）的一条：佐证之间不一致时宁可保守。
    return None if not levels else min(levels)


def _coerce_hazard(raw: object) -> HazardType | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return HazardType(value) if value in {item.value for item in HazardType} else None


def _coerce_level(raw: object) -> RiskLevel | None:
    """等级白名单：LLM 说 3、"3"、"3 级"、3.0 都收；说"特大"或 7 一律丢回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    value: object = raw.strip().rstrip("级") if isinstance(raw, str) else raw
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return RiskLevel(int(float(value)))
    except (ValueError, TypeError):
        return None


def _coerce_confidence(raw: object) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


__all__ = [
    "CEILING_WITHOUT_RULE",
    "DEFAULT_REVIEW_CONFIDENCE",
    "LEG_LLM",
    "LEG_RETRIEVAL",
    "LEG_RULE",
    "LEG_RULE_DECLARED",
    "DisasterTextParser",
    "EvidenceNote",
    "EvidenceProvider",
    "LegFinding",
    "ParsedDisaster",
    "detect_hazard",
    "detect_level",
    "extract_metrics",
    "extract_region",
]
