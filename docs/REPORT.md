# REPORT：实测记录与证据链

这份文件只收**由真实产物量出来的数字**（命令、口径、样本量都写清楚），以及明确还没测到的东西。
代码注释里的"见 REPORT"指向这里。任何数字过期就删掉重测，不做估算。

## 环境与口径

- 日期：2026-10-01；机器：单台 x64 Windows，CPU only（无 GPU、无独立推理卡）。
- 存储后端：`store_backend=memory`（除特别说明外）；总线 `memory`；交付通道 `mock`。
  触达时延里的 1.2s 是 mock 通道适配器注入的模拟链路耗时，不是真实短信/北斗时延。
- 测试规模（`pytest --collect-only` 与 `wc -l` 实测，2026-10-02 复核）：全量 `pytest` **2025 passed / 72 skipped**，
  分布在 87 个测试文件；`backend/src/` 92 个模块、20,510 行。
  （本行此前写的是 2026-10-01 那一刻的 1628 条 / 57 文件 / 85 模块 / 18,461 行——
  数字过期就重测，这是这条报告自己的规矩。）
- 门禁：`ruff format --check`、`ruff check`、`mypy src/aegis`、全量 `pytest` 四项必须全绿才算完成。

## 考核指标实测

### 降级模式（平台独立完成，无智能体）

`python -m scripts.metrics_report --rounds 10`（seed 202609，10 轮，29 个事件，致命错误 0）

| 指标 | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- |
| 常规任务调度响应 ≤2s | 2000ms | P50 0.055 / P95 0.081 / 最大 0.088ms | 达标（越限 0） |
| 预警信息生成 ≤3min | 180000ms | P50 0.0 / P95 0.0 / 最大 1.0ms | 达标（越限 0） |
| 预警信息靶向触达 ≤20min | 1200000ms | P50 1204.5 / P95 1230.4ms | 达标（越限 0） |
| 多节点数据共享同步 ≤3s | 3000ms | 样本 0 | 该模式不跑工作流，未测得 |
| 异常工况识别与重调度 ≤10s | 10000ms | 样本 0 | 同上 |

### 智能体在场模式

`python -m scripts.metrics_report --agents --rounds 6`（seed 202609，17 个事件，51 个事务样本）

| 指标 | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- |
| 多节点数据共享同步 ≤3s | 3000ms | P50 3.04 / P95 6.00 / 最大 7.63ms | 达标（越限 0） |
| 异常工况识别与重调度 ≤10s | 10000ms | P50 28.9 / P95 36.5 / 最大 37.6ms | 达标（越限 0） |
| 常规任务调度响应 ≤2s | 2000ms | P50 30.5 / P95 40.2ms | 达标（越限 0） |
| 预警信息生成 ≤3min | 180000ms | P50/P95/最大 0.0ms | 达标，但见下方"口径提醒" |
| 预警信息靶向触达 ≤20min | 1200000ms | P50/P95/最大 0.0ms | 达标，但见下方"口径提醒" |
| 多智能体协同联动成功率 ≥90% | 90% | 100%（51 个事务） | 达标 |

**口径提醒（不要把这些 0.0 读成"零耗时"）**：

- `warning_generation_ms` 量的是"定级结论 → 预警记录构造"这一段纯内存动作，本身就在亚毫秒级，
  端到端的预警耗时看链路总时延而不是这一项。
- `--agents` 下 `warning_reach_ms` 记为 0.0，是因为投递由执行智能体完成后回执给平台，
  平台侧不再走自己的通道埋点。两种模式量到的不是同一条路径，横向比较要用同一模式。

## 检索与知识层实测（bge-m3 / bge-reranker int8，CPU）

权重：`backend/data/models/bge-m3-int8`（559MB）与 `bge-reranker-v2-m3-int8`（561MB），
由 `scripts/fetch_retrieval_models.py` 预置并带 sha256 清单。

| 量 | 结果 | 说明 |
| --- | --- | --- |
| 会话装载 | 冷启动 embed 2102ms、rerank 4732ms | 已挪到容器启动期（`warm_retrieval`），不再由第一条预警付 |
| 查询侧嵌入（单条短文本） | 热态中位 26.5ms | 密集腿每请求只嵌一条查询 |
| 文档侧嵌入（5 条预案正文，非合成短句） | 热态中位 3807ms | 属于建索引路径，不在预警请求路径上 |
| 交叉编码重排 5 对（真实正文 ~305 字） | 热态中位 4201ms | 决定预算的主要成本项 |
| 交叉编码重排 10 对 | 热态中位 8361ms | 因此 `rerank_top_n=5`：多排的候选会被 k 截断丢掉 |
| 词法腿（BM25，16 条案例语料） | 热态中位 ~1ms | 零外部依赖，权重缺失时仍能出结果 |
| 端到端一条链路（含检索 + 重排） | 单区域 4.3—4.5s | 占 ≤3min 预警口径的 2.4%，无降级记录 |

据此定的默认值：`retrieval_budget_ms=5000`、`_RERANK_TOP_N=5`，两者是一组配套数字，
改一个必须重测另一个（`tests/unit/test_retrieval_wiring.py::test_default_budget_covers_a_measured_rerank_round`）。

**准确率口径的红线**：权重缺失时密集腿会切到 `HashingEmbedder`（确定性词面近似，无跨词面泛化能力）。
该降级必须在 `GET /api/v1/integrations` 的 `driver`/`degraded` 上看得见，其分数不得进入任何
"预警准确率 ≥90%" 类汇报。

## 工作流引擎实测

容器启动后 `WorkflowEngine.node_types` 返回 **16** 类节点（≥10 的指标要求），
时延见上表"异常工况识别与重调度""常规任务调度响应"两行；证据与对照分析在
`docs/adr/0004-self-built-dag-workflow.md`。

## 在线取证（2026-10-01，Docker 真实服务端）

