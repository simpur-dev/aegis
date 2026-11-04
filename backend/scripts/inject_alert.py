"""演练告警注入：验收"告警卡 2 秒内出现"与"演练隔离"的专用入口。

为什么需要脚本：真实告警要等一条真链路跑完才有，验收分级打断与确认闭环不能等天公作美。
脚本与 `/drill/run` 的区别：后者跑完整五段链路（慢、依赖智能体），这里直接广播一条
**已发布预警**的到达事件——测的是交互面，不是判级。

两种模式：
- 总线广播（默认）：连 NATS 发布 `platform.alert.<level>.<region>`，平台的告警订阅者与
  事件流各自收到——这是**完整实时路径**。memory 总线是进程内的，独立脚本发出去没人收，
  脚本会直接拒绝并提示改法，不假装注入成功。
- `--persist-only`：只写 `assistant_alerts` 台账（POSTGRES），不经过总线。用于验证台账
  与 `/alerts` 接口本身；此时前端的实时卡不会弹，靠轮询/对账出现。

默认 `--drill`（演练标识为真）：注入本来就该是演练，演练不升级、不响铃；
要验收"超时升级"这条线，才显式给 `--real`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from typing import Any

from aegis.bus import subjects
from aegis.config import get_settings
from aegis.domain.alert import now_iso
from aegis.domain.messages import WarningRecord, make_event
from aegis.integrations.channels import build_transport
from aegis.persistence.postgres import PostgresStore

_BROADCAST_SOURCE = "scripts.inject_alert"


def _new_warning_id() -> str:
    return f"wrn_{secrets.token_hex(10)}"


def _payload(*, warning_id: str, level: int, region_code: str, title: str, drill: bool) -> dict[str, Any]:
    # 形状与 `pipeline/stages.py::_broadcast` 逐键一致：告警落账只认这些键。
    return {
        "warning_id": warning_id,
        "risk_level": level,
        "regions": [region_code],
        "title": title,
        "drill": drill,
    }


async def _broadcast(level: int, region_code: str, warning_id: str, title: str, drill: bool) -> dict[str, Any]:
    settings = get_settings()
    if settings.bus_backend != "nats":
        return {
            "published": False,
            "reason": (
                f"bus_backend={settings.bus_backend} 是进程内总线，独立脚本发布的消息没有订阅者。"
                "部署用 NATS 时重跑即可；或用 --persist-only 直写台账。"
            ),
        }
    transport = build_transport(settings)
    await transport.connect()
    try:
        event = make_event(
            source=_BROADCAST_SOURCE,
            action="platform.broadcast",
            trace_id=f"trc_{secrets.token_hex(8)}",
            target=f"platform.inject_{level}",
            payload=_payload(warning_id=warning_id, level=level, region_code=region_code, title=title, drill=drill),
        )
        await transport.publish(subjects.alert(level, region_code), event)
    finally:
        await transport.close()
    return {"published": True, "subject": subjects.alert(level, region_code), "warning_id": warning_id}


async def _persist(level: int, region_code: str, warning_id: str, title: str, drill: bool, dsn: str | None) -> dict[str, Any]:
    store = PostgresStore(dsn=dsn or None)
    try:
        await store.connect()
        # warnings 行也补一条（released 态）：/alerts 对账时 LEFT JOIN 才有标题与灾种可取
        record = WarningRecord(
            warning_id=warning_id,
            event_id=f"evt_{secrets.token_hex(6)}",
            trace_id=f"trc_{secrets.token_hex(8)}",
            hazard_type="drill",
            region_codes=[region_code],
            risk_level=level,  # type: ignore[arg-type]
            title_zh=title,
            body_zh=f"（演练注入）{title}",
            released_at=now_iso(),
        )
        await store.warnings.put(record)
        alert, created = await store.raise_alert(
            alert_id=f"alt_{secrets.token_hex(8)}",
            warning_id=warning_id,
            level=level,
            region_code=region_code,
            drill=drill,
        )
    finally:
        await store.close()
    return {"persisted": True, "created": created, "alert": alert}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="注入一条演练/真实告警（验收用）")
    parser.add_argument("--level", type=int, default=1, choices=[1, 2, 3, 4], help="1=Ⅰ … 4=Ⅳ，数值越小越严重")
    parser.add_argument("--region", default="540121", help="区域代码（6..24 位大写字母或数字）")
    parser.add_argument("--warning-id", default=None, help="复用已有 wrn_ 编号（缺省生成一条新的）")
    parser.add_argument("--title", default="演练：沟道泥位快速抬升", help="告警标题")
    parser.add_argument("--real", action="store_true", help="按真实告警注入（参与升级）；缺省按演练处理")
    parser.add_argument("--persist-only", action="store_true", help="只写台账，不走总线（前实时卡不弹）")
    parser.add_argument("--dsn", default=None, help="覆盖 AEGIS_PG_DSN（--persist-only 用）")
    args = parser.parse_args(argv)
    warning_id = args.warning_id or _new_warning_id()
    drill = not args.real
    try:
        result: dict[str, Any] = {"warning_id": warning_id, "level": args.level, "region": args.region, "drill": drill}
        if args.persist_only:
            persisted = asyncio.run(_persist(args.level, args.region, warning_id, args.title, drill, args.dsn))
            result.update(persisted)
        else:
            result.update(asyncio.run(_broadcast(args.level, args.region, warning_id, args.title, drill)))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
