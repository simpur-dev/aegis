# 可观测与告警取证环境

这一套配置是考核指标（SLA）证据链的落地面：**平台自己数出来的时延账本**要能被第三方在同一份
Prometheus/Alertmanager/Jaeger 里复核。这里的每条命令都在本机 Docker 上跑过，命令与输出原样记在
`docs/REPORT.md` 的"在线取证"一节。

```
backend /metrics ──scrape──► prometheus ──rule_files: alerts.yml──► 告警
                                       │                              │
                                       └── alerting: alertmanager ◄───┘
backend 跨度 ──OTLP/HTTP──► jaeger（按 traceID 复核单条链路）
```

## 起环境

```bash
# observability profile 内含 prometheus / alertmanager / jaeger 三个服务
docker compose --profile observability up -d prometheus alertmanager jaeger
```

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 由 `.env` 的 `OTEL_ENDPOINT` 决定；留空时跨度只记本地，
`GET /api/v1/integrations` 的 `tracing` 行会写成 `driver=local`——**"装了 OTel"与"跨度真的在上报"
是两件事，必须以这一行为准**，别看健康检查。

## 四道校验

| 目的 | 命令 | 通过的判据 |
| --- | --- | --- |
| 规则表达式与阈值本身 | `docker run --rm -v "$PWD/../..:/w" -w /w/deploy/observability --entrypoint promtool prom/prometheus:v2.55.0 test rules alerts-test.yml` | `SUCCESS`（9 条用例，与 9 条告警一一对应：既验"越限要报"，也验"健康不误报"） |
| 规则文件格式 | 同上，`promtool check rules alerts.yml` | `SUCCESS: 9 rules found` |
| 分发配置 | `docker run --rm --entrypoint /bin/amtool -v "$PWD:/cfg:ro" prom/alertmanager:v0.27.0 check-config /cfg/alertmanager.yml` | `SUCCESS`，并列出 route / 1 inhibit rule / 2 receivers |
| 整条链在真服务上跑通 | `cd backend && AEGIS_TEST_ALERTMANAGER_URL=http://127.0.0.1:9093 AEGIS_TEST_PROMETHEUS_URL=http://127.0.0.1:9090 pytest tests/integration/test_alerting_live.py` | 3 项通过：Prometheus 加载 9 条规则且 health=ok、发现 `alertmanager:9093` 为活动目标、critical→`duty-escalation` 而 warning→`platform-oncall`、同实例同团队的 warning 被抑制为 `suppressed` |

静态那一层（`backend/tests/unit/test_alerting_topology.py`）钉的是四份文件之间的漂移：
`prometheus.yml` 写的 `rule_files` 路径必须真被 compose 挂进容器、规则里的 `aegis_*` 指标名必须真的出现在
`/metrics`、路由分组键与抑制对齐键必须在每条告警的 labels 里都取得到值。少了这层，"规则文件存在"和
"告警链路在跑"就又会变成两件事。

## 验收时怎么取证据

1. **违约判据**：`GET :9090/api/v1/alerts` 列出的告警带 `indicator`/`target` 标签，直接对应考核条目；
   `GET :9093/api/v2/alerts/groups` 看它被路由到哪个接收者、是否被抑制。
2. **单条链路**：预警产物里的 `trace_id`（`trc_<16hex>`）补零成 W3C 32 位十六进制，
   `GET :16686/api/traces/<32hex>` 就能把 perceive→assess→plan→execute→feedback 的逐段跨度取回来；
   每条跨度上的 `aegis.*` 属性（region/readings/risk_level/stages/ok/degradations）与平台台账同源。
3. **量测口径**：`GET :8000/api/v1/metrics/latency`（`sla_thresholds` + 每条指标的 P95 与违约数，预算取自同一份
   `Settings`）应与 `:9090/graph` 里的 P95 对得上——两边都是 P95，直方图桶由 `MetricsExporter._BUCKETS_MS` 决定。

## 现场需要补的一件事

`alertmanager.yml` 的两个 receiver 目前没有 `webhook_configs`：短信/钉钉/企业微信的网关地址要现场才确定。
这里是**有意留空**的——占位 URL 会让"通知已发出"变成一句无法验证的话，而空 receiver 至少保证告警在
`/api/v2/alerts` 里可见、可截图。补齐渠道后必须重跑上面第三道与第四道校验。