| 取证对象 | 服务端实测版本 | 命令 | 结果 |
| --- | --- | --- | --- |
| PostgreSQL + PostGIS + pgvector | PostgreSQL 17.5 / PostGIS 3.5.2 / pgvector 0.8.6（`deploy/postgres` 镜像） | `AEGIS_TEST_PG_DSN=... pytest tests/integration/test_persistence_postgres.py` | 19 项全通过：扩展 DDL、`ST_DWithin` 按米、多边形覆盖、pgvector 余弦排序、写缓冲在库不可达时保住热路径 |
| ClickHouse 分钟物化 | ClickHouse 26.9.5.2 | `AEGIS_TEST_CLICKHOUSE_HOST=... pytest tests/integration/test_analytics_clickhouse_live.py` | 8 项全通过，并**因此发现并修掉两个真缺陷**：MV 的 SELECT 缺 `AS` 别名（THERE_IS_NO_COLUMN）、聚合列类型没跟服务端推断的 `Nullable` 状态类型对齐（CANNOT_CONVERT_TYPE）。服务端聚合与 Python 参考实现 `materialize_minutes` 逐键对账一致 |
| Jaeger 链路找回 | all-in-one（镜像摘要 `ab6f1a1f…`） | `AEGIS_TEST_OTEL_ENDPOINT=http://127.0.0.1:4318 pytest -m slow tests/integration/test_telemetry_otel.py`；另跑一条真实灾害链路 | 3 项通过；真实链路以 `hazard_chain` 为根导出 6 个跨度（根 + perceive/assess/plan/execute/feedback），可用 `/api/traces/<traceID>` 找回，`aegis.*` 属性（region/readings/risk_level/stages/ok/degradations）在 UI 侧可读 |
| 告警规则与采集配置 | promtool（prom/prometheus v2.55.0） | `docker run --entrypoint /bin/promtool ... check rules deploy/observability/alerts.yml` | `SUCCESS: 9 rules found`；`check config deploy/prometheus.yml` 亦通过 |
| 告警链路端到端（规则求值→分发→抑制） | prom/prometheus v2.55.0（摘要 `378f4e03…`）、prom/alertmanager v0.27.0（摘要 `e13b6ed5…`） | `promtool test rules deploy/observability/alerts-test.yml`；`amtool check-config alertmanager.yml`；`AEGIS_TEST_ALERTMANAGER_URL=… AEGIS_TEST_PROMETHEUS_URL=… pytest tests/integration/test_alerting_live.py` | 9 条阈值的 promtool 单元用例 `SUCCESS`（每条告警一对"越限要报/健康不误报"）；amtool `SUCCESS`（route + 1 inhibit rule + 2 receivers）；真服务上 3 项通过：Prometheus 加载全部 9 条规则且 `health=ok`、把 `alertmanager:9093` 发现为活动分发目标、critical 路由到 `duty-escalation` 而 warning 落到 `platform-oncall`，同实例同团队的 warning 被抑制为 `suppressed` |
| 站点清单 HTTP 面 | 同上（PostgreSQL 17.5 + PostGIS 3.5.2），进程内 ASGI 直连真库 | `GET /api/v1/stations`、`GET /api/v1/stations?region_code=540221`、`GET /api/v1/stations?region_code=5401` | 返回两条真实记录：维表登记的 `RG-540121-01` 带站名/高程 3650m/`SRID=4326;POINT(91.28 29.896)`；只报过数未登记的 `NEW-540221-77` 坐标为 `null`（**不编造坐标**，前端据此列入"未定位"）。区划过滤生效，非法区划码 422 |
| 离线一张图的自烘焙资产（quantized-mesh 地形 + PMTiles 底图） | 本机：`scripts/build_offline_tiles.py`（纯标准库 zlib/struct 出 PNG 与 PMTiles v3，无时间戳、可复算）+ 仓库里真装的 `pmtiles@4.5.0`、`@cesium/engine@1.145` 当读取器；`node:http` 本机静态服务（带 Range） | `python scripts/build_offline_tiles.py --max-zoom 2`；`uv run pytest tests/unit/test_offline_tile_format.py`（16 项）；`npx vitest run src/components/map/offline-assets.spec.ts`（8 项）；门禁 `npm run typecheck` + 前端 295 项 + 后端 1957 项全绿 | 此前仓库里**没有一个真实瓦片字节**，"弱网离线一张图"只被 mock 探针验证过。现在：42 张 `.terrain` 共 949,578B（**这是加密前的口径**，受控区加密后为 110 张 / 2,486,990B，以下一行"地形按关注区域加密"的实测为准）、`aegis.pmtiles` 21 张 256×256 PNG 共 44,921B，整轮烘焙 2.4s。真 `pmtiles` 读回 tileType=2、层级 0..2、21 条目录项，取回的 (1,1,1) 字节**逐字节等于**归档里那段 PNG，问 z=5 得到 `undefined` 而不是报错或读到邻居；前端 `createBasemapSetup` 在真探针→真选源→真影像层之后，`requestImage` 递给位图解码器的三段字节分别是两张 256² 与一张缺瓦替身 1×1；全程请求主机只有 127.0.0.1。**只有真读取器才会暴露的三类问题**：① 我第一版校验用的 glob 写成 `terrain/*/*.terrain`（实际三层深），"资产存在"这条断言于是在空集合上空转——修成 `*/*/*.terrain` 才真的在数 42 张；② pmtiles 只从**首个 16384 字节**里切根目录，根目录若排在元数据/瓦片之后就会被切成半截，所以生成器必须把根目录紧挨 header 写；③ jsdom 造的 `AbortSignal` 不被 undici 的 `fetch` 认（`Expected signal to be an instance of AbortSignal`），所有探针在传输层失败并被读成"资产不在"，该文件因此改用 node 环境。**照实记未验证项**：这张瓦在 GPU 里的实际渲染与法线 shading（当时本机浏览器几何不可用；下一行已补上真机渲染的实测，但 shading 观感质量仍未评）；以及生态其他消费者对 quantized-mesh 那 6 字节 magic 前导的要求——本仓库对齐的是自己装的那版 Cesium，它的 `createQuantizedMeshTerrainData` 从 `pos=0` 直接读 center、全引擎搜不到 `0x024b`，换引擎时要核对的口径已写进 `public/terrain/README.md`；格式偏移另有两条"上游漂移即红"的守卫用例盯着 `node_modules` 里的真实源码 |
| 浏览器真机渲染与影像矩形前提（同一批离线资产） | 本机 Chromium 内核（Electron 43.1.1 / Chrome 150.0.7871.114，`webgl: true`，dpr 1.5）+ `vite preview` 同源静态服务（Range 实测返回 `206` `bytes 100-117/44921`）+ `@cesium/engine@1.145` | `npm run build` 后打开 `/map`；`uv run pytest tests/unit/test_offline_tile_format.py`（19 项）；`npx vitest run src/components/map/basemap.spec.ts src/components/map/offline-assets.spec.ts`（41 项）；`npm run typecheck` + 前端 302 项 + 后端 ruff/mypy 全绿 | **这一条是真机跑出来的，295 项 jsdom 用例证不到这一层**：底图一挂上就 `An error occurred while rendering`，栈停在 `ImageryLayer._createTileImagerySkeletons` 读 `undefined.x`。数值根因写在两处源码里：PMTiles 头部把经纬度存成 1e7 定点 int32，生成器给的 85.05112877980659 经 `round()` 存成 850511288、读回 **85.0511288**，比 Web Mercator 切片方案的纬度上界（`WebMercatorTilingScheme` 反投影 ±(半长轴·π) 米 = `atan(sinh(π))` = 85.05112877980659°）大 2e-8°；而 Cesium 的注释把前提写得很直白——「imagery TilingScheme 的 rectangle 总是完全包含 ImageryProvider 的 rectangle」，越界的角点让 `positionToTileXY` 返回 undefined，紧接着就解引用 `.x`。修两侧：provider 构造时取交集（与切片方案无交集就响亮抛错，由装配层降级成"不挂影像"），生成器把边界量化改成**只往框内缩**（west/south 向上取整、east/north 向下取整）并重烘资产。复验事实：面板显示「底图：PMTiles 单文件 /basemaps/aegis.pmtiles：探针可用」「地形：自托管 quantized-mesh：/terrain」，无渲染错误遮罩，1.2s 内 199 个 rAF；`GET aegis.pmtiles` 13 次全 206、`terrain/*.terrain` 22 次全 200（滚轮缩小后又新增 10 张更粗的地形瓦，说明瓦片管线在持续跑而不是停在首帧）；影像层署名（合成高程/合成影像那句出处说明）出现在 Cesium 署名容器里；请求主机只有 `localhost:4174`。浏览器里 Range 读回的头部边界已是 (-180, -85.0511287, 180, 85.0511287)，界内。**照实记**：①"画面上有没有内容"是用截图像素与 `globe.baseColor`（#101923 深墨）对比判定的——`readPixels` 在未开 `preserveDrawingBuffer` 的上下文里读到全 0，不能当证据；②地形 shading 与影像观感质量仍未评（合成底图本就是浅色坡面 + 经纬网，没有真影像可比）；③同一轮实测还暴露一条并已修掉：`vite preview` 对不存在的 `/basemaps/0/0/0.png` 返回 `200 text/html`（SPA fallback），而探针只看状态码就判"可用"——HTML 兜底页会被当成瓦片源挂上影像层。**判据本身也是实测出来的**：修完之后在同一页里 `HEAD /basemaps/aegis.pmtiles` 返回的 `content-type` 是**空串**，所以只能"是 HTML 就拒"，不能"不是图片 MIME 就拒"，否则真归档被误杀、底图直接消失；两条口径各写成一条用例（假 fetch + 真 HTTP）。修好后复验：兜底页判不可用、真归档仍判可用、面板仍是「底图：PMTiles 单文件…探针可用」、无渲染错误遮罩、1.0s 内 165 个 rAF |
| DuckDB spatial 点-多边形语义（P1 边缘单文件分析） | 本机 duckdb 1.5.6 + 已缓存的 spatial 扩展（`~/.duckdb/extensions/v1.5.6/windows_amd64/spatial.duckdb_extension`），不依赖任何外部服务 | `uv run pytest -q -m slow tests/integration/test_analytics_duckdb_spatial_live.py`（8 项）；`uv run pytest tests/unit/test_analytics_duckdb.py`（40 项） | 单元套件里几乎每条用例都传 `allow_spatial=False`（无网机器不能把扩展可达性当测试前提，这是诚实的），代价是**几何语义从来没有被真跑过**：`ST_MakePoint(lon, lat)` 的经纬顺序、内环方向、无坐标事实会不会漏进来，任何一处写反现有测试全都照样绿。补上后与一个**独立实现**（自写射线法，不用 shapely 也不用 duckdb）逐点对账：160 个采样点里 65 个在区内，引擎与参照完全一致；写了内环才排除洞（不写内环时洞内点全部被召回）、凹多边形判对；经纬顺序用正反两个框钉死——把框的经纬互换时召回为 0；无坐标的事实不进空间结果；单文件重开后 spatial 重新加载、结果不变。**因此测出一个真缺陷并修掉**：`within_polygon` 碰到坏 WKT 时把驱动的 `InvalidInputException` 原样上抛，而且异常文本里**整段几何被抄进去**（现场多边形可达数千字，会连带进日志与状态面），与本模块「错误必须定型」的口径不一致——现收口为 `WarehouseError`，只保留原因、几何字符数与驱动类型，并另加一条**不需要扩展**的离线用例钉住这个映射（离线机也得有这条保障）。实测 160 行单次点-多边形判定 2.8ms |
| 跨端漂移门禁的取材方式（节点类型 / 装配腿名） | 无外部服务：直接解析仓库源码文本 | `npx vitest run`（295 项）+ `npm run typecheck` | 原来 `components.spec.ts` 里那份“后端 16 类节点”是**手抄清单**，注释还钉着 `nodes.py:351-366` 的行号——它只能证明“两份抄写一致”，证明不了后端改没改：后端加一类或删一类，前端测试照绿。现在两处跨端门禁都改成**现场解析真源**：新增 `src/testing/repoSource.ts`从 cwd 往上定位仓库根（vitest 里 `import.meta.url` 是 `/@fs/` 虚拟路径，不能直接拿去读文件），解析 `NodeSpec("name"` 与 `IntegrationState(name="...")`，并把 `integrations.spec.ts` 里那份本地实现收敛成共用工具；解析不到就抛错而不是 skip——门禁静默失效比红更糟。考核口径“≥10 类节点”改为对**解析结果**断言，不再对抄来的数字断言；后端侧的 `test_workflow_engine.py` 本来就按同一口径钉住 `len(node_types) >= 10` | 
| 站点台账的生产入口（`upsert_station` 到底谁在调） | 本机：内存 store + 进程内 ASGI 直连真应用；Postgres 侧的 `upsert_station` 早已有在线取证，缺的一直是调用方 | `uv run pytest -q tests/unit/test_stations_import.py`（28 项）；全量 `uv run pytest -q` exit 0；`uv run mypy src scripts`（96 files）干净；ruff check/format 干净 | `upsert_station` 此前**只有测试调用方**，于是 `GET /api/v1/stations` 里每个站都是 `name_zh=''`、坐标 null——地图上全站进「未定位」不是渲染问题，是入口缺失。现补 `python -m scripts.import_stations --source stations.csv`：整份校验通过才写（部分导入会让「这个区有几个站」随批次漂移）、坐标必须成对、列名不认识就拒（`logitude` 这类拼错会静默把坐标丢掉）、站号重复与空文件都算问题、`--dry-run` 只验不写；内存视图默认拒绝导入，显式 `--allow-memory` 才放行，且结果里写 `durable=false` 并往 stderr 提醒「只活在本进程」。同时给内存维表补上同签名同语义的 `upsert_station`（`StoreProtocol` 里声明，两端签名一致性有对账用例），导入后 HTTP 清单立刻看到名称/高程与 `SRID=4326;POINT(91.28 29.896)`；没坐标的站照样在清单里、坐标留 None——宁可列进未定位，也不按区划中心或站号编一个经纬度，错的坐标会被当成实测证据 |
| compose 与 .env 的落点契约（「照文档做完仍起不来」那类） | 本机 docker compose v2 CLI（客户端解析即可，不需要守护进程）+ 仓库内 `deploy/docker-compose.yml`、`.env.example`、`README.md` | `docker compose --env-file .env -f deploy/docker-compose.yml --profile observability config --quiet`（另跑 `--profile app --profile iot`）；`uv run pytest -q tests/unit/test_deploy_env_contract.py`（6 项） | 照 README 原样抄命令是起不来的：compose 的变量插值只在 compose 文件所在目录（`deploy/`）找 `.env`，而模板与文档都把凭据写在仓库根。实测不带 `--env-file` 时 `config` 直接报 5 个必填变量缺失；带上之后 observability / app / iot 三组 profile 全部解析干净（退出码 0）。现在文档里的 compose 命令显式带 `--env-file .env`，报错文案点名「仓库根那份」，并加 6 条静态对账钉住：每个 `${VAR:?}` 必须在 `.env.example` 有出处、引用了模板里没有又无默认值的变量算违规、README 里每条 compose 命令必须带 `--env-file`、两条 cp 指令（根 `.env` 给 compose、`backend/.env` 给本机进程）都必须还在、报错不许再出现含糊的「需在 .env 中设置」、容器 `env_file` 的 `../.env` 必须与 `--env-file` 指同一份。同一轮把部署面工件搬进 CI：新增 `deploy-artifacts` job，按文档路径 `cp .env.example .env` 后带 `--env-file .env` 解析 observability/app/iot 三个 profile，并用 promtool（check rules + check config）与 amtool（check-config）在构建里真跑一遍——「alerts.yml 单独特训过 promtool，但 prometheus.yml 少挂了 rule_files」那次事故就是这么防的；契约测试里再钉一条「CI 必须校验部署面」的对账，防止这个 job 被悄悄删掉。顺带一个只有逐条测才看得见的边界：服务级 `env_file` 指向的文件必须存在，所以「只带 --env-file .env.example、仓库根没有 .env」时 compose 直接 exit 1，按文档 cp 之后才 exit 0 |
| Zenoh 站端↔网关链路（P1） | eclipse-zenoh 1.10.1（本机 Python 运行时，两个 peer 会话经 tcp/127.0.0.1 直连） | `pytest tests/unit/test_edge_zenoh_transport.py`；`AEGIS_TEST_ZENOH=1 pytest tests/integration/test_edge_zenoh_live.py` | 新增 44 项替身驱动单测 + 4 项真运行时在线用例，全部通过；**因此发现并修掉三个真缺陷**：① `build_config()` 写了本版本不存在的 `scouting/multicast/join_interval`、`join_messages`，且 `interface` 被当数组传（应为字符串），而 `from_env()` 默认就开组播——也就是环境装配路径一 connect 就抛 `unknown key`；② 边缘缓冲的 store/query 原本"逐条 reply + 收尾 done 标记"，本版本 queryable 对同一 ke 连发多条时请求侧只收到其中一条，实测改成"一次查询一份完整应答"；③ 通配查询无法构造应答 ke 时原先静默不回，让对端干等到超时，现在回 `reply_err` 说明原因 |
| NATS JetStream 平台总线（P1：总线仍用 NATS） | nats:2.10-alpine（本机容器 `-js -sd`）+ nats-py（核心依赖） | `AEGIS_TEST_NATS_URL=nats://127.0.0.1:4222 pytest -q -m nats tests/integration/test_nats_bus.py`（连跑三轮全绿）；`pytest tests/unit/test_nats_api_contract.py`（21 项） | **这条 P1 此前从未在真服务器上跑通过**，接上以后一次暴露四个问题：① `error_cb` 是同步方法，而 nats-py 在 `connect()` 里就校验回调必须是协程 → 真实总线连不上（`InvalidCallbackTypeError`）；② `duplicate_window` 按纳秒传，库内又乘一次 1e9 → 1.2e20 溢出 int64，服务端对建流回 `invalid JSON`；更要命的是 `connect()` 把建流异常咽在 `log.debug` 里并照旧 `_connected=True`，平台于是带着一条"每条发布都失败"的已连接总线继续跑——现在改为抛 `BusNotReadyError` 并关掉已建立的连接，装配面看得见；③ `js.subscribe(queue=..., durable=...)` 两者不等会被库拒绝 → 网关的持久上行订阅一条都建不起来，队列组名改为沿用消费者名；④ 用例侧两处只在真服务器上才成立的问题：每条用例换流前缀却不删流（NATS 2.10 拒绝主题重叠建新流，err_code 10065），以及一条用例断言的 `received` 列表根本没有订阅方往里写。CI 的 `integration-nats` job 原来用 `services:` 起 NATS：既没开 `-js`，健康检查又打向未启用的 8222 监控端口——这条 job 在此之前不可能变绿，已改成显式 `docker run ... -js -m 8222` 并以 `/jsz` 的 `enabled:true` 做就绪自检 |
| 混合检索落到 seekdb（可选索引引擎） | 本机 OceanBase seekdb 1.3.0.0（`bin/seekdb`，MySQL 协议 2881）+ pymysql 1.2.3；嵌入侧分别是 `HashingEmbedder`（确定性 1024 维）与真 bge-m3 int8 | `AEGIS_TEST_SEEKDB_HOST=127.0.0.1 pytest tests/integration/test_retrieval_seekdb_live.py`（13 项）；`pytest tests/unit/test_retrieval_seekdb.py`（26 项）；容器端到端装配真跑一次 | 两腿都在真引擎上跑通：16 条内置案例灌数 **0.51s**；三条中文查询的密集+词法腿全部完成、零降级，耗时 **59.2 / 30.7 / 39.4ms**（预算 8000ms），top-1 分别是"沟口即时转移""下游两岸撤空""沟口即时转移"；换成真 bge-m3 + 重排的装配路径后三腿齐跑 **3834ms < 5000ms 预算**，top-1 为 `case_lo_downstream_evacuate`（rerank 5.27）。**引擎侧的真实坑**：seekdb 默认全文解析器是 `space`（`min_token_size=3`），中文整句成一个 token，四条案例对 `MATCH('泥石流')` 一律返回 0 分——表照建、索引照建、查询不报错，只是永远召不回，所以 `prepare_schema()` 必须回读 `SHOW CREATE TABLE` 校验解析器而不是相信自己的 DDL；向量侧 `VECTOR(1024)` + `WITH (distance=cosine, type=hnsw)` + `ORDER BY dist APPROXIMATE LIMIT` 可用，恶意 `doc_id`（含引号分号）经绑定参数只落成一行数据。**顺带纠一处装配口径**：状态行的 `index` 原来是装配期快照，启动期建好表灌好数之后仍显示"未建表 / indexed_docs=0"，现改为读取时取现值，并加了钉住这条的用例 |
| seekdb 整体迁到 D 盘并服务化 | 同一台机器：程序 `D:\seekdb\app`（与 `C:\Program Files\seekdb` 逐文件 SHA256 对账：100 个文件全等，只有 `etc/seekdb.cnf` 按设计改指 D 盘）、数据 `D:\seekdb\data`（全新空 store，旧库经用户确认无任何业务数据）；Windows 服务名 `seekdb`，Automatic，LocalSystem | `seekdb.exe --base-dir=D:/seekdb/data --nodaemon` 引导 → `seekdb.exe --install-service seekdb --base-dir=D:/seekdb/data` → `Restart-Service seekdb` → `SHOW PARAMETERS` 核对 `redo_dir=D:\seekdb\data\store\redo` → `AEGIS_TEST_SEEKDB_HOST=127.0.0.1 pytest` 在线 13 项 + 单测 26 项（服务态与前台态各跑一轮） | **迁移后按同一口径重测**：灌 16 条内置案例 **11.3s**（含 bge-m3 int8 会话冷装载，与索引无关）；密集+词法两腿 **116.5–126.6ms，中位 123.4ms**；三腿（含重排）**4096–4726ms**，其中前两条查询 5002/5021ms 越过 5000ms 预算并如实降级成 `重排超时，保留 RRF 次序`——重排会话冷装载正好落在预算上，这条实测就是 `warm_retrieval` 存在的理由，不是缺陷。旧日志按用户要求先归档再删源：324,851,198B → gzip 30,655,278B，解压回读长度与 sha256 一致。**两个只有真删真装才会撞见的陷阱**：① `msiexec /x {93706848-…}` 卸载旧安装时**把同名 Windows 服务 `seekdb` 一起删掉了**——"卸载只碰 Program Files"是想当然，脚本里那条"服务没了就从 D 重装"的自愈分支因此不是装饰；② 默认 `syslog_level=WDIAG` 并不压 INFO：空闲 **37KB/s（≈3.2GB/天）**，而这个 Windows 发行版没有按 `max_log_file_size=256MB` 轮转（实测单文件长到 629MB），改成 `WARN` 后空闲 **0 字节/秒**、跑完整 13 项在线用例只增 0.2MB |
| MQTT 推送腿（站端→平台） | aiomqtt 2.5.1 + paho-mqtt 2.1.0（`[iot]` extra），eclipse-mosquitto 2（broker，本机线级验证；生产按方案用 EMQX 5.8） | `pytest tests/unit/test_mqtt_connector.py tests/unit/test_mqtt_wiring.py`；`AEGIS_TEST_MQTT_HOST=127.0.0.1 pytest tests/integration/test_mqtt_live.py` | 新增 43 项单测 + 3 项真 broker 用例全部通过：三条 topic 解出四条读数（含批量与单条两种形状、多级子路径），坏载荷按原因计数而不是静默丢弃，broker 端口不通时 `connected=false` 且退避重连有计数与 `last_error`；缓冲有界（满则丢最旧并计数），凭据不进状态面。**过程中的真实约束**：paho 的 asyncio 支持依赖 `loop.add_reader/add_writer`，Windows 默认 Proactor 循环没有这两个钩子（Linux 容器部署不受影响），已把这条写进模块头并把错误翻译成可操作提示 |
| 内置案例库的性质披露 | 无外部服务（`backend/src/aegis/knowledge/data/hazard_cases.json` 本体） | `pytest tests/unit/test_knowledge_dataset_provenance.py` | 17 项通过：16 条逐条带可核对出处（最短 48 字，全部指向 NexusMind 骨架移植或平台自编兜底）、`plan_brief()` 不漏一条出处、图谱侧无本体时不编造出处、`observed_at` 不落在未来、`confidence` 有区分度（15 个不同值/16 条）、`/api/v1/integrations` 与 `/api/v1/cases/recall` 两端同时披露库性质。**修掉两处**：① 配置注释与装配文档原写"16 条西藏案例"，读起来像从真实事件里清洗出的经验库，实际是自编预案模板；② `.gitignore` 的 `data/` 通配把 `backend/src/aegis/knowledge/data/` 一起吞了——内置案例库**从未入库**，新克隆的知识层降级没有兜底数据（本地测试因为文件在盘上照样全绿），已显式反排除并加一条 `git check-ignore` 守卫用例 |
| Graphiti 落在真 Neo4j 上（P0 时序知识，第一档：不需要模型凭据） | Neo4j Kernel **5.26.31** community（`neo4j:5-community`，跑在搬家后的 Docker Desktop 4.93.0 / 引擎 29.8.1 上） | `docker start aegis-neo4j`；`AEGIS_TEST_NEO4J=bolt://127.0.0.1:7687 AEGIS_NEO4J_USER=neo4j AEGIS_NEO4J_PASSWORD=... uv run pytest -q -m slow tests/integration/test_knowledge_graphiti_live.py` | 原来整份 live 文件挂在"要有 LLM key"上，等于**Neo4j 通不通也得先有凭据才知道**——把用例拆成两档后第一档 2 passed / 2 skipped（skip 理由正确指向缺凭据）：驱动按配置面的 user/password 连上真图并读回 `RETURN 1` 与 `dbms.components()`；缺凭据时 `prepare_schema()` 报成类型化 `GraphitiUnavailableError`、理由点名 `llm_api_key`、且失败后没有半截写实例被缓存。**这一档当场揪出一个真缺陷**：`build_graphiti()` 两个角色都会先构造 embedder，而凭据门禁只设在 `build_llm_client` 上，所以真图上缺 key 时读路径抛的是 SDK 裸 `OpenAIError`——装配面会把它归成"无法解释的错误"而不是"这条腿没配凭据"；现在 embedder 也先过门禁，并有用例钉住"门禁排在 `import graphiti_core` 之前"（没装 graph extra 的机器也该报缺凭据）。另有两处被自己的用例抓住的想当然：`dbms.components()` 的 name 实测是 **`Neo4j Kernel`** 不是 `Neo4j`，`versions` 是列表不是字符串。同轮门禁：后端单测 **1843 passed / 0 failed / 0 skipped**（junitxml 计数）、ruff format+check、mypy 全绿。**照实记未验证项**：`learn` + `recall` 第二档仍未跑（要模型凭据），所以"图谱写入与混合检索在真图上可用"目前只有替身级证据；本机 Java 21 在位（原生 Neo4j 可作 Docker 之外的备选） |
| 公开气象拉取腿（第三条接入腿） | httpx 真客户端 + `httpx.MockTransport`（形状约定见连接器头注释） | `pytest tests/unit/test_weather_connector.py tests/unit/test_ingest_isolation.py` | 新增 19 + 6 项全部通过：12 个观测量按单位真源出读数、null/非数字/缺站名/区划过短逐条跳过、负值除气温外判 `suspect`、区划大写并截 24、`observed_at` 归一到 `Z`；形状不符（缺 `stations`、响应不是对象）抛 `SchemaInvalidError` 而不是当成"本轮无数据"；HTTP 5xx 与连接失败按源隔离，错误摘要里的请求 URL 整段换成 `<endpoint>` 占位（凭据常在运营方填的 base_url 里）；注入的客户端不被连接器关闭，自建的在 `shutdown` 时关闭。**真 socket 上另跑了一轮**（`tests/integration/test_weather_transport_live.py`，用标准库 `asyncio.start_server` 起本机 HTTP 服务，常驻回归）：对端记录到 `GET /observation HTTP/1.1`、两轮共用同一连接池（`rounds=2/readings=6`）、容器一轮摄取 3 条入库且状态行如实报 `driver=http`；对端只写响应头就断开时，`httpx.HTTPError` 上抛并计一次失败、错误摘要里只剩 `<endpoint>`。顺带确认一处口径：拉取腿的接入时延按"上游观测时刻 → 入库可查时刻"计，上游给一条旧读数就会判"接入时延超 SLA"——这是设计意图，数据新鲜度本来就是 ≤5min 接入指标的一部分。**修掉一个结构性的真缺陷**：`IngestService` 用 `asyncio.gather(..., return_exceptions=False)` 采集，第一个异常会被直接抛出 `ingest_once`，下面那段"按源写 `sources_failed`"的分支永远走不到——模块头部承诺的"单源失败隔离（高原弱网常见）"只存在于注释里；改为 `return_exceptions=True` 并补 `tests/unit/test_ingest_isolation.py`（此前没有任何测试构造过"有好有坏"的源组合） |
| 预警准确率回放落到报表出口 | 本机 CLI（`scripts.metrics_report --dataset`），三组合成/现场结构的 JSONL 数据集 | `python -m scripts.metrics_report --rounds 2 --dataset <f.jsonl>`，另跑 `pytest tests/unit/test_accuracy_replay_report.py` | 判据分支全部在真命令上跑过：合成集 24 例 → `status=not_measured`、`indicator=synthetic_only`、`official_accuracy=None`、实测算式 20/24=0.8333 仍如实给出、退出码 0；现场结构 45 例（TP10/FP20/FN5/TN10）→ `measured`+`not_met`、`official_accuracy=0.4444`、判定"未达标"、退出码 1（CI 可据此拦）；不给 `--dataset` → 准确率不进"指标判定"，只留在"未覆盖指标"并写明用哪个开关补。此前 `persistence/replay.py` 的口径早已定死但**没有任何生产入口**，准确率只能停在"未测得"。同一轮把演练/报表脚本里 `container.transport.idle()` 的隐性假设收进 `bus.transport.drain_pending()`：只有内存总线有"本地在途队列"可排，换 NATS 时不该撞 AttributeError |
| 配置面旋钮可达性（静态对账） | 无外部服务（`Settings` 字段 ↔ 生产源码 ↔ `.env.example`） | `pytest tests/unit/test_config_knob_reachability.py` | 9 项通过并**抓到三个拧不动的旋钮**：`simulator_enabled`（`.env.example` 明写 `AEGIS_SIMULATOR_ENABLED`，容器却只看 `with_simulator` 形参）、`weather_api_base_url`（连接器写了但装配层从不构造）、`connector_poll_seconds`（与 `simulator_interval_seconds` 同义重复，两个旋钮管同一件事注定有一个是假的）；另有两个纯元数据字段 `app_name`/`hazard_scope` 删除（灾种口径的唯一真源是 `domain/enums`，配置里再抄一份必然漂移）。守卫口径：`Settings` 每个字段都要在生产源码里被读到，`.env.example` 里每个 `AEGIS_*` 都要落到被读的字段；确实暂未接线的必须进 `RESERVED` 清单**且代码路径响亮拒绝**（`delivery_mode=http` 抛 `NotImplementedError`，不静默回落 mock 通道） |

