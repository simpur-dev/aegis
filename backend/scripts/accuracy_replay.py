"""预警准确率回放（Postgres 侧）：真值标注入库 + 与已落库 `warnings` 配对算分。

用法（DSN 走环境或 --dsn，与部署形态同一个库）：

    # 1) 导入现场标注（按 case_id 幂等，可反复修）
    AEGIS_PG_DSN=postgresql://aegis:…@127.0.0.1:5432/aegis \\
        uv run python -m scripts.accuracy_replay --import-labels /data/labels_2026_10.jsonl

    # 2) 回放：真值来自 warning_truth_labels，预测来自 warnings
    uv run python -m scripts.accuracy_replay --from-store \\
        --since 2026-09-01T00:00:00Z --kind field --out reports/accuracy_replay.json

为什么单独一条命令而不塞进 `metrics_report`：后者的口径是"这次在线运行量到的时延与成功率"，
准确率来自历史标注与历史产出，两件事的扫描窗、数据源与可复现条件都不同，
混在一个报表里会让人以为它们出自同一次运行。

判定算术**不在这里也不在 SQL 里**：本脚本只取配对，TP/FP/FN/TN、比率、
"样本不足"与"能不能算官方达标"全部来自 `aegis.persistence.replay.measure`（唯一事实源）。
`--kind` 默认 `unspecified`，即"没说这份数据是现场标注"，此时官方准确率一栏恒为 None——
把没声明的数据当成现场标注来报 ≥80%，是这类报表最容易犯 also 最难发现的错。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aegis.persistence.accuracy import DEFAULT_WINDOW_SECONDS, read_label_file
from aegis.persistence.postgres import PostgresStore, apply_migrations
from aegis.persistence.replay import DatasetKind, ReplayDataset, measure
from aegis.storage.store import PlatformStore


def parse_moment(raw: str) -> datetime:
    """时间参数必须带时区：裸时间会被按本地时区解释，回放窗整体平移几小时看不出来。"""
    text = raw.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间参数不是合法 ISO 8601：{raw}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"时间参数必须带时区（如 …+00:00 或 …Z）：{raw}")
    return moment


async def run(
    *,
    dsn: str,
    import_labels: Path | None,
    from_store: bool,
    since: datetime | None,
    until: datetime | None,
    window_seconds: int,
    kind: str,
    region_code: str | None,
    target: float | None,
    out: Path | None,
) -> int:
    if not from_store and import_labels is None:
        raise ValueError("至少要给 --import-labels 或 --from-store 之一")
    store = PostgresStore(dsn=dsn, read_model=PlatformStore(), pool_min_size=1, pool_max_size=2)
    await store.connect()
    applied = await apply_migrations(store.require_pool())
    print(f"[迁移] 本次实际应用 {len(applied)} 个文件：{applied or '（无待应用）'}", file=sys.stderr)

    imported: int | None = None
    if import_labels is not None:
        rows, file_kind, source, note = read_label_file(import_labels)
        imported = await store.put_warning_labels(rows)
        print(
            f"[导入] {imported} 条真值标注（kind={file_kind}，source={source}）" + (f"，备注：{note}" if note else ""),
            file=sys.stderr,
        )

    if not from_store:
        return 0
    if since is None:
        raise ValueError("--from-store 需要 --since")

    cases = await store.accuracy_replay_cases(since=since, until=until, window_seconds=window_seconds, region_code=region_code)
    report = measure(
        ReplayDataset(
            cases=tuple(cases),
            # argparse 的 choices 已经把取值限定在三个合法字面量里，这里只是把类型交给 mypy
            kind=cast("DatasetKind", kind),
            source=f"postgres:warning_truth_labels×warnings since={since.isoformat()}",
            note=f"配对窗 {window_seconds}s；预测只认观测时刻之后发出的预警",
        ),
        target=target,
    )
    payload: dict[str, Any] = {
        "imported_labels": imported,
        "cases_read": len(cases),
        "window_seconds": window_seconds,
        "report": report.as_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[已写入] {out}", file=sys.stderr)
    await store.close()
    # 达标与否决定退出码：这条命令因此能当回放门禁用，而不是"跑出一份没人看的 JSON"。
    # 未量测（官方准确率为 None，含样本不足与非现场标注）判 0：那不是失败，是没有证据。
    return 0 if report.official_accuracy is None or report.indicator == "met" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 预警准确率回放（Postgres 侧）")
    parser.add_argument("--dsn", default=None, help="缺省读 AEGIS_PG_DSN")
    parser.add_argument("--import-labels", type=Path, default=None, help="导入现场标注 JSONL（按 case_id 幂等）")
    parser.add_argument("--from-store", action="store_true", help="从库里配对并出报表")
    parser.add_argument("--since", default=None, help="回放窗起点（ISO 8601，必须带时区）")
    parser.add_argument("--until", default=None, help="回放窗终点，缺省为现在")
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS, help="真值之后多久内的预警算作产出")
    parser.add_argument("--region-code", default=None)
    parser.add_argument("--kind", choices=("field", "synthetic", "unspecified"), default="unspecified")
    parser.add_argument("--accuracy-target", type=float, default=None, help="覆盖配置里的准确率阈值 (0,1]")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    dsn = (args.dsn or os.environ.get("AEGIS_PG_DSN") or "").strip()
    if not dsn:
        parser.error("需要 --dsn 或 AEGIS_PG_DSN（准确率回放的预测侧来自落库 warnings）")
    try:
        since = None if args.since is None else parse_moment(str(args.since))
        until = None if args.until is None else parse_moment(str(args.until))
        if args.from_store and since is None:
            raise ValueError("--from-store 需要 --since")
        if args.from_store and since is not None and until is None:
            until = datetime.now(UTC)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        return asyncio.run(
            run(
                dsn=dsn,
                import_labels=args.import_labels,
                from_store=bool(args.from_store),
                since=since,
                until=until,
                window_seconds=int(args.window_seconds),
                kind=str(args.kind),
                region_code=args.region_code,
                target=args.accuracy_target,
                out=args.out,
            )
        )
    except Exception as exc:  # 响亮失败：只把类型与消息打出来，不打印栈也不静默返回 0
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