同一轮还纠正了一处装配缺陷：`telemetry.init_telemetry()` 过去只有测试在调用，生产进程从未装配 provider，
导致"接了 OpenTelemetry + Jaeger"实际是跨度全留本地。现由应用生命周期负责装配/关停，
并在 `GET /api/v1/integrations` 增加 `tracing` 一行（`otlp`/`local` + 端点 + 丢弃跨度数）；
端点在这一行与启动日志里统一只留 `scheme://host:port/path`，采集器凭据不进任何对外面（导出器仍拿完整值）。

另一处同类缺陷是**告警链路本身**：`deploy/observability/alerts.yml` 早就写好并单独过 promtool 校验，
但 `deploy/prometheus.yml` 里既没有 `rule_files` 也没有 `alerting` 段——Prometheus 于是只抓样本、
从不求值那九条 SLA 规则，Alertmanager 更是一个告警也收不到。"规则文件存在"与"告警链路在跑"是两件事，
现已补齐挂载与 alertmanager 服务，并由 `tests/unit/test_alerting_topology.py`（静态四向对账：
`rule_files` 路径必须真被 compose 挂进容器、规则里的 `aegis_*` 必须真出现在 `/metrics`、
路由分组键与抑制对齐键必须在每条告警 labels 上取得到值）与上面那条在线用例一起钉住。
取证环境与四道校验的操作口径写在 `deploy/observability/README.md`。

本机 seekdb 的运维口径（2026-10-02 迁到 D 盘后）：程序 `D:\seekdb\app`、数据 `D:\seekdb\data`，
Windows 服务 `seekdb`（Automatic / LocalSystem），`net start|stop seekdb` 或 `Restart-Service seekdb` 管理，
连接仍是 `127.0.0.1:2881`，平台侧只需 `AEGIS_RETRIEVAL_INDEX_BACKEND=seekdb`。
C 盘那份 MSI 安装（`C:\Program Files\seekdb`）与旧数据目录（`C:\ProgramData\seekdb`）已删除，
`PATH` 里的 `C:\Program Files\seekdb\bin` 换成 `D:\seekdb\app\bin`；迁移过程的分步证据留在
`D:\seekdb\migration-evidence\`（pass1–pass3 日志 + 授权前后 ACL + 服务安装输出 + 一份日志滚动脚本），
旧日志归档在 `D:\seekdb\data\archive\`。**两条尚未验证的尾巴照实记**：① `syslog_level=WARN` 是
`ALTER SYSTEM SET` 的动态集群参数，能否跨服务重启保留**未测**——重启需要管理员，本轮 UAC 提权未获批准，
因此没做过一次"停服—起服—回读参数"；② 迁移期空闲日志长到的 629MB 还没滚掉，因为文件句柄在服务进程手里，
需要"停服→删→起服"一趟，这条已经写成可复跑的 `D:\seekdb\migration-evidence\roll-server-log.ps1`
（归档-回读校验-才删，与旧日志那条同一口径），但**尚未执行**。
| 地图渲染的两道门禁（无头真 Cesium 解析 + Playwright 真机画面） | 本机 `@cesium/engine`/`cesium@1.145`（与浏览器同一份模块）+ `node:http` 本机静态服务；Playwright 1.5x chromium（Chrome for Testing 153.0.8010.12，SwiftShader 软件 GL，本机跑、不用任何云测服务）+ `vite preview` 构建产物 | `npx vitest run src/components/map/terrain-parse.spec.ts`（14 项）；`npm run test:e2e`（4 项，13.0s）；`npm run typecheck` + 前端 **323 项**全绿 | 上一轮的教训是"实现+替身测试"不等于"真跑通"，这轮把两层都变成常驻门禁。**第一层（零新依赖）**：真 Cesium 逐瓦解析盘上全部 `.terrain`（写出该行时为 42 张，受控区加密后同一门禁解析 110 张），除"能解析"外还钉住三条不变量——① 按 `maxShort=32767` 反算的顶点高程必须铺满头部声明的高度界（我一开始用 65535 反算，得出"高度只有一半"的**假故障**，所以这个数由门禁对着已安装源码核）；② 峰必须落在它该在的瓦角落（v 口径一旦翻转就会偏十几度，而按瓦最大值判是判不出来的）；③ 邻瓦同经线接缝高差 < 1 m（实测 0）。另有三档缺件都得响亮：老实 404 → `RequestErrorEvent`，`vite preview` 的 `200 text/html` → `RangeError: Invalid typed array length`（证明 HTML 真被喂进了 quantized-mesh 解析器），以及"layer.json 声明可用但瓦不在位"→ reject 而不是静默出平地形。**第二层（真 GPU 画面）**：`/map?mapDebug=1` 打开构建产物，实测画布 720×490、2861 种颜色、主色占 17.2%、亮度 1.1..255，无 `An error occurred while rendering` 遮罩；6 张地形瓦响应各 22,609B、全 2xx、无一张是 HTML；场景侧 `globe.getHeight(85°E,31°N)=4196m`（椭球面只会给 0，所以这条就是"provider 是谁"的行为证据——生产包里类名已被压成 `hd`，看名字判不出）。**因此改掉的装配面事实**：新增显式 opt-in 的只读场景句柄 `debugHandle.ts`（URL 不带 `?mapDebug` 就不挂，销毁必清），此前 e2e 只能"看截图猜"；`vite preview` 默认只绑 `[::1]`（实测 netstat），门禁命令里补 `--host 127.0.0.1`。**照实记**：① 贴瓦边取到 -8243m 这类负值是引擎裙边（`skirtHeight = 该层几何误差 × 5`，maxzoom 2 时 L2 误差还有 19km），不是资产坏了，所以画面侧只判"这里没有高原"不判下界；② CI 仍只跑 `npm run test`，e2e 需要下载浏览器与真 GL，按"本机/现场交付前跑"的口径提供 `npm run test:e2e`，不进云端门禁；③ 同一批截图还暴露一条口径矛盾：画面左下角固定画着引擎自带的厂商署名（一段带外链的 `<a target="_blank">` + 图片），而本项目一张托管资产都不取——已用公开访问器把它换成"自托管、无第三方瓦片服务"（`applyOfflineCredits`），署名容器仍说清出处，e2e 与 `credits.spec.ts` 各钉一条；④ 只烘到 maxzoom 2 的浅金字塔让引擎裙边深达 96km，贴瓦边采样会取到几万米负值，这不是资产坏了（逐顶点反算已证），修法与口径见清单第 6 条 |
| 地形按关注区域加密（全球浅 + 受控区深） | 同一台机器：`scripts/build_offline_tiles.py`（新增 `--refine-max-zoom/--refine-region`）+ 真 `@cesium/engine@1.145` 无头解析 + Playwright 真机 | `python scripts/build_offline_tiles.py --max-zoom 2`；`uv run pytest -q tests/unit/test_offline_tile_format.py`（22 项）；`npx vitest run src/components/map/terrain-parse.spec.ts`（14 项）；`npm run test:e2e`（5 项）；`npm run typecheck` + 前端 327 项 + 后端 2008 项全绿 | 上一轮量到的"贴瓦边取到 -8243m 负高程"根因是**浅金字塔**：引擎裙边 = 该层几何误差 ×5，全球 maxzoom 2 时 L2 误差 19 km ⇒ 裙边 96 km。全球烘深不成立（Geographic 下层级 z 有 2^(z+1)×2^z 张瓦，烘到 5 层就是 2730 张 ≈ 62 MB），所以生成器把两件事分开：`--max-zoom` 是全球铺满深度（父瓦链必须完整，Cesium 假定"层级 n 有瓦 ⇒ 父瓦都在"），`--refine-max-zoom` 只作用于受控区（默认西藏 78–99°E, 26–37°N）。实测：110 张瓦 / 2,486,990 字节，逐层 2/8/32/2/6/15/45，烘焙 3.5s；`layer.json` 的 `available` 深几层只声明局部范围（**行号按 TMS 自南向北写**，Cesium 读时翻一次；翻错就是"区域外判有瓦、区域内停在浅层"，两侧各有一条对账钉住）。真机复测：地形瓦请求从 6 次涨到 14 次、渲染层级到 4、高原处 `getHeight` 从 4196m 升到 **5045m**（解析面峰值 5573m，浅层采样必然低估）、画布上高原山体阴影像素占 **8.33%**——"画面发白"那句是我读缩略图读错，实测有起伏着色，已按实测改判。区域作用域也可判：完全落在受控区的矩形 `computeBestAvailableLevelOverRectangle` 给到最深一层，向南只多探 0.3° 就老实退回上一层 |
| 部署形态起服务（真 JetStream + 真库）与量测脚本自身的取证能力 | 本机容器：nats:2.10-alpine（`--jetstream --store_dir /data -m 8222`）+ `aegis-pg:local`（PostgreSQL 17 + PostGIS + pgvector）；locust 2.46.6 经 compose 发布端口打本机 | `AEGIS_PG_DSN=… uv run python -m scripts.load_curve --profile deployed --levels 1 --duration 10s` → 退出码 0，服务从起进程到 `/healthz` 可答 **1.61s**，5 请求零失败；`uv run pytest -q tests/unit/test_bus_naming.py tests/unit/test_load_curve_report.py`（17 + 26 项） | "照 README 把部署形态跑一遍"是这批里产出最多的一跑：**两个真缺陷同时现形，而它们在内存形态的 2000 多项单测里全都看不见**。① durable 消费者名把 `agent_id` 里的点带了进去（`d_perceive_perceive.mock01`），nats-py 判非法直接抛 `ValueError` → uvicorn `Application startup failed`，也就是应用根本起不来；规则收进唯一真源 `aegis/bus/naming.py`（保守字符集，**只在真的改写过或超限时**追加原名短哈希——不区分就会让 `a.b` 与 `a_b` 静默共用同一个服务端投递状态，那比启动失败更难查；全空时给非空名字而不是退化成"无 durable 订阅"），门禁**直接 import 上游 `_validate_consumer_name`/`_INVALID_NAME_CHARS` 对账**，它改规则我们就红，而不是拿自己的正则自我证明。② 定位①的过程暴露量测脚本自己会擦掉现场：判"服务进程提前退出"却把子进程 stdout/stderr 送进 `DEVNULL`，那句话只存在于服务自己的 stderr 里，只能手工再跑一遍 `python -m aegis.main` 才拿到；现在尾巴由 `reports/load_curve_server.log` 直接带进报错与产物（`server.log`）。子进程同时改 `-u` 起——重定向到文件时解释器对 stdout 块缓冲，而"挂住不返回 /healthz"这条路是被 terminate 收摊的，不加 `-u` 恰好在最需要看日志的那一刻读到空文件（正常退出反而会 flush，于是缺陷只出现在一半路径上）；这条按真子进程行为量：不 flush 地打印后睡死的进程，父进程必须在它活着时就读得到那行。**照实记三条**：a) 我第一版调用点断言只看字节码 `co_names` 里有没有 `consumer_name`，漏写 import 时它照样绿——而且真发生过（`bus/gateway.py` 加了调用点忘了 import，内存形态全绿、部署形态一跑就炸 `NameError`），断言已改成"从函数全局命名空间解析得到、且与唯一真源是同一个对象"，并用变异检查（把绑定临时摘掉再跑）确认新断言抓得到这类漏；b) 用例里等子进程"自然退出"才读日志，`_stop_server()` 是 terminate，抢在 flush 前杀就得到空文件——第一版就踩了这条，所以两条路径各自有用例；c) 本行只是"起得来、答得了"的烟雾档，部署形态的容量拐点见下文「部署形态复测（2026-10-02，同一台机器，真总线 + 真库）」 |
| 工作流外呼节点（`api_call` / `device_control`）与其 SSRF 闸 | 本机：真应用容器装配 + 注入的假 httpx 传输（全程不触网）；结构门禁直接读 `workflow/nodes.py` 源码文本 | `uv run pytest -q tests/unit/test_workflow_outbound.py tests/unit/test_workflow_services_bridge.py`（20 + 12 项）；全量 `uv run pytest` **2025 passed / 72 skipped**；`ruff format --check`/`ruff check`/`mypy`（98 个源文件）全绿 | 独立审计抓到的实现缺口：`WorkflowServices.http_call` 字段与两个 handler 的 `require_service("http_call", ...)` 一直都在，唯独装配桥从来没传它，于是这两类节点在产品形态下 100% 抛 `NodeError`——"≥10 类节点可视化编排"这句话在 16 类里实际只有 14 类能跑；桥这一层此前**零用例**，所以谁都踩不到。反过来直接放开也不成立：URL 是编排画布上填的，等于让流程定义者决定平台往哪儿发请求（元数据端点只差一跳）。新出口 `workflow/outbound.py` 的口径是**配了主机白名单才开**：只放行 http/https、主机名精确匹配、拒绝 URL 内嵌凭据、一律不跟随重定向，异常与状态只留 `scheme://host`，调用/拒发/失败三个计数可外显；`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 留空时节点按"缺少依赖服务"响亮失败并在装配日志说明原因。**门禁防的是整类问题**：从节点源码读出全部 `require_service` 声明，逐个断言装配桥真的给出去（反向也核，防止字段堆积无人用），并用变异检查证明它抓得到——把 `notify` 从桥返回里删掉，两条用例立刻红。**照实记三条**：① 我第一版调用点断言只看字节码 `co_names`，漏 import 时它照样绿（真发生过一次 `NameError`），已改成"`__globals__` 解析得到且与唯一真源同一"；② `AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 这两个键还没进 `.env.example`（该文件受工具写入策略保护，需要人工补两行），先记在 README 配置节；③ 这条腿目前只在装配日志与用例里可见，还没进 `GET /api/v1/integrations` 的腿表 |
| 空间轨迹查询从应用可达（PostGIS 半径 / 轨迹面） | 本机容器：PostgreSQL 17.5 + PostGIS 3.5.2 + pgvector，经 compose 发布端口 127.0.0.1:5432；HTTP 侧用 ASGI 直连真应用 | `uv run pytest -q tests/api/test_geo_endpoints.py`（15 项）；`AEGIS_TEST_PG_DSN=… uv run pytest -q tests/integration/test_geo_endpoints_live.py`（8 项，真库跑通）；复跑既有 `tests/integration/test_persistence_postgres.py`（22 项）确认互不污染 | 审计结论是"SQL 写好了也测过了，但没有任何调用方"：`stations_within` / `hazard_trace_summary` 既没进存储协议也没进 API，所以"PostGIS 用于空间轨迹查询"只在测试进程里成立。这轮把它接成三个只读端点 `/api/v1/geo/{stations-within,stations-in-polygon,hazard-trace}`，回答里带 `driver:"postgis"` 说明是谁算的。几何能力作为**可选能力面** `SupportsGeoQueries` 单列而不并进 `StoreProtocol`：内存实现没有 PostGIS，硬要"也能查"就得再养一份 haversine/射线法，两套几何口径同时存在时看不出差别的是验收方、付代价的是被漏掉的站；缺能力时端点以 503 + `E_GEO_UNAVAILABLE` 回答并写明要配 `AEGIS_STORE_BACKEND=postgres`。真库取证：1.9km 两点按米升序、半径收紧只剩中心站、空候选集回空表、面内恰好罩住两站、区号叠加过滤仍为空、非法 WKT 与裸时间/逆序窗在触达数据库之前就被拒。**顺带修一条边界缺陷**：持久层的 `QueryArgumentError` 不是 `AegisError` 的子类，而应用侧只挂了 `AegisError` 处理器——此前没有任何端点会抛它所以从没暴露，接上几何查询后它以 500 出去（调用方会以为服务端坏了），现在补了处理器统一 422 |
| 预警准确率的库侧回放（真值表 + 与落库 warnings 配对） | 本机容器：PostgreSQL 17.5 + PostGIS + pgvector；另跑一次真 CLI 冒烟（导入 2 条标注 → 库侧回放 → 写 JSON） | `uv run pytest -q tests/unit/test_persistence_accuracy.py`；`AEGIS_TEST_PG_DSN=… uv run pytest -q tests/integration/test_accuracy_replay_live.py`（9 项，真库）；`AEGIS_PG_DSN=… uv run python -m scripts.accuracy_replay --import-labels labels.jsonl --from-store --since … --kind field` → 退出码 0，产物里 `imported_labels=2`、`status=insufficient_sample`、`official_accuracy=None` | 审计结论：算式与判据分支都在，但真值在 Postgres 里**没有表**，回放是纯 Python + 文件读，于是"PostgreSQL 用于预警准确率回放"这半句不成立。补 `sql/004_accuracy_labels.sql`（幂等，走既有 `apply_migrations` 清单；漂移门禁按文件名清单从三个改判为四个）+ `persistence/accuracy.py`：按 `case_id` upsert 导入真值，`LEFT JOIN LATERAL … ORDER BY w.generated_at LIMIT 1` 取"事件之后第一条产出"。两条口径写死在用例里：① 预测侧只认 `observed_at` **之后**发出的预警——把事件之前就存在的预警算成命中，是"预报准确率"最典型的造假方式（真库上有一条专门这样断言，结果是 fn 而不是 tp）；② 配对不按灾种过滤，否则"报对灾种"恒为真。判定算术仍只在 `replay.py`：`case_from_row` 只搬字段，且有一条用例断言它与 `parse_case` 对同一份数据产出完全相等的案例，防"两套准确率"。窗口默认 3600s、上限 6h；裸时间与逆序窗在触库前就被拒。`--kind` 默认 `unspecified`：数据没自己声明是现场标注时官方准确率恒为 None，报表自带 `provenance_warning` |

## 并发曲线实测（2026-10-01，Locust）

命令：`uv run python -m scripts.load_curve --levels 1,10,30,60 --duration 25s`
与 `uv run python -m scripts.load_curve --levels 120,200 --duration 30s`
（脚本自己拉起 uvicorn 子进程、跑完收摊；每个点断言"请求数 > 0"）。
环境：本机单 uvicorn worker、内存总线、mock 通道、模拟器开启；服务冷启动到 `/healthz` 可答 **1.12s**。

| 并发用户 | 请求数 | RPS | 整体失败率 | 聚合 P50/P95 (ms) | 演练 `drill_run` P95 | 站点清单 P95 | 就绪探针 P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 11 | 0.48 | 0% | 110 / 130 | 130 | — | — |
| 10 | 266 | 10.83 | 0% | 5 / 130 | 170 | 9 | 18 |
| 30 | 769 | 30.71 | 0% | 12 / 130 | 190 | 23 | 21 |
| 60 | 1363 | 54.32 | 0% | 32 / 260 | 710 | 95 | 100 |
| 120 | 2505 | 83.46 | 0% | 180 / 1000 | 2200 | 300 | 300 |
| 200 | 1918 | 63.82 | 2.50% | 850 / 4600 | 5800 | 1500 | 1300 |

判据与告警同源（`aegis.observability.load_policy`：只读端点 4000ms、一次完整演练 5000ms）。结论：

- 1→120 并发全程零失败，RPS 随并发近线性上升（10.83 → 83.46）；
- **拐点落在 120→200 之间**：RPS 不升反降（83.46 → 63.82），演练段 P95 从 2200ms 涨到 5800ms 越过预算，
  演练路径失败率 12.37%（整体 2.50%）；同期只读端点仍在预算内（1500/1300ms < 4000ms）。
- 所以这一版实现能对外承诺的容量口径是：**本机单 worker + 内存总线下，≤120 并发的"读 + 演练"混合负载全部达阈**。
- 口径边界：这不是 Linux + uvloop + 4 workers + NATS JetStream 的部署形态，部署环境那条曲线仍需重跑。

### 部署形态复测（2026-10-02，同一台机器，真总线 + 真库）

命令：`AEGIS_PG_DSN=… uv run python -m scripts.load_curve --profile deployed --levels 1,10,30,60 --duration 25s`
与 `… --levels 120,200 --duration 30s`（产物 `reports/load_curve_deployed.json`、
`reports/load_curve_deployed_high.json`，两者都写明 `server.profile=deployed`）。
形态：nats:2.10-alpine（`--jetstream`）+ PostgreSQL 17（PostGIS + pgvector）容器，
应用仍是本机单 uvicorn worker、mock 通道、模拟器开启；服务从起进程到 `/healthz` 可答 **1.63s**。

| 并发用户 | 请求数 | RPS | 整体超预算率 | 聚合 P50/P95 (ms) | 演练 `drill_run` P95 | 站点清单 P95 | 就绪探针 P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 13 | 0.57 | 0% | 130 / 180 | 180 | — | — |
| 10 | 255 | 10.41 | 0% | 8 / 170 | 310 | 210 | 26 |
| 30 | 745 | 29.62 | 0% | 13 / 200 | 280 | 48 | 33 |
| 60 | 1289 | 51.28 | 0% | 65 / 700 | 1300 | 520 | 170 |
| 120 | 2244 | 74.42 | 0% | 160 / 2700 | 3700 | 1100 | 610 |
| 200 | 1753 | 58.79 | 7.24% | 840 / 6100 | 9300 | 2600 | 1300 |

读数与结论：

- **拐点形状与内存形态一致，落在 120→200 之间**：RPS 从 74.42 回落到 58.79，
  所以对外承诺的部署形态容量口径是 **≤120 并发的"读 + 演练"混合负载全部达阈**（与上表内存形态同一档，
  即"接上真总线真库没有把可承诺容量拉下来"）。
- **代价看得很清楚**：演练段 P95 在 60/120 并发分别是 1300/3700ms，而内存形态同档是 710/2200ms
  ——真 JetStream + 真落库把演练路径抬了约 1.5–1.8 倍；只读端点也整体抬升（站点清单 60 并发 520ms vs 95ms）。
  这是"多一跳总线 + 一次真写"的应有代价，不是回归。
- **"失败率 7.24%" 这句必须换个说法才诚实**：逐条读 locust 的 failures 台账，200 并发那一档
  **没有一条 5xx、没有一条连接错误**，全部是压测档案自己判的 `drill 超阈值: 5050..13642ms > 5000ms`
  （drill_run 44.64%）与两条 `stations 超阈值: 6542/9080ms > 4000ms`。
  也就是说这一档的语义是"SLA 预算被越过"，而不是"服务出错"——两者进同一列会误导读者，
  所以上表列名写作"整体超预算率"。（上一张 2026-10-01 的内存形态表仍沿用"失败率"三字：
  那一档的逐条 failures 台账已被后续运行覆盖，没法照同样的口径复核，不去替它改判。）
- 1 并发那一档只跑到演练任务（13 次请求），其余端点当次没记到样本，所以表里是 `—`。
- **口径边界（照实说）**：这仍是 Windows + 单 worker + 无 uvloop，且数据库/总线容器与压测机、被压机
  共享同一份 CPU；它证明的是"接上真依赖后容量没塌"，不能当成 Linux + 4 workers 的部署环境数字。

**这一轮的先决条件是压测档案本身能被修好**：`tests/load/locustfile.py` 里写着
`float(response.elapsed)`，而 locust 2.46 底下层是 httpx、`elapsed` 是 `timedelta` —— 每次任务都抛
`TypeError`，Locust 把它记成 task error，汇总表却是 `Aggregated 0 requests, 0(0.00%)` + 退出码 0。
也就是说这份档案从写下到今晚**一次请求都没发出去过，而它在 CI 里是绿的**。
阈值与判定口径因此挪进 `aegis/observability/load_policy.py`（locust 一 import 就 monkey-patch ssl，
测试进程没法 import 档案，判定逻辑必须住在产品包里才测得到），两侧钉住：
`tests/unit/test_load_policy.py` 测行为（耗时换算、等于预算判合格、预算与 `Settings` 同源），
`tests/perf/test_load_profile.py` 走 AST 测结构（档案必须调预算函数、
`float(response.elapsed)` 不得回潮、清单里列出的端点必须真的被任务打到——
`/readyz` 与站点清单原本就列了却没有任务打）。

**读数层自己也曾是"没被测过的那层"**：`scripts/load_curve.py` 把 locust 的两张表解析成结论，
这 100 多行解析/合并/零流量判定今晚之前没有任何用例。现在由
`tests/unit/test_load_curve_report.py`（26 项）钉住，fixture 是提交进仓库的**真 locust 2.46.6 输出**
（`tests/fixtures/locust_summary_locust-2.46.6.txt`，本机 2 并发 8s 原样落盘），
断言的数字全部来自它，而不是想象的表格长相。

同时记录两条不好听的：
1. `tests/unit/test_accuracy_replay_report.py` 落地时 `scripts` 不在 pytest 的 `sys.path` 上，
   收集期直接 `ModuleNotFoundError`——而 pytest 在收集阶段报错是**整场中断**，不是少跑一个文件。
   我在 `ab3c683`/`ccc800e` 两次推送前只跑了筛选过的命令，于是"全绿"这句话当时是错的。
   现在 `pyproject.toml` 显式 `pythonpath = ["."]`，并由
   `test_repo_hygiene.py::test_scripts_imports_are_backed_by_pythonpath` 守住这行配置；
   全量 `uv run pytest` 现为 **1839 passed / 49 skipped**。
2. `tests/unit/test_edge_zenoh_transport.py` 里"起任务后固定 sleep 20ms 再取 `session.gets[0]`"
   在整套会话里被别的任务挤掉过，随机 `IndexError`（单跑该文件三次全过，抓不到）。
   已改为条件等待 `_wait_for(...)`（带 2s 上限，超时判失败），并把三处采集类用例的响应窗口
   从 60–80ms 放到 400ms——那些用例断言的是"应答收得全"，超时边界另有专门的用例守。

重构后端到端复跑了一次真流量：`--levels 1,10 --duration 8s --port 8138`
→ users=1 requests=4 P50/P95=130/130ms、users=10 requests=84 P50/P95=7/170ms、零失败、退出码 0、
无残留服务进程。

## 还没测到的（诚实清单）

1. **部署形态的并发曲线**：真总线 + 真库那一档已在 2026-10-02 量过（见上文"部署形态复测"，
   拐点同样是 120→200、容量口径仍是 ≤120），但**仍不是生产环境数字**：缺的部分是
   Linux + uvloop + 4 workers 的那条曲线（本机复现不了），以及"按站数梯度"的资源占用
   （CPU/内存/连接数随在册站数怎么涨）——后者连量法都还没定，属于下一批要开口子的地方。
   另外这次的 `delivery_mode` 仍是 mock，演练段里不含真实触达链路。
2. **弱网工况**：Zenoh 链路已在真实运行时上验过互通、请求-响应、边缘存留与按序重放
   （见上表），但还没在真实丢包/高时延链路（4G/卫星回传）上量过；`poc_report.py` 的丢包梯度是
   本机注入的合成时延，不是空口实测。
3. **真实通道对接**（短信/北斗/广播）：目前 `delivery_mode=mock`，`warning_reach_ms` 里的 1.2s
   是 mock 适配器注入的模拟链路耗时，不是真实触达时延。
4. **图谱在线**：Graphiti/Neo4j 的读写分离与召回映射在单测里用替身驱动覆盖；live 用例已拆两档，
   第一档（真 Neo4j 连通性 + 缺凭据时的类型化降级）已在 **Neo4j Kernel 5.26.31 community** 上跑过
   （见上表），但 `learn` + `recall` 第二档仍未跑——它必须有模型凭据（图谱抽取与查询向量化都由模型完成），
   所以"图谱写入与混合检索在真图上可用"目前只有替身级证据。
5. **离线底图与站点坐标的数据侧**：代码链路是通的（Cesium 自建 quantized-mesh 地形 + PMTiles 底图，
   零 Ion/谷歌依赖，前端有守卫用例禁止引入外部 token；`GET /api/v1/stations` 也已能返回真实清单），
   仓库里也已烘进**合成**资产（`public/terrain` 110 张 quantized-mesh：全球 0–2 层 + 受控区加密到 6 层，2.49MB；`public/basemaps/aegis.pmtiles`
   21 张 256² PNG，见上表两行取证），缺的是**真实测绘数据**：换真 DEM/影像的做法与要对齐的口径写在
   两份 README 里；站点坐标同理——平台侧没有任何生产代码向
   `monitoring_stations` 写站名/经纬度（`upsert_station` 只被测试与运维导入路径调用），
   所以地图上点位的多少取决于现场导入的站点台账，不是代码能力。
6. **一张图：链路已证通，缺的是真实测绘数据而不是代码**：两道门禁把原来记在这条里的"挂上地形就不出几何"
   彻底推翻——真机量到 `globe.getHeight(85°E,31°N)=5045m`、渲染层级到 4、画布上高原山体阴影占 8.33% 像素，
   字节侧三条不变量（反算铺满头部高度界、峰落在该在的瓦角落、邻瓦同经线接缝 0 差）与逐顶点对解析面的
   对账都在。原来"发白"的判断是我读缩略图读错（低海拔本就是浅米色，画面外是深墨色天空），已按实测改判。
   裙边深度靠"受控区加密"压到公里级（见上一行取证）。剩下的观感差距只可能来自**数据**：仓库里是解析函数
   合成的 DEM 与底图——等高程面、等经纬网，不是真实测绘。换真 DEM/影像的口径写在
   `public/terrain/README.md` 与 `public/basemaps/README.md`（含现场建议 `--refine-max-zoom 12–15`、
   nginx 需补 `application/vnd.quantized-mesh` 的 MIME 项）。同批发现的一条署名矛盾已修：画面左下角原本
   固定画引擎自带的厂商 logo（带外链），与"零托管服务"口径矛盾，现由 `applyOfflineCredits` 换成自托管说明。
7. **案例库的经验侧**：`hazard_cases.json` 的 16 条是**自编预案模板**（骨架移植自 NexusMind 干预库，
   处置内容为川藏沿线实战口径），`confidence` 与 `estimated_delay_hours` 是编制判断值。
   召回、排序、图谱摄取三条路径都在真实数据上跑通了，但"预警准确率"意义上的**案例命中率**
   还没有可核对的真实事件复盘集可以做对照——这一项不能进任何"经验证于历史灾害"的表述。
   性质声明由代码与接口两处外显（`DATASET_PROVENANCE` 进 `/api/v1/integrations`、
   `source_note` 进每条召回命中），用例见 `tests/unit/test_knowledge_dataset_provenance.py`。
8. **预警准确率的数据侧**：算式与报表出口都已就位（`persistence/replay.py` +
   `scripts.metrics_report --dataset`，判据分支见上一节的三条真实命令），缺的是**数据**：
   仓库里没有真实现场标注的 JSONL（也不该造一个），所以"≥80%"这项至今是 `not_measured`。
   现场交付时要按 `{"dataset":{"kind":"field","source":…}}` + 逐案例
   `truth_warning/predicted_warning` 的口径导入，且总案例数须 ≥ `MIN_FIELD_CASES`（30），
   否则报表判"样本不足"——这一条同样是机器拦住的，不靠人记得。

9. **准确率回放的库侧通路已接上，缺的仍是数据与出口**：真值表已存在（`004_accuracy_labels.sql`），
   预测侧改从落库 `warnings` 配对（`scripts.accuracy_replay` 一条 CLI，取证见上一节），
   判定仍只在 `persistence/replay.py` 一处。剩下两件事：① 仓库里没有真实现场标注（也不该造一个），
   所以"≥80%"这项照旧是 `not_measured`；② 回放只有 CLI 入口，没有 HTTP 端点——
   大屏要看回放结果，得等这条通路接到 `/api/v1/…` 上。
10. **图谱写入侧没有生产调用方**：`graphiti_store.learn()` 与 `prepare_schema()` 只被
    provider 与用例调用（`recall` 是真的接进了预案生成链路），所以一套新部署的图是空的，
    每次召回都会落到内存预案库——"案例时序知识"目前只有读路径的替身级证据 + 真 Neo4j 上的
    连通性证据，写路径要在应用里接上一条真实链路才算数。
11. **两处工程配套待补**：① 工作流外呼这条腿（`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` /
    `AEGIS_WORKFLOW_HTTP_TIMEOUT_MS`）还没进 `GET /api/v1/integrations` 的腿表，
    `.env.example` 也因为工具写入策略没被本次自动改动覆盖（需人工补两行）；
    ② Playwright 真机门禁仍不在 CI 与默认测试命令里（见"地图两道门禁"行的照实记②），
    它是唯一能证到 GPU 那一层的证据，静默停跑的风险由人承担而不是由流水线承担。

上述十一项补完后把命令、日期和输出摘要追加到上一节，并删掉对应条目。
