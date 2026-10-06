# REPORT：实测记录与证据链

这份文件只收**由真实产物量出来的数字**（命令、口径、样本量都写清楚），以及明确还没测到的东西。
代码注释里的"见 REPORT"指向这里。任何数字过期就删掉重测，不做估算。

## 环境与口径

- 日期：2026-10-01；机器：单台 x64 Windows，CPU only（无 GPU、无独立推理卡）。
- 存储后端：`store_backend=memory`（除特别说明外）；总线 `memory`；交付通道 `mock`。
  触达时延里的 1.2s 是 mock 通道适配器注入的模拟链路耗时，不是真实短信/北斗时延。
- 测试规模（`--junitxml` 计数与 `find | wc -l` 实测，2026-10-02 22:44 复核）：全量 `pytest`
  **2176 passed / 83 skipped（共 2259）**，分布在 93 个测试文件；`backend/src/` 93 个 .py、21,180 行。
  前端另有 `vitest` 327 项 / 17 个文件与 `vue-tsc` 类型检查（同轮实测）。
  （这一行每轮都重测：上一轮写的是 2136 passed / 82 skipped（共 2218）、92 个测试文件、21,112 行。
  数字过期就重测，这是这条报告自己的规矩。计数口径也换过一次：`pytest -q` 的尾行在重定向时会被截掉，
  现在一律读 junitxml 的属性，不读终端输出。）
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
| 浏览器真机渲染与影像矩形前提（同一批离线资产） | 本机 Chromium 内核（Electron 43.1.1 / Chrome 150.0.7871.114，`webgl: true`，dpr 1.5）+ `vite preview` 同源静态服务（Range 实测返回 `206` `bytes 100-117/44921`）+ `@cesium/engine@1.145` | `npm run build` 后打开 `/map`；`uv run pytest tests/unit/test_offline_tile_format.py`（19 项）；`npx vitest run src/components/map/basemap.spec.ts src/components/map/offline-assets.spec.ts`（41 项）；`npm run typecheck` + 前端 302 项 + 后端 ruff/mypy 全绿 | **这一条是真机跑出来的，295 项 jsdom 用例证不到这一层**：底图一挂上就 `An error occurred while rendering`，栈停在 `ImageryLayer._createTileImagerySkeletons` 读 `undefined.x`。数值根因写在两处源码里：PMTiles 头部把经纬度存成 1e7 定点 int32，生成器给的 85.05112877980659 经 `round()` 存成 850511288、读回 **85.0511288**，比 Web Mercator 切片方案的纬度上界（`WebMercatorTilingScheme` 反投影 ±(半长轴·π) 米 = `atan(sinh(π))` = 85.05112877980659°）大 2e-8°；而 Cesium 的注释把前提写得很直白——「imagery TilingScheme 的 rectangle 总是完全包含 ImageryProvider 的 rectangle」，越界的角点让 `positionToTileXY` 返回 undefined，紧接着就解引用 `.x`。修两侧：provider 构造时取交集（与切片方案无交集就响亮抛错，由装配层降级成"不挂影像"），生成器把边界量化改成**只往框内缩**（west/south 向上取整、east/north 向下取整）并重烘资产。复验事实：面板显示「底图：PMTiles 单文件 /basemaps/aegis.pmtiles：探针可用」「地形：自托管 quantized-mesh：/terrain」，无渲染错误遮罩，1.2s 内 199 个 rAF；`GET aegis.pmtiles` 13 次全 206、`terrain/*.terrain` 22 次全 200（滚轮缩小后又新增 10 张更粗的地形瓦，说明瓦片管线在持续跑而不是停在首帧）；影像层署名（合成高程/合成影像那句出处说明）出现在 Cesium 署名容器里；请求主机只有本机预览源。**这句现在是门禁断言，不再是观察记录**：`e2e/map-visual.spec.ts` 里新增一条离线用例，采集页面全部请求、剔除 `data:`/`blob:` 之后要求源只剩页面自身 origin，并先断言"至少抓到过一条 http 请求"以免采集器失效让断言空转——2026-10-02 真机实跑 6 项全过，116 条 http 请求、主机集合只有 `127.0.0.1`。浏览器里 Range 读回的头部边界已是 (-180, -85.0511287, 180, 85.0511287)，界内。**照实记**：①"画面上有没有内容"是用截图像素与 `globe.baseColor`（#101923 深墨）对比判定的——`readPixels` 在未开 `preserveDrawingBuffer` 的上下文里读到全 0，不能当证据；②地形 shading 与影像观感质量仍未评（合成底图本就是浅色坡面 + 经纬网，没有真影像可比）；③同一轮实测还暴露一条并已修掉：`vite preview` 对不存在的 `/basemaps/0/0/0.png` 返回 `200 text/html`（SPA fallback），而探针只看状态码就判"可用"——HTML 兜底页会被当成瓦片源挂上影像层。**判据本身也是实测出来的**：修完之后在同一页里 `HEAD /basemaps/aegis.pmtiles` 返回的 `content-type` 是**空串**，所以只能"是 HTML 就拒"，不能"不是图片 MIME 就拒"，否则真归档被误杀、底图直接消失；两条口径各写成一条用例（假 fetch + 真 HTTP）。修好后复验：兜底页判不可用、真归档仍判可用、面板仍是「底图：PMTiles 单文件…探针可用」、无渲染错误遮罩、1.0s 内 165 个 rAF |
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
| 工作流外呼节点（`api_call` / `device_control`）与其 SSRF 闸 | 本机：真应用容器装配 + 注入的假 httpx 传输（全程不触网）；结构门禁直接读 `workflow/nodes.py` 源码文本 | `uv run pytest -q tests/unit/test_workflow_outbound.py tests/unit/test_workflow_services_bridge.py`（20 + 12 项）；全量 `uv run pytest` **2025 passed / 72 skipped**；`ruff format --check`/`ruff check`/`mypy`（98 个源文件）全绿 | 独立审计抓到的实现缺口：`WorkflowServices.http_call` 字段与两个 handler 的 `require_service("http_call", ...)` 一直都在，唯独装配桥从来没传它，于是这两类节点在产品形态下 100% 抛 `NodeError`——"≥10 类节点可视化编排"这句话在 16 类里实际只有 14 类能跑；桥这一层此前**零用例**，所以谁都踩不到。反过来直接放开也不成立：URL 是编排画布上填的，等于让流程定义者决定平台往哪儿发请求（元数据端点只差一跳）。新出口 `workflow/outbound.py` 的口径是**配了主机白名单才开**：只放行 http/https、主机名精确匹配、拒绝 URL 内嵌凭据、一律不跟随重定向，异常与状态只留 `scheme://host`，调用/拒发/失败三个计数可外显；`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 留空时节点按"缺少依赖服务"响亮失败并在装配日志说明原因。**门禁防的是整类问题**：从节点源码读出全部 `require_service` 声明，逐个断言装配桥真的给出去（反向也核，防止字段堆积无人用），并用变异检查证明它抓得到——把 `notify` 从桥返回里删掉，两条用例立刻红。**照实记三条**：① 我第一版调用点断言只看字节码 `co_names`，漏 import 时它照样绿（真发生过一次 `NameError`），已改成"`__globals__` 解析得到且与唯一真源同一"；② `AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 这两个键还没进 `.env.example`（该文件受工具写入策略保护，需要人工补两行），先记在 README 配置节；③ ~~这条腿当时只在装配日志与用例里可见~~——2026-10-02 已补成 `GET /api/v1/integrations` 上的一行 `outbound`（见下一行取证），拒发原因也随之成为可读的降级事实 |
| 空间轨迹查询从应用可达（PostGIS 半径 / 轨迹面） | 本机容器：PostgreSQL 17.5 + PostGIS 3.5.2 + pgvector，经 compose 发布端口 127.0.0.1:5432；HTTP 侧用 ASGI 直连真应用 | `uv run pytest -q tests/api/test_geo_endpoints.py`（15 项）；`AEGIS_TEST_PG_DSN=… uv run pytest -q tests/integration/test_geo_endpoints_live.py`（8 项，真库跑通）；复跑既有 `tests/integration/test_persistence_postgres.py`（22 项）确认互不污染 | 审计结论是"SQL 写好了也测过了，但没有任何调用方"：`stations_within` / `hazard_trace_summary` 既没进存储协议也没进 API，所以"PostGIS 用于空间轨迹查询"只在测试进程里成立。这轮把它接成三个只读端点 `/api/v1/geo/{stations-within,stations-in-polygon,hazard-trace}`，回答里带 `driver:"postgis"` 说明是谁算的。几何能力作为**可选能力面** `SupportsGeoQueries` 单列而不并进 `StoreProtocol`：内存实现没有 PostGIS，硬要"也能查"就得再养一份 haversine/射线法，两套几何口径同时存在时看不出差别的是验收方、付代价的是被漏掉的站；缺能力时端点以 503 + `E_GEO_UNAVAILABLE` 回答并写明要配 `AEGIS_STORE_BACKEND=postgres`。真库取证：1.9km 两点按米升序、半径收紧只剩中心站、空候选集回空表、面内恰好罩住两站、区号叠加过滤仍为空、非法 WKT 与裸时间/逆序窗在触达数据库之前就被拒。**顺带修一条边界缺陷**：持久层的 `QueryArgumentError` 不是 `AegisError` 的子类，而应用侧只挂了 `AegisError` 处理器——此前没有任何端点会抛它所以从没暴露，接上几何查询后它以 500 出去（调用方会以为服务端坏了），现在补了处理器统一 422 |
| 预警准确率的库侧回放（真值表 + 与落库 warnings 配对） | 本机容器：PostgreSQL 17.5 + PostGIS + pgvector；另跑一次真 CLI 冒烟（导入 2 条标注 → 库侧回放 → 写 JSON） | `uv run pytest -q tests/unit/test_persistence_accuracy.py`；`AEGIS_TEST_PG_DSN=… uv run pytest -q tests/integration/test_accuracy_replay_live.py`（9 项，真库）；`AEGIS_PG_DSN=… uv run python -m scripts.accuracy_replay --import-labels labels.jsonl --from-store --since … --kind field` → 退出码 0，产物里 `imported_labels=2`、`status=insufficient_sample`、`official_accuracy=None` | 审计结论：算式与判据分支都在，但真值在 Postgres 里**没有表**，回放是纯 Python + 文件读，于是"PostgreSQL 用于预警准确率回放"这半句不成立。补 `sql/004_accuracy_labels.sql`（幂等，走既有 `apply_migrations` 清单；漂移门禁按文件名清单从三个改判为四个）+ `persistence/accuracy.py`：按 `case_id` upsert 导入真值，`LEFT JOIN LATERAL … ORDER BY w.generated_at LIMIT 1` 取"事件之后第一条产出"。两条口径写死在用例里：① 预测侧只认 `observed_at` **之后**发出的预警——把事件之前就存在的预警算成命中，是"预报准确率"最典型的造假方式（真库上有一条专门这样断言，结果是 fn 而不是 tp）；② 配对不按灾种过滤，否则"报对灾种"恒为真。判定算术仍只在 `replay.py`：`case_from_row` 只搬字段，且有一条用例断言它与 `parse_case` 对同一份数据产出完全相等的案例，防"两套准确率"。窗口默认 3600s、上限 6h；裸时间与逆序窗在触库前就被拒。`--kind` 默认 `unspecified`：数据没自己声明是现场标注时官方准确率恒为 None，报表自带 `provenance_warning` |
| 知识层写入侧的生产调用点（案例入库端点 + 启动期图谱索引） | 本机：真应用容器装配（内存总线，ASGI 直连，全程不触网）；图谱与 Neo4j 侧由替身驱动 | `uv run pytest -q tests/api/test_knowledge_case_endpoint.py`（15 项）/ `tests/unit/test_knowledge_provider.py`（35）/ `tests/unit/test_knowledge_wiring.py`（33）/ `tests/unit/test_knowledge_graphiti.py`（45）；全量 `uv run pytest` **2100 passed / 81 skipped（共 2181，junitxml 计数）**；`ruff format`/`ruff check`/`mypy`（93 个 .py）全绿 | 审计原话是"`learn()` 与 `prepare_schema()` 只被 provider 与用例调用"，于是一套新部署的图是空的、每次召回都落到内存预案库，"案例时序知识"只有读路径的证据。补三件事：① `KnowledgeProvider` 协议加只读成员 `driver` 与 `learn_case`，返回值 `LearnOutcome{case_id,driver,degraded,reason,detail}` 把"这条案例究竟落在哪一侧"变成响应事实——图谱写失败时降级链照旧把案例收进内存库，只看 HTTP 200 会被读成"已入图"，而图是空的；② 唯一写入口 `POST /api/v1/knowledge/cases`（读端点 `/api/v1/cases/recall` 不变；契约测试新增一条断言把它排除在"只读"模糊测试集合之外，因为一次 `add_episode` ≈ 4—7 次 LLM 调用）；③ 启动期 `warm_knowledge()` 用能力协议 `SupportsSchemaPreparation` 判定"这条腿要不要建索引"——纯内存装配根本不实现它，所以判定不是按配置字符串猜；失败与超时（上限 20s：Neo4j 驱动默认连接超时会干等约 30s，平台启动不该被一条可选腿拖住）作为 `schema_error` 进 knowledge 那一行，并出现在 `degraded` 清单里。**协议成员是不是真被管住，做了变异实测**：`runtime_checkable` 的 `isinstance` 在成员改成只读属性后仍按"属性存在与否"判定，缺 `driver` 或缺 `learn_case` 的实现都被判 False，这条写成了用例而不是靠口头保证。嵌套降级链也钉住：内层已经降级时外层原样转发事实、**不再重复写自己的内存库**（否则同一条案例两个落点、外层还会把内层的失败读成成功）。兜底那一侧写不进时是响亮抛错而不是静默丢弃。**照实记两条**：真 Neo4j + 真模型凭据下"写进去再召回"仍未跑（`AEGIS_LLM_API_KEY` 为空，`-m slow` 第二档照旧 skip）；16 条内置预案模板当时没有批量回填入口、只能逐条 POST——这一条已被下一行的批量命令补掉，逐条入口仍在 |
| seekdb 能否替掉 PostgreSQL：一次能力实测与随之而来的默认值定案 | 本机同一时刻两个引擎都在位：`5.7.25-OceanBase seekdb-v1.3.0.0`（127.0.0.1:2881）与容器 `aegis-postgres`（PG 17.5 + postgis 3.5.2 + vector 0.8.6）；同一批 6 个站点坐标、同一中心点、同一个多边形 | `docker exec aegis-postgres env` 取 `POSTGRES_*` 现拼 DSN（口令不回显）→ 两侧同一批点各算一遍距离与集合；`uv run pytest -q tests/unit/test_retrieval_wiring.py -k SeekdbIsNotInTheDefaultShape`（3 项，含两次变异注入的对照） | 起因是"seekdb + Neo4j 已够，Postgres 没必要"。**我原先的两条判断被实测推翻**：seekdb 有 `ST_GeomFromText/ST_AsText/ST_Within/ST_Contains/ST_Buffer/ST_Distance_Sphere` 与 `geometry` 列 + `SPATIAL INDEX`（列须 NOT NULL，否则 1252），而且**支持 `LEFT JOIN LATERAL`**、CHECK 约束真拦得住（3819）、外键真拦得住（1452）、`ON DUPLICATE KEY UPDATE` 幂等——所以回放那条 `LEFT JOIN LATERAL` 的形状是**可移植**的，不是墙。真差异只剩四条：① **`ST_DWithin` 不存在**（1305，`MBRContains`/`ST_PointFromText` 也没有）→ `persistence/geo.py:86` 的半径谓词得改成 `ST_Distance_Sphere(...) <= r`，代价是半径剪枝不再走索引；② **DDL 不进事务**（建表后 rollback，查 `information_schema` 表还在），而 `postgres.py` 有 3 处 `conn.transaction()`，其中"建迁移台账 + 应用 DDL"的原子性正压在这上面；③ `COPY` 批量灌数无等价物；④ `INSERT ... RETURNING` 不支持（1064，好在生产代码没用）。**口径差异量到了米**：PostGIS `geography`（WGS84 椭球）vs seekdb 球面，441 m 差 0.4 m、9.6 km 差 3.8 m、221 km 差 383.5 m、314 km 差 609.1 m（最大相对差 0.194%）；半径从 1 km 扫到 400 km，**400 档里只有 221 km 这一档两引擎集合不同**（日喀则翻出/翻进）——排序两边始终一致，面内判定 `ST_Within` 与自写射线法参照三方全一致。定案：**保留 Postgres 作关系与空间主存，把 seekdb 从默认形态里摘出去**（它本来默认就是 `local`，只是没人钉）；新增 `TestSeekdbIsNotInTheDefaultShape` 把 `aegis.retrieval.seekdb` 伪装成"导入即炸"，local 装配必须照样成功且不记 seekdb 降级，并做两次变异注入证明它咬得住——把分支改成"永不构造外部索引"时反向用例红，改成"总是构造"时 local 用例红。`.env` / `.env.example` 里的 7 个 `AEGIS_SEEKDB_*` 键保留为 POC 开关（默认 `local` 时整条路径不存在），CI 的 `--extra seekdb` 只为跑 seekdb 那套 live 用例 |
| 工作流外呼成为装配面板上的一行事实（`outbound` 腿）+ 白名单入口收敛 | 本机：真应用容器装配，外呼传输由注入的 `httpx.MockTransport` 承担（全程不触网）；前端漂移门禁直接读后端装配源码 | `uv run pytest -q tests/unit/test_integrations.py`（36 项）/ `tests/unit/test_workflow_outbound.py`（21 项）；`cd frontend && npx vitest run`（327 项，其中装配契约 28 项）；`npx vue-tsc --noEmit` | 上一轮外呼闸落地时照实记了一条"这条腿还没进 `/api/v1/integrations` 的腿表"，这次补齐：白名单为空是 `enabled=false / driver=off`，配了白名单是 `driver=http` 并带 `allowed_hosts`/`timeout_ms` 与 `calls`/`rejected`/`failures` 三个计数和 `last_error`；画布上填了不放行的主机会让这条腿显示"降级运行"，因为那正是"节点为什么一直失败"的第一现场。**顺带修掉一个凭据出口**：白名单以前按逗号切串后原样进状态面，运维写成 `user:pass@host` 等于把口令贴到匿名可读的接口上；现在条目统一过 `urlsplit().hostname` 收敛（scheme、路径、userinfo、端口一律剥），**并且与匹配侧共用同一份规范化**——两份各写一半时"配了白名单却永远匹配不上"是查不出来的那类故障。剥端口意味着白名单是主机粒度，这一点写进 docstring 与用例而不是留给人猜。前端"不硬编码腿清单"的约束这次真的用上：从装配源码抓出的 `IntegrationState(name=...)` 集合必须与 `LEG_LABELS` 键严格相等，加腿不贴标签立刻红；那条防空转的断言也从"长度为 7"改成逐条列出八条，免得"抓取被改写空了"与"两边都空"在门禁面前长得一样。**照实记**：`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` / `AEGIS_WORKFLOW_HTTP_TIMEOUT_MS` 仍未进 `.env.example`（该文件受工具写入策略保护，需人工补两行）|
| 16 类节点的引擎级分派证据 + `upstream` 必须是画布上的连线 | 本机：真装配容器（内存总线 + mock 通道 + 注入的外呼替身，全程不触网）；预警链按 `data_fetch → threshold → risk_assess → warning_generate → warning_publish → feedback_collect` 整条跑 | `uv run pytest -q tests/unit/test_workflow_node_dispatch_matrix.py`（21 项）；连同既有工作流套件 `test_workflow_engine/model/properties/services_bridge/outbound` 共 130 项一起绿；`ruff format`/`check`、`mypy` 全绿 | 上一轮只证到"装配桥把每类节点要的依赖服务都给了出去"（`require_service` 清单核对），这次补的是"每类节点都被**装配好的引擎**真的驱动过一次"：新增分派矩阵，`TestCoverage` 拿注册表 `names()` 与场景里出现的类型做双向比对（漏一类红、写了不存在的类型也红）。**矩阵当场测出两条真缺陷**：① `join` 的 `config.upstream` 指了一个没连线的节点时，此前要等实例跑到汇聚那步才炸成"汇聚节点缺少上游结果"——现场只看到"流程莫名卡住"。现补定义期交叉校验 `_validate_wired_upstreams`：配置里点名的 `upstream` 必须是该节点的入边，单值与列表同一条口径；同时留一条"连了线的正常图照常通过"防住自己改严。② 节点失败原因被压成"节点执行异常: 类名"：外呼被拒时运维只看到 `OutboundTargetError`，看不到"主机不在白名单：169.254.169.254"——因为引擎只对 `NodeError/NodeConfigError` 保留 message，其余 `AegisError` 落到通用分支把原因丢了。现在所有 `AegisError` 都原样带上，拒因同时进节点 `error` 与 `outbound` 那行的 `last_error`（这条是变异验过的：把引擎分支改回去，那条用例立刻红）。预警链那条用例钉的是"下游吃的是上游真产出的东西"：`delivery.warning_id == warning.warning_id`、`delivered > 0`（mock 通道一条都没发出去时，这条证据就是空的）。人工节点测到三段事实：挂起 `awaiting_human` → 决策恢复走到 `approve` 分支 → 非法决策值在 `resume` 门口就 400 且实例仍停在等待态（不被染成失败）。**照实记**：`feedback_collect` 单独一类跑时拿到空 warning_id 也算"成功"，所以这类证据必须由链来给，拆成单节点用例会证到假的东西 |
| 预案案例的整批回填入口 + 图谱连接的关停路径 | 本机真 Neo4j 5（`aegis-neo4j` 容器，`bolt://127.0.0.1:7687`）+ 仓库真装的 `graphiti-core`；模型凭据缺位（这一档刻意不需要它）；单测侧用真装配容器 + 替身驱动 | `uv run pytest -q tests/unit/test_ingest_cases.py`（31 项）/ `tests/unit/test_knowledge_wiring.py`（39 项）；真图 `AEGIS_TEST_NEO4J=bolt://127.0.0.1:7687 … -m slow tests/integration/test_knowledge_graphiti_live.py` → **5 collected / 3 passed / 2 skipped**；整库实跑 `AEGIS_KNOWLEDGE_GRAPHITI_URI=bolt://127.0.0.1:7687 … uv run python -m scripts.ingest_cases` → **exit=3，total 16 / landed 0 / degraded 16 / by_driver {in_memory: 16} / recall_checked 16 / close_error None** | 上一行留下的"没有批量入口"是真的：内置 16 条模板此前只有单条 POST 一个调用方，于是新部署的图谱是空的，而每次召回都从内存库拿到命中——`/api/v1/knowledge/recall` 看起来一切正常，图谱那侧一条事实都没有。补两件事：① `scripts.ingest_cases` 走**同一个** `build_knowledge` 装配点（脚本里禁止自己 new 提供者：它造出的链和生产用的链不是同一条时，"回填成功"只对本进程成立），逐条报 `driver/degraded/reason`，写完默认回读一次并报告命中的是哪条腿，契约外异常整批中止但把已落地的前缀原样交回；退出码分四档（1 中止或输入不合法 / 2 形态被拒 / 3 有降级 / 4 写得进去却召回不到），纯内存形态默认拒绝。② 关停路径：`FallbackKnowledgeProvider` 没有 `close()`、容器 `shutdown()` 也不关知识腿，图谱实例里的 Neo4j 驱动与 LLM 客户端就这么跟着进程一起烂。新增能力协议 `SupportsShutdown` 与 `integrations.close_knowledge`（上限 10s，比启动期那 20s 更短：索引没建成还能靠内存兜底继续服务，连接关不上会把整个停服卡住），失败作为 `close_error` 进 knowledge 那一行，且主腿关不上也要把兜底腿走完再把第一个异常抛出——变异实测过：把那条循环改回"先抛先退出"，`test_主腿关不上也要把兜底腿走完` 立刻红；把容器里那行 `close_knowledge` 注掉，`test_容器停服真的关掉了图谱连接` 与状态行那条一起红。**`durable` 这个字段是我自己判错过的**：第一版按"目标驱动是不是 graphiti"给值，真机一跑就露馅——图谱一条都没收下、16 条全在内存时它仍写 `durable=true`；改成只看实际落点（`landed == total > 0` 且目标是图谱），并补进单测与真图用例。**照实记**：真凭据下"16 条真的进了图谱"仍未跑（`-m slow` 第二档 2 项照旧 skip，`llm_api_key` 为空），这次实跑证明的是**命令在这种形态下说的是真话**（报 degraded=16 并退出 3），而不是回填能力已被验证；另一个上游现象记下备查：graphiti-core 在实例构造失败时会漏一个未 await 的协程（`RuntimeWarning: coroutine 'Neo4jDriver._execute_index_query' was never awaited`），不影响本仓库判定，但换版本时要重看"降级原因"还是不是同一个 |
| 后端镜像第一次真建起来并跑起来（`deploy/Dockerfile` + compose 的 `app` 这条 profile） | 本机 Docker：`docker build -f deploy/Dockerfile -t aegis-backend:verify .` + `docker run`（容器内内存总线，18000→8000，不依赖宿主 Python） | `docker build` 成功；容器内 `python -m scripts.ingest_cases --dry-run` → `rows: 16`；`/healthz`、`/readyz`、`/api/v1/stations`、`/api/v1/cases/recall?query=泥石流` 全 200；`/api/v1/integrations` 列出 knowledge=`in_memory`/`fallback_cases=16`；门禁：`tests/unit/test_deploy_env_contract.py` + `test_repo_hygiene.py` + `tests/contract` + `tests/api` 共 116 项 + 全量 `pytest` + `ruff` + `mypy`（93 个 .py） | **这是这个镜像在本机第一次被构建出来**，`--profile app` 此前从没跑过，两个只有真跑容器才暴露的缺陷当场抓出：① `python` 用的是基础镜像的系统解释器，`aegis` 不在它的搜索路径里，`docker run` 直接停在 `ModuleNotFoundError: No module named 'aegis'`——而三个可选腿的依赖在 `.venv` 里其实都装好了（aiomqtt / asyncpg / pgvector / clickhouse-connect / duckdb / neo4j+graphiti-core / onnxruntime+tokenizers / nats 逐个 import 过），修法是 `.venv/bin` 进 PATH 且 `src` 进 PYTHONPATH；② Dockerfile 写的 `ENV AEGIS_CONTRACTS_DIR=/contracts` 在配置面**没有对应的键**，`extra="ignore"` 把它静默丢掉，容器于是按源码树相对路径去找 `/app/contracts`，启动期炸成"契约目录不存在"——修法是补真实设置 `contracts_dir`（`create_container(contracts_dir=…)` 的显式覆盖仍优先），并把这类幻影键钉成门禁：`test_镜像里声明的每个AEGIS环境变量都是配置面真有的键`。**顺带补上仓库里没有的 `.dockerignore`**：没有它时构建上下文实测 1.7GB+（含 `.env`、`frontend/node_modules`、宿主 `.venv`、1.1GB 模型权重），补后 buildkit 报 `transferring context: 3.16MB`；`COPY` 的源（`backend`、`contracts`）由 `test_dockerignore不排除Dockerfile真正COPY的东西` 反向盯着，防止有人把整目录列进忽略表、把镜像掏成空壳。**照实记**：容器这次是以内存总线起的；`docker compose --profile app up`（NATS JetStream + Postgres + Neo4j 三个容器本机都在跑）仍未跑，那一步要读仓库根 .env 里的真凭据 |
| 部署形态整栈第一次跑通（compose + 真 NATS JetStream + 真 Postgres + 真 Neo4j） | 本机 Docker：`docker compose --env-file .env -f deploy/docker-compose.yml --profile app up -d --no-deps backend`，依赖侧沿用已在跑的 aegis-nats / aegis-postgres / aegis-neo4j | 容器 healthcheck 到 `Up (healthy)`；容器内读 `/api/v1/integrations` → `store=postgres`（`target=postgres:5432/aegis`，DSN 不带凭据）、`knowledge=graphiti` 且带 `schema_error="…llm_api_key 为空"`、`retrieval=hashing-ngram-1024`、`/readyz` 200；文档里那条命令原样跑：`docker compose … exec -T backend python -m scripts.ingest_cases --case-id … --case-id …` → **exit 3，total 2 / landed 0 / degraded 2 / by_driver {in_memory: 2} / durable false / recall_checked 2 / close_error None**，stderr 直接写"2 条没进图谱，落在兜底腿" | 两个只有整栈跑才会露面的东西：① 上一行修的镜像缺陷在部署形态下确认生效（`docker run` 起得来的容器现在真的能 `python -m aegis.main`）；② **服务名 DNS 不通**——这台机器上三个依赖容器是`docker run` 起的、挂在默认 `bridge` 上，而 compose 把 backend 放进了 `aegis_default`，于是 backend 里的 `nats`/`postgres`/`neo4j` 三个名字全都解析不到，日志表现是"NATS 连接异常 + Failed to DNS resolve address neo4j:7687"而**每条腿仍然各自优雅降级**（store 退回内存读视图、knowledge 落兜底库），也就是说：只看 `/readyz` 200 会把一条完全没接上依赖的部署读成成功。取证时用 `docker network connect --alias nats aegis_default aegis-nats`（postgres/neo4j 同理）把依赖挂进同一网络，三条腿立刻变成 `store=postgres / knowledge=graphiti`；测完把 alias 断开、backend 停掉，恢复原拓扑。**照实记**：图谱那一档仍是"库通着、缺模型凭据"——`schema_error` 与 `degraded=2` 说的就是这件事，"预案真的进了图"要到有 `AEGIS_LLM_API_KEY` 才能验 |
| bge-m3 / bge-reranker int8 在**部署镜像**里真的装得上（一条被 `.env` 优先级静默废掉的 P0 腿） | 本机：`aegis-backend:verify` 镜像 + `-v backend/data/models:/models:ro`（宿主权重实测 1.1GB，`bge-m3-int8/model_int8.onnx` + tokenizer 系列） | 镜像内 `build_retrieval()` → `driver=bge-m3-int8`、`rerank_leg=True`、`degraded=""`；启动期预热（装载权重）**13.7s**；`docker compose … config` 解析出的挂载源是 `E:\python_xmegisackend\data\models` → `/models`；修好后 `printenv AEGIS_RETRIEVAL_MODEL_DIR` 在容器里回 `/models` | 上一行那次整栈跑里露出第二个更阴的问题：容器 retrieval 行报的是 `driver=hashing-ngram-1024`（词面近似替身），而不是 int8 模型。根因是配置优先级——镜像 `ENV AEGIS_RETRIEVAL_MODEL_DIR=/models` 被 `env_file: ../.env` 里的 `./data/models` 盖掉（`environment`/`env_file` 高于镜像 ENV），容器于是去 `/app/backend/data/models` 找权重而那里是空的，密集腿**静默**降级成替身，`/readyz` 依旧 200：P0 那一行"bge-m3 + bge-reranker ONNX int8 CPU"在部署形态里就这样没了。修法是在 compose 的 `environment` 里把模型目录钉成挂载目标 `/models`，并加门禁 `test_retrieval_model_dir_matches_the_mounted_weights_target`（断言"挂到哪儿"与"告诉进程去哪儿找"必须是同一个路径）。**顺带纠正一个我自己的读法**：`/api/v1/integrations` 的 `dense_leg` 为 False 在这里不是故障——它报的是"有没有向量索引可写"（pgvector/seekdb 那条外部索引腿），与"ONNX 稠密向量算不算得出来"是两件事；替身与真模型的分界一律看 `driver` 与 `degraded`。**照实记**：镜像内 `search()` 端到端时延与"≤3min 预警生成"在部署形态（真权重 + 真 pgvector）下的合账仍未量，本机权重档位此前只有宿主进程级证据 |
| 预警准确率回放的 HTTP 出口（`GET /api/v1/accuracy/replay`）与真库取证 | 本机容器：PostgreSQL 17.5 + PostGIS；HTTP 侧 ASGI 直连真应用；内存 store 只用来验降级回答 | `uv run pytest -q tests/api/test_accuracy_replay_endpoint.py`（16 项）；`AEGIS_TEST_PG_DSN=… uv run pytest -q tests/integration/test_accuracy_replay_live.py`（10 项，含真库上这条端点用例）；契约测试已把该路径列入考核相关端点 | 诚实清单第 9 条那句"回放只有 CLI 入口，没有 HTTP 端点"补上了。端点**不做任何算术**：`status/indicator/official_accuracy` 全部来自 `persistence/replay.measure`，用例把 HTTP 响应与"对同一批库侧案例直接调 measure"的结果逐字段对比——防的就是两套准确率。三条判据钉住：① `kind` 默认 `unspecified`，数据集没自证是现场标注时官方准确率恒为 None 并带 `provenance_warning`；② 内存 store 没有库侧配对能力 → 503 + `E_ACCURACY_UNAVAILABLE` + 明写要配 `AEGIS_STORE_BACKEND=postgres`（能力用 `SupportsAccuracyReplay` 探测，绝不在内存里伪造第二套配对阵列）；③ 裸时间、逆序窗、配对窗越界、区划格式错全部在**触库之前**被拒，用例断言 store 的调用计数没有增加。真库取证：两条标注 + 两条落在事件之后的预警 → `status=measured`、`official_accuracy=1.0`、`indicator=met`；把 `kind` 省掉就回到 `official_accuracy=None`。**顺带收成一份真源**：`parse_moment` 从 `scripts/accuracy_replay.py` 搬进 `persistence/accuracy.py`，CLI 与端点共用——两处各写一份 ISO 解析，迟早分裂成"CLI 拒的日期界面收下了"。**照实记**：这里的 `met` 来自门禁自己写进库的两条标注，只证明通路对，不证明真实世界的准确率；现场标注数据仓库里仍然没有（也不该造一个）|

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

## 批次 B/C/D 平台侧补强实测（2026-10-03，本机 Windows / 内存总线 / mock 通道 / 无 LLM 凭据）

本轮落地《课题6_完善计划》批次 B（语义交互 + 人工上报）、C（三路融合解析 + 规则库版本化 + 标定闭环 +
解析评测集）与 D2/D3（5 灾种工作流模板 + 案例驱动推演）。数字全部来自下列可复跑命令。

| 取证项 | 命令 | 结果 |
| --- | --- | --- |
| 后端门禁规模 | `cd backend && uv run python -m pytest -q --junitxml=junit_final.xml` | **2523 项、0 失败、0 错误、83 跳过**（跳过全部是需真库/真总线/真图的用例；2259 → 2460 是批次 B/C/D 那轮，2460 → 2503 是 2026-10-04 凌晨浏览器深巡那批，见下文"夜间浏览器深巡实测"，2503 → 2507 是同日第二轮操作巡检那批，见"第二轮操作巡检实测"，2507 → 2519 是同日第四轮（内置注册判据、撤销归档、运行中改参、核签留痕、采纳标签口径、认知镜像的"没找到"、事件流心跳），见"第四轮操作巡检实测"，2519 → 2521 是助手三条缺参数芯片指路那批，2521 → 2523 是分支大小写与"没出口"说明那批） |
| 后端风格与类型 | `uv run ruff format --check src tests scripts` / `ruff check` / `mypy src` | 三项全绿（mypy 覆盖 99 个模块） |
| 前端门禁规模 | `cd frontend && npm run typecheck && npm run test` | typecheck 无错；**34 文件 / 681 项全绿**（434 → 492 是 2026-10-04 凌晨浏览器深巡那批补的门禁：画布选中态与实例聚焦 8 项、时延单位出口对账 15 项、指标页读数与失败面 8 项、助手示例句与建议条 7 项、422 detail 还原 6 项、桩客户端键集编译门禁带出的 4 项等；492 → 580 是同日第二轮操作巡检那批，见"第二轮操作巡检实测"；580 → 642 是第三、四轮那几批；第四轮本会话实跑的是 601 → 642：撤销归档 9 项、运行中改参与插入点名 4 项、核签留痕与签字人 4 项、待签工单列表 4 项、助手会话连续性与执行回执 7 项、事件流心跳与判死 3 项、等人签计数与实例列表对账 10 项、迟到的响应不许盖当前视图 10 项（流程页 7 项 + 预警抽屉 3 项）、总览那张表的两个写手不许互相抹 2 项、一张图换区域不许被上一趟无过滤快照换回 3 项源码门禁、刚发的预警不许排在最后一页 2 项、监测台账最新在上而时间轴不许跟着翻 3 项、首次开机两张空表的中文空态 2 项；此后到 675 之间是助手 ID 指路 2 项、窗口口径 4 项、区域网格 2 项、指标页新鲜度 3 项；队列自查那批 675 → 681：队列自查 3 项（页面开着也要自己长出新工单 / 后台页签不打接口 / 先发后回的旧快照不许盖回）+ 面板时间戳 2 项 + 挂载起表卸载停表的接线源码门禁 1 项） |
| 人工上报进链路（第四条接入腿） | `POST /api/v1/reports`，文本"24小时累计降雨95毫米，沟道泥位抬升1.2米"，20 次 | 20/20 产出预警与 STU；`report_intake_seconds` 台账 **P50 1.225s / P95 1.239s**，预算 300s（≤5min 口径，占 **0.41%**）；`reports={submitted:20, measured_by_rule:20, review_required:0}` |
| 上报走的是同一条链路 | `GET /api/v1/events`、`GET /api/v1/tasks/{id}`、`GET /api/v1/warnings` 按上报返回的 id 回读 | 三处均命中同一条 `trace_id`/`event_id`/`warning_id`；感知段说明文字为"上报文本判定：命中 1 条…"，与遥测路径可区分 |
| 三路融合解析准确率（合成回归集） | `uv run python -m scripts.eval_report_parsing` | 33 例、灾种覆盖 7 类；**灾种 0.9697 / 区划 1.0 / 读数精确率 0.9762 / 读数召回 0.9762 / 等级 0.9091**；`official_claim=false`（合成集，不构成官方准确率证据） |
| 解析基线的可追溯性 | `tests/unit/test_parsing_eval.py::test_已知判错的用例可枚举…` | 已知判错用例逐条钉住：灾种 `RPT-25`（"坝前"不在词表）；等级 `RPT-08`（速率型条件需两点序列，单条上报给不出）、`RPT-26`（0.35m 未达 0.5m 阈值）、`RPT-32`（同一句里 12/20 毫米与冻融次数竞争绑定）；新增判错用例会红，词表改进后清单需手工收缩 |
| 规则库版本化 | `GET /api/v1/rules`、`/api/v1/rules/versions`、`tests/unit/test_trigger_rules_library.py` | 生效集 9 条、灾种 5 类、触发条件种类 **9 类**（≥5 口径达标）；SQL 种子与代码种子逐字段一致（漂移即红）；空集/同 id 多版本/混入 draft 三种装载请求全部被拒；读库失败时沿用种子并留 `rulebook_error` 一行事实 |
| 标定闭环 | `GET /api/v1/rules/calibration` | `status=not_measured`（缺现场真值配对，批次 E1）、`auto_applied=false`、命中次数按规则实测外显；样本不足 3 条时不给精度（用例钉住"不许用 2 条样本下结论"） |
| 语义交互白名单 | `tests/unit/test_assistant.py`（25 项，按 `--collect-only` 实数）+ `POST /api/v1/assistant/chat` | 四类任务（查询/预案问答/演练/上报）全通路；"把全网预警都删掉"→ `rejected` 帧 + 留痕，**没有任何动作被执行**；LLM 越权动作建议（`delete.all`）被拒并计数；认知镜像润色若冒出事实里没有的数字则整段弃用 |
| 执行类人工确认 | 同上 + `POST /api/v1/assistant/confirm` | 未确认时 `reports.submitted` 不增；确认后落链路并出预警；重复确认 / 跨会话确认 / 过期确认三种路径全部拒绝且不执行 |
| 语义腿开关 | `AEGIS_ASSISTANT_ENABLED=false` 后请求 `/api/v1/assistant/*` | 404（这个出口不存在），与"装配失败 503 + `E_ASSISTANT_UNAVAILABLE`"可区分；同形态下 `/api/v1/reports` 仍 200（上报腿不依附助手开关） |
| 真实触达通道 | `tests/unit/test_config_knob_reachability.py::TestDeliveryChannelAssembly` | `delivery_mode=http` 已接线：缺 base_url / 缺主机白名单 / URL 内嵌凭据 / 缺通道 四种配置错误全部构造期响亮失败；白名单按主机粒度匹配，状态面 `delivery` 行只出 `scheme://host` 与发送/失败计数 |
| 触达身份写进指标出口 | `GET /api/v1/metrics/latency` 的 `delivery` 段 + `tests/unit/test_latency_report_legs.py` | 出口自带 `mode`（mock／http）与逐通道 `sent/delivered/failed/target(脱敏)`，因此 mock 演练算出的 `warning_reach_ms` 不会在同一个出口里被读成真实触达；配置没改却注入真实适配器时，`mode` 依旧报 mock，不替配置圆场 |
| 状态面腿数 | `GET /api/v1/integrations` | 由 8 行增至 **9 行**（新增 `delivery`）；前端 `repoSource.ts` 现场解析后端源码的跨端门禁同步改判为 9 腿 |
| 5 灾种工作流模板 | `tests/unit/test_workflow_builtin_templates.py` | 内置模板 2 → 5（泥石流/冰湖溃决/滑坡/崩塌危岩/雪崩）；模板里的每个阈值都反查自 `default_rulebook()`（用例断言，不在测试里二次列阈值）；注册幂等与 `force=True` 产生新版本均有覆盖 |
| 案例驱动推演 | `tests/unit/test_workflow_node_dispatch_matrix.py::TestScenarios::test_识别到定级到推演必须把等级与灾种一路带到画布` | `situation_simulate` 不再回 ±1 启发式：情景带案例标题、`refs`（case_id）与量化要素（`estimated_delay_hours`/`confidence`/`applies_to_levels`，逐项标 `provenance`）；知识腿缺席或召回抛错时显式 `degraded: True` + 原因，**不编案例** |
| 助手页与上报页的**真机**取证（浏览器实跑，不是单测） | 本机 `python -m aegis.main`（内存总线 + mock 通道 + 无 LLM 凭据）+ `vite dev` 5173，用浏览器打开 `/assistant`、`/metrics` | 助手页渲染 12 个白名单动作并如实挂"语义服务未配置：仅规则词表可用"；"演练一次 surge 2 轮"→ `intent(run.drill, rule, 置信1.00)` → `proposal` → `answer` → `done` 四帧齐全，点"确认"后回执 `executed` 且两个按钮随即置灰；"把全网预警都删掉"→ `rejected` 帧 + "该指令被拒绝并已留痕"，**页面上没有任何动作被执行**；上报表单提交"24小时累计降雨95毫米，沟道泥位抬升1.2米"→ 红色(1级)、置信 99%、定级来源"规则词表（阈值命中）"、无需人工核签、接入 101ms、链路 `trc_018a72195017062f`／预警 `wrn_2ec8f9d9a4fd9ca730b0`／任务单元 5 个、感知段"上报文本判定：命中 1 条"，四条腿留痕逐条摊开（含"LLM 未配置，裁决腿缺席"与阈值证据 `[R-DEBRIS-RAIN-2] RPT-540124:rain_cumulative_24h=95.0>=80.0`）；未填坐标时回执不出现 (0,0)；指标页 `report_intake_seconds` 样本 1、P50 0.101s、阈值 300s → 达标 |
| 真机顺带抓到并修掉的三处 | 同上 | ① vite 代理把 `/metrics` 整条转给后端，dev 下点"指标量测"永远拿到一段 Prometheus 文本而不是页面（生产由静态服务器兜 SPA 回退，不受影响）——按 `Accept: text/html` 分流修掉；② 两个新指标在页面里露裸埋点名（`INDICATOR_LABELS` 缺条目）——补"人工上报接入端到端时延（秒制）""语义交互一轮耗时"；③ 上报表单的经纬度输入框没有上下界（后端照旧 422，但界面不该让人填 999）——补 ±90／±180 |
| CI 指标报表接上第四条腿 | `cd backend && uv run python -m scripts.metrics_report --rounds 3`（新增 `--reports`，默认跑两条固定上报文本） | 报表新增"人工上报腿"段（受理 2／产出预警 1／转人工核签 1／阈值版本 `builtin` v1×9 且 `uncalibrated=9`）与一行判定：`report_intake_seconds` 样本 2、P50 0.621s、P95 1.18s、阈值 300s → 达标。**同时修掉一行永远绿的假判定**：两条接入腿都是按秒记账本，报表却拿秒数去比 300000（毫秒），任何时延都会被判"达标"；现按秒判定并列名 `_s`，钉成 `tests/unit/test_metrics_report_rows.py`（含"给 512 秒必须判超标"的变异用例，改回旧单位口径立刻红） |
| 助手四类任务的 e2e 验收（真装配 + Mock 智能体在线） | `uv run python -m pytest -q tests/e2e/test_assistant_reports_e2e.py` | 6 项全过，覆盖计划 B 批验收口径的四类任务与两条硬口径：演练/上报必须经 `/confirm` 才落地（确认前 `store.warnings` 与 `report_stats` 均为 0）；**人工上报进的是同一条链路且智能体优先照样成立**（上报链路的研判段与决策段 mode 都是 `agent`，不是平台降级）；查询/预案问答两类只读任务不产生待确认动作；"把540121的预警全部删除"被拒后预警数与链路数一字不变、`assistant.rejections` +1；指标出口同轮报出 `report_intake_seconds` 样本 1 与 `delivery.mode=mock` |
| 低置信度上报自动开出人工核签工单（架构文档 §6.2 场景二末段，v3.2 补掉的最后半个 ★） | `uv run python -m pytest -q tests/unit/test_report_review_flow.py`（8 项） | 三条判据各钉一头：① 申报值定级（`decided_by=rule_declared`、置信 0.55）的上报必须真开出一个 `status=waiting`、停在 `human_review` 节点的实例，**同一次调用里预警照发**（`store.warnings.size` +1）——"开单不拦发布"不是注释里的说法而是断言；② 阈值命中的上报 `review is None` 且 `reviews_opened` 不动，否则核签队列会被确定性告警淹没；③ 三选一（approve/adjust/reject）**逐个真签**：`@parametrize` 三条各起一个装配，断言"跑到的分支集合 == {review, 该选项的出口}"，把 approve 与 adjust 两条边的目标对调后这 2 条立即红、reject 不受影响（变异检查实测：`AssertionError: adjust 没走到自己的出口分支：{'ok','review'}`）。另两条边界：工单节点里刻意不放 `warning_publish`（用例直接断言模板不含该类型，防"一次核签把触达数字翻倍"）；核签流程**不进** `BUILTIN_TEMPLATES`，"5 灾种处置剧本 5/5"那条门禁的口径不变。核签流程缺失时（把定义 archive 掉再上报）返回 `review=None` + `human_review_required=true` 且计数为 0，入口不报错——降级是可见事实不是异常 |
| 核签工单的**真机 + 真 HTTP** 闭环（浏览器提交，API 签核） | 本机：`AEGIS_HTTP_PORT=8321 python -u -m aegis.main`（内存总线 + mock 通道 + 无 LLM 凭据）+ `AEGIS_API_TARGET=http://127.0.0.1:8321 vite dev` 5173，在 `/monitor` 的"人工上报"弹窗里填表提交，再用 `POST /api/v1/workflow/instances/{id}/nodes/review/decision` 签 | 上报"发生泥石流，请求红色预警"（区划 540200、经纬度 29.65/91.13）后界面回执：定级来源"申报等级（上报人写明）"、徽标"需人工核签"、新增一行 **`核签工单 wfi_d2c8a3384723 ｜ 待签节点 review ｜ 选项 approve / adjust / reject ｜ 签核入口 /api/v1/workflow/instances/{instance_id}/nodes/{node_id}/decision`**，同一行下面仍是"链路 `trc_010de506970452b9` ｜ 预警 `wrn_166c814ac2fc24816213` ｜ 任务单元 5 个 ｜ 感知段 上报文本判定：命中 2 条（智能体 0 条）"——**预警与工单同一次产生**，现场形态下也成立。`GET` 该实例：`status=waiting`，节点四态 `review=awaiting_human / ok,adjust,drop=pending`，`payload` 里签字人要看到的事实齐全（`note=发生泥石流，请求红色预警`、`decided_by=rule_declared`、`confidence=0.55`、`why_review=['等级来自上报人申报值而非阈值命中，转人工核签']`、`location=[91.13,29.65]`、`warning_id`）。`POST` 签 `adjust`（by=值班指挥员 + 一句批注）后：`status=succeeded`，`review` 记下决策原文，**只有 `adjust` 分支 `succeeded`（`{'notified': True}`），`ok`/`drop` 都是 `skipped`**。指标出口同轮报 `reports={"submitted": 1, "measured_by_rule": 0, "review_required": 1, "reviews_opened": 1}`。**照实记取证方式**：本轮截图不可用（in-app 浏览器无可见面，`take_screenshot` 直接拒绝），上面读的是真浏览器渲染后的 DOM `textContent` 与真 HTTP 响应体，不是 jsdom 单测；页面 console 无 error/warn。8000 端口被本机另一无关进程占用（PID 6008，未动它），因此这轮用 8321 起后端、用 `AEGIS_API_TARGET` 指过去——两个都是既有的环境变量，没改任何配置文件 |
| 工单键名的跨端漂移门禁 | `npx vitest run src/api/reports.spec.ts`（16 项） | 前端 `ReportReviewDto` 的键集合与后端 `_open_report_review()` 返回字典的键集合**逐名对比**（两边都由正则从源码现读，测试里没有第二份清单）。这条为什么值得钉：后端改键名而前端没跟上时，界面显示的是"未开出"，把一个待办悄悄读成没有待办。变异检查实测：把 `container.py` 里的 `pending_node` 改名后该条立即红（`1 failed | 15 passed`），改回即绿 |
| 语义交互这条腿的 ≤3s 终于有人判了（完成度复核收的缺陷） | `uv run python -m pytest -q tests/unit/test_latency_report_legs.py`（4 项）+ `uv run python -m scripts.metrics_report --rounds 2` | 复核时发现 `assistant_reply_ms` **只有一个记账点、没有预算**：`register_sla_budgets` 里没登记它，于是"语义交互 ≤3s"这项考核从来不会被判违约——样本再慢也不进 `violations`。这类漏登记没有症状（本机口径下它显然远小于 3s），所以只能靠用例顶着：新用例两头都钉（出口有样本 `count==1`、账本 `budget_ms==3000`），再灌一条 5000ms 的样本断言它被判成 `violations["assistant_reply_ms"]==1`；把预算那行删掉后用例红在 `KeyError: 'budget_ms'`（变异实测）。CI 报表同时补一行"语义交互一轮响应 ≤3s"，本轮实测样本 2、P50 0.0ms、越限 0 → 达标；报表里另开一段 `语义交互腿`，明写这是"无 LLM 凭据、纯规则词表"口径 |
| 部署形态端到端合账（批次 D4 收口） | Docker Desktop 4.93.0（WSL2 后端）上的整栈：`aegis-nats`(JetStream) + `aegis-postgres`(`aegis-pg:local`：PG 17.5 + PostGIS 3.5.2 + pgvector 0.8.6) + `aegis-neo4j` + 现建镜像 `aegis-backend:d4`，1.1GB 权重只读挂 `/models`；命令 `docker exec aegis-backend python -m scripts.metrics_report --rounds 5 --reports 2` | 接线只认 `/api/v1/integrations`：9 条腿，`store.driver=postgres`（`migrations_on_start=true`，不再 `read_model_only`）、`retrieval.driver=bge-m3-int8` 且 `dense_leg=true`、`delivery=mock`、`knowledge` 因缺 LLM 凭据降级（外显 `schema_error`）。容器内实测：**预警生成 ≤3min** 样本 15、P50 1.677ms、P95 1.989ms、最大 2.248ms、越限 0；**人工上报 ≤5min** 样本 2、P50 0.661s、P95 1.225s、最大 1.288s；**语义交互 ≤3s** 样本 2、P50 0.154ms、越限 0；调度两段 ≤2s P95 0.05ms / 1.68ms；触达 P50 1201.976ms（**mock 注入，不是现场触达**）。阈值版本 `source=postgres`、9 条规则×5 灾种、`uncalibrated=9`；上报台账 `{submitted:2, measured_by_rule:1, review_required:1, reviews_opened:1}`（核签工单在容器形态也真开出来了）；5 轮 14 个事件、致命错误 0。**这一档没有的**：真实网关凭据、LLM 凭据、外部智能体 ⇒ 协同成功率/同步时延真样本与准确率仍是"未测得/未覆盖"，报表里如实列着 |
| ≤3min 预警生成此前是一条永远绿的假判定（D4 才收出来） | `uv run python -m pytest -q tests/unit/test_warning_service.py`（20 项） | `warning_generation_ms` 的旧算法是 `now - record.generated_at`，而 `generated_at` 就在**同一次调用里**刚盖上——等于拿现在减自己，恒为 0.0ms：报表上那一行"样本 9、P50 0.0ms、达标"就是这么来的。修法是把链路起点（`_run_chain` 里的 `time.monotonic()`）传到执行段，没传就只量本步（含藏语翻译调用，另有一条用例断言"翻译 50ms 必须算进生成时延"）。`WarningDraft.generation_seconds` 原来是事后反推的 property，测试只写着 `>= 0.0`（永真断言）——改成随结果带出的实测秒数，并补一条"超线样本必须被判违约"（预算 180s，灌 240s → `breaches==1`）。**变异实测**：把算法改回旧口径，那条用例立即红。这条缺陷与形态无关（同一处代码），但只有在"按部署形态合账"时才会被看见——宿主档位跑报表时没人怀疑过 0.0ms |
| live 总线用例与运行中的部署共用主题契约（改判为 skip） | `AEGIS_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest -q tests/integration/test_nats_bus.py` | 四条用例在整栈跑着的时候整批红在 `err_code=10065 subjects overlap with an existing stream`。这不是平台缺陷：`stream_prefix` 只隔离"用例 vs 用例"，隔离不了"用例 vs 一个正在跑着的平台实例"，因为双方用的是**同一套契约主题**（`data.>` 等），而主题名是产品契约、外部智能体按它接入，不能为测试改。该文件的 docstring 2026-10-02 就写了这条前提，代码却仍然 fail ⇒ 现在按 10065 判 `pytest.skip` 并指名"本组用例需要干净的 JetStream，勿与部署共用"。判 skip 而非 fail 的理由：把"这台机器此刻有平台在占主题"报成红，下次部署形态取证会被误读成总线坏了 |
| 一次门禁抖动，照实记 | `cd frontend && npm test` 复跑 | 第一次全量跑里 `src/components/map/terrain-parse.spec.ts` 记 1 failed（其 14 项显示为 skipped）；单独跑该文件 14/14 通过，随后全量复跑 407/407 通过。当时浏览器与 dev server 正在跑，**根因未定位，不当作已修处理**；下次复现时先看是否与 vitest worker 复用有关。v3.2 这轮全量跑（413/413 通过）同样是在浏览器 + dev server 在跑的条件下做的，未复现那次抖动——仍然只有一次样本，不下"已消失"的结论 |
| **真机巡检：/workflow 整页空白**（本轮最严重的一处，前端从未装配 pinia） | 浏览器实跑 `npm run dev` 后逐页走七张页面 | 现象：`/workflow` 正文只剩 152 字符（导航 + 标题 + 页脚），画布、节点面板、实例区全没了。控制台 **211 条 warn、0 条 error**——Vue 只打组件栈不打异常对象，页面空白而现场查不到原因。加 `app.config.errorHandler` 把真异常打出来才看到：`getActivePinia() was called but there was no active Pinia`。根因是 `frontend/src/main.ts` **从来没有 `app.use(createPinia())`**，而 `stores/workflow.ts` 被六个画布组件在 setup 顶层取用（`WorkflowView` + Inspector/Palette/RuntimePanel）。**为什么 434 项前端测试全绿也照不出来**：每个 spec 都用 `global.plugins: [createPinia()]` 自己挂了一份，于是"组件在测试里能用"被当成了"应用装配后能用"。修法一行；另补 `src/appWiring.spec.ts` 只对账装配本身（pinia/router/antd 都在 `mount` 之前装上），变异实测：删掉 `app.use(createPinia())` 后该文件 2 条立即红。修完真浏览器复跑：画布在、16 类节点面板全出 |
| 真机巡检：装配事实面板把对象数组渲染成 `[object Object]` | 浏览器实跑 `/dashboard` 的腿面板 + `npx vitest run src/api/integrations.spec.ts` | `触达通道腿` 那行是 `channels=[object Object]、[object Object]、[object Object]、[object Object]`——`formatDetail` 对数组走 `value.join('、')`，元素是对象时被隐式 `String()`。而这条腿唯一要给人看的恰恰是"每个通道叫什么、发出去几条、失败几条"。改为数组元素逐个走同一套规则、对象摊平成 `k=v`；顺带堵掉另一条路：嵌套对象此前是 `JSON.stringify` 整坨输出，**顶层的凭据键过滤管不到对象内部**，现在每一层都过滤。修完真浏览器上 `channels=channel=sms mode=mock、…`，全文进 tooltip。既有测试钉过"单个对象→JSON"却没钉过"对象数组"，所以这个形状一路漏到真机 |
| 真机巡检：秒制指标显示在标着 `(ms)` 的列里 | 浏览器实跑 `/metrics` + `npx vitest run src/utils/metricUnits.spec.ts`（8 项） | 表头写 `P50(ms)/阈值(ms)`，可同一张表里 `ingest_end_to_end_seconds`、`report_intake_seconds` 记的是**秒**——"0.01 / 阈值 300"读成毫秒就差 1000 倍。判定本身没错（两侧同为秒才可比），骗人的只有标签，这类缺陷不会让任何东西变红，只会让人对着绿字得出错的结论。修法：单位由埋点名决定（`src/utils/metricUnits.ts`），值与单位一起显示（`0.009 s` / `5.406 ms`），列头去掉 `(ms)`，图表把秒制项换算成毫秒后再进同一根对数轴。跨端对账一条：从 `instrumentation.py` 现读所有 `_seconds` 埋点名，逐条断言前端按秒处理——后端新增一条秒制埋点而前端没跟上时这条会红 |
| 真机巡检：界面时间戳一律裸 UTC ISO，与文档口径不符 | 浏览器实跑 `/warnings`、`/monitor` | 生成时间显示成 `2026-10-03T16:45:13.840Z`。后端 `now_iso()` 全程 UTC 毫秒（线上与存储该如此，跨组件比对不能掺时区），但**界面不能就这么给人看**：值班指挥员读的是墙上时钟，那条预警实际是"次日 00:45"发的，差八小时在应急平台里不是排版问题。而 §7.2 又写着"时间 UTC+8 毫秒"——两头都不对得上。修法：`src/utils/clock.ts` 固定换算东八区并在列头标注 `（UTC+8）`，**不跟随浏览器时区**（否则同一条预警在不同机器上显示成两个时刻）；解析不了的值原样退回、空值给破折号，不猜。文档同步改成"线上一律 UTC 毫秒、界面固定 UTC+8 并标注"。真浏览器复跑：`2026-10-04 00:45:13`，跨日进位正确，与本机时钟一致 | 
| 顺带修掉的两处"线连了、值没传" | 同上（两条断言各自可变异） | ① `hazard_identify` 把结论挂在 `hazard` 子键下，`risk_assess` 原先只读顶层 → 识别→定级这条边上 hits 恒空，**永远产出"保守四级"（蓝）**；现按 2 级（橙）产出，断言直接盯 `risk_level == 2` 与依据里点名的 `R-DEBRIS-RAIN-1`。② `_situation_simulate` 原先只回传 `scenarios`/`horizon_minutes`，`degraded`/`degraded_reason`/`case_count`/`declared_level` 全被丢掉 → 画布上只剩看不出锚点与出处的等级数字；现整份外显，并从上游（含 `risk`/`hazard` 子键）并入等级、灾种、区域与命中证据 |

照实记三句：① 上表的解析准确率来自**平台侧自编的合成回归集**，它证的是"三路融合解析的算术与词表可达到的水平"，
不是现场识别率——现场数字要等 E1 的标注案例集；② `report_intake_seconds` 的 P50 1.225s 与预警生成耗时同源
（内含 mock 通道触达），因此它是"上报到链路完成"的本机口径，不是短信送达时间；③ 本轮全部取证都在
内存总线 + mock 通道 + 无 LLM 凭据的形态下完成，**带 LLM 的裁决腿与语义润色路径本轮未测得任何数字**
（`AEGIS_LLM_API_KEY` 为空时这两条腿按设计缺席，报表里 `degradations` 会写明）。



## 夜间浏览器深巡实测（2026-10-04 凌晨，本机 Windows / 内存总线 / mock 通道 / 无 LLM 凭据）

这一轮的做法与前面几批不同：**用 Playwright 与内置浏览器把八张页面逐页驱动**（每张都真点、真发请求、
真读接口回来对账），并把接口用路由拦截打成 500 / 422 / 503 各走一遍，看失败面说实话没有。
下面每一行都是这样查出来的，没有一行是读代码读出来的结论。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 核签工单在画布上能签了（本轮最要紧的一处） | 真机走 `/workflow`：点实例 → 看画布 → 点"核签通过（approve）" | 修前：运行区写着"待人工核签：review"，画布却是 **0 节点**、标题"未命名防控链路"——选不到节点就打不开决策面板，值班员只能回去 curl。修后：画布出 4 颗节点、`review` 高亮并自动选中；`POST /api/v1/workflow/instances/wfi_fdedeaf56c60/nodes/review/decision` **200**，载荷 `{choice:"approve",by:"值班指挥员"}`，实例转 `succeeded`，两条未命中的分支显示"分支跳过"，三颗决策按钮转灰 |
| 画布编辑动作逐个真机点过 | 真机在空画布上：点面板项、HTML5 拖入、拖动已有节点、handle 对 handle 拉线、删线、删节点 | 点 `data_fetch` → 画布 1 节点、头部计数"1 节点 / 0 连线"、本地校验提示消失；拖入 `risk_assess` → 2 节点且落在指针松开处；拖动节点 → `transform` 改变并写回模型；源 handle 拉到目标 handle → `.vue-flow__edge` 1 条、连线行显示 `data_fetch_1→risk_assess_1` 且条件输入可用；点"删除"→ 回到 0 连线；"删除节点"入口在。全程 0 条 pageerror |
| 画布选中态此前只活在 Vue Flow 内部 | 真机读 `.wf-node.is-selected` 与接口轮询节奏 | 视图每次由 `defToGraph` 整体重建，1.5s 一次的实例轮询把内部点选整批冲掉：`selectedNodes: []` 而右侧检查器明明显示"人工核签"。改为选中态由模型（`store.selectedNodeId`）给出后真机命中那颗节点；`focusInstance` 的程序化选中也第一次可见 |
| 节点组件被响应式代理（控制台脏） | 真机控制台计数 | `WORKFLOW_NODE_TYPES` 未 `markRaw`，被 Vue Flow 收进 reactive 状态后每次渲染刷 `Vue received a Component that was made a reactive object`；补 markRaw 后同一页控制台 **0 条 warning**。用例先把映射放进 `reactive()` 再问 `isReactive`——直接对导出对象问恒为 false，那样的门禁证明不了任何事 |
| 决策按钮中间那颗露英文裸值 | 真机读三颗按钮文案 | 模板三元式只认识 approve/reject，而定义里的候选是 `["approve","adjust","reject"]`，中间那颗直接显示 `adjust`。改为"核签通过（approve）/核签更正（adjust）/核签退回（reject）"，提交出去的仍是裸值 |
| 秒制指标被键名说成毫秒（差 1000 倍） | 真机读 `/api/v1/metrics/latency` 与页面表格逐格对账 | `ingest_end_to_end_seconds` 发的是 `p95_ms: 0.019 / budget_ms: 300`——值是按**秒**记的，键名却声称毫秒。改为账本自带 `unit` + 中性键名（`p50/p95/budget`），前端只认 `unit`；页面显示 `0.015 s / 300 s` 与真值一致。**没有改指标名**：`deploy/observability/alerts.yml` 等 44 处按名字引用，改名会打断告警与历史序列 |
| Prometheus 直方图也在这么错 | `tests/unit/test_latency_units_contract.py`（11 项）+ 真出口复核 | 直方图叫 `aegis_latency_ms`、桶是毫秒桶，而导出器把秒值原样 `observe()`——看板读到的是"0.007 毫秒"。现在灌入前按单位换算，用例断言 `aegis_latency_ms_sum{metric="ingest_end_to_end_seconds"} == 2000`（不是 2） |
| 毫秒级 KPI 被粗时钟量成恒为 0（本轮最难发现的一处） | 真机看 `/metrics` 那行 `assistant_reply_ms` 全 0 → 量时钟 → 改时钟 → 复量 | `time.monotonic()` 在本机是 `GetTickCount64()`，**步进 15.6 ms**（`get_clock_info` 的 resolution 实测 0.015625，最小步进实测 16.0 ms）。一轮约 0.3 ms 的语义交互被量成 0：修前账本 `p50/p95/max/mean` **全为 0.0**（客户端墙钟却是 11.85–16.56 ms）；改用 `perf_counter` 后同机复量得 **p50 0.739 / p95 2.202 / p99 2.347 / max 2.383 ms**（累计 5 样本时），同一进程继续跑到 9 样本为 **p50 1.477 / p95 15.537 / max 20.413 ms**。"语义交互一轮 ≤3s"此前是一条永绿的 0。同一改动也让 `warning_generation_ms` 在同机一次演练（3 条预警）后量得 **p50 67.62 / p95 74.58 / max 75.353 ms**——这个数字在 D4 修好算法、本轮修好时钟之前一直是 0.0。另补源码门禁：测量起点禁用 monotonic、终点不得与起点分属两个纪元（跨纪元相减的负数会被 `max(..., 0)` 夹成一个看起来合理的 0） |
| 人工上报接入时长用的是墙上时钟 | 同一批门禁 | `intake_seconds = max((utc_now() - started).total_seconds(), 0.0)`：一次时钟回拨就会被夹成一个像样的 0 秒。改用 `perf_counter` |
| 跨度属性也在拿毫秒比秒级预算 | `test_秒制指标的账本预算换算成毫秒后才做超时判定` | `span_stage` 不显式传预算时直接取账本值，而秒制指标的预算按秒登记（300）——于是 1.2 秒的接入（1200 ms）会被判超标。现在按指标单位换算后再判，跨度属性 `aegis.sla.budget_ms` 写出 300000 |
| 取不到数被显示成"现场没有" | 路由拦截把每张页打成 500/422/503，八张页各走一遍 | 首屏失败时态势总览写"在线智能体 **0** 个 / 已发布预警 **0** 条 / 任务单元 **0** 个"、指标页写"协同事务总数 **0** / 越限项数 **0**"——页脚明明说了取数失败。改为未取到一律 `—`；指标页另加一条留在页面上的原因行（首屏失败不谎称"还留着上一次的数"）。503 一轮八张页无可疑串、无 `[object Object]`、无 `NaN` |
| 出口形状对不上时的页面行为 | 真机对着**改动前起来的旧后端进程**（出口没有 `unit`）跑新前端 | 三个统计位都是 `—`，页面写明"时延账本没有声明单位：assistant_reply_ms（拿到 undefined，只接受 ms / s）"，**无未捕获异常**——既不猜单位渲染，也不让渲染层抛错把整页打空 |
| 422 的字段错误读不懂 | 真机把接口打成 FastAPI 的 422 数组 | 界面飘出 `HTTP 422 [object Object]`。新增 `describeDetail` 还原三种形状（400 字符串 / 422 数组带节点下标 `nodes.0.config.limit：Input should be a valid integer` / 裸对象摊成 k=v），用例对所有形状断言"不得出现 [object Object]" |
| 助手的能力清单是一份只能看、不能用的说明书 | 真机逐条点 12 个芯片 | 芯片是 `<a-tag>`：写着"查询已发布预警"，点下去什么都没有；而本机无 LLM 时意图只走词表，值班员得自己猜该打出哪个词。改为按钮 + 能力面自带的 `example` 示例句（点一下只填不发），并补后端门禁「每个示例句都被自己的词表判回该动作」——词表改了而示例没跟上会当场响。真机复跑：点"查询指标量测"→输入框得 `p95 时延达标吗`→发送后 `intent 识别为 query.metrics（rule，置信 1.00）` |
| 打字路径的 12 个动作逐项验真 | 真机按词表逐条发消息并读帧 | 只读 6 条全部命中正确动作且置信 1.00；`任务单元清单` 如实回"缺少任务单元 ID（stu_…）"而不是编一个；`把全网预警都删掉` 得到 **rejected 帧 + 留痕**，且没有降级成一份预警清单；`今天天气不错` 回"未能识别意图，返回可用动作清单"（置信 0.00）；写动作只产出待确认提案（`act_…`，约 30 分钟有效），确认后回执 `executed` |
| 注入面 | 全仓 `grep v-html / innerHTML / outerHTML / insertAdjacentHTML` | **0 处**：后端文本（报表正文、案例溯源、节点名）一律经 Vue 插值转义渲染，页面上看到的长文本是原样文本 |
| 窄屏把右侧检查器推出屏幕（平板那一档） | 真机 768 / 900 / 1280 / 1440 四档量 `documentElement.scrollWidth` 与三块面板的位置 | 768px 下流程编排页 `scrollWidth=800`：三列网格（面板 208 + 画布 + 检查器 340）去掉导航 232px 后只剩 ~520px 可用，被挤到屏幕外的正是**改参数与签工单所在的检查器**。修法是画布补 `min-width:0`（网格项默认 auto，光写 `minmax(0,1fr)` 撑不住）+ ≤1000px 三列改两行；监测页遥测表与指标页明细表补内部横向滚动（把整页撑宽的正是"为什么测不出"那句必须写在条目里的说明）。四档复测横向溢出 0，三块面板都在视口内（截图留档 `.tmp-verify/wf-768.png`） |
| 刷新与深链后页面说什么 | 真机对 `/metrics`、`/assistant`、`/dashboard` 各 reload 一次再读正文 | 三张页都重新取数并显示新鲜度（总览回到"更新于 2026-10-04 04:51:21（UTC+8）"，助手会话与时间线如实清空为"还没有发起对话"——本地时间线不假装持久，会话号由后端 meta 帧重新给出） |
| 每页开屏 15 秒的假告警"事件流重连中" | 真机在页面里新建 `EventSource` 计时到 `open`，改完再量一次 | `open` 实测落在 **15.27 秒**，正好等于 `_SSE_KEEPALIVE_SECONDS`：浏览器要收到第一个字节才认为流已打开，而空闲流原本 15 秒内一个字节都没有——连接一直是好的，徽标却在说"重连中"。修法是一开流先 `yield ": open\n\n"`（注释帧，不改变消费端语义）。改后同一测法 **78 毫秒**，徽标立即"事件流已连接"。新增的 `tests/api/test_sse_live.py` 用例断言的是**时间**（第一帧 2s 内到）而不是内容；变异实测：删掉那一行 yield，用例在 2s 处 TimeoutError 变红 |


| 桩客户端漏方法这一类缺陷 | `npm run typecheck` | 桩此前用 `as unknown as WorkflowClient` 绕过键集检查，漏掉 `definition` 时运行时只表现为一条 error 文案加一块空白画布。改为 `Record<keyof WorkflowClient, unknown>` 全量声明：客户端加方法而桩没补就是编译错误 |

照实记三句：① 这一轮全部在**降级形态**（内存总线 + mock 通道 + 无 LLM 凭据）下取证，带 LLM 的裁决腿与
语义润色路径本轮仍未测得数字；② 时钟那处结论只在 Windows 上成立（Linux 的 `monotonic` 是纳秒级），
源码门禁的意义正是让两个平台都别再退回粗时钟；③ 出口改动**没有**在部署形态（真 PG/Neo4j/NATS + Prometheus
抓取）上复跑过，`aegis_latency_ms` 的换算目前只有本机用例与本机 HTTP 出口两处证据。


## 第二轮操作巡检实测（2026-10-04 上午，Playwright 驱动 5173 + 8321，本机内存总线 / mock 通道 / 无 LLM 凭据）

第一轮是"每页都走一遍看有没有崩"，这一轮换了做法：**像值班员一样把每个入口用到底**——
每个输入框填边界值与非法值、每次保存后回去读接口真值、每个提示文案拿接口响应核对一遍。
仍然是没有一行结论来自读代码。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 同一类后端错误在三个页面上长成三种写法 | 真机分别把上报、编排、助手三条出口打成 422 | 上报表单已修（`险情描述（note）：…`），工作流只报机器名 `name：…`，**助手页把整段原因丢了**：贴 2001 字进对话框，页面只有"对话中断：对话请求失败（HTTP 422）"。三处收到 `utils/validationDetail.ts` 一处共用（字符串 / 422 数组 / 再套一层 `{detail}` / 裸对象四种形状，数字与布尔不再被吞成空串），字段名按各页自己的中文页签配标签，带下标的路径（`nodes.0.config.limit`）保持原形。工作流 store 的 `HTTP 422 流程名称（name）：…` 补上分隔，否则那串会被读成"HTTP 422 是字段名" |
| 助手页 axios 那三条不翻原因 | 真机让 `/confirm` 与 `/sessions/{id}` 分别吃 422 / 404 | `/chat`（fetch）翻了，另外三条直接把 `Request failed with status code 422` 交给界面——同一个页面两种写法。现在同用一处还原：会话 404 读成"会话不存在或已过期"，**超时没有响应体时保留 axios 原话**（那种情况下"HTTP 0"后面必须跟着真原因） |
| 上限要在填之前看得见 | 真机往对话框填 2005 字，再填小写区划与单字名义，全程数 `/chat` 请求条数 | 修前：等到 422 回来才说失败，那 2001 字还留在框里。修后：输入框带 2000 字上限并显示"N / 2000 字"计数，**填 2005 字落回 2000**；区划代码 `54012a`、上报人名义"李"这两种就地拦下，`/chat` 请求计数停在 0（合法那一条才涨到 1）。`session_id` 刻意不在前端挡：它是后端 `meta` 帧给的，本地拒绝会把整场对话锁死 |
| 抄来的上限有没有过期 | 新增 `graphLimitsContract.spec.ts` + `assistant.spec.ts`、`reports.spec.ts` 里的对账段：现读 `workflow_api.py`、`model.py`、`assistant_api.py`、`app.py` 比对 | 节点/连线数量、SLA 与超时上下界、重试次数与退避、名称与说明长度、决策值与核签意见、节点号与类型正则、消息 1..2000、上报四格的长度与区间、区划 `^[0-9A-Z]{6,24}$` 逐条与后端同源（23 项）。变异检查六处：`max_attempts` 写 6、说明写 500、消息写 1999、区划写 `{6,20}`、正文写 1999、纬度写 -91，各立即红，改回即绿 |
| 名称里的首尾空格进了定义 | 真机在检查器里把节点名改成 `  带空格的名字  `，保存后直接 `GET /api/v1/workflow/definitions/{id}` 读真值，再"新建画布 → 从列表打开"对账 | 修前存进去就是带空格的，列表里出现"看着同名、其实不同名"的两行；`"    "`（4 个空格）更能过 `min_length=2` 一路存进去，界面上一行看不见的名字。现在后端三个写模型先裁再判（真机：接口真值 `带空格的名字`，重开一致；全空白名吃 422 且 422 里点得出是哪一格），前端逆映射同步裁，所以提交的、留在 store 的、画布显示的是同一份 |
| 界面说的话与系统做的事相反 | 真机改重试次数为 5 并保存，读接口回来的 `retry` | 检查器 hint 写着"定义接口当前不接受 retry 字段，保存时不下发，仅本地留存"，而 `NodeInput` 早已收 retry（后端补它正是为了让"打开→存回"不吃 422）：实测存回的是 `{"max_attempts":5,"backoff_ms":200}`。照那句读的值班员会以为改白改，于是反复重填。文案改为"会随定义一起保存，仍需点保存定义" |
| 超界值该在哪一步处理 | 真机在超时框敲 999999（该节点 SLA=5000）、重试次数敲 99 | `max` 属性管不住键入，只管步进按钮。改为**改完即夹**：超时落回 5000、重试落回 5，与 SLA/超时那两格同一手法；名称框补 `maxlength`（真机 80 字进来剩 64） |
| 表单写着上限却不拦 | 真机在上报弹窗填小写区划、七十个字的上报人、2005 字正文，全程数 `POST /api/v1/reports` 条数 | 标题本来就写着"上报人（2..64 字，后端校验）""险情描述（4..2000 字）"，却什么都不拦——写着上限不限，等于把标题当装饰。四格补 `maxlength`、经纬度 `min/max` 改读 `REPORT_LIMITS`、正文加字数条（真机填 2005 字剩 2000 并改口"（再长就不收了）"）；`preflightReport()` 取代原先只有"空正文"那一条特例（真机小写区划时 POST 计数为 0，合法那一条照样进链路 `trc_b80856f6e4803d38` 并开出核签工单）。编排侧同源：定义名/描述、决策值、核签意见的 `maxlength` 从硬编码改读常量 |
| 五份 HTTP 客户端，四份还在把 axios 英文塞进界面 | 把接口打成 422/503 八张页逐页扫，再补跨客户端门禁 `api/errorSurface.spec.ts` | 上一轮只把"原因怎么还原"收到一处，没统一"什么时候还原"：`client/map/workflow/reports` 四份的 `error.message` 仍是 `Request failed with status code 422`，谁直接渲染 `error.message` 谁就说英文（真机八张页只剩"一张图"露出来：`数据读取失败：Request failed with status code 422`）。现在五份同构：有响应体就在构造期还原原因，没有响应体（超时/连不上）保留 axios 原话——那种情况下"请求没回来"才是事实。门禁钉三条底线（后端原文到得了界面、不漏 `[object Object]`、不剩英文原话），变异检查：把一张图那份改回 `error.message`，它自己那两条立即红、其余十四条照绿 |
| 还原前移之后叠出来的双前缀 | 单测 + 真机各走一遍"没有响应体"的超时 | `HTTP 0：接口调用失败（HTTP 0）：timeout of 20000ms exceeded`、`上报失败：上报接口调用失败（HTTP 500）：…`——一句错误里两个码，读的人以为是两次故障；没有 `detail` 的超时最容易撞（只能回落 message）。编排 store 改成"有 detail 才自己拼码"，上报表单去掉自己那句前缀 |
| 一行放不下的原因要截断 | 把 `detail` 换成一整页反代 HTML 打给五个客户端 | `detail` 是字符串时原样还原是特性（"Bad Gateway" 就该看得见），但网关错误页几千字会撑成一条满屏红字。统一折行并截到 200 字，跨客户端门禁另钉一条"不把整页塞进提示" |
| 拦下来了，屏幕上却还挂着上一条的回执 | 真机顺序：提交一条合法上报（出红色 1 级、`链路 trc_d12a795b42616be8`）→ 把区划改成 `54012a` → 再提交 | 本地拦截那条只写了错误行、没收回执，页面上同时存在"链路 trc_… ｜ 预警 wrn_…"和"这一条没有发出去"——**前一句看着就像刚提交这条的下落**。改为拦下时一并收掉回执（真机复跑：回执数从 1 变 0）。同一轮把助手那条执行腿也照实复跑：点"确认"→ 回执行"已执行 create.report（后端回执 executed）"、两颗按钮转灰。变异检查：去掉收回执那两行，新用例立即红（`expected true to be false`） |
| 单测绿而真机不一样（本轮自己踩的一处） | 先写断言 `textarea.attributes('maxlength')`，再到真机读 `getAttribute('maxlength')` | jsdom 替身里 `maxlength` 走 attrs 落到 `<textarea>`，**真 antd 的 TextArea 把它当 prop 吃掉**（真机该属性为 `null`，截断由组件在输入处做）。这种断言测的是替身不是组件，改成读模板绑定 + 真机验行为（填 2005 剩 2000）；`a-input` 那两条是真属性（真机读到 `maxlength="64"`／`"24"`），断言保留 |
| 门禁规模（本轮实跑） | `python -m pytest -q --junitxml=…` / `npx vitest run` + `npm run typecheck` | 后端 **2507 项、0 失败、0 错误、83 跳过**（2503 → 2507 是本轮工作流裁剪与全空白名 422 两条，后半段只动前端）；前端 **34 文件 / 580 项全绿**（492 → 580：校验错误还原 11 项、跨客户端一致性 16 项、助手上限与原因 22 项 + 助手页 28 项、取值域对账 23 项、检查器夹紧与文案 4 项、上报边界与截断若干、其余为随之改的断言）；typecheck 无错 |

照实记两句：① 这一轮同样只在降级形态下取证，部署形态（真 PG/Neo4j/NATS）没有复跑本轮改动；
② 本轮**没有**新抓到大面积功能缺陷，抓到的都是"话说得不准"这一类——422 原因被丢、hint 与接口相反、
上限只在后端而界面上看不见。这类问题不会让任何自动化变红，只有把人填的东西追到接口那一头才会现形。


## 第三轮操作巡检实测（2026-10-04 下午，Playwright 驱动 5173 + 8321，本机内存总线 / mock 通道 / 无 LLM 凭据）

这一轮问的是**顺序与时间**：连点、翻页后再换条件、上一轮没回来时发下一条、实时推送到不到、
以及"筛选"这两个字在两页里是不是同一件事。全部真点真数，读的是渲染后的 DOM 与真接口响应。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 总览的"链路执行记录"看不见刚发生的链路 | 真机连发两条上报，比对表格第一行与接口 `items` 尾部 | 后端 `BoundedCollection.latest()` 给的是"最近 N 条、旧→新"，表格照原样摆 → 第一页永远是旧记录，新记录在第 2 页；而同一屏的 SSE 时间线是即时的（0→2 条）。改为表格最新在上、标题写明"最新在上"，并在事件流出现表里没有的链路时补一次取数（1.2 秒内多条事件只补一次，页签在后台不补，补数失败照旧进失败行）。真机复验：新链路 `trc_243fa65d23ba285b` 产生后 3 秒即在第一行 |
| 同一条险情被重复上报成两条预警 | 真机提交成功后再点一次"提交上报"，数 `POST /api/v1/reports` | 正文提交成功后留在框里，回执看着像还在跑，再点一次就**又发一条**（实测 POST 从 1 变 2，两条链路两条预警）。改为按「区划 + 正文」判重：一字不改就按住按钮并把原因写在旁边，换个县报同一句话仍放行（真机：拦下时 POST 不变，改正文后放行） |
| 监测页的"筛选"其实不是筛选 | 人为把先发的遥测请求拖慢 1.6 秒，再连点两个区域；并对比本地窗口与后端单查的条数 | 台账里 **50,000 条**读数，页面固定取最新 2,000 条再本地挑：本地窗口里 540121 只剩 **570 条**，而后端按区域单查能填满 2,000 条——标题写着"rain_10min 时序（540121）"，读的人以为看的是这个区域的时序。一张图页一直是发参数查的，同一控件两页两种语义。改为 `region_code`/`metric` 都走后端 + 条件一变就重取；再加**请求序号守卫**（连点时先发的那个更慢回来是常态）：摘掉守卫，用例红在 `expected ['ST-慢区'] to equal ['ST-快区']`；真机复验停在后选区域、表格是后选区域的数据 |
| 翻页后换条件，表格停在空页上 | 真机翻到第 3 页再换区域 | antd 表格在数据换一批时不会把 `current` 拉回第 1 页。以前表格只有几百行够不着，筛选改走后端参数后每个区域实打实 200 页，"翻到很远再换一个只有几十条的指标"就成了普通操作：一行不显示、也不说为什么。页码改为受控、换筛选回第 1 页（真机：活动页码 `3 → 1`） |
| 对话框里按 Enter 什么都不做 | 真机敲完一句按 Enter，数请求与帧 | 0 条请求、0 帧、页面上没有任何一处说有"发送"这回事——一个多行输入框里 Enter 静默无效又不写说明，人只能判断成"我这一下没生效"。改为 Enter 发送、Shift+Enter 换行，约定写在字数条旁边（真机：1 条 `/chat`、6 帧、输入框清空）。流式进行中键盘不并发第二条：按钮那条路实测点不动（连点三次只有 1 条 POST），但键盘会绕过它——把守卫摘掉，用例红在"应调用 1 次、实到 2 次"。另确认上报表单三格里的 Enter **不会**误提交（各按一次，POST 计数始终 0），这条不改 |
| 一张图"定位到有点的区域"像颗死按钮 | 真机点击后读 `视野` 读数与未定位清单 | 站点在册但**一个经纬度都没有**（`/api/v1/stations` 的 `lon/lat` 全为 null），外接框算不出来，相机退回全局视野而界面本来就在全局视野上。现在把原因说一句（真机：`6 个在册站点都没有经纬度，只能退回全局视野…`），与同页"站点无经纬度 × 6"对得上数；顺带核对 `540100 / 540101` 后端实测 **0 条遥测**，所以"能选到的就这 3 个区域"今天不是截断，没有为不存在的问题加后端出口 |
| 探针自己错了两次（记下来免得当成产品缺陷） | 同一份脚本改数据源后复跑 | ① 把遥测全部打桩会连"区域下拉的可选项"一起造出来（自造出一个 `(全部)` 选项），必须让不筛的那条放行给真后端；② 读 `.ant-select-dropdown:not(.hidden)` 会同时命中两个面板，把"区域 4 项 + 指标 11 项"读成"区域下拉里有 15 项"——分开数之后各自正确 |
| 画布上写着"未保存"，F5 却一声不响清掉 | 真机摆 2 个节点（界面标"未保存"）后刷新，并挂 dialog 监听 | 上一批只给"新建画布/打开已存定义"加了覆盖确认，而最容易丢图的是刷新与关页：实测刷新后**节点归零、浏览器 0 次确认**。补 `installUnsavedGuard(isDirty)`（脏才 `preventDefault`，不脏不拦，卸载摘监听）。真机复验：空画布刷新 0 次弹框、有改动刷新弹的是 `beforeunload`。断言只钉 `defaultPrevented`——jsdom 把 `returnValue` 实现成 DOM Level 2 的布尔别名（赋空串读回 `false`），拿它断言就是在测替身方言 |
| 一条假设被证伪（不改，但记下来） | 真机聚焦一条实例后数 4 秒内的实例接口请求 | 怀疑过"新建画布后轮询继续为看不见的实例打接口"。实测：`focusInstance` 起了轮询，但那条实例已经 `succeeded`（终态），第一跳 `instanceMutable=false` 就自己停表——**这是正确行为**，`store.resetDefinition()` 不需要补 stopPolling；同理，运行面板保留上一个实例的号并有"已停止轮询"标记，不是"看起来还在跟" |
| 门禁规模（本轮实跑） | `npx vitest run` + `npm run typecheck` / 后端复跑 | 前端 **34 文件 / 597 项全绿**（550 → 597：总览排序与事件补数 2 项、重复上报守卫 1 项、监测筛选参数与竞态与页码 6 项、助手键盘 4 项、定位说明 4 项、刷新拦截 4 项，其余为随之改的断言）；typecheck 无错；后端 **2507 项、0 失败、0 错误、83 跳过**（本轮没动后端） |

照实记一句：本轮全部仍在降级形态下取证，部署形态（真 PG/Neo4j/NATS + Prometheus 抓取）没有复跑这些改动。


## 第四轮操作巡检实测（2026-10-04 晚，Playwright 驱动 5173 + 8321，本机内存总线 / mock 通道 / 无 LLM 凭据）

这一轮是上一轮巡检的**收尾**：真机上点"归档"把内置核签流程收走之后，页面没有任何一步能把它带回来，
于是顺着这条线查下去——问的是"一个一按生效、又撤不回的动作，代价该由谁承担"。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 归档一次，低置信度上报的核签工单整个班次都开不出来 | 真机列表读到 `人工上报核签流程 v1 archived`（上一轮点掉的，本轮仍在）；同进程内每次上报回执都是 `review=null`；接口侧 `GET /definitions` 与 `POST /reports` 各读一次 | 根因是幂等判据只认"存在过"：注册只在 `container.start()` 跑一次，运行中不补，而归档只把 status 改掉、定义仍留在版本链上，于是"这份存在"永远被当成"已注册"。改为**认状态**（只有启用中的同名定义才算注册过），被归档的内置流程在下次注册时以新版本回来，历史不动。判据摘掉状态那半条，用例红在 `assert None == '人工上报核签流程'` |
| "重启也带不回来"这句我没在真机上验证，所以不写进结论 | 读代码确认定义没有任何落库出口（`WorkflowEngine` 在 `container.py:815` 构造时不注入仓储，存储层也没有定义的表） | 定义是**进程内**的：重启必然回到出厂 6 套。所以修复的证据只有单测（引擎层"归档 → 再注册 → 启用中的 v2"）与同一进程内数小时的实测停发；"重启"这一条不在证据链里，也没当成卖点写 |
| 归档没有确认、也没有撤销 | 真机把整条动线点到底：列表 → 点归档 → 提交上报 → 回列表 → 取消归档 → 再提交 | 补 `POST /definitions/{id}/restore`（与 `archive()` 对称，不产生新版本）+ 按钮按这一行的状态显示"归档 / 取消归档" + 归档前 Modal 确认把代价说清楚。真机六步全过：确认框文案照读；点之前这一行仍是`启用中`；确认后变`已归档`且按钮改口；此时上报回执 `需人工核签，但核签流程未注册：本次没有开出工单`；点「取消归档」0 个确认框、这一行回到`启用中`；再上报得 `核签工单 wfi_2c9c1fd509fc ｜ 待签节点 review`。console 0 条 error |
| 撤销口会不会让人以为恢复了、实际仍指向归档的新版 | 同名两版：v1 建、v2 修订、归档 v2，再对 v1 发 restore | 按名字取用（核签开单、`start_from_name`）只看最新版，恢复旧版是静默无效，所以当场 400 并指名：`同名已有更新版本 v2，要恢复的是那一版而不是 v1`。把守卫摘掉，用例红在 `expected 400, got 200` |
| 状态回写画布时，会不会顺手把值班员没存的改动标成"已保存" | 打开定义 → 改节点名（脏）→ 归档同一条 → 读 `isDirty` | `syncCurrentStatus` 只在画布本来就干净时重打基线（状态不是人改的，不该弹"未保存"与刷新拦截）；有未保存改动时状态照样跟过来、脏标记照样留着。把 `if (!wasDirty)` 摘掉，用例红在"刚改的名字服务端还不知道"那条 |
| 又发现一条：值班员存的流程会被服务重启带走，而页面上它叫"服务端已存定义" | 重启 8321 前后各读一次 `GET /definitions` | 重启前 8 行（含本轮与上一轮摆的 `未命名防控链路 v1 / v2`），重启后只剩 6 行内置——存的东西一行不剩。这不是文案问题：定义确实没有落库出口。**本轮没动它**（要么落库、要么把话说成进程内会话，是两个方向的产品决策，见"完善计划"D8 待决条目） |
| 运行中改参：把一格清空，页面显示成功，旧值照旧发出去 | 真机启动核签实例 → 选 `核签通过` 那颗待执行节点 → 清空「通报内容」→ 点「以当前参数下发改参」，读响应体 + 实例台账 + 画布上那一格 | 三处各说各话：HTTP **200**、响应体里 `config.text` 仍是**原文案**、台账记 `运行中改参: ['level']`（这次根本没发 text 这个键），而画布上那格显示空。根因是两侧语义一撞——界面上"清空"= 把这个键从载荷里删掉（`ConfigFields.write(key, null)`），引擎是 `{**旧, **新}` 的按键合并，删不掉。于是值班员以为通知正文改掉了，它照旧广播。修法取"不假装"这一侧：改参回来后拿服务端配置的键与这次发出去的键比对，没能生效的点名出来。真机三例复验：只清空正文 → `这 1 格没能改成空：text——…服务端仍是原值；要改请填新值`；填回新值 → 没有失败行（不多嘴）；两格都清空 → 点名 `text、level` |
| 运行中插入的节点只活在实例里，画布一动不动 | 真机启动核签实例 → 选 `核签通过` → 插入一个 notify → 数画布节点数、读接口与实例快照 | 后端做对了：`POST …/nodes` 200、实例里多出 `notify_1`、签完 approve 之后它真的 `succeeded`（`output={"notified": true}`）——柔性这条腿是通的。可**画布节点数 4 → 4**：这个节点只属于实例，而画布画的是定义那一张图，于是点完这一下看着像没发生。补一句点名（`已在实例里插入 notify_1（画布显示的是定义那一张，这个节点只属于这条实例）`），真机复验提示条就在。要让它画得出来，得让插入接口回实例的完整图（或 `instance_detail` 带 edges），由前端按实例重建画布——那是契约改动，记入待决不当场做 |
| 数值型参数遇坏值（同一批里量完，免得留半成品结论） | 真机把 `核签通过` 那颗的 SLA 依次填 `-5`、`999999`，分"没移开焦点"与"移开焦点"两种时机读框里的值与节点卡片 | 夹回下限发生在**焦点移开**那一刻：没 blur 时框里是 -5、卡片仍是 5000（编辑中的原值，不是"已经存进去"）；blur 之后框里 100、卡片同步 `SLA 100 ms`，并且本地校验立刻指出 `节点 [ok] timeout_ms (5000) 不得大于 sla_ms (100)`、顶部挂出"画布尚有 1 处待修正"。999999 这一侧**没有上界**（前后端都不拦，卡片照收）——内置模板自己就写着 `sla_ms=900000`，所以这不是漏检，不动。结论：这一路不静默，坏了会说、且说的与卡片读数对得上 |
| 签完的核签结果在界面上读不回来（留痕只在接口里） | 真机签"核签更正"并写批注，比对页面与 `GET /instances/{id}` | 接口台账里全：`output.decision = {choice: adjust, by: …, comment: 实际是坡面裂缝…}`，`adjust` 分支 `succeeded`；而**页面一个字都不显示**——节点卡还是那句提问，运行态面板还是"等待人工决策"。这套流程的说明写着"结果留痕…供阈值标定回看"，回看的人在自己脸上看不到自己刚签了什么。补留痕行：`已签：核签更正（adjust） ｜ 签字人 巡护员扎西 ｜ 批注 实际是坡面裂缝，改判滑坡`（真机两例：不填签字人那一例显示"未填（台账记为 unknown）"，与接口的 `by="unknown"` 逐字对得上） |
| 前端把签字人写死成"值班指挥员" | 读 `stores/workflow.ts` 提交载荷，并真机签一单看台账 | 平台没有登录态，`by` 只能是签字的人自己写的字；此前每个决策都由代码替填"值班指挥员"，于是台账上那句"谁签的"从来不是一条事实、而是一句套话。改为运行面板上一格"签字人"（上界与后端 `DecisionInput.by` 同一条，走 `graphLimitsContract` 的漂移门禁），**不填就不发这个键**、由后端落成 `unknown`；后端补一条接口用例钉住"填了用填的、没填落 unknown" |
| 探针自己被"夹紧时机"骗了一次 | 同一份脚本改读法后复跑 | 第一次填完 -5 立刻读数，看到"框里 100、顶部有警告"；第二次同一步读到的是"框里 -5、什么都没有"——差别只在中间多点了一次选节点（等于 blur）。**夹紧写在 change 上，没 blur 的读数拿到的是编辑中的原值**，这类断言不固定 blur 时机就会自相矛盾 | 真机把 notify 节点的 `级别` 填成"紫色"下发改参，再签核把实例跑完 | 改参 **200**、节点 `succeeded`、`output={"notified": true}`，而 `_notify` 是 `config.get("level", "info")` 原样塞进服务载荷（`nodes.py:374-377`）——"紫色"就这么作为事件级别进了总线。定义期与运行期都不拦。**本轮没改**：给取值加约束要动 `NodeSpec` 的校验面与前端字段镜像，牵连 16 类节点，不是巡检该顺手改的（记入 D8 待决） |
| 顺带读出来的一条语义缝隙 | 读 `engine.start()` / `start_from_name()` 对 status 的处理，并跑同名归档流程 | 两处都不看 status：归档 today 只等于"从默认列表收走 + 断了按名字取用的自动化"，**并不拦执行**——画布上打开那一版照样能启动实例。与上一条一起记为待决，不在本轮擅自改成"归档即停用" |
| 探针自己选错了一次选择器 | 同一份脚本改选择器后复跑 | antd 给两个字的按钮中间插了个空格（DOM 里是"归 档"），按文案点选超时；改用 `.ant-modal-confirm-btns button.ant-btn-primary` 之后照点。这是探针的缺陷，不是页面的 |
| 探针又误读了一次"页面没吭声" | 同一份脚本改读法后复跑 | 第一次只读 `.ant-message-notice`（antd 的 toast），据此写下"改参没有任何提示"；这一页的失败面是常驻的 `.wf__error` 段落而不是 toast——改读段落之后，点名提示在 A/C 两例里都在。**"界面无提示"这类结论只按 toast 断，就会把已经修好的记成没修** |
| 门禁规模（本轮实跑） | `python -m pytest -q --junitxml=…` / `npx vitest run` + `npm run typecheck`，结论只读 junit 产物 | 后端 **2521 项、0 失败、0 错误、83 跳过**（2507 → 2509 注册判据两条，2509 → 2513 restore 三条 + 核签恢复一条，2513 → 2514 改参按键合并语义一条，2514 → 2515 签字人落账一条，2515 → 2516 采纳标签口径一条，2516 → 2518 认知镜像"没找到"两条，2518 → 2519 SSE 心跳帧一条；等人签与迟到回写两批只动前端；2519 → 2521 是助手三条缺参数芯片指路的两条（含一条"仍要明说这次什么都没查"的对照））；前端 **34 文件 / 675 项全绿**（601 → 610：撤销口 1 项、定义行按钮与确认 5 项、store 状态同步与脏标记 3 项；610 → 613：改参清空点名 2 项、改参范围说明 1 项；613 → 614：插入节点点名 1 项；614 → 618：核签留痕 3 项 + 签字人载荷 1 项，另把 `submitDecision` 的旧期望从"固定 by"改成"由签字人填"；618 → 622：待签工单排最前 / 数得出来 / 只看这些 / 筛空要解释 4 项；622 → 624：助手页换页接得上 2 项；624 → 626：F5 接得上 + 只留最近 60 帧 2 项；626 → 627：放弃过的动作刷新后不被重新递一遍 1 项；627 → 629：执行回执带上能跟进的号 2 项；629 → 632：心跳帧不进时间线 / 静默判死并重连 / 一路有心跳就不判死 3 项；632 → 642：等人签张数与实例列表对账 7 项（签完 / 绕过 / 中止 / 顶栏刷新 / 轮询进终态 / 未映射状态 / 列表失败不许报成核签失败）+ 顶栏按钮接线与 store 导出、三个动作各补对账的源码门禁 3 项；642 → 649：迟到的响应不许盖掉当前视图 7 项——连开定义、新建画布、连点实例、轮询迟到、核签迟到、中止迟到、保存迟到各一；649 → 652：预警抽屉里迟到的回查不许贴到新的那条上 3 项——链路结论、任务单元表、加载收尾各一；652 → 654：总览链路表的两个写手（轮询迟回、补数迟回）各一；654 → 657：一张图那趟迟到的无过滤快照不许换回全区域（占号在发请求之前、比对在写画面之前、守卫存在且排在第一次写之前，三条源码门禁）；657 → 659：预警列表按生成时间倒序 + 列头写明"最新在上"各一；659 → 662：监测台账倒序、时序图正序、列头口径各一（图表那条起初只查 series 会空跑，补上 xAxis 期望值后才拦得住轴被翻）；662 → 664：第一次开机的中文空态 2 项（预警表、链路表各一，变异是整块删 `#emptyText`）；664 → 666：抽屉里要能读到助手要的标识（预警标识行 + 任务标识列）各一；666 → 670：列表窗口的口径 4 项（预警页说窗口与总数、没取满不多嘴各一，链路表标题说窗口与总数、窗口未满不报总数各一）；670 → 672：风险区域网格不许拼假区划代码 + 网格标题说出窗口各一；672 → 675：指标页自己更新、时间戳、没取到数不许冒充 3 项；typecheck 无错；`ruff format --check`（213 个文件）/ `ruff check` / `mypy`（99 个源文件）全清 |
| 核签工单没有队列：11 张等人签的单混在 16 条实例里靠滚动撞见 | 造两张待签实例后逐页读：`GET /instances` 数 `status=waiting`，再在 `/workflow`、`/dashboard`、`/warnings` 三页正文里数"核签 / 待签 / 人工"字样 | 接口层 16 条实例里 **11 条 waiting**；`/dashboard` 与 `/warnings` 上三个词各 **0 次**，唯一的入口是流程页那张按创建顺序排的列表（最旧的在第一行）——低置信度上报开出的工单等的是"这条预警要不要按现状生效"，靠人翻是不负责任的。改为等人签的排最前、其余按新到旧，小标题直接写"等人签 11 张"并给一个"只看这些"的开关（真机：16 行 → 11 行且状态列全是"等待人工"，取消勾选回 16 行；筛完为空时留一句"这一列里没有等人签的实例了"，不留空列表不解释）。**跨页的待签工作台没做**——那要一个新的数据出口与页面归属，记待决 6 |
| 助手页写着"会话保留 30 分钟"，可一换页对话就整段没了 | 真机发一句拿到 `as_59af8b82cda9` → 点"一张图"再点回"智能助手"，读会话行与时间线帧数 | 后端确实在按时限保留会话，但页面把 `frames` / `sessionId` 放在组件里——路由一切换组件卸载，会话行回到"未开始"、时间线清空、**页面一句说明都没有**；没来得及签的待确认动作也跟着从页面上接不回来了（后端那 30 分钟还留着）。把对话状态搬到组件外（`composables/useAssistantSession.ts`）后真机四步复验：清空时间线只清记录、会话号留着；第二句 `POST /chat` 带的仍是原 `session_id`（接着聊而不是新开一场）；换页回来会话号与 6 帧时间线都在。**再往下一档也做了**：状态另存一份进 `sessionStorage`（只留最近 60 帧），真机按 F5 之后会话号 `as_c2c7dd7fa14f` 与那 6 帧都接得上，第二句 `POST /chat` 仍带同一 `session_id`；关掉标签页浏览器就把这块清了，与后端 30 分钟的时限各走各的 |
| 回执里"采纳"这条标签在四条腿上不是同一个意思 | 顺着"未定级的上报去哪了"往下查：真机读 `/monitor` 上报回执的逐腿区，再读 `semantic_parser._fuse` | 起初怀疑"报了警却什么都没发生"是缺陷——查下来**不是**：未定级的上报照样开出一张核签工单（`wfi_1bacfe566685`），回执逐腿写清了每条腿说了什么，这条要记成"证伪"。但回执里确实有两处会误导人：① 检索腿的 `used` 标的是"有没有同灾种佐证"，于是结论没有任何等级时页面写着 **"采纳 检索佐证 ｜ 等级=未给出等级"**；而等级真的来自 LLM 时那条腿从没被标过，永远显示"未采纳"（把 `used` 改回旧语义，新用例红在 `assert set() == {'llm'}`）；② 被采纳那条腿的置信一直是 0%（腿自己的置信没填），页面就成了"采纳 申报等级 ｜ 等级=红色 ｜ 置信=0%"。改为四条腿共用一个定义：`used = 这条腿的等级进了结论`，被采纳那条带上结论的置信。真机复验：未定级那例四条全"未采纳"；申报红色那例只有申报等级腿"采纳"且 **置信=55%** |
| 三条怀疑当场查完，前两条不成立（记下来免得当成缺陷再查一遍） | 真机逐列对账：/warnings 的"触达（演练口径）"与"区域"列对着 `GET /warnings` 的 `deliveries`/`region_codes`；/map 的"未定位 8"对着定位说明的取数 | ① "报了警却什么都没发生"**不成立**：未定级的上报照样开出核签工单，回执逐腿写明每条腿说了什么（顺着这条倒是挖出了上面那条"采纳"标签的问题）；② 触达列的分子是 `status ∈ {delivered, retried}` 的通道数（真机 3 通道 3 成功 → "3/3 通道成功"），详情区还有逐通道表，区域列 `v-for` 全量渲染 —— 都不是写死的数；③ 定位那句"6 个在册站点都没有经纬度"与表头"未定位 8"**不矛盾**：前者按 `kind === 'station'` 只数站点（接口 6 个站点、6 个都没有经纬度），后者数所有落不了图的要素——清单原文写着"站点无经纬度 × 6"加"该区域无锚点（未烘焙 region-anchors.json）× 2"= 8，两处各自准确。唯一量出来成立的是**/dashboard 同时挂着两条 SSE**（App 要 connected、总览要 events，各一次 `useEventStream()`），记为待决 7 |
| 助手页"放弃"一张待确认动作之后按 F5，那张卡又变回可确认 | 真机走完整条 proposal 路径：文本"发起一次演练" → 规则词表给出 `run.drill` 提案（`act_57c1265c8398`，30 分钟有效）→ 点放弃 → F5 | 上一批把时间线与会话号存进了 `sessionStorage`，**但没存"决定"**：刷一次页，卡片回到待确认、确认按钮又能点了——等于把值班员刚拒绝的一次演练重新递到他手上。改为决定与时间线一起存。真机复验两头：放弃过的那张刷新后仍在、确认按钮 `disabled`、旁边写着"仅本页放弃：后端没有取消接口…"；执行过的那张刷新后仍是"已执行 run.drill（后端回执 executed）"且不可再点。反例（把决定从持久化载荷里去掉）新用例即红 |
| 同一条路上量到的两件好事（不改，记下来） | 真机连点与回执文案 | ① 确认按钮在第一次点击后立刻禁用，连点两次只发出 **1 次** `POST /confirm`——上报表单那类重复提交问题在这条路上没有；② 后端没有取消接口，页面就把"放弃"明写成"仅本页放弃…确认或过期才会失效"，没有假装撤销成功 |
| 经助手报上去的险情，回执里一个能跟进的号都没有 | 真机走完整条语义路径："我要上报：540200 沟道泥位抬升 1.5 米…" → 提案卡 `create.report` → 确认 → 数接口增量、读决策行 | 执行确实落地了（实例 +1、等人签 +1），`/confirm` 也**把整份结果带回来了**——是页面只留下一句"已执行 create.report"，值班员手里一个号都没有，得自己去别的页翻。现在决策行写成 `已执行 create.report（后端回执 executed）｜ 链路 trc_b4d59b5cc163e1c9 ｜ 工单 wfi_ac0549fbf5c6`；后端没回号时明说"没回可跟进的号"，不只写"已执行"。顺带把这条路上的两个猜测量掉，并**改口一次**：**数字与单位之间的空格不影响解析**（"抬升 1.2 米"与"抬升1.2米"读出的读数一致，都是 `debris_level=1.2`）；那句上报没出预警**不是**区域阈值差异也不是丢单——我先写下"该区划没有命中的阈值"是错的，实测同一句在 540200 与 540121 表现一致：单独的"沟道泥位抬升 1.2 米"确实不命中任何规则，而把"24小时累计降雨95毫米"并进去就命中（`decided_by=rule`、等级 1，两个区划都一样）。也就是规则要的是"降雨 + 泥位"的组合触发，一句话只说泥位就不会有预警，这条与产品口径一致，不动 |
| 助手问"这条预警为什么定这个等级"，查不到时那句话读起来像查到了 | 真机拿一个不存在的号问 `explain.warning`，逐帧读 `result` 与 `answer` | `result` 帧里明明写着 `"found":false`，而给人看的 `answer` 帧只有 **`warning_id=wrn_00000000000000000000`**——`_brief` 会把假值字段滤掉，于是"没找到"这个事实整个丢了。改为 `found is False` 一律先说"没找到 + 为什么"：真机现在读作 `没找到 wrn_00000000000000000000：预警与链路的读视图只保留本进程最近若干条`。两条后端用例（查到与查不到各一，防止把"没找到"当万能前缀），反例（条件写成 `found is None`）红在 `assert '没找到' in 'warning_id=wrn_…'`。同一轮里另外两处**量过但不算缺陷**：`explain.*` 的真答案本来就有单独一段人话（`answer` 帧），`result` 帧是原样证据；缺 id 时后端直接回"缺少链路标识（trc_/evt_…）"，没有糊 |
| 后端死了，页头的 SSE 徽标还说"事件流已连接" | 真机把 8321 进程杀掉，页面上的 /dashboard 开着不动，每 20 秒读一次三个徽标与失败行，再重启后端看恢复 | 同一屏上"后端不可达"和"事件流已连接"并排挂着，而且一挂就是 45 秒以上——浏览器的 EventSource 不会因为上游进程消失而报错，原先的保活是注释帧（`: keep-alive`），**JS 根本收不到**，前端连"多久没动静"的依据都没有。改成：后端把保活发成一帧真心跳 `data: {"type":"heartbeat","ts":…}`（不进时间线，只当流的证据），前端两刻多钟没心跳就判死、自己关掉重连；后端都不在线时徽标直接说"事件流已中断（后端不可达）"。真机复验：杀进程 20 秒/45 秒两个采样点都读作 `事件流已中断（后端不可达）`，重启后 20 秒内回到 `事件流已连接` 且"更新于"继续走。**判死那一路只有单测与协议证据**（fake timers 推 40 秒 → 徽标 stalled、旧流被 close、另起一条流），"上游活着但流断了"的真机场景没造出来 |
| 签掉一张工单，小标题那句"等人签 N 张"还留在旧数上，连「刷新实例」也带不动它 | 真机启动一张新的核签实例 → 点「核签通过」→ 2.5 秒 / 6 秒 / 点按钮之后各读一次面板与 `GET /instances` | 接口那边 `status=waiting` 从 **1 → 0**，面板小标题三次读数都还是 `服务端实例（1） 等人签 1 张`——**连顶栏那颗「刷新实例」点下去也不变**。根因是两处各算各的：张数数的是实例列表（`RuntimePanel` 的 `waitingRows` 过 `store.instances`），而运行中操作只更新当前那一条的详情，没人重拉列表；那颗按钮又只绑了 `refreshInstance`（只拉详情）。数错张数不是难看而已——值班员就按这个数判断还剩几张要签。改为：核签 / 绕过 / 中止之后各补一次列表对账；轮询发现这条进了终态时对一遍（还挂在人工那步时不多打一次接口）；「刷新实例」换成把详情与列表一起对。真机复验：签完 2.5 秒面板就落到 `服务端实例（2）`（等人签归零时那句提示照旧整体消失，不写"0 张"），6 秒后与点按钮后一致，console 0 条 pageerror。反例七条各自点名红过：摘掉 `submitDecision` 的对账红 3 项、摘掉绕过与中止的红 3 项、`refreshAll` 不带列表红 1 项、轮询那半条改成"进终态才拉"的反面（无条件重拉）红 2 项、把列表失败塞回 `runAction` 的红 2 项（那句会误报成"核签失败"，而单子其实已经签出去了） |
| 一次响应迟到，页面就把"你在看哪一条"写成两条不同的链路（面板一条、画布一条） | 用 `page.route` 把某一趟响应人为拖 2 秒，再按值班员的动线点：① 连开两条定义；② 连点两条实例；③ 先看住等签那条（轮询中），趁一次轮询在路上时改点另一条 | 三条都中。① 最后打开的是 4 节点那条，画布却停在「雪崩气象型预警与交通管控流程」11 个节点；② 同理，面板实例号与画布标题分属两条；③ **最要命**：面板写着 `wfi_ed2670e23f3a`、画布画的是 `wfi_ac42219df5d8` 那一张，而轮询还在替不被看着的那条跑——这时在画布上点一个节点提交决策，发出去的是"这条实例 + 那张图的节点号"，打的是一件谁也没在看的事。根因是同一条：异步回写动手之前没人问一句"人还在看我在发起这趟请求时的那条吗"。改为 store 里加一个视图序号（切一条就 +1），六处回写各自比对（打开定义 / 保存定义 / 启动实例 / 聚焦实例两处 await / 轮询），实例级动作（核签 / 绕过 / 中止 / 插入）回的快照只在仍是当前那条时才覆盖，中止那趟还会据此决定要不要停轮询。真机复验三个方向都一致（拖慢的方向换过来也一致），另测 6 张等签的现场：签一张 6→5、再签一张 5→4，接口与面板逐次对上。规模：前端 642 → 649 项，六路变异各自红（摘掉打开定义的守卫红 2 项、保存的红 1 项、聚焦的红 1 项、轮询的红 1 项、快照按 id 的判定摘掉红 2 项、`resetDefinition` 不切序号红 2 项） |
| 探针自己不算证据的一条：正常速度的连点复现不出来 | 同一份脚本不加延迟，4 轮 ×2 方向连点 | 8/8 都一致——本机接口 1ms 级返回，两趟请求根本没来得及交错。**这类缺陷只能靠延迟注入取证，不能靠"多点几下"**；反过来也说明它在真机上要看网络 RTT，本机测不出来不等于不存在 |
| 预警页抽屉：上一条迟到的回查结论，贴到了新点开的那条身上 | 先跑一次演练造出"有链路的预警"（`POST /api/v1/drill/run` 拿到 3 条链路、各带 4-5 个任务单元），再用 `page.route` 拖住第一趟 `/api/v1/events`（2.5 秒），点 A 的「详情」→ 半秒后点 B 的「详情」 | 量到的是**一个自相矛盾的面**：抽屉标题是 B（西藏崩塌危岩预警·红色）、表里列着 B 自己的 **4 个**任务单元，顶上却挂着 A 那句 `最近 200 条链路里没有这条预警的执行记录（链路保留窗口有限），任务单元无从判断`。这句话对 B 是一个谎——值班员会以为这条预警没派任务。根因与流程页那件同一个：`inspect()` 回查链路是异步的，回来时没人问"人还在看原来那条吗"；`finally` 里的 `tasksLoading = false` 还会被迟到那次抢先收尾，把 B 判成"没有产出任务单元"。改为每次点开带一个序号，三处回写（找链路、填任务单元、收尾）各自比对。真机复验：迟到落地之后提示（无）、任务行数仍是 4。规模：前端 649 → 652 项；三路变异各自红（摘掉链路后的守卫红 2 项、摘掉任务单元后的守卫红 1 项、`finally` 不认这次点开的是谁红 1 项）。**探针在此处翻过一次车**：抽屉打开时遮罩盖住表格，`click({force:true})` 仍被遮罩接走，第二次点击根本没发生——读数看着"标题还是 A"就差点写下"抽屉没切换"的错误结论；改用 DOM 层 `btn.click()` 才真的走到 `inspect`，重测读到的才是上面这件 |
| 总览"链路执行记录"这张表有两个写手，谁后到谁说了算——15 秒轮询那份**更旧**的快照能把刚补上的新链路再抹掉 | 单测把两趟请求的先后钉死（补数快回、轮询迟回，以及反过来各一次）；真机再用 `page.route` 只迟"送"响应不迟"发"请求：`route.fetch()` 先取走挂载那趟的快照，3 秒后才 `fulfill` 给页面，中间发一次演练让 SSE 触发补数 | 这块表 15 秒轮询一次，而事件流是即时的——补数正是为了消掉"时间线里有了、表里还要等下一次轮询"。可它自己也会被抹：`refresh()` 发得早、回得晚时，那份旧快照整个替换掉 `chains`，刚补上的链路又不见了，页面上一句提示都没有。真机两档读数：摘掉守卫那趟，本次演练的 3 条新链路从 1.4 秒到 8.6 秒一直是 **0/3**，表头首个是上一轮演练留下的旧 trace；守卫回来之后，3.9 秒起 **3/3** 并一直保住。**照实记一句**：这条真机读数是佐证不是主证——拖住同一 URL 的第一趟会连带改变第二趟的落地时机（浏览器把同 URL 的并发 GET 排在后面），所以我不能声称现场看到的顺序就是自然顺序；把方向钉死的是那两条单测（正反各一路，摘掉任半条守卫对应那条就红）。规模：前端 652 → 654 项 |
| 一张图换了区域，3 秒后又被上一趟无过滤的快照换回全区域 | `page.route` 只迟"送"不迟"发"地拖住无过滤那一趟 `/api/v1/warnings`（`route.fetch()` 先取快照、3 秒后 `fulfill`），期间点刷新 → 立刻把区域换成 540121 | 复现得干干净净：换区域后未定位清单先是对的 **18 条**（4.3–5.7 秒三个采样点都是 18），旧快照在 6.2 秒递进来，**6.4 秒数字跳回全区域的 64 条并一直停在那里**，而选框上仍写着 `540121`——筛的是 A、图上画的是全部，页面一句提示都没有；30 秒自动刷新与手动刷新撞在一起是同一条路径。`load()` 里加一个号：每趟占号（占在发请求之前），写画面之前比对，不是自己那趟就直接丢掉。真机同法复验：18 条从 4.3 秒到 12.5 秒八个采样点都没再跳过。规模：前端 654 → 657 项（MapView 挂着 Cesium、这一页没有组件级挂载用例，所以是三条源码门禁；摘掉比对那行红 2 条，把占号挪到发完请求之后红 1 条） |
| 刚发出去的红色预警在这张"预警发布"页上排在最后一页 | 先读接口顺序，再逐页数页码与"生成时间"列：`GET /api/v1/warnings` 给的是**旧→新**（29 条，最早 00:22:40、最新 01:14:34），页面 `warnings.value = data.items` 原样塞进 `a-table`（每页 10 条） | 三页读数：第 1 页 `00:22:40 → 01:09:11`、第 2 页 `01:09:11 → 01:11:58`、第 3 页 `01:12:57 → 01:14:34`——**刚发那条在第 3 页（最后一页）**，而这张页的名字就叫"预警发布与靶向触达"；值班员发完红色预警回到这页，第一屏看不到自己刚发的那条，得先翻到末页。这与总览"链路执行记录"先前是同一件事（那边已按"最新在上"改过），说明这条口径没长到所有列表上。改为页面自己按 `generated_at` 倒序排（不依赖后端给的顺序），列头写明"最新在上"。真机复验：第 1 页 `01:14:34 → 01:11:58`（含最新那条）、末页落到 `00:22:40`。规模：前端 657 → 659 项；变异两路各自红（表格改回用接口原顺序红排序那条、列头去掉"最新在上"红列头那条）。**探针一次自我误读**：第一版按"标题文案"判断最新那条在不在本页，而这张表里多条预警标题相同（都是"西藏泥石流预警（红色）"），于是判成"第 1 页就有"；改按生成时间对账才看到它其实在末页 |
| 监测台账第一页摆的是最旧的 10 条读数（最新那条要翻到第 49 页） | 接口顺序先量：`GET /api/v1/telemetry?limit=500` 返回 500 条、下标 0 是最旧（`observed_at` 最小），最大值在下标 486；页面把这份数组同时喂给时序图和表格（每页 10 条） | 这张页的标题是"实时监测"，可刷新之后第一屏看到的是几十分钟前的老账。修法不能整体翻转——时序图左到右就该是旧到新，翻了轴就错——所以只给表格排一份 `ledgerReadings`（按 `observed_at` 倒序），图表继续用原序，列头写明"观测时刻（UTC+8，最新在上）"。真机复验：第 1 页 10 行时间从 `01:42:30` 非递增排到 `01:42:22`，摆的是几秒钟之内的新数；canvas 1 个（图照旧画）。规模：前端 659 → 662 项。**这条用例差点空跑**：图表那条断言起初只查 `series.data`，我把 `xAxis.data` 翻成倒序之后它照样绿——补上轴的期望值（`10:00:00 / 12:00:00 / 14:00:00`）之后同一改动立刻红，说明"配对判"才拦得住 |
| 第一次开机：两张最重要的空表只剩一句英文 "No data" | 新起一个空后端（`AEGIS_HTTP_PORT=8322`，0 预警 / 0 链路 / 0 实例）+ 另开一台 vite（5174）指向它，逐页走查七个页面的正文、告警条、空态文案、禁用按钮与 ≥400 的接口 | 首屏量到的：`/warnings`（预警发布）与 `/dashboard`（链路执行记录）落回 antd 默认英文 "No data"——整站是简体中文值班台，第一屏唯一的一句英文既没区分"确实还没有"与"取数没成功"，也没说下一步去哪儿造一条；而这台机器上线第一天的默认画面就是这个样子。两处补 `#emptyText`：中文说明 + 具体路径（监测页发起演练 / 提交会命中阈值的上报）。真机复验：两句都渲染出来、页面里 "No data" 已无，pageerror 0 条。同一次走查里 **`/map` 对 `/healthz` 发的 HEAD 收到 405**——查下来不是缺陷：探针 `probeSource` 明写"`ok` 或 405 都算可用"（不少静态服务器不接 HEAD），状态面据此仍报"源齐备"，不动，记下来免得再查一遍。规模：前端 662 → 664 项；两处变异是**整块删掉 `#emptyText`**（不是改文案），对应两条用例各红 |
| 助手页三条要 ID 的芯片是死胡同：缺参数那句只说"缺少 …ID"，而整个界面没有任何一处能读到这些 ID | 空后端上逐条点芯片 → 发送，读回每一轮的 answer 帧：`query.tasks` 得到"缺少任务单元 ID（stu_…）"、`explain.warning` 得到"缺少预警编号（wrn_…）"、`explain.chain` 得到"缺少链路标识（trc_/evt_…）"，三句都到此为止；再逐页查这些 ID 出现在哪儿——`wrn_` 与 `stu_` 在七个页面上**一处都没有**（预警表没有编号列、任务表只以 task_unit_id 作 row-key 不上屏） | 能力面把 `example` 当"点这里试一条"的预填文本交给前端，这三条示例句原样发出去必然缺参数，于是芯片成了按下去只会撞墙的入口。改两处：① 三句回答补上"去哪儿读这个 ID"（链路号在总览"链路执行记录"第一列；预警编号在预警详情抽屉顶部；任务标识在同一张抽屉的任务表里）；② 抽屉真加上「预警标识」行与「任务标识」列，指路才不落空。**没走的路**：把示例句里预填一个真 ID——能力面的 example 是写死在代码里的静态串，进程一重启那个 ID 就是假的。真机双向验：6 行抽样里 `预警标识` 与 `stu_` 全都读得到（6/6），再从抽屉抄 `wrn_4c0728335d61efd2f54a` 回助手问"解释…为什么定这个等级"，拿到的是灾种/等级/区域/通道回执那段真解释；抄 `stu_e9efcc9c9ea16597` 去"查询任务单元"也回出了这个任务。规模：后端 2519 → 2521 项（两路缺参数指路 + 一条对照"仍要明说这次什么都没查"），前端 664 → 666 项。变异：删掉抽屉「预警标识」那一行 → 前端那条红；删掉「任务标识」那一列 → 另一条红；未改文案前，后端两条中第一条即红（实测红在 `assert '预警发布' in '缺少任务单元 ID（stu_…）'`） |
| 列表其实是"最近 100 条"的窗口，而页面上没有一个字说到这一点 | 不是注入——真造数据：连发 26 次演练把这台进程推到 `warning_count=113 / chain_count=108`，再回页面看它怎么说 | `/warnings` 取 `limit=100`：页面上"窗口/最近/上限"一个字都没有，6 个页码按钮看着就是"所有预警"，**13 条预警等于凭空消失**；`/dashboard` 的链路表取 `events(20)`，卡片只写"最新在上"，看不出 88 条在表外。这与总览 KPI 早前那条"拿列表长度当总数"是同一个坑，只是这次是反方向：长度小于总数时不报差额。改为两处都按台账数说窗口——预警页取满 100 条时多一行 `列表取的是最近 100 条，服务端共 113 条——另外 13 条在这一页之外（本页没有翻页到更早数据的入口）`，链路表标题改成 `链路执行记录（最近 20 条，最新在上；…）｜服务端共 108 条`；总数来自 `/readyz`，读不到时只报窗口不编总数。真机复验两句原文照抄在上；`/readyz` 报 `50000` 条遥测对监测台账（窗口 500）与一张图（窗口 100/2000）也是同一件事，**这两处本轮没动**，记入待决 8。规模：前端 666 → 670 项；变异：删掉说明段红一条、卡片标题退回写死那句红两条 |
| 指标页写着"数据来自运行时埋点"，但它只在挂载时取一次，也没有任何时间戳 | 打开 `/metrics` 静置不用，隔 45 秒再读同一行样本数，并与 `/readyz` 的 `telemetry_count` 对 | 静置 45 秒里页面一直停在 `ingest_end_to_end_seconds 49616`，接口侧已经是 50000，而页面上"更新于"三个字**根本没有**——站着看这页的人无从判断自己读的是多久以前的实况；监测页与预警页的同款问题早前都修过（"刚发布的预警不会自己出现，而且没有任何提示"），这页是漏下的那一张。补两处：与另两页同节奏的 15 秒自动刷新（页签在后台不打接口、卸载就停表），以及 `更新于 2026-10-05 02:41:16（UTC+8，每 15 秒自动取一次）`；一次数都没取到时写"尚未取到数据"，不拿当前时间冒充"更新于"。真机复验：时间戳 02:41:16 → 02:41:31 → 02:41:46 每 15 秒自己走，`/api/v1/metrics` 一共打了 3 次（我没点任何按钮），pageerror 0 条。规模：前端 672 → 675 项；三路变异各红（去掉定时器红"自己刷新"那条、删掉时间戳那三行红"写了没有"那条、没取到数也照写时间戳红"不许冒充"那条）。顺带记下两条**查过不是缺陷**的：总览"离线状态"面板需要连续 2 次探针确认才切档，真机 52 秒采样它按时从「降级运行」翻到「源齐备」并稳住；监测页两个演练按钮已有重入守卫（`if (drilling.value) return` + `:disabled`），助手输入框也已有 `maxlength` |
| 分支名与边条件只差大小写，整条支路被静默跳过，实例还写着"成功" | 通过 `POST /definitions` 建一张最小图：`branch` 节点规则 `then="OK"`、出边条件也写 `"OK"`，再 `POST /instances` 带 `payload.risk_level=3`，读 `GET /instances/{id}` 的逐节点状态；另一张 `then="escalate"` 但只连了 `ok`/`nope` 的图用来看"完全没出口"那档；最后在浏览器里读节点卡 | 根因是**两侧归一不对称**：`EdgeDef.condition` 存下来是 `.strip().lower()` 过的（`model.py` 校验器），而节点发出的分支名是原样的（`branch` 节点的 `then` 由人填，人工核签的 `choice` 也由人写），引擎比的是 `str(output["branch"]) == edge.condition`。于是 `gate succeeded (branch="OK")`、**两条下游全部 `skipped`**，实例状态仍是 `succeeded`——那条"命中 OK 分支"的通知一条都没发出去，屏幕上没有任何线索。修法两处：① `model.normalize_branch()` 成为唯一归一函数，条件校验器与 `_edge_active` 都调它（比较前把分支值也归一），杜绝再各改各的；② 分支值确实没有任何出口连线时，把原因写在**发出分支的节点**上（原先下游只得到"上游分支未命中，级联跳过"，看不出是哪个分支值没处去）。真机复验（新起后端跑修复后的代码）：`notify_ok succeeded {"notified": true}`、`notify_else skipped`；"没出口"那档（两条边分别写 `nope`、`ok`）节点上写着 `分支 escalate 没有任何出口连线匹配（连线上写的分支名：nope、ok）；下游会被级联跳过`，在 4175 的**构建产物**预览里点这个节点，运行面板上原样读得到（Playwright 一条，留痕文本逐字对得上）。规模：后端 2521 → 2523 项（两条都先红后绿：改前红在 `expected 'skipped' == 'succeeded'` 与 `节点上没写明是哪个分支值没出口：[]`）；两路变异各红（比较退回只归一侧 / 去掉那句说明）；`ruff format --check` 213 文件、`ruff check`、`mypy` 99 文件全清，整条 suite **2523 项、0 失败、83 跳过** |
| 流程页的实例队列不会自己长出新工单，而页头还写着"事件流已连接" | 页面开着不动，用另一条路开一张新工单（`POST /api/v1/workflow/instances` 起内置「人工上报核签流程」，与同事在监测页提交上报走的是同一个出口），只读不点：10/20/40 秒各抄一次"服务端实例（N）"与"等人签 N 张"，同时读 `GET /workflow/instances` 的真实条数 | 修前：**页面停在"服务端实例（6）"整整 40 秒，而接口已经是 7 条**，"等人签"那条子一直没出现过——值班员越是盯着这一页等单子，越是什么也等不来。根因是分工缺口：唯一的定时器（`POLL_INTERVAL_MS=1.5s`）刷的是**当前盯着的那一条实例详情**，而且一遇到不可变实例就自己 `stopPolling()`；队列列表只在挂载与手动「刷新实例」时拉，从来没人自查。改三处：① `QUEUE_POLL_INTERVAL_MS=15_000` 的队列自查（与总览/指标页同节奏，页签在后台不打接口、卸载停表）；② `loadInstances` 加 `listToken`，**后发的那一趟赢**——四个写手（挂载/手动/动作后对账/自查）交错时旧快照不许把数字往回带；③ 队列标题下写 `更新于 …（UTC+8，每 15 秒自己对一遍）`，一次都没取到时写"实例列表还没取到"。**没走的路**是用事件流驱动：信号确实到了这一页（徽标"事件流已连接"），但把推送接进队列要动 `useEventStream` 的共享形态（待决 7），这一批先按同页同节奏的轮询把洞补上。真机复验（4175 构建产物，全程不点）：10s 页面 7／接口 8（那一趟 15 秒还没到）、**20s 页面 8／接口 8，"等人签 2 张"自己出现**；时间戳 03:30:23 → 03:30:38 自己走。规模：前端 675 → 681 项（队列自查、后台页签不打接口、旧快照不许盖回、面板两句时间戳、挂载卸载接线源码门禁）；六路变异各红且**各只红自己那一条**（定时器改成空转 / 摘掉可见性闸 / 摘掉 `listToken` 比对 / 没取到数也写"更新于" / 挂载不起表 / 卸载不停表）；`npm run build` **✓ built in 43.69s**、`vue-tsc` exit 0 |
| 构建产物与画面门禁 | `npx playwright test`（配置里 `webServer` 就是 `npm run build && vite preview`，跑 dist 不是 dev） | 撤销归档那批之后 **6 passed（1.2 分钟）**；核签留痕那批之后 `npm run build` **✓ built in 45.28s**（e2e 只覆盖一张图画面，未重跑——它不涉及流程页）；等人签计数那批之后 `npm run build` **✓ built in 46.31s**（exit 0），真机证据来自 5173 开发服务器（HMR 直接喂源码），dist 只验到"编得出来"；迟到回写那批之后 `npm run build` **✓ built in 44.26s**（exit 0），typecheck 单独跑也 0 错；预警抽屉那批之后 `npm run build` **✓ built in 46.78s**（exit 0）、`vue-tsc` exit 0；总览两写手那批之后 `npm run build` **✓ built in 44.09s**（exit 0）、`vue-tsc` exit 0；一张图那批之后 `npm run build` **✓ built in 46.84s**、`vue-tsc` exit 0；预警排序那批之后 `npm run build` **✓ built in 43.38s**、`vue-tsc` exit 0；监测台账那批之后 `npm run build` **✓ built in 46.91s**、`vue-tsc` exit 0；首次开机空态那批之后 `npm run build` **✓ built in 43.95s**、`vue-tsc` exit 0；窗口口径那批之后 `npm run build` **✓ built in 49.58s**、`vue-tsc` exit 0（本批只动前端，后端 suite 未重跑）；区域网格那批之后 `npm run build` **✓ built in 44.37s**、`vue-tsc` exit 0；指标页新鲜度那批之后 `npm run build` **✓ built in 45.98s**、`vue-tsc` exit 0；实例队列自查那批之后 `npm run build` **✓ built in 43.69s**、`vue-tsc` exit 0，且修前修后两份 40 秒对照都在 4175 的 dist 里读（全程不点任何按钮）；分支与队列这两批之后画面门禁重跑 **6 passed（1.2 分钟）**，但这一趟的配置要如实分开：`webServer` 没带 `AEGIS_API_TARGET`，代理打到默认的 `127.0.0.1:8000`（无人监听），日志里一串 `ECONNREFUSED`（`/healthz`、`/api/v1/events/stream`），所以这 6 条绿的是"**后端不可达时** Cesium 建场、资产解析与渲染异常不被吞"这一档，**不覆盖接上后端的动线**——接后端那部分证据靠 4175/4173 显式指 8321 的单独几趟（含本轮队列自查那份） |
| 一张图的区域筛选下拉是不是也被窗口藏住（顺着队列窗口那条线索查过来，**量过判为不是缺陷**） | 在真页上直接对账：`GET /api/v1/telemetry?limit=2000` 覆盖到哪些 `region_code`、`GET /api/v1/stations?limit=500` 的 6 个站点落在哪些区域、`/readyz` 的 store 计数，再读 `regionOptions` 的代码来源（`anchors ∪ readings`）与 `layerCounts`/`plottedCount` 的分工 | 三项都对上：这台进程的遥测窗口覆盖 540121/540221/540321 三个区域，站点台账 6 个点也恰好落在这三个区域，预警 0 条——**窗口没有藏住任何这里存在的区域**，所以不判缺陷（不拿"理论上台账更宽"当实测结论）。两处如实记下的口径边界：① 选项来自 2000 条窗口的 `readings`，台账区域一旦多于窗口区域就会"选不到"，而页面没有一个字说明选项是窗口给的（与待决 8 同族，同一张图还有一处 `notes` 已说明数据是窗口）；② 预警所在区域不进选项（只并 `anchors` 与 `readings`），本机 `warnings=0` 造不出这种区域，属于"未实测"而非"已验无问题"。顺带核对两处**已经修好且仍然对得上**的：`layerCounts` 数的是要素本身（与开关无关，"触达标记"默认关也照样数得出），`plottedCount` 才同时看开关与视野；面板四行都有中文标签（监测站点/预警落点/触达标记/灾害分区面），没裸数。另确认一份**资产确实缺而不假装**：`public/basemaps/` 里只有 README 与 `aegis.pmtiles`，`region-anchors.json`、`hazard-zones.geojson` 在源码与 dist 里都不存在，页面用两句中文点名缺哪份文件、后果是什么（区域锚点缺 → 预警与站点无处落点），并把 6 个无坐标站点收进「未定位清单」而不是画到 (0,0) |
| **查出来但本轮没修的下一件**：`/workflow` 的「服务端已存定义」列表与刚修的实例队列是同一半件事——它既不会自己长出新定义，也没有一句"这是几点取的" | 本轮 19:17 那次快照就是证据：我用 `POST /workflow/definitions` 建了两条（`大小写分支验证A/B`）并各起一条实例，同一时刻页面「服务端已存定义」只列着 6 行内置、`服务端实例（0）`，两处都与服务端真值不符且都没有时间戳；实例那一半刚补了自查（`QUEUE_POLL_INTERVAL_MS`），定义这一半还挂在挂载与手动刷新上 | ⚠ 记下来不当已修。修法与实例那半同形：把 `loadDefinitions()` 并进同一趟队列自查（各自报"定义列表没刷新 / 实例列表没刷新"，不与别的失败混写），并在标题下写 `更新于 …`；风险点要一并验：`syncCurrentStatus` 只在画布本来干净时重打基线（这条已有用例守着），自动重刷不能把值班员没存的改动标成"已保存"，也不能把正打开的那条定义的状态改动直接盖到画布上。附带一条已排除的猜想：`/monitor` 上报表单**没有**半边坐标静默丢弃的问题——`toReportBody`（`api/reports.ts:120-138`）与 `ReportForm.vue:43-46` 的提示都按后端口径（`app.py`：`lat`/`lon` 缺一即整段当没定位）处理过。**这条是读码确认，没有真机把表单填一遍**（本轮直接对 `/api/v1/reports` 发 `只填纬度` 的载荷撞的是必填项 422，走不到那条提示，故不作为真机证据） |
| 把这些修复在**构建产物**里再验一遍（此前真机证据大多来自 5173 开发服务器） | `npx playwright test`（跑 dist）+ 另起 `vite preview`：4173 指长驻后端 8321、4174 指新起的后端 8323，逐页读数 | 画面门禁 **6 passed（1.2 分钟）**。dist 里逐项对上：链路表与网格标题都带"最近 20 条"；KPI `已发布预警 121` 与 `/readyz` 真值 121 一致；预警页那句窗口口径照抄（"…服务端共 121 条——另外 21 条在这一页之外"）；抽屉里 `wrn_43b57e815a2a133a3b2d` 读得到；指标页时间戳 02:50:39 → 02:50:54 自己走；流程页"等人签 4 张"与接口 4 张一致；pageerror 0。**顺带抓到自己一个取证陷阱**：同一轮在 dist 里读到的缺参数回答仍是旧那句"缺少任务单元 ID（stu_…）"——不是构建旧，是那个长驻后端进程比 `assistant.py` 的改动更老（它在改动之前起的）。换跑当前代码的新后端（8323）+ 4174 再读，三句指路都齐：`…ID 在「预警发布」页点「详情」后那张「关联任务单元」表的任务标识列；也可以先「查询最近链路」，结果里的 task_units 就是这些 ID`、`…编号在「预警发布」页点「详情」后抽屉顶部的「预警标识」`、`…链路号在总览页「链路执行记录」的第一列`。空库那档也在 dist 里复验：预警表与链路表都是中文说明、页面里 "No data" 已无，且空库时标题里**不出现**"服务端共 N 条"（不编没有的数） |
| 风险区域网格会拼出一个**长得像真区划代码的假区域** | 直接给用例喂一条 `risk=null` 且 `task_units=['stu_5401210abcd']` 的链路（这台机器的演练数据永远带 risk，真机造不出这种链，所以这一条是代码路径取证 + 用例钉住，如实标注）；网格标题另用真机看 | 原代码 `chain.risk?.region_code ?? chain.task_units[0]?.slice(4, 10)`：没研判出风险的链，"区域"是从**任务单元号第 5 位起截 6 个字符**——`stu_5401210abcd` 截出 `540121`，一个与真区划代码完全同形的数摆进"风险区域网格"，读的人没有任何办法知道它是拼出来的（用例红在 `expected ['540121'] to deeply equal ['未标注区域']`）。改为只认真实字段，研判缺失就写"未标注区域"。同一张网格卡片吃的还是那条 20 条窗口，标题原先只写"风险区域网格"，与旁边已写"最近 20 条"的链路表不一致：现在标题同步为 `风险区域网格（按最近 20 条链路归集）｜服务端共 116 条`（真机照抄，区域列出现的是 540321/540121/540221 三个真值）。规模：前端 670 → 672 项；变异两路各红（退回截字符串红第一条、标题退回写死红第二条） |
| 跨页数字对账（本轮复核，没查出新缺陷） | 把 `/readyz`、`/api/v1/warnings?limit=100`、`/api/v1/events?limit=20` 的真值与页面上每个显示数字逐一对 | 总览 KPI `在线智能体 5 个 / 已发布预警 113 条 / 任务单元 504 个 / 协同成功率 100.0%` 与 `/readyz`、`/metrics` 全部对得上（早前那条"拿列表长度当总数"的修法在 113 > 旧 50 上限下仍成立）；`/metrics` 的"越限项数 0"与逐行 `判定` 一致（四行有阈值的 p95 都远低于阈值）。**这条也要记**：网格"更老的链里独有区域被窗口吃掉"在本机量不出差异——演练数据只覆盖 540121/540221/540321，`region_code` 传新码（540130）后端不产链，所以那半句留在纸面上、没当实测结论 |

照实记一句：本轮同样只在降级形态（内存总线 + mock 通道 + 无 LLM 凭据）取证；部署形态未复跑，
而"定义不落库"这条在两种形态下都成立（存储层没有定义的出口），所以它的严重程度不随形态变化。


## 值班员动线实测（2026-10-05，Playwright 驱动 4175 构建产物 + 8321，本机内存总线 / mock 通道 / 无 LLM 凭据）

用户看过七个页面后给的判断是"这个 web 还是不适合人来用"。这一轮不再随机找洞，而是**按值班员的一项任务走完整条动线**——
落地页 → 发现有人在等签 → 打开那张单 → 签掉 → 回落地页确认数字掉下去——每一步先量摩擦、再修，
最后一条探针把整条动线重跑一遍并留下读数（`test-results/op-walkthrough/summary.json` 修前 / `summary2.json` 修后）。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 落地页对"有人在等签"一无所知 | 修前探针：接口读 `waitingCount` 真值，同时在 /dashboard 正文里找"待签／等人签／核签／工单／人工／待办"六个词 | 真值 **2 张**在等，六个词正文命中 **0 次**（`dashboardHints: []`——命中的只有导航条目"流程编排／智能助手／态势总览"）。值班员落在这页开工，屏幕上没有任何一处告诉他"有两张单等你签"。修法：`DashboardView.vue` 的 refresh 并取 `workflowApi.instances()`，`pendingSign` 用 null 表示"还没数到"（不与 0 混）；命中 >0 时页头挂 `有 1 张工单在等人签。` + `去处理` 链到流程页。修后真机条子 y=118、`landingUrl` 就在 /dashboard。取数失败时整条不出现也不写 0——"没数到"不许冒充"没有单" |
| 点进"流程编排"第一屏是空草稿的报错，等签队列在折叠以下 | 修前探针读 /workflow 首屏：队列标题文字与它是否在 1440×900 视口内（`boundingBox`），同时读画布校验条 | 首屏是一张"未命名防控链路"空草稿加红字"工作流至少需要一个节点"，而 `服务端实例（8） 等人签 2 张` 的队列在右栏折叠以下（`queueVisibleWithoutScroll: false`）——等签的人打开这一页，看到的是报错，不是自己的单。修法：`WorkflowView.vue` 画布上方加待办条（`有 N 张工单在等人签。` + `打开第一张`），`store.openFirstWaiting()` 打开**新到旧里最新**的那张（`[...instances].reverse().find`）。修后真机条子 y=134，点「打开第一张」直接落在等签节点上，探针全程走通 |
| 界面夹源码腔：运行态面板四处 `（engine.py:539-540）`，节点卡写裸枚举 `approve / reject` | 修前真机照抄 runtimeText；修后再抄一遍全文，并单独扫 `engine.py` 字样 | 修前四处行号原文在案（539-540 / 582-583 / 547-569 / 515-516）；这类括号对改代码的人有用，对签字的人只是噪音。删除后引用回注释与文档里查；候选决策改中文：按钮 `核签通过（approve）`（中文 + 原值后缀，与命令/回执里的枚举对得上），节点卡窄位置只用中文 `核签通过 / 核签退回`。修后 `enginePyOnWorkflowPage: false`（整页扫过） |
| 决策按钮落在 1440×900 首屏之外 | 探针打开第一张单、选中等签节点后按 `boundingBox` 量「核签通过」按钮底边，断言 ≤ 900 | 修前实测 **976.33**，断言落红——核签控件排在三道控件与长说明（改参／旁路／插入）之后，打开第一张单还得在右栏滚一段才够得着。修法：决策区（签字人／核签意见／候选按钮）整体前置到运行态面板最上（留痕行之后）。修后 `y=756.03 + h=33.59 = 789.6` 过线。防回归的是源码顺序门禁（决策块必须排在"以当前参数下发改参"之前），变异把决策块**真搬回**改参行之后即红 |
| 整条动线复验（探针一条跑完） | /dashboard 发现 → 去处理 → 打开第一张 → 填签字人 `值班员-验收` → 点「核签通过」→ 回 /dashboard 对账 | 全过：条子 y=118 → 编排页条子 y=134 → 决策可点 789.6 → 留痕 `已签：核签通过（approve） ｜ 签字人 值班员-验收 ｜ 批注 （无批注）` → 回落地页待办条消失（`（无待办条）`，接口侧 1 → 0 与页面对账）→ `errors: []`（pageerror 0）。**照实记**：探针每跑一次真签掉一张单，两张原待签工单都被验证消耗掉，0 张的现场直接再跑会在第一步就停——复验前要先造数 |
| 门禁规模（本轮实跑） | `npx vitest run` + `npx vue-tsc --noEmit` + `npm run build`（后端本批未动） | 前端 **34 文件 / 690 项全绿**（681 → 690：总览待办条 3 项、编排页待办条与 store 2 项 + 2 项源码门禁、运行态去行号 1 项、决策前置顺序门禁 1 项；另把两张 human 卡片用例的断言从裸枚举改中文）。六路变异**各只红自己那条**：把总览工单数写死为 0 → `有单在等时点名叫人处理，并给出去流程页的入口`（1 failed \| 18 passed）；待办条少一次倒序 → `待办条打开的是列表首行那张：新到旧里最新的一个等签`（1 failed \| 61 passed）；回夹源码行号 → `运行态面板的说明是给人读的，不夹源码行号`（1 failed \| 57 passed）；决策块真搬回改参之后 → `决策控件排在改参/旁路/插入之前`（1 failed \| 57 passed）；NodeHuman 退回裸枚举 → 两张 human 卡片用例红（2 failed \| 56 passed）；编排页待办条 `v-if` 改 false → `只在这一页真有等签工单时摆出，动作接 store.openFirstWaiting`（1 failed \| 57 passed）。`vue-tsc` exit 0、`npm run build` **✓ built in 22.06s** |

照实记两句：**待决 6（跨页待签工作台）只履行了一半**——落地页与编排页现在都知道"有几张在等、一键能打开"，
但"专门的一张待签工作台页"（按 pending_node + 上报上下文出的数据口）仍缺页面归属决策，待决 6 保留；
本批取证同样只在降级形态，且**全在构建产物里**（4175 指 8321），证据等级与前几批的 dist 档一致。

## UI 基座改造实测（2026-10-05，Playwright 驱动 4175 构建产物，对标 NexusMind）

用户对七个页面的判词是"整体的UI也很差，你可以参考一下nexusmind的前端风格"，并把方向钉死：
"tab几个界面的按钮学nexusmind放在屏幕上面偏中间这种……越像越好"。经确认按三件事拍板：范围分两批
（本批=基座，第二批=逐页 hero，待办 #25）、主色 #2563EB、字体自托管（保"弱网离线可用" P0）。
对标物不是猜的：NexusMind 真实代码 `frontend/src/views/Process.vue` 的 `.navbar` / `.immersive-stepper`
（1914–2216 行）就是那套顶栏——玻璃拟态横条、绝对居中步进器、渐变选中药丸、右侧状态胶囊，本批逐值移植。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 顶栏步进器真的居中（非"看起来居中"） | 1440×900 真机读 `.nav-center` 包围盒中心与视口中线之差 | 步进器中心点 **720.05 vs 720，偏差 0.05px**；条高 68px；选中药丸计算值 `linear-gradient(135deg, rgb(37,99,235), rgb(29,78,216))` |
| 1280 窄屏不许两组相撞 | ≤1400px 时绝对居中回退为三段弹性排布；1280 重载后量三组包围盒 | 品牌右缘 218.1 = 中心组左缘 218.1、**中心组右缘 941.4 = 状态组左缘 941.4，零重叠**（截图 ag2-dashboard-1280.png 存档）。**这条量的是三组"外框相接"，文字级重叠当时没测**——第三批机器体检在 1280 量到副标题与页码徽标互压 290px²，顶栏已改三列网格并常驻门禁（见"UI 缺陷体检"一节） |
| 设计令牌落没落实到计算样式 | 真机读卡片/标签/主按钮 computedStyle | 卡片 radius `16px`、border `rgb(230,232,239)`、shadow `rgba(0,0,0,0.04) 0px 2px 8px`；标签 `999px` 胶囊；主按钮渐变 + `rgba(37,99,235,0.22) 0px 6px 16px` |
| 字体自托管、"弱网离线可用"未被破坏 | 监听整页请求 URL；`document.fonts` 清单与品牌元素计算字体栈 | 加载 dashboard **外部请求 0 条**（全部本机 4175），woff2 全取自 `/assets/*.woff2`；SG 400/600/700 与 JBM 700 `loaded`，品牌字体栈命中 'Space Grotesk'。7 个拉丁子集 woff2 共 183,448 字节随构建分发（OFL-1.1，附 LICENSE-OFL.txt） |
| 会静默回归的三件事进源码门禁 | 新增 `src/uiTheme.spec.ts` 5 项（外链检查＋文件在盘＋字重齐＋main.ts 引入顺序＋主色令牌＋**顶栏覆盖 router 全部页面路由**） | 690 → **695 项全绿**（35 文件）、`vue-tsc` exit 0、`npm run build` **✓ built in 31.95s**；五路变异各红各的：删 Space Grotesk 600 块 → 字体 2 项红；字体源换 fonts.gstatic 外链 → 自托管项红；抽 theme.css 引入 → 入口项红；顶栏漏 `/metrics` → 覆盖项红；主色改 `#1677FF` → 令牌项红；全部还原后 5 passed、无 `.mutbak` 残留 |

照实记：本批只动外壳与全局样式（`App.vue`、`main.ts`、`styles/`、字体资产），页面内部结构留给第二批；
七页改后截图（1440×900）与前批同档构建产物里出，观感结论以图为准、数值结论以上表为准。

## UI 逐页横幅实测（2026-10-05，Playwright 驱动 4175 构建产物，对标 NexusMind 第二批）

第二批按"越像越好"把 NexusMind 的深色横幅落到七页内部：对标物是真实代码
`IncidentWorkspaceView.vue` 的 `.global-status-card`（1345–1474 行）；七页共用新组件 `PageHero.vue`，
页级动作按钮整体搬进横幅、`data-testid` 原样保留，值班动线的既有探针零改动继续跑。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 横幅与 NexusMind 同值（非"看起来像"） | 1440×900 真机读 `.page-hero` computedStyle 与包围盒 | background `linear-gradient(145deg, rgb(13,43,62), rgb(15,61,82), rgb(13,74,98))`；radius `16px`；shadow `rgba(15,60,82,0.35) 0 8px 30px + rgba(37,99,235,0.15) 0 0 0 1px`；标题 14px/700/白；横幅顶 88 vs 顶栏底 68（20px 间隙） |
| 页级动作真的搬到位（不是复制一份） | 真机对每页断言控件在 `.page-hero` DOM 之内 | monitor `open-report`/`drill-surge`、warnings 刷新、workflow 保存定义、metrics 重新读取、assistant `caps-refresh`/`open-report` 全部命中；态势页横幅内点刷新 → **/readyz 请求 +1**、时间戳 12:33:54→12:33:55（网络级证据） |
| KPI/指标大字与 NexusMind `.stat-num` 同路数 | 读 `.ant-statistic-content` computedStyle | `"JetBrains Mono", monospace` + weight `700`（态势页与指标页两处 `:deep` 生效） |
| 会静默回归的第 4 件事进源码门禁 | `uiTheme.spec.ts` 第 6 项：从 `router.ts` 推导 `XxxView.vue`，逐页对账 import＋模板使用 PageHero | 695 → **696 项全绿**（35 文件）、`vue-tsc` exit 0、`npm run build` **✓ built in 21.71s**；两路变异各红 1 项（删 MapView import／删其模板使用），还原后 hash 一致、无 `.mutbak` 残留 |
| 预警页用例随 UI 变位收窄选择器 | 变异：把收窄过的 `[data-testid="cell-action"] .button` 改回惰性的 `.button` | 恰好 1 项红（横幅里的刷新按钮现在渲染在表格之前，点到它抽屉不开）；还原后 19 passed |

七页实拍（1440×900，构建产物）：`frontend/test-results/ui-benchmark/batch2-{dashboard,monitor,warnings,map,workflow,metrics,assistant}.png`（gitignored）。
照实记：①助手页首卡曾与第二张卡撞名"对话"，真机截图发现后改名重出图；②地图片横幅下"降级运行·缩放 9"是
既有的本机资产状态（PMTiles/地形探针均在），非本批回归；③编排页右栏"打开/归档"两字按钮折行是既有排版，未动（第三批已修并进了门禁，见下一节最后一行）。

## UI 缺陷体检与第三批移植（2026-10-05，先调研再动手，判据全部来自包围盒与计算样式）

用户口径是"继续改造、最大程度模仿 NexusMind，可以先调研真正好看的 UI 是什么样，再用工具截图查有没有
重叠之类的 UI bug，要修好"。所以这一批的顺序是：**调研 → 机器体检 → 修 → 把体检固化成门禁**。

调研两路。①NexusMind 全量清点（24 个 `.vue`、约 3.5 万行，样式全在 scoped 块里）：它内部并存**五套调色板**
（#2563EB 系 / Tailwind teal / Home 页青灰 / nm-teal / App.vue 紫青），只有 `Step5Interaction.vue`＋`Process.vue`
那一支与 aegis 主色同源，就只移植这一支；它 `index.html` 引 Google Fonts CDN，**不移植**（离线是 P0）。
②公开口径给的可测判据：KPI 主数字 28–32px、一屏 KPI 卡 4–6、紧凑表行 36–40px、正文对比度 4.5:1、
空态需"图形＋一句话＋一个出口"、过渡 200ms 档。

体检探针跑七页 × 1440/1280/1200/1100，四类缺陷全部量出来并修好：

| 缺陷（修前实测） | 修法 | 修后取证 |
| --- | --- | --- |
| 顶栏三组文字互压：1280 有 `副标题 × 页码徽标` 互压 **290px²**；1100 有四处（124–760px²，含"智能助手"×"后端在线"） | 顶栏改**三列网格** `minmax(0,1fr) auto minmax(0,1fr)`（左右等宽 ⇒ 中间恒等于屏幕中线），分档收字：≤1360 收副题、≤1320 收步进器文字、≤1140 收状态文字 | 七页 × 四档 **overlap=0**、步进器偏离中线 **0px**（判据 ≤1px） |
| 状态胶囊被**静默裁切**（列宽不够时包围盒还在、字看不见——重叠检查看不到它） | 体检补第三条判据 `scrollWidth > clientWidth`；把收字阈值从 1240/1080 提到 1320/1140 | 四档 **navClip=0**（顶栏三组都没被裁） |
| 落地页"可选子系统"长事实串（`dataset=内置预案模板…`）压到右邻一行：每一档每一页都量到 **2178px²** 互压，1100 档右缘 1120 出屏 | `LegStatusPanel.vue`：事实区 `flex-wrap` + `gap`，正文 `overflow-wrap: anywhere` | **overlap=0、beyond=0、clipped=0** |
| 正文级灰字不达 AA：全站 **199 项** sub-4.5:1（workflow 64、assistant 17、map 9、dashboard/monitor/metrics 各 1–2） | ①`color:` 里 #8c8c8c/#999/#86909c/#bfbfbf/黑 45% 共 **27 处**统一 #5a6072（6.1:1）；②antd 分页"•••"1.83:1 → 四级选择器压过它的 `css-` 哈希类；③绿/橙/金标签与助手芯片 3.37:1 → #1d6b14/#8a4005；④选中步骤编号"白字压半透明白"3.49:1 → 实心白底深蓝号 | 七页 × 四档 **lowContrast=0**（底色按渐变首停 + 前景 alpha 合成，避免"白字压藏青"被误判成白底白字） |
| 编排页右栏"打开/归档"两字按钮被挤成竖排（1440 真机截图里就是"打/开"两行） | `DefinitionInspector.vue`：按钮 `flex: 0 0 auto` + `white-space: nowrap`、行 `flex-wrap`，只让名字那段折（"7 节点 / 6 连线""启用中"是词组，折在中间读起来像坏了） | 体检补第五条判据（数按钮文本自己的行盒，不用按钮高度判，带内边距的 flex 按钮会误报）：修后七页 × 四档 **folded=0**；把该文件退回修前 → workflow@1440 **folded=16** 落红 |

第三批移植（NexusMind 的打磨原子）：玻璃卡（半透白 + `blur(18px) saturate` + 内圈白描边 + `0 18px 44px`
蓝雾 + 圆角 18）、卡头 ◆ 前缀与那道渐变细线、主按钮悬停微升 + 次级按钮主色雾、`tabular-nums`、
`accent-color`、**`:focus-visible` 焦点环**（NexusMind 全站没有，这条是补它缺的）、`.empty-hero` 空态三件套；
KPI 从白卡搬进**横幅内托盘**（对标 `.overview-stats`：黑 15% 底板、24px/800 白等宽大数、10px 大写标签、
白 15% 竖线），态势页四栏与指标页三栏同一套。顺手修掉真机截图里第一屏的英文 "No data"（智能体空表 →
中文说明 + 下一步出口）。

| 门禁与规模 | 结果 |
| --- | --- |
| 新增常驻门禁 `frontend/e2e/ui-overlap.spec.ts`（7 页 × 4 档 = 28 项：重叠/出屏/文字被裁/按钮文字折行/顶栏静默裁切/对比度/居中偏差 ≤1px），跑构建产物 + 本机 chromium | **28 passed**（3.1m）；两路变异各红各的：1320 收字阈值改回 1100 → 七页 @1280 全红（overlap=1）；`DefinitionInspector.vue` 退回修前 → workflow@1440 folded=16；还原后 hash 一致 |
| 单测与构建 | 696 → **697 项全绿**（35 文件，新增智能体空表中文态；该项变异 testid 后恰好 1 红）、`vue-tsc` exit 0、`npm run build` ✓ 42.94s |
| 改后实拍 | `frontend/test-results/ui-benchmark/b3-{七页}.png` 与 `b3-dashboard-1280.png`（gitignored） |

照实记：①那层 `backdrop-filter` 在浅灰底上肉眼几乎不可辨，观感收益其实来自大半径蓝雾投影 + 18px 圆角 +
卡头细线，别把"毛玻璃已生效"当成结论；②体检只跑到 1100，更窄档位不在值班台口径内、也没测；
③KPI 大数取 24px 而非调研建议的 28–32px（深色底上再大就压过标题），是**有意不照抄**；
④`ui-overlap` 与地图门禁一样不进 CI，靠人跑。

## UI 第四批：列表行习语、行内计量条与外壳定高（2026-10-05）

继续按"越像越好"移植 NexusMind 剩下的三样打磨原子，并顺手被自己的门禁逮出一处真重叠。

| 取证项 | 怎么做的 | 结果 |
| --- | --- | --- |
| 列表行习语（`.run-item` 那一套：平时透明、悬停上主色雾、**选中用边框**） | `RuntimePanel.vue` 实例队列 + `DefinitionInspector.vue` 已存定义行；实例队列补 `is-picked`（右栏上面那块到底是哪张实例，之前只能靠回忆） | 七页 × 四档体检全 0 违规、**28 passed**（2.6m）；`is-picked` 写在状态色之后，"等人签"与"正在看"同现时边框说后一句 |
| 行内计量条 `.mini-bar` 与 → 项目符号的"使用提示"列表 | 指标页：成功率数字下一条 4px 计量条（与数字同判色、`role=progressbar`，没取到账本留 0 宽不画半条）；"口径说明"三条拆句 | 实拍 `b4-metrics.png`：横幅托盘三栏 + 绿色计量条 + 三条 → 口径 |
| **真重叠**：编排页实例列表最后几行盖在页脚上（最大 1597px²，四档各 6 处） | 根因是外壳——`app-shell: min-height:100vh` 让正文高度不定，编排页只能猜 `calc(100vh - 220px)`，横幅加进来后那个 220 就不成立了。改为**外壳定高 + 正文自滚**，`.wf__body` 用 `flex:1`，页脚移进正文；编排页三块面板并进卡片族（蓝细描边 + 14px 圆角） | 修前 workflow 四档各 overlap=6、map=3；修后 **28 passed**；实拍 `b5-workflow.png` 三列占满一屏、右栏内部滚动 |
| 判据自身：滚出滚动视口的节点包围盒还躺在文档坐标里，与页脚几何相交但没被画到那儿（假红 dashboard=6 / map=3） | 体检矩形改为逐层裁到最近可滚动祖先的可视区（`paintedRect`），完全在视口外不参与判定 | 判据未削弱：复跑变异（1320→1100）仍七页 @1280 全红 overlap=1，还原后 hash 一致 |

规模：单测 697 项全绿（35 文件）、`vue-tsc` exit 0、`npm run build` ✓ 41.22s。
照实记：图标芯片、环形成品率、dashed"待办"胶囊本轮未做（待办 #27）；`is-picked` 只有真机视觉证据，
没有用例钉住（要连后端造数才能验选中态与实例号的对应）。

## UI 第五批：横幅图标芯片、未测胶囊，与弹层体检（2026-10-05）

| 取证项 | 怎么做的 | 结果 |
| --- | --- | --- |
| 图标芯片（对标 `.tool-icon-wrapper`：34px / 圆角 12 / 渐变底 + 同色雾投影；NexusMind 在那儿放 emoji，这里放**单汉字**——离线、无版权、中文台不违和） | `PageHero.vue` 加 `icon` 属性（有芯片时说明文字缩进 44px 与标题对齐）；七页分别 势/测/警/图/程/标/语 | 实拍 `b5b-metrics.png` / `b5b-dashboard.png`；`uiTheme.spec.ts` 加断言"每页 `<PageHero>` 必须带 icon"，变异（删态势页 icon）→ 恰好 1 红，还原 hash 一致 |
| dashed"未测得"胶囊（对标 `.pill--pending`：透明底 + 虚线边） | 指标页口径第三条改成行内虚线胶囊——实心色标签在界面上意味着"测出来的状态"，未测的不该用实心 | 指标页四档 lowContrast=0；未新增任何未量数字 |
| **弹层此前完全没有机器证据**：体检的 `skip()` 把 `.ant-modal/.ant-drawer` 整块跳过，而值班员恰好在抽屉/弹窗里做决定 | `inspect()` 改为可传 `scope`/`inOverlay`（弹层内不跳过弹层、只查该子树），新增 9 项：监测页/助手页上报弹窗 + 预警详情抽屉 × 1440/1280/1100，各出截图 | **37 项全绿**（28 页 + 9 弹层）：弹层内 overlap/beyond/clipped/folded/lowContrast 全 0；抽屉 69 个文本节点、上报弹窗 10 个 |
| 判据自身可证伪：一个节点都没看到时也会全绿 | 体检返回 `leafCount`，页面项 >30、弹层项按各弹层实测数量给下限（弹窗 8、抽屉 40；placeholder 是伪元素不算节点） | 第一版统一阈值 20 把上报弹窗误判成空跑而落红 → 按实测改成逐项下限 |

规模：单测 697 项全绿（35 文件）、`vue-tsc` exit 0、`npm run build` ✓ 41.42s、门禁 37 项 ✓（28 页 + 9 弹层）。
照实记：弹层测到 1100 档，更窄视口与详情抽屉固定 520px 的挤压仍未测；
环形成品率（SVG ring + `stroke-dashoffset`）本轮未做。（下一节已做。）

## UI 第六批：窄档扩测修掉栅格挤压、指标页环形 KPI、门禁去抖（2026-10-05）

| 取证项 | 怎么做的 | 结果 |
| --- | --- | --- |
| 体检扩到真正窄的档位 | 页面加 1000/900、弹层加 1000（共 54 项） | 首轮 @1000 有 1 项落红，隔离复跑 3 轮 × 10 项全绿 → 判定是**探针自己在整批负载下量到"数据还没落地"的半成品页**（`leafCount` 下限误报），加 `waitForLoadState('networkidle')` 后 **54 passed**（7.1m），无一次异常计数 |
| 900 档截图看出来的可用性缺陷：态势页/监测页固定 `:span="10"/"14"` 在 ≤992px 仍并排，链路表被挤到 trace id 变省略号、五段标签堆成三行 | 四个栅格位改 `:xs="24" :lg="N"`（沿用"一张图"页既有写法，不是新发明） | 真机 900 量测：四列都是 **864px**、四张卡顶边 **268/423/1109/1411** 纵向堆叠；`b6-dashboard-900-stacked.png`；两页单测 37 项全绿 |
| 环形成品率（对标 `WorldStateHero.vue` 272–307 `.ring-progress`） | 指标页那条 4px 横条换成 44px SVG 环：12 点起算、`stroke-dashoffset` 0.6s ease-out、判色与数字一致、`role=progressbar`；没量到留空环（不画一圈灰冒充 0%）；`.mini-bar` 样式随之删除 | 实拍 `b6-metrics-ring.png`（100% 一整圈绿）；指标页四档与全批 54 项判据仍全 0 |

规模：单测 697 项全绿（35 文件）、`vue-tsc` exit 0、`npm run build` ✓ 41.52s、门禁 **54 项** ✓ 7.1m。
照实记：那次偶发落红的具体页没抓到（当时输出过滤把计数行滤掉了），再出现按 `leafCount` 明细定位；
900 档只测了七页默认态与上报弹窗，地图页在 900 下的画布没有单独判据。
（前一句已推进：偶发例后来定位到 `monitor@1000`，见下一节。）

## UI 第七批：故障态、空数据态、焦点态进判据（2026-10-05）

54 项判据此前全在"后端正常 + 有数据"下跑，而文案最长的恰恰是降级画面。这批把三档状态纳入同一套
几何与色彩判据（+17 项 → **71 项**），并补掉判据自己的两个洞。

| 取证项 | 怎么做的 | 结果 |
| --- | --- | --- |
| 故障态 | `page.route` 掐 `/readyz` 与 `/api/**`，七页 × 1440/1100 = 14 项；判据不放宽，只把 leafCount 下限降到 12 | ✅ 14 项全绿（leaves 29–92），截图 `test-results/ui-audit/fault-*.png` |
| 空数据态 | `route.fulfill({items:[]})` 造预警页与态势页空态，并要求**真的找到 `.empty-hero`**（防空跑） | ✅ 全绿；变异（改 `empty-hero` 类名）→ `empty=null` 落红。顺带发现旧查询写成 `.empty-hero, .ant-empty`，态势页是靠风险网格那块 a-empty 蒙过的——已收紧 |
| 焦点态 | Tab 走七页各 10 次：每次落焦点要有可见指示（不能只靠颜色）且不被裁到视口外 | ✅ `problems=0`；但变异（删我们的 `:focus-visible`）**不红**——antd 自己画了焦点框，故补 `brandRings>0`（至少一处焦点环用主色蓝）把"我们的规则生效"也钉住，补后该变异即红 |
| **判据的洞**：`paintedRect` 把 `position:fixed` 也按滚动祖先裁 | 想造一处重叠来验故障态判据：给错误条加 `position:fixed; top:22px; z-index:999` 盖住顶栏，判据仍报 0——是判据错了。改成遇 fixed 停止裁切、最后与视口求交 | ✅ 同一变异修后 → 态势页故障态 **overlap=9**、其余六页 0；还原后 grep 计数 0 |
| 上一批"偶发落红"的账 | 这次从失败目录名与 `error-context.md` 读出是 `monitor@1000 leafCount=26`（台账表未落地，前面地图项在吃 GPU）→ 改成"量到内容够才继续"的自适应重试（≤6×2s） | **71 passed**（9.0m），重试一次都没触发 |

规模：单测 697 项全绿（35 文件）、`vue-tsc` exit 0、`npm run build` ✓ 40.86s、门禁 **71 项**。
照实记：故障态是 `route.abort()` 模拟的，验的是"前端拿到错误后的画面"，不是后端进程级故障；
空数据态只测两页；焦点态每页只走 10 次 Tab，上报弹窗内的焦点顺序没测。

## UI 第八批：画布进判据，量出一处节点压叠并修掉（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 画布此前是死角：`skip()` 整块跳过 `.vue-flow`，节点卡互相压叠没有任何判据 | 新增"画布节点不变量"：从右栏定义列表挑**节点最多**的那份打开（第一行只有 2 个节点，压不出问题），量一次；点"自动布局"再量一次。判据①节点卡不互相压叠②标题不画到框外 | 一份 11 节点的定义打开后量到 **overlap=1**：`调取公路雪况实况（占位地址，需现场替换） × 生成预警 压叠 20×37px` |
| 根因：`.wf-node` 只有 `min-width:188px`，标题一长就把盒子往右撑进邻卡 | 宽度钉死 `width:208px` + 标题省略号（`overflow:hidden`/`text-overflow:ellipsis`/`min-width:0`）+ `:title="label"`（全文还能在右侧检查器看） | 修后两趟 `overlap=0 spill=0`；变异（换回 `min-width:188px`）→ 同一处 20×37 复红，还原 hash 一致 |
| 判据自己的误报 | 第一版 `spill` 里写了 `scrollWidth > clientWidth`——那是省略号在起作用，把修好的东西报成 bug（182/260 vs 131 两条误报） | 改成只判几何后 `spill=0`；截图 `canvas-opened.png` / `canvas-auto-layout.png` |

照实记：画布只覆盖"打开已有定义 + 自动布局"两态，拖拽落点造成的压叠没测；节点宽度钉到 208px
后超长标题一律省略号，看全文要 hover 或右侧检查器——这是有意的取舍。门禁 71 → **72 项**。

## UI 第九批：一条判据抓出整类英文空态，改由语言包兜底（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 中文值班台上不该有 antd 默认英文空态。此前靠截图发现一处补一处（链路表、预警表、智能体表），**没写过的表一直漏着** | 新增 `englishEmpty` 判据：空态节点里"有字但一个汉字都没有"就算中，接进页面 42 项 + 故障态 14 项 + 空数据 2 项 | 上线即量到故障态 **monitor×3、map×2、metrics 若干** 处 `No data`——正常态被逐页补丁挡住，后端一挂就露出来 |
| 修法不再逐页补 `#emptyText` | `App.vue` 的 ConfigProvider 挂 `ant-design-vue/es/locale/zh_CN`，语言包一次盖住整类；已有三处 `.empty-hero` 保留（它们还多给"下一步去哪"） | 挂上后 @1440 全组 + 空数据态 **21 项全绿**，`englishEmpty=0` |
| 变异本身也要能真的重出包 | 第一次变异只删 `:locale` → `vue-tsc` 报未使用导入、**build 失败**，门禁跑在旧 dist 上报"7 passed"（假证据）。换成类型合法的变异：`zh_CN`→`en_US` | 换 en_US 后故障态 monitor 复红三条 `No data`；还原 hash 一致、重建 ✓ 41.67s |

照实记：判据项数仍是 72（变的是每项里多一条断言）；语言包只管 antd 组件的默认文案，自己写的文案不归它管，
`.empty-hero` 那几处仍要自己维护。

## UI 第十批：编排页右栏改 pill 切换，折行判据从按钮扩到短词（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 右栏三块面板（节点检查器 / 定义检查器 / 运行态）纵向叠放，**签工单那块在最下面**。#22 那轮就量过"决策在首屏之外"，但这轮才动手 | 移植 NexusMind `.tab-pill`（36px / 999 圆角 / 悬停上主色雾并 `translateY(-1px)` / 选中 `135deg #2563eb→#1d4ed8`）：右栏顶部加三档切换，默认"运行态"，档位记在 localStorage。真机截图 @1440 比较首屏内容 | 修前运行实例列表要滚出首屏才看得到；修后 `b8-rail-default.png` 里实例列表第一屏即右上，`b8-rail-def.png` 是切到"定义"档的样子。**待签张数挂在 pill 上**，停在别的档也知道有几张在等 |
| 折行判据此前只测按钮内的短词，列表行里的折字看不见 | 把判据从"按钮"扩到"任何不含天然断点的 ≤8 字短词被折成多行"。斜号、括号处断字不算（"在途/上限""触达（演练口径）"是正常换行），长短语正常折行也不算 | 上线即在编排页量到 **`"已完成" 折成 2 行` ×4**：两个长 ID 把状态词挤成竖排。修法：`.wf-run__state{flex:0 0 auto;white-space:nowrap}`，两个 ID 改省略号（完整 ID 在实例详情里看得到） |
| 1100 档预警表把"冰湖溃决""崩塌危岩"从词中间切断 | 表加 `:scroll="{ x: 'max-content' }"`：列取自然宽，放不下就横向滚，而不是把词掰断 | @1440 + @1100 共 **38 项全绿（4.7m）**，`folded=0` |
| 画布项此前直接读定义列表第一行（只有 2 个节点，压不出问题） | 改成先点 `rail-def` 切档、再挑**节点最多**的那份打开 | 顺带证明了 pill 切换真的换了面板：画布项 @1440/@1100 绿 |

照实记：①变异口径要讲清——单独删 `nowrap` 或单独删 ID 省略号都不会红，两半各自就够用（belt and braces），
不是判据失灵；真正的前后证据是判据刚上线时在未修代码上当场红 4 处。②pill 把"三块同屏"换成"一块同屏"，
要同时对照节点参数与运行态的人得多点一次，缓解是画布上方那条待签横幅（跨页同一句话同一个数）＋张数徽标。
③门禁项数仍是 72（变的是 `folded` 的覆盖面），localStorage 只存一个档名、不含业务数据。
④收尾全量：单测 **697 passed（35 files）**、门禁 **72 passed（9.9m）**。

## UI 第十一批：深色底上的控件、画布浮层与提示条落点（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 亮色主题的控件搬进藏青横幅会失读。全局对比度判据对 `.ant-btn[disabled]` 是**豁免**的（浅底上灰字＝"点不动"的提示，不算读不到），横幅里不能这么豁免 | 新增 `heroLow`：`.page-hero__actions` 里每个按钮（含禁用）一律 ≥4.5:1，接进页面 42 项 + 故障态 14 项 + 画布项 | 变异（`.page-hero .ant-btn` 改名）当场红 9 项，报的就是 `ratio=1.13<4.5 "保存定义" rgba(0,0,0,0.25) on rgb(12,41,60) [disabled]`——antd 的禁用态在深底上就是暗底暗字。修法：横幅内按钮三态重定，禁用态留 58% 白 + dashed 边（虚线＝点不动，与 `.pill-dashed` 同口径）。修后 `heroLow=0` |
| 画布右下角那块"空白矩形"是 @vue-flow 的小地图：库里默认样式只有 `background-color:#fff` 一行，缩放控件只有一条 2px 投影 | 判据：有节点时两个浮层必须圆角 ≥8、有描边或投影、且不出画布边界；0 节点时小地图**不该存在** | 修后量得 minimap `202×156 radius=12 border=1 shadow=true inside=true`、controls `36×141` 同规格；变异（选择器改名）→ `radius=0 border=0 shadow=false` 复红 |
| antd 提示条默认 `top:8px`，顶栏定高 68px | 新增"提示条落点"项：造一次必然失败的调用（`route` 返回 500 + 点"刷新实例"），量 `.ant-message-notice` 顶边与 `.navbar` 底边 | 变异 `top:'8px'` → 报"提示顶边=8，顶栏底边=68"（真机截图里一条 500 报错把步进器第 7 档和状态胶囊整个糊住）；改 `message.config({ top: '76px' })` 后 `top=76 navBottom=68` |
| 故障文案比在线文案长，右列放不下时会**往左长出轨道**压进步进器 | 补一条**容器级**判据 `navIntrude`：`.nav-brand`×`.nav-center`、`.immersive-stepper`×`.nav-status` 的包围盒不许相交 | 关键取证：文本叶子那套重叠判据在这里抓不到——容器级压 63×28px 时叶子级报 0。实测 `.nav-status` 内容 374px 撞进 295px 的 `1fr` 轨道，`justify-self:end` 让它左溢 78px。修法：胶囊可缩 + 文字省略号 + 完整那句进 `title`，并删掉"事件流已中断（后端不可达）"里与左邻胶囊重复的半句 |
| 故障态判据自己的时间洞 | `waitForFunction(/不可达\|已中断/)` 在顶栏只翻了一半时就满足（SSE 那条先是"事件流重连中"，短 65px），量到的是"还没长全"的顶栏 | 改成要求两句都在。这个洞是实测出来的：只等一半时那 7 项全绿，而容器级判据同时报出 3×28px 侵入 |
| 空画布只有一片网格，没说下一步去哪 | 画布内提示（图形 + 一句话 + 出口），`pointer-events:none` 让它不吃拖拽落点 | `CANVAS[empty] nodes=0 minimap=false heroDisabled=1 heroLow=0` |

照实记：①门禁 72 → **73 项**（新增一项，另三条是接进已有项的断言）；②"禁用态豁免"只在浅底成立，这条口径
写在判据注释里，否则下一个人会以为它全局适用；③跑门禁须带 `AEGIS_API_TARGET=http://127.0.0.1:8321`——
不带时"服务端已存定义"是空的，画布项会在 `bestCount>1` 那里红，那是环境没起好而不是界面缺陷；
④小地图/缩放控件的样式写在 `styles/theme.css`（全局），因为它们是库自己渲染的 DOM，scoped 样式进不去；
⑤收尾全量：单测 **697 passed（35 files）**、门禁 **73 passed（17.4m）**。故障态那 14 项从"固定等 3s"改成
"等文案长全再量"，每项 22–28s，整趟从 9.9m 涨到 17.4m——这是把假绿换成真判据的代价。

## UI 第十二批：横幅状态位——"现在怎么样了"提到第一眼（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 深色横幅只有标题＋版本号，"这一页现在要不要动手"要滚进正文才知道。NexusMind 的 `.global-status-card` 那句带圆点的状态语就是干这个的 | `PageHero` 加 `status:{tone,text}`：8px 圆点按 tone 上色（ok `#34d399`／warn `#fbbf24`／bad `#f87171`／idle 50% 白 + 同色辉光），文字 `rgba(255,255,255,.86)` 12px/600；`flex:0 1 auto;min-width:0` + 省略号 + `title`——**可缩不可撑**，不许吃掉右侧动作钮 | 真机 @1440：workflow 状态语 x=248..382、动作钮 x=1076..1394（间隙 694px）；warnings 状态语 x=233..418、动作钮 x=1330..1394（间隙 912px）。截图 `b12-workflow.png`／`b12-warnings.png` |
| 状态语只写"数据决定的"那一句 | 编排页看 `store.isDirty`（这一页最贵的失误就是离开页面丢掉重画的流程）；预警页看 `risk_level===1` 的条数 | `HEROSTATUS workflow cls=is-idle text="空白画布，还没放节点"`；`HEROSTATUS warnings cls=is-bad dot=rgb(248,113,113) text="窗口内红色预警 6 条（共 6 条）"`——与 `/readyz` 的 `warning_count:6` 对得上 |
| "取不到"与"没有"在界面上不能长成同一句话 | 预警页 `storeTotal===null`（没读到台账）走 idle"还没取到预警台账"，而不是 ok"窗口内没有预警" | 故障态两档复测绿（该档下 `storeTotal` 为 null，走的就是这句） |
| 判据 | 新增"横幅状态位"两项：状态语非空（防空跑）、必须带 tone 类、圆点 ≥6×6、**非 idle 时圆点不许还是默认灰**、字号 ≥12 | 变异各当场红：`.is-bad` 选择器改错 → "is-bad 的圆点还是默认灰"；`heroStatus` 返回 `undefined` → "横幅没有状态语——这条判据在空跑"。第一次变异（删模板绑定）让 `heroStatus` 变成未使用变量、`vue-tsc` 报错、build 失败没跑成——按上一批的教训换成类型合法的写法 |

照实记：①门禁 73 → **75 项**；②另外五页暂未给状态语，判据按页写、没有拿"每页都得有"当判据——
硬凑会让 tone 变成装饰，assistant 的动作槽已有一枚红/橙/绿能力面标签再加一句就是重复；
③workflow/warnings 六档 + 故障态两档共 33 项复测全绿（`folded=0 clipped=0 overlap=0 navClip=0`）；
④收尾全量：单测 **697 passed（35 files）**、门禁 **75 passed（13.9m）**。

## UI 第十三批：蓝系归一、标签三件套、主入口钮，与顶栏合成一枚胶囊（2026-10-05）

先补调研再动手：把 NexusMind 此前没读过的 19 个文件逐行过了一遍（`Home/MainView/Step1-4/GraphPanel/HistoryDatabase/WorldState` 一族），
另按"多调研优秀开源项目"的要求把 `uptime-kuma` 与 `go-view` 浅克隆到工作区之外（`E:\_ui_refs`）读源码——结论分两段。

### 从 NexusMind 拿到的原值（只取蓝系那一支）

| 项 | 原值与出处 | 落到哪里 |
| --- | --- | --- |
| 蓝系 token | `MainView.vue:434-438 ≡ Step1GraphBuild.vue:474-481`：`--accent #3b82f6`、`--accent-strong #2563eb`、`--accent-soft rgba(59,130,246,.12)`、`--line rgba(148,163,184,.18)`、`--shadow-soft 0 18px 40px rgba(15,23,42,.08)` | 与我们的主色一致；页面底纹那三层 radial 未移植（`backdrop-filter` 叠层太多） |
| 标签三件套 | `Step1GraphBuild.vue:630-662`：10px/600 大写 + `padding:5px 10px` + 12% 同色雾底 + 18–20% 同色 1px 描边 + 压深字（success `#15803d`、warning `#b45309`、processing `#2563eb`、pending `#94a3b8`） | `theme.css` 的 `.ant-tag-*`；中文不做大写、字号留 11px（10px 中文低于可读线），绿字改 `#166534`——`#15803d` 压在 12% 绿雾上只有 4.2:1 |
| 顶栏右列 | `Process.vue:2176-2187`：**一枚** `.nav-status`（`min-width:180px`、`padding:8px 12px`、999 圆角、白 56% 底、1px `rgba(22,93,255,.12)` 描边、12px `#4E5969`）里一个 6px 圆点 + 一句状态语 | 见下面"合成一枚胶囊"那条 |
| 主操作钮 | `Step1GraphBuild.vue:951-1003`：`min-height:46px`、`135deg #3b82f6→#2563eb`、`radius:14px`、禁用态用 `rgba(148,163,184,.48)` 实底而不是 opacity | 横幅动作钮提到 36px（46px 在深底横幅里会压过标题）；禁用态实底的做法留给下一步 |

### 从两个开源项目学到的（含"不能抄"）

| 项 | 实测出处 | 判断 |
| --- | --- | --- |
| **新鲜度表达**：`uptime-kuma` 在心跳条两侧各放一个时间——左边是窗口起点（`12h`/`3d`/`40m`），右边是"距上次心跳"，中间一道 1px 线（`HeartbeatBar.vue:23-28,232-238,839-849`），文案是 `"{0} ago"` / `"now"` | 不用 hover 就能看出"这数多旧" | **下一步移植**到预警台账与监测时序（我们的"窗口口径"那批正是缺这个表达） |
| `go-view` 的轮询是全局固定 `requestInterval:30` 秒（`designSetting.ts:62,68`），界面上**没有任何**"数据何时到"的表达 | 大屏类项目的通病 | 我们的"取数于 …/每 15 秒自己对一遍"是加分项，不是抄来的 |
| 间距刻度：`go-view` 用 `$spacing-base-size:1em` × `0/.25/.5/1/1.5/2.5` → `0/4/8/16/24/40px`（`var.scss:15,29-36`） | 一家之言，但与我们现行 4/8/12/16/24 的节奏一致 | 不改动 |
| **不能抄**：`uptime-kuma` 白字压 `$primary #5cdd8b` = **1.72:1**（`Status.vue:2` + `app.scss:129-131`，"Up" 胶囊）、`#aaa` 压白 = 2.32:1；`go-view` `#86909c` 压白 = 3.24:1、状态点 `#34c749`/`#fcbc40` 压白只有 2.23/1.69（低于 3:1 图形下限） | 两家都在浅色态下不过 AA | 我们的对比度判据（≥4.5:1、图形 ≥3:1）不接受这类值 |
| 两家都**不外链字体**（`uptime-kuma index.html:6-14` 只引本地图标；`go-view index.html:14-16` 本地 favicon + index.css） | 与我们的离线 P0 同口径 | 记为佐证 |

### 本批量出来并修掉的缺陷

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 滚动条漂色：`theme.css` 抄的是 NexusMind 另一支（紫青 `#a7f9ff→#887dff→#ff68d6`），口径是"只移植蓝系" | 源码门禁（判据先剥 CSS 注释再看生效声明） | 变异改回紫青 → 红 `theme.css 里出现了非蓝系的 #a7f9ff`；换成 Home.vue 的 `#93c5fd→#60a5fa` 并补 Firefox `scrollbar-color` |
| 顶栏三枚独立胶囊在 @1440 就装不下：在线文案 316px、故障文案 374px，列宽只有 295px。**上一批的省略号只是把"压进步进器"换成"两条真话各截一半"**（截图里 `后端在线 v0…`、`事件流已…`） | 真机截图 + 逐档量 `.nav-status` 宽度与每个文字叶子的 `scrollWidth>clientWidth` | 合成一枚（NexusMind 原值）：@1440 胶囊 252px < 列宽 295、步进器右缘 1105 vs 胶囊左缘 1164（间隙 59px）、@1280 不截断、@1100 收字后 86px；版本号挪进 `title`。截图 `b13-navbar-1440/1280/1100.png` |
| 判据自己的第二个盲区：`navClip` 判容器（子项自己缩了、容器没超），`clipped` 又放过带 `text-overflow:ellipsis` 的，于是"截断"在 75 项里全绿 | 新增 `navTruncated`：≥1280 档顶栏状态文字出现省略号即算缺陷（更窄档是有意收字，不判），接进页面 42 项 + 故障态 14 项 | ✅ 三档实测 `navTrunc=0` |
| 主入口钮 24px 太小 | 判据 `heroShortBtns`（<34px 即算中） | 变异删 `min-height:36px` → `heroShort=4` 复红；修后真机 `heroBtnH=36` |
| 新雾底搬到藏青横幅上变成暗底暗字 | `assistant@1440` 当场抓住：`ratio=1.63<4.5 "语义服务未配置" rgb(138,64,5) on rgb(41,57,56)` | 深底反过来（底压深、字提亮 `#6ee7b7/#fbbf24/#fca5a5/#93c5fd`），源码门禁钉一条防回退 |

照实记：①门禁项数仍是 75（变的是每项里多了两条断言），源码门禁 6 → 7 项（单测 697 → **698 passed**）；
②参照仓库克隆在工作区之外（`E:\_ui_refs`），不进本仓；③收尾全量：单测 **698 passed（35 files）**、
门禁 **75 passed（14.4m）**；④NexusMind 的 46px 主操作钮与"禁用态用实底而不是 opacity"
本批只落了前一条（36px），后者留给下一步——它牵动所有 `:disabled` 的浅底控件。

## UI 第十四批：新鲜度条——"几点取的 + 多久之前"一条话说清（2026-10-05）

这一条不是从 NexusMind 抄的，是第十三批读开源时挑出来的：**NexusMind 与 go-view 都只说"每 N 秒轮询"，
界面不说数据什么时候到的**。uptime-kuma 的心跳条两侧各放一个时间（左＝窗口起点、右＝距上次心跳），
中间一道 1px 线（`HeartbeatBar.vue:23-28,232-238,839-849`），不 hover 就看得出"这数多旧"。

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 三页三种写法、两处只给绝对时间：`MetricsView.vue:160` 与 `RuntimePanel.vue:23` 各一行"更新于 …（UTC+8，每 15 秒…）"，预警页只有窗口口径没有取数时刻，监测页两样都没有 | 新建 `src/components/FreshnessBar.vue`：左端口径、右端 `取数 HH:MM:SS（UTC+8） ｜ N 秒前`，中间一道线；年龄每 1 秒自己走；`age > 2×interval` 转 warn 色并说"比预期周期慢"；没取到数说"尚未取到数据" | 真机 @1440 预警台账：`left="台账 6 条 ｜ 每 15 秒自刷" right="取数 00:05:52（UTC+8） ｜ 3 秒前" gap=1037 ruleH=1`，截图 `test-results/ui-audit/freshness-warnings.png` |
| 组件自己得可证伪 | `src/components/__tests__/FreshnessBar.spec.ts` 5 条：两端与那道线在位、null 分支说"尚未取到数据"、超两个周期上 warn 态、**假时钟推 5 秒后"3 秒前"要变"8 秒前"**、不像时间的字符串按"没取到"处理且不出现 `NaN` | 变异各当场红：tick 改成 `now.value = now.value` → "期望 8 秒前"；null 分支改成当前时间戳 → 两条断言红。**过程里差点交假证据**：第一次用 perl 注入变异时被 bash 吃掉反引号、变异根本没落上而 5 passed，改用 Edit 落变异才量到 3 failed |
| 门禁 | 新增"新鲜度条 @1440"一项：两端都非空、右端必须**同时**报出取数时刻与相对年龄、两端间隙 >0、线高 ≥1 | ✅ 75 → **76 项** |

照实记：①四页全接上了（预警／监测／指标／编排运行态），指标页与运行态面板原来那两行散写文案整段删除，
连带删掉因此变成未使用的 `formatOperatingTime` 导入（不删 `vue-tsc` 会让 build 直接失败）；
迁移时四条原断言改钉"取数 + 多久之前 + 节奏"，意思不变（`components.spec.ts:912-931`、`MetricsView.spec.ts`）；
②右端那个 `00:05:52（UTC+8）` 不是浏览器本地时间：`utils/clock.ts` 固定按东八区换算，
同一条预警在不同机器上不该有两个时刻；③失败路径不刷新时间戳（`load()` 只在取数成功那一步写 `fetchedAt`），
所以后端挂久了这条会自己变橙说"比预期周期慢"，而不是冻在一个看起来很新的小数字上；
④变异复验：右端只报时刻不报"多久之前" → warnings 与 monitor 当场红。过程里先写了一版**类型不合法**的变异
（`age` 变成未使用 → `vue-tsc` 报 TS6133、build 会失败、门禁会跑在旧包上报假绿），改成 stale 分支仍用 `age` 才合法；
⑤收尾全量：单测 **703 passed（36 files）**、门禁 **79 passed（15.4m）**（76 → 79 项＝新鲜度条从 1 页扩到 4 页）。

## UI 第十六批：竖向运行时间线，顺带修掉窄栏里被截掉的新鲜度条（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 右栏只看得到"这条实例已完成/等待人工"，**走到哪一步、哪一步卡住、每步多久**要点开节点卡才知道 | 新建 `components/workflow/RunTimeline.vue`，形状移植 NexusMind `Step4Report.vue:3627-3740`（`grid-template-columns:24px 1fr`、10px 圆点带 `border:2px solid #fff` 的 knockout、2px 连接线 `margin-top:-2px`、未开始一档透明底 + dashed 边框、最后一行不画线） | 真机 @1440：轴宽四行一致 24px、点与线同轴（偏心 ≤1px）、`lines=rows-1`；截图 `test-results/ui-audit/run-timeline.png` |
| 它的色板属 nm-teal 那一支（`--wf-active-dot:#006680`、`--wf-done-dot:#2a9d8f`），口径是"只移植蓝系" | 底纹/描边全换主色系：active 渐变 `rgba(239,246,255,.95)→rgba(219,234,254,.8)` + `rgba(37,99,235,.3)` 边 + 光环 `0 0 0 3px rgba(37,99,235,.16)`；bad `rgba(239,68,68,.06)`；todo dashed `rgba(37,99,235,.28)` | 截图上四档一眼可分：成功绿点、失败红底带"第 3 次尝试 ｜ 上游 502：网关无响应"、待签上主色、未开始是虚线框 |
| 状态文案不能再立一套表（"两张皮"是我们记在案的缺陷类） | 行内状态字直接取 `utils/graph.ts` 的 `NODE_STATE_STYLES`（画布节点卡用的就是它）；文字色固定 `#334155`——状态色当 11px 文字用会掉到 AA 以下（`#52c41a` 压白 2.2:1） | 单测钉住 `awaiting_human → 待人工核签`；真机第 3 行就是这句 |
| 实例载荷里只有 `node_id` | 名字回手上这份定义取（`store.current.nodes` 的 `name`），取不到退回 ID，`title` 始终挂真 ID | 真机 `named=[{"name":"上报核签","id":"review"},{"name":"核签通过","id":"ok"}]` |
| 判据里的空判洞是**自己抓出来的**：种子数据里的实例全都跑完了，前六条一行"未开始"都没有 ⇒ dashed 那一档根本没被验到 | 新增"运行时间线 @1440"一项分两段：第一段真数据量几何与取名；第二段用 `page.route` 灌一份五状态详情（succeeded/failed/awaiting_human/pending/ready）量四档底色、错误行、耗时小字 | 防空跑断言 `expect(sawTodo).toBeGreaterThan(0)` 当场红过一次；改成灌详情后确定性覆盖，且不往台账里落真数据（真启动一条实例会消耗现场） |
| 时间线截图顺带暴露：编排页右栏只有 ~316px，**新鲜度条两端都被省略号截掉**（"每 15 秒…"→"每 1…"） | `FreshnessBar` 加 `stacked`（窄栏改竖排，宁可多占一行），判据加 `cut`：任一端 `scrollWidth > clientWidth` 即算缺陷 | 变异（去掉 `stacked`）当场红并量出实数 `left 需要 215px 只有 141px`、`right 需要 192px 只有 126px`；修后四页 `cut=[]`，workflow 走 `stacked=true` |

照实记：①门禁 79 → **80 项**，单测 703 → 712（时间线 8 条 + `stacked` 1 条）；②圆点色沿用画布那份表，它是装饰而不是
唯一信息载体（状态同时以文字写出），这条口径写进组件注释，免得下一个人以为可以随手换色；
③`cut` 这条判据按 `stacked` 分支分别要求"并排时间隙 >0"与"竖排时两端不并排"，没有拿一个阈值硬套两种排布。

## UI 第十七批：系统输出浮层，并把事件流改成全应用一条（2026-10-05）

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 引擎与通道的实时输出只能开浏览器控制台看，值班台上没有出口 | 新建 `components/SystemTerminal.vue`，形状移植 NexusMind `Step1GraphBuild.vue:1054-1234`（深底 `rgba(15,23,42,.85)` + `blur(12px)` + 顶边一道主色 + 三颗 9px 窗控点 + 标签 `10px/.18em` + 11px 等宽日志行 + 时间戳固定宽 78px），色相从 indigo/紫青重映射到蓝系；默认收起，芯片挂事件计数 | 真机 @1440：芯片 169×31、展开 `h=162 top=738 bottom=900`、面板内 `overlap=0 lowContrast=0 leaves=4`；截图 `test-results/ui-audit/sys-term-open.png` |
| 每调一次 `useEventStream()` 就 `new EventSource` 一条流；顶栏徽标已经占了一条，页面再要实时事件就是第二条（浏览器对同一来源的并发连接有限，HTTP/1.1 是 6 条） | 改成引用计数共享：第一个调用者建流、后来者复用；最后一个卸载才真关流并清空句柄 | 新增用例逐条钉：共用一条流、第一个卸载不关、最后一个才关、之后再挂是全新一条（历史不残留）。改造过程还抓出自己的错——`onBeforeUnmount(disconnect)` 留在 `createStream` 里等于把关流绑在第一个使用者身上，三条用例当场红，删掉后 20 passed |
| **fixed 浮层翻车**（这是常驻门禁救下来的一次） | 第一版照 NexusMind 用 `position:fixed; right:24px; bottom:24px`。页面项当场红：`"10 条/页" × "系统输出" area=418`、`"10 条/页" × "0" area=315`，六档全中（900 档 3 处） | 改成走文档流（页面最后一块、靠右对齐）后 monitor 六档 `overlap=0`。NexusMind 那层是 absolute 在**定高画布**里，我们这一页是整页滚动的正文，没有可借的底——这条差别写在组件注释里 |
| 深底面板的字色不能照抄 | 自己算：`#64748b` 压在 `rgba(15,23,42,.85)` 上只有 **3.74:1**（11px 文字要 4.5）→ 次要文字 `#94a3b8`（≈6.6:1）、主文字 `#cbd5e1` | 面板内 `lowContrast=0`；另补一条源码门禁（第 8 项）钉住这个组件里不许出现 `#64748b/#475569/#334155/#1e293b` 当 `color:` |

照实记：①门禁 80 → **81 项**（15.5m 全绿），单测 712 → **719 passed（38 files）**；②**面板里的日志行在本机没被真机判据量到**——这台机上没有正在跑的链路，
`events` 为空、走的是空态那句（`rows=0 empty=1` 就是证据），行内样式由 5 条单测与那条源码门禁兜住，这个差别如实记；
③"全应用一条流"解决的是前端侧多开连接；后端仍按浏览器标签计数（每标签一条），
第四轮待决里那条"SSE 双连接"讲的是另一件事，没被本批顺带关掉，不许混着报。

## UI 第十八批：空态出口、地图双重空态，与弹层焦点围栏（2026-10-05）

### 先记两件"抄不得"的（都是量出来否掉的）

| 想抄的 | 否掉它的证据 |
| --- | --- |
| sticky 卡头带状态点（`Step4Report.vue:2934-2971`） | 真机七页：`overViewport=0`（没有一张卡高过 900 视口，最高 dashboard 692）、`scrollers=[]`（没有一个 `.ant-card-body` 自己滚）——没有滚动容器时 sticky 完全不生效，加了就是死样式 |
| 禁用态用实底而不是 opacity（`Step1GraphBuild.vue:970-975`） | 加规则 + 拿掉判据里 `.ant-btn[disabled]` 豁免 → 七页 @1440 仍全绿；逐页 dump `button[disabled]` 才看清：带字的禁用按钮只有编排页那两个**自绘** `.wf-run__button`（`#5a6072` 压 `#f5f5f5`＝5.6:1，本来就过），其余是 `text=""` 的分页箭头。规则与豁免都已回退，豁免那行补了注释。另记：WCAG 1.4.11 本来就把"非活动组件"排除在外 |

### 落地的三件

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 空数据档只有两页被测（warnings/dashboard），监测页与地图页的空态没人看过一眼 | 灌空接口扩到四页；判据加两条硬规则：**全站不许出现 antd 默认的"暂无数据"**、有 `.empty-hero` 就必须带"下一步去哪"那个出口 | `EMPTY monitor heroes=1 withAction=1 bare=0`、四页 `bare=0`。监测页表格补了空态：图形 + "这个筛选下没有遥测读数" + `发起一次激增演练 →`（直接调 `drill('surge')`） |
| 地图未定位清单在空数据时**叠两句互相打脸的空态** | 真机 @1440 灌空量到同一块卡里既有"全部要素均已落到图上"又有列表自己那句"暂无数据"（`a-list v-else` 在数组为空时照样渲染） | 改 `v-else-if="props.unlocated.length > 0"`；改后 `EMPTY map bare=0 custom=2` |
| 弹层里的键盘焦点此前没人管（"焦点态"只走正文） | 新增"弹层焦点不外泄 @1440"三项：Tab 十二次不许跑到页面背后、Esc 必须关得掉 | 第一次跑就抓到真缺陷：预警详情抽屉 `inside=2/12`，焦点两次之内溜到 `body`，接着走顶栏 `a.brand-link` 与七个 `a.flow-step`，**Esc 还关不掉**（按键监听挂在抽屉上，焦点不在里面收不到）。新建 `composables/useFocusTrap.ts`（挂上即把焦点拉进容器、首尾循环、焦点漏出去立刻抓回、Esc 交回调用方、detach 摘干净）接到 `@after-visible-change`；修后三项全 `inside=12/12 escapes=0 stillOpen=false`，另加 6 条单测 |

照实记：①门禁 81 → **86 项**（空数据 2→4、弹层焦点 +3），单测 719 → 725，收尾全量 **86 passed（16.2m）**；②判据第一版把 rc-dialog 焦点锁的**哨兵 div** 当成"逃出去"（两个 Modal 各 11/12），
改成判"是否跑到页面背后"（`.ant-modal`/`.ant-modal-wrap`/`.ant-drawer` 之内都不算逃）才对得上本意；
③`useFocusTrap` 里筛可见性不能用 `offsetParent`——jsdom 永远返回 null，会让单测退化成"一个可聚焦元素都没找到"（三条红过一次），改判 `hidden`/`aria-hidden`；
④这个焦点围栏是我们自己补的能力，NexusMind 没有（它的抽屉同样会漏）。

## UI 第十九批：画布落点的四类"看不见"，与吃掉它的 init-fit（2026-10-05）

拖节点进画布此前只保证"放得下"，没保证"看得见"。连拖三个节点真机量到四类缺陷，四类各钉一条判据、各做过变异。

### 四类缺陷与各自的变异

| 缺陷（真机实测） | 怎么修的 | 变异检查 |
| --- | --- | --- |
| 往同一处连拖两个节点，第二张**整个压在**第一张上：压叠 `389×234px`（落点原样取光标位置，而节点卡 208px 宽） | 新增 `freeDropPosition(taken, wanted, gap, bounds, avoid)`：落点先收进可视框，再按"右下优先"的环形候选挑不压别人的坐标（`NODE_FOOTPRINT=208×120`，步长＝卡宽/高 + 24） | 去掉 `bounds`/`avoid` → `outside=1 第 3 张出画布（941,762,1128,876 vs 画布 240,202,1068,880）` |
| 只躲压叠不躲可视区：卡片以落点为左上角，贴着下沿拖就整张伸到画布外（`y=704..957`，画布下沿 880） | `onDrop` 里用 `visibleFlowBox(event.currentTarget, screenToFlowCoordinate)` 把画布矩形投影进 flow 坐标传进去；框内确实没空位时才允许越界（越界还能平移画布找回来，压住邻居只能一颗颗拖） | 同上（`outside` 先红）；另加单测钉"贴下沿落要被收进框内""连投四张每张都在框内" |
| 收进框内之后又压在**小地图**那块不透明卡片下（`171×102px`） | 新增 `overlayFlowBoxes()` 把 `.vue-flow__minimap`/`.vue-flow__controls` 一起投影成禁落区；判据加"节点卡不许与画布浮层重叠 >4px" | 只去掉 `avoid`、保留 `bounds` → `covered=1 设备联动 被浮层盖住 171×102px` 而 `outside=0`——两条判据互不遮蔽 |
| **空画布上的第一颗**贴着右下角落：那一刻小地图还没挂载（`v-if="view.nodes.length > 0"`）⇒ 禁落区是空的，节点落好后立刻被刚挂载起来的小地图盖住 | 小地图改成**常驻 DOM、空画布时 `visibility:hidden`**（`:class="{'is-ghost': 无节点}"` + `theme.css` 一条规则）——不画出来那块"什么都没有的白框"，但尺寸量得到。判据里"空画布不许画小地图"同时改成量**是否被画出来**，并加一条"必须仍在 DOM 里" | 把 `v-if` 加回去 → `covered=1 外部接口 被浮层盖住 171×114px`（同时 `outside=1`），拖拽用例落红 |

### 根因不在让位逻辑，在 `fit-view-on-init`

第一版修完方向偏好之后仍然出框，取证才发现：空画布时 init-fit 不跑，**第一颗节点落下才跑**，
而库里那一处调用是 `viewportHelper.value.fitView()`——**不带任何参数**，
`fit-view-options` 这个 prop 在 `@vue-flow/core@1.48.2` 的运行时里根本搜不到（`grep fitViewOptions` 零命中），
所以单颗节点就把 zoom 顶到 `maxZoom=2`，可视区缩到 **414×339 flow 单位**（不到两个节点宽），
第二颗**怎么放都出框**。更糟的是它只跑一次：被用户手动拖入的第一颗吃掉之后，
再打开已存定义就不会收拢视野了。

改法：删掉 `fit-view-on-init`，改由 `fitToGraph()` 显式做——`watch` 定义 ID 变化才收拢，
`fitView({ padding: 0.2, maxZoom: 1 })`（这里传参是有效的），节点尺寸还没测出时最多重试 12 次。
「自动布局」按钮同时改成 `autoLayoutAndFit()`：重排之后收拢视野，不然重排完节点跑到视口外，看上去像链路被删了。
判据顺序也改成**先钉 zoom 再钉症状**——变异把 `fit-view-on-init` 加回去，先红的就是 `zoom=2` 那条。

### 与 n8n 的画布约定对了一遍（改完之后才去核，不是照着抄）

新克隆的参照 `E:\_ui_refs\n8n`（`--filter=tree:0` + sparse-checkout 只取 `packages/frontend/editor-ui/src`）里，
同一条路是这么走的（file:line 已自己开文件复核）：

- `<VueFlow>` 上**没有** `fit-view-on-init`（`Canvas.vue:2059-2060` 只有 `:min-zoom="0"`/`:max-zoom="4"`），
  fit 只在加载/导入工作流时通过事件触发（`:1947`），且一律 `fitView({ maxZoom: defaultZoom /* =1 */, padding: 0.2 })`（`:1579`）
  ——与我们这次改的"换定义才 fit、fit 不许放大"**同一套口径**。
- 它还有一道我们**暂时没有**的守卫：`document.hidden` 时跳过 fit、切回可见再补做（`:1575-1578`）。
  Vue Flow 在容器量不出尺寸时会退回 500×500，这道守卫防的正是"按假尺寸收拢一次"。已记成待办（任务 #33）。
- 落点侧 n8n 反而**不如我们**：手动拖放那条路径 `normalize:false` 只 snap 网格（`nodeViewUtils.ts:214-215`），
  既不夹紧可视区也不躲邻居；它的 `canUsePosition`（`:134-146`）+ 45° 对角级联（`:217-255`）只用在"加号按钮/连线插入"上。
  所以"贴着边落也要看得见"这一条是我们的补强，不是抄来的。
- 小地图：n8n 干脆**只在平移时出现**（`Canvas.vue:1861-1874`，1s 延迟、左下角、xs 档不显示）。
  这是"浮层不压内容"的另一种解法，比我们"常驻但空画布时 `visibility:hidden`"更激进，留作下一档候选。

### 照实记

①第一版让位只按"从 -ring 往外扫"，结果把第二张推到画布**左边外面**（屏幕 x=9，而画布左边缘是 240）——
不压叠但看不见，是同一类缺陷；改成"右下优先"的评分排序（`score = 左/上各罚 100 + 环数`）。
②往 Vue 模板的**属性位**塞注释是隐蔽性很高的写法坑：`/* */` 让 vue-tsc 报 `TS1117`（把注释里的引号当成重复属性），
换成 `<!-- -->` 之后改报 5 个 `TS6133`——注释把后面的 `@connect`/`@node-click` 等属性整个吃掉，
处理器变成"未使用"而模板里的绑定悄悄没了。注释只能放子元素位置。
③`fit-view-options` 这个 prop 在 `@vue-flow/core@1.48.2` 运行时里搜不到（`grep fitViewOptions` 零命中），
写了等于没写——库里的 init-fit 是无参调用。以后要控 fit 的行为只能自己调 `fitView(options)`。
④单测 725 → **735**（新 `src/utils/dropPosition.spec.ts` 10 条：空画布不位移、够远原样返回、压住要让开、
连投五张两两不重叠、gap 越大让得越远、只往右下让、贴下沿要收进框内、连投四张都在框内、
压浮层要挪开、框内无空位时优先保不压叠）；⑤门禁 **87 项**（画布落点那条从"两个节点"扩成"三颗 + 四类判据"，
项数不变仍是 87），收尾全量 **87 passed（16.7m）**，四次变异各落一次红（`outside`／`covered`／`zoom=2`／
把 `v-if` 加回小地图后 `covered=1 外部接口 171×114px`）。


## UI 第二十批：风险等级芯片——把图例和筛选合成一排（2026-10-06）

「一张图」右栏原先是两截：上面一排 `a-switch` 图层开关（**整排没有任何颜色**），下面一块小方块图例
（`MapPanel.vue:130-136`）。要看懂"红色是什么"得先读图例，再回头猜哪一层用它；
而想"只看红色预警"根本没有入口。按 NexusMind 的 `.filter-chip`
（`CausalGraphView.vue:13-43` 模板 + `:301-315` 芯片底 + `:333-336` 未选中留 35% 描边 + `:338-346` 选中态）
合成一排：**不点也是图例，点下去才是筛选**。

| 取证项 | 量法 | 结果 |
| --- | --- | --- |
| 图例不许说谎 | 芯片的 `--chip/--chip-line/--chip-wash/--chip-halo` 全部由 `riskLegend()` 那一个色值算出（同一份 `RISK_COLORS`）；单测与真机门禁各钉一次 | ✅ 真机 @1440 五枚色值 `[#cf1322,#fa8c16,#fadb14,#1677ff,#8c8c8c]` 与 `RISK_COLORS` 逐项相等；变异（把 `--chip` 写死 `#8c8c8c`）→ 单测 `expected '--chip: #8c8c8c;…' to contain '#cf1322'` 落红 |
| 筛选判据只有一处 | `entities.ts` 新增 `filterLayersByRisk` / `riskLevelCounts`；`MapView` 里 `viewLayers` 是唯一被送去渲染与计数的图层集，`layers` 保持全量给图例读数 | 6 条单测：空选择＝原对象返回、只留红色不剩黄色、多选是并集且与点法顺序无关、**没评级的要素任何筛选下都保留**、计数只数有评级的、计数与逐条重数自洽 |
| 数字变小要讲清 | 筛选生效时图层计数写 `筛后 / 全量`，芯片下方加一行说明 | 真机 `RISKCHIP after-click counts=["0 / 0","0 / 0","0 / 0","0 / 0","未达阈值"] note=true`；变异（`countText` 只返回筛后值）→ `expected ['2','1','2','0','未达阈值'] to include '2 / 6'` 落红 |
| 语义色不许当字用 | 文字固定 `#334155`，色相只放描边与圆点（圆点无 textContent ⇒ 不算文本叶子）；芯片是文本叶子，自动落进常驻判据的六档重叠/折行/对比度检查 | @1440 截图：五枚排成两行，无折字、无压叠、未选中四枚各自带 35% 同色描边 |
| 为什么这排色不算"跑色" | 全站源码门禁只扫 `theme.css` 里那六个非蓝**装饰**色值（紫青/indigo），本批没往 `theme.css` 加任何色；风险五级是数据自己的状态色，标签与图上点早就在用 | 口径记入 `docs/课题6_完善计划.md` 批次 D28 |

**本机验不到的一件事要说清**：`/map` 上图要素恒为 0（缺 `region-anchors.json` 与站点经纬度，见"还没测到的"），
所以真机只能验"颜色真值 / 按下态 / `0 / 0` 写法 / 说明行 / 几何"，
**验不到"筛掉之后地图上少了几颗点"**——那部分由单测夹具钉住（夹具里站点会继承区域风险，
第一次就是靠它把"预警 1 条 = 等级 1 共 1 条"的错误预期纠正成 2 条）。

照实记：①门禁 87 → **88 项**，单测 735 → **748**，收尾全量 **88 passed（16.8m）**；
②两次变异各落一次红（`--chip` 写死灰 ⇒ 色值真值用例红；`countText` 去掉全量 ⇒ `2 / 6` 用例红）；
③第一次用 perl 注入变异没落上（替换串里的引号没对上，`grep -c` 得 0 而测试照绿），改回 Edit 落变异才拿到红——
这条坑第十四批就记过一次，这次是靠"先 grep 确认变异串"当场拦住的。

## UI 第二十一批：画布快捷键，与 fit 的"标签页在背后"守卫（2026-10-06）

这批的参照是新克隆的 **n8n**（`E:\_ui_refs\n8n`，用 `--filter=tree:0` + sparse-checkout 只取
`packages/frontend/editor-ui/src`，整个参照占几十 MB 而不是整仓）。它把两件事做了而我们没做，
而且其中一件正是第十九批留下的口子。

| 取证项 | 出处（已自己开文件复核） | 落地与读数 |
| --- | --- | --- |
| fit 不许跑在"量不出尺寸"的容器上 | `Canvas.vue:1575-1579`：`if (document.hidden) { fitViewWhileHidden = true; return } await fitView({ maxZoom: defaultZoom, padding: 0.2 })` | `fitToGraph()` 开头加同一道守卫（隐藏时记账 `pendingFit`，`visibilitychange` 里补做，`onBeforeUnmount` 摘监听）。**档位说清**：这条只有代码审读 + n8n 同处理的佐证——本机与 jsdom 都造不出"标签页真在背后、容器仍量得出尺寸"的场景，不算实测过 |
| 画布该能键盘操作 | `Canvas.vue:549-558`（`1` fit、`0` 默认档、`+/-` 缩放、`ctrl_a`、方向键选同类节点），并在控件按钮上用 `KeyboardShortcutTooltip` 标出来 | 纯函数 `canvasShortcut(key, heldModifier)` + `window` 监听；真机 `SHORTCUT start=0.9 minus=0.75 zero=0.9 plus=1.08 fit=1`。快捷键写在横幅 caption 里（画布中央那块提示是 `pointer-events:none` 的居中文案，加行会撞折行判据） |
| 默认缩放不能写两处 | —— | `CANVAS_DEFAULT_ZOOM = 0.9` 同时给模板 `:default-viewport` 与快捷键 `0` 用；真机 `start=0.9 zero=0.9` 证明确实是同一档 |
| 打字时不许抢键（我们自己定的口径，n8n 靠 `:disable-keyboard-a11y` 另实现一套） | —— | 处理器先判 `defaultPrevented` 与焦点是否在 `INPUT/TEXTAREA/SELECT`/可编辑区。真机：焦点在"中止理由"框里按 `0` ⇒ `typing value="0" zoom=1`；**变异**（守卫短路成 `false && isTypingAt(...)`）⇒ `typing value="" zoom=0.9` 落红 |

照实记：①门禁 88 → **89 项**，单测 748 → **757**（`src/utils/canvasShortcut.spec.ts` 9 条），收尾全量 **89 passed（16.7m）**；②变异第一版写成"整段删掉 `isTypingAt` 调用"会让 vue-tsc 报 `TS6133`
⇒ build 失败、门禁跑在旧包上报绿（这个假绿法第十四批就记过），改成 `false && …` 保持类型合法才拿到红；
③n8n 的手动拖放路径**不做**可视区夹紧（`nodeViewUtils.ts:214-215` 只 snap 网格），
所以第十九批那条"贴着边也要看得见"仍是超出参照的补强，不是抄来的。

## UI 第二十二批：给"悬停"补一档判据，当场抓到全站主按钮读不清（2026-10-06）

前面廿一批 89 项判据全程不悬停。但我们移植的 NexusMind 习语有一半是 hover 习语
（行悬停上主色雾、pill `translateY(-1px)` 微升、卡片抬投影、按钮描边变色）——
**改底色就可能改对比度，改位移就可能压到邻居**，这两类缺陷一档都没被看过。

### 新增的一档

`ui-overlap.spec.ts` 加"悬停态体检 @1440"：逐页对十类可悬停元素
（`.flow-step`/`.page-hero button`/`.ant-list-item`/`.wf-rail__pill`/`.wf-def__list-item`/`.wf-run__pick`/
`.wf-palette__item`/`.risk-chip`/`.sys-term__chip`/`.ant-card-hoverable`）逐个 `hover()`，
每次悬停后跑**同一个** `inspect()`，判 `对比度/压叠/裁切/折行` 四项。
基线（不悬停）那四项在常驻判据里已经是 0，所以这里任何命中都只能是悬停造成的；
`hovered>=20` 防空跑。

### 第一次跑就抓到的缺陷（全站）

```
monitor 悬停 .page-hero button 第2个 → 对比度：ratio=3.68<4.5
  "发起灾害演练（激增）" rgb(255,255,255) on rgb(59,130,246) 14px/400
```

主按钮的 hover 底色是**往浅走**（`linear-gradient(135deg,#3b82f6,#2563eb)`），
白字压 `#3b82f6` 只有 3.68:1，而按钮字是 14px/400（要 4.5:1）——
也就是说**只要鼠标停在任何一个主按钮上，那个按钮的字就不合格**，七页全中。

改法：hover/active 一律往**深**走（hover `#1d4ed8→#1e40af`、active `#1e40af→#1e3a8a`），
"悬停"这件事继续由 `translateY(-1px)` 和更深的投影表达，不靠变亮；
`App.vue` 令牌补 `colorPrimaryHover:#1D4ED8`/`colorPrimaryActive:#1E40AF`（antd 默认把 hover 派生成浅色）。
修后 `HOVER targets=29 problems=0`，并留 `hover-primary.png` 作视觉存证。

### 一条很值得记的过程坑

第一次修只改了 antd 令牌，判据**照红**，读数仍是 `rgb(59,130,246)`——
但 cssinjs 的 hash 类名变了（`css-ol1awv` → `css-1wri7v5`），说明样式确实重算了，
只是赢的选择器不是我们改的那条：真凶是 `theme.css` 里带 `body` 前缀的那条规则，
它比 `.page-hero .ant-btn-primary:hover` 更具体，把我们的规则吃掉了。
**"读数与预期色值不一致"就是"我改的那条根本没生效"的信号**——
先比对读数与源码里的字面值，再决定去改哪一处。

照实记：①门禁 89 → **90 项**，单测 757 不变（本批没有新逻辑，只有状态覆盖），收尾全量 **90 passed（17.8m）**；
②这条判据的可证伪不需要额外变异——修复前它连续两次是红的；
③仍未覆盖：窄档折叠后的悬停、以及触屏设备（触屏没有 hover；值班台是鼠标，先这么记着）。

## UI 第二十三批：画布缩到"看全局"那一档时，连线只剩 0.32 个像素（2026-10-06）

参照还是 n8n（`CanvasEdge.vue:212` 的 `stroke-width: calc(2 * var(--canvas-zoom-compensation-factor, 1))`
与 `useZoomAdjustedValues.ts:17-33` 的"缩小不只是变细、还要变深"）。
按第十九批定下的规矩：**先量现状再定阈值**。

### 量出来的现状

打开定义列表里最大的那份（**111 个节点、11 条连线**），`fitToGraph` 把 zoom 落在 **0.318**——
也就是说"看全局"不是用户手动缩出来的特例，而是**打开大定义之后的默认状态**。
此时：

| 元素 | 声明值 | 实际设备像素 |
| --- | --- | --- |
| 连线（库里默认还是浅灰 `#b1b1b7`） | 1px | **0.32** |
| 节点卡描边 | 1px | **0.32** |
| 节点左侧类别条 | 3px | 0.96 |

连线基本看不见——而看链路拓扑恰恰就是缩到这一档的唯一理由。

### 改了什么

- `utils/graph.ts` 新增纯函数 `strokeCompensation(zoom)` = `1/zoom` 夹在 `[1, 3]`；
  `useWorkflowCanvas` 按 `viewport.zoom` 算出 `strokeScale`，绑成 `.wf__canvas` 上的 `--canvas-stroke-k`；
  边线与节点描边/类别条都用 `calc()` 消费它。
- 连线颜色从库里那支浅灰提到 slate-400 `#94a3b8`，选中边用主色 `#2563eb`。
- 封顶取 3 是量出来的：0.318 档补满仍有 **1.43 设备像素**，再往上补只会让线糊成一团。
- 顺带归一"同一屏三种蓝"：`.wf-node.is-selected`（含光环）、`.wf-palette__item:hover`、
  `.wf-run__button--primary` 三处的 antd 默认蓝 `#1677ff` 全改成移植来的主色 `#2563eb`
  （白字压 `#1677ff` 只有 4.6:1，压 `#2563eb` 是 5.17:1）。

修后真机读数：`STROKE zoom=0.318266 edges=11 线=1.43设备px/rgb(148,163,184) 描边=0.95 类别条=2.86`；
截图 `canvas-lowzoom-stroke.png` 里 111 节点的拓扑连线重新可见。
**变异**：把 `stroke-width` 退回 `1px` → `连线只剩 0.32 设备像素` 落红。

### 一条摆出来等拍板的（本批没动）

`utils/graph.ts` 的 `NODE_STATE_STYLES` 整套是 **antd 默认色板**，里面有
紫 `#722ed1`（待人工核签）、品红 `#eb2f96`（超时）、青 `#13c2c2`（执行中）——
正是第十三批写进"别抄"清单的那三支。源码门禁只扫 `theme.css`，所以一直漏着。
它被**画布类别条、运行时间线、状态标签**三处共用（单一来源这点是对的），
但 11 个状态要彼此分得开，重排是语义决策不是样式决策，先摆出来等拍板。

照实记：①门禁 90 → **91 项**，单测 757 → **762**，收尾全量 **91 passed（18.2m）**；②新判据自带三条防空跑（`bestCount>30`、`edges>3`、`zoom<0.6`），
否则定义列表哪天变小、或 fit 落在高倍档，它会静默变成"什么都没量"；
③插入用例时把下一条的注释开头两行吃掉了，`npx playwright test --list` 当场报
`SyntaxError: Identifier directly after number`——**挪动/新增用例之后先 `--list` 再跑测试**。

## UI 第二十四批：定义行不该靠"意外折行"排版，外加一条 NexusMind 的入场动效（2026-10-06）

### 先记两条**实测否掉**的移植

| 想抄的 | 否掉它的证据 |
| --- | --- |
| 悬停才显形的行内按钮（`.baseline-del-btn`，`IncidentWorkspaceView.vue:1612-1624`） | 全仓只有 3 处、真正的列表行只 1 处，**NexusMind 的主流是常驻按钮**（另一处删除钮 `:270` 就是常驻的）；且它只改 `opacity`——`focus-within`/`focus-visible`/`@media (hover`/`(pointer`/`prefers-reduced-motion` 在它整个 `frontend/src` 里 **grep 零命中**，照抄会同时撞我们的可聚焦性与第二十二批刚加的悬停态判据 |
| HUD 切角 / 四角刻度 | 不成型：`corner-mark` 全仓**只有一处**（`HistoryDatabase.vue:61,980-990`），`clip-path` 只出现在 `Home.vue` 的 4 条规则里，没有四 span 组件、没有 `border-radius:0` 的 HUD 框 |

### 落地的四件

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 「服务端已存定义」每一行靠**意外折行**排版 | 真机 @1440 逐行量 `getBoundingClientRect()` | 322px 的右栏装不下"名字 + N节点/M连线 + 状态 + 两颗 42px 按钮"（合计 ≈347px）；修前行高 **62px**，两颗按钮落在**不同行的不同位置**（"打开"右缘 1384、"归档"右缘 1150）⇒ 一列右缘参差，扫一眼比不了长短 |
| 改成刻意的两行网格 | `display:grid` + 手工指定 `grid-area`：第一行 名字 + 版本号 + 打开，第二行 计数 + 状态 + 归档 | 行高统一 **56px**（12 行省 72px ≈ 1.3 行），两颗按钮右缘同为 **1384**（对齐成一列）；新判据钉"行高 ≤60 / 右缘相等 / 两颗分行 / 行内子元素两两求交为 0" |
| 长名字仍会把行撑高 | 新判据第一次跑就抓到"第 9 行 63px" | 名字改单行省略号 + 全文进 `title`（与画布节点卡同一口径），**版本号单独一格**并钉 `^v\d+$`——名字再长也不许把"v几"截掉 |
| 入场动效（NexusMind 4 处重复的原子） | `Step1GraphBuild.vue:770-773` 等：`fadeIn 0.2s ease-out` + `translateY(5px)` | 移植为 `aegis-rise-in` 挂 `body .ant-card`；**用 `backwards` 不用 `both`**——`both` 会把最后一帧的 `transform` 永久留在元素上，让卡片一直充当 `position:fixed` 后代的包含块。判据同时钉 `animationName` 含 `aegis-rise-in` 与 `fill-mode != both` |
| 我们自己补的一条 | NexusMind 全站没有 `prefers-reduced-motion` | `@media (prefers-reduced-motion: reduce)` 关掉卡片入场与脉冲点；判据用 `page.emulateMedia({reducedMotion:'reduce'})` 验 `animationName === 'none'`（真机读数 `MOTION normal {"name":"aegis-rise-in","duration":"0.2s"}` → `reduced {"name":"none","fill":"none","transform":"none"}`） |

另外把"实例状态轮询中"这句**静止**文本配了脉冲点（NexusMind 蓝合规变体 `Process.vue:2198-2200`：
`#2563eb` + `box-shadow 0 0 10px rgba(37,99,235,.38)` + `pulse 1.5s`）——它说的恰恰是"此刻还在动"。
核对后确认**早已移植**、本批不重复做的有：顶栏 live 脉冲点、发丝竖分隔线、蓝色滚动条、列表行悬停上雾。

新参照 **Grafana** 已用同一套 sparse-checkout 方式落到 `E:\_ui_refs\grafana`（9.6M，只取
`grafana-ui` 的 Badge / spinners / themes 与统一告警组件），给"状态色板"那条待拍板项当依据。

照实记：①门禁 91 → **93 项**，单测 762 不变，收尾全量 **93 passed（18.3m）**；②**同一批内复犯**一次"插入用例吃掉下一条注释开头"，
`--list` 报 `Total: 0 tests` 才拦住——这条得从"记得看一下"升成固定动作：**每次改完 spec 先跑 `--list`**。

## UI 第二十五批：窄档也过一遍悬停，芯片形状归一，并按 Grafana 重定"状态色"口径（2026-10-06）

### 三件落地

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| 悬停判据只在 @1440 跑过 | 把扫描抽成 `hoverSweep(page, {perPage})`，新增窄档一项跑 1000 与 900 | `HOVER-NARROW 1000 targets=21 problems=0`、`900 targets=21 problems=0`。**没抓到缺陷**——`.wf-rail__pill:hover` 与主按钮的 `translateY(-1px)` 在更挤的轨道里没压到邻居；这条判据现在的价值是防回归。只判压叠/出容器/裁切/折行/对比度，**不判省略号截断**（窄档收字是有意设计，一律判就成了逼产品改口径） |
| 助手页动作芯片是 4px 方角 | 与全站比：其余芯片/标签/胶囊都是 999 圆角一族 | 形状归一（999 / `3px 10px` / 12px / 600 / `min-height:26px` / hover 上雾），**颜色保留语义**——绿=只读查询、红=会改状态需确认，且标签里本来就写着"（需确认）"，不单靠色相表意 |
| "语义色不许就地写死"要机器可查 | 源码门禁加第 9 条 | 钉 `.caps__chip` 圆角与高度、钉 `MapPanel` 必须走 `chipStyle(entry.colorCss)` 且不许出现 `#cf1322/#fa8c16/#fadb14/#1677ff` 字面值；变异（圆角退回 4px）→ 落红 |

### Grafana 读完了：我们那条"只许蓝系"其实比它更严

新参照 Grafana（`E:\_ui_refs\grafana`，同一套 sparse-checkout，9.6M）解决的是同一个矛盾——
**十几种状态要一眼分得开，但颜色不能无限多**：

- `utils/rules.ts:346-366`：把 **15 个原始告警状态塌成 4 支色 + 灰**
  （`Firing/Alerting/Error→bad`、`Pending/Recovering/Paused→warning`、`NoData/Unknown→info`、`Inactive/Normal/OK→good`）。
- 严重度**靠文字与图标**，不靠色相：`StateTag.tsx:22` 永远渲染 `{children || state}`，
  `AlertStateTag.tsx` / `RuleHealth.tsx:18-24` 是 `Tooltip + icon + 文字` 三件套。
- 调色板文件里写明了分工：`palette_new.ts:56,82,108,134` 注释是 **"Status: error/warning/success/info"**，
  而 `:121`（青）、`:160`（紫）、`:173`（粉紫）标的是 **"Categorisation"**——分类色不承担状态。
- 深浅不拿来加状态：`palette_new.ts:48-55` 定的是"同族隔 7 档 ≥4.5:1"，服务对比度。
- Badge 只有 **7 个色 token**（`createComponents.ts:189-196`），芯片是"底色 alpha .15 + 描边 .25"
  （`:338-341`）——与第十三批我们自己量的"12% 雾底 + 同色描边 + 压深字"同一套做法。
- 减动效兜底比我们做得更足：`Spinner.tsx:47-48` 在 `prefers-reduced-motion` 时**换成 hourglass 图标**
  并带 `aria-label="Loading"`，不是简单把动画关掉。

**结论**：aegis 该保留红/琥珀/绿/蓝作状态语义，该挪走的是紫/品红/青的**状态职责**（它们只做分类）。
`NODE_STATE_STYLES` 的 5 条重排方案（含"待人工核签改中性灰 + 图标文字"与配套源码门禁）已写进任务 #35 **等拍板**，
本批**没有动任何语义色**。

照实记：①门禁 93 → **94 项**，源码门禁 8 → **9 条**，单测 762 → **763**，收尾全量 **94 passed（20.9m）**；
②**跨用例边界的 Edit 会吞掉邻居**——这次 `old_string` 把相邻用例末尾两条断言和收尾 `})` 一起吃掉了，
与廿三/廿四批"吃掉注释开头"同源但形态不同；对策升成两条：**改完 e2e 跑 `--list`（管语法），
改完源码门禁立刻单独跑那个文件（管用例数与断言没少）**。

## UI 第二十六批：减动效全网站体检，顺手抓到"脉冲把标签字淡掉"（2026-10-06）

### 先量再改：探针把七页各跑两遍

一遍读计算样式，一遍读 `document.getAnimations()`（后者才算"真在跑"，声明层会骗人）。
量出来两类缺陷，**都在 7/7 页成立**：

| 取证项 | 怎么量的 | 结果 |
| --- | --- | --- |
| reduce 兜底名单漏了新增的那条 | 七页各跑"正常态 → `emulateMedia({reducedMotion:'reduce'})`"，比对无限循环动画的条数 | 修前 `normal-infinite=7 offenders=14`（每页两条来源：声明层 + `getAnimations` 里的 `iter=Infinity`）——卡片入场、顶栏 live 点、轮询点原本都兜住了，**漏的正是第二十四批新加的 `.page-hero__badge`** |
| 那支脉冲淡的是**文字不透明度** | 把动画停在 0/25/50/75% 四处，逐帧按"元素透明度叠到底色上"重算对比度 | 10px/700 的「实时」：`t=0 5.76:1 → t=0.5 3.08:1`（要 4.5:1），**每两秒有将近半秒读不清**，七页全中 |

### 改法取自 NexusMind 自己那一支"动环不动字"

`pulse-dot`（`Step4Report.vue:2961-2969`，`box-shadow 0 0 0 3px α.2 → 5px α.12`）、
`step-pulse`（`IncidentWorkspaceView.vue:1936-1947`）、`pulse-border-blue`（`Home.vue:2020`）动的都是环/边框；
徽标改成 `0 0 0 2px α.26 → 0 0 0 5px α.10`，opacity 恒为 1，静止那一圈写进声明（reduce 关掉动画后是"亮着的环"而不是"没了环"）。
**它自己的 `.gsc-badge`（`IncidentWorkspaceView.vue:1450-1463`）淡的正是 opacity 1 → 0.6**（9px 的字）——
我们原先照抄的就是这一支，这一处不抄。修后实测：`MOTION-CONTRAST sampled=7 low=0`、`REDUCED-MOTION normal-infinite=7 offenders=0`、
`opacity=1`、徽标距标题右缘 10px（5px 的环不触字，`box-shadow` 也不参与布局）。

### 门禁与夹具

- 真机门禁 94 → **96 项**：新增「七页开减少动效后一个无限动画都不许留（正常态确有动效，不是在空跑）」
  与「循环动画不许把带字的标签淡到 AA 以下」。**两条都是先在未修的真实代码上落红才转绿的**（3.08:1 / offenders=14）。
- 源码门禁 9 → **10 条**：扫 `frontend/src` 全部 `.vue/.css` 里含 `infinite` 的 `animation` 声明，
  逐条核对 theme.css 那个 reduce 名单（伪元素要连 `::before/::after` 一起对上）——
  新增脉冲忘兜底**在写下来的当场就红**，不用等体检。修前它报的就是真缺陷，不是人造变异。
- 那条"低 zoom 描边"的判据原先**依赖环境里恰好留着一份大定义**：后端换过一次实例（store 是内存态）
  之后列表里最大只剩 11 节点，判据按设计落红"在空跑"。现在门禁自己种
  `ensureBigDefinition()`——幂等 POST 一份 **45 节点（9 段 × 5 路 + 每段一条跨道连线）** 的 `GATE-UI大定义`，形状变了先归档旧的。
  夹具**必须是二维的**：40 颗串成一条长链时画布是 9280×120 的细条，撞上 `min-zoom=0.2` 后 fit 只能收到 0.2、
  **40 颗里 26 颗在视野外**（2s/5s/10s 三次一样）。同一条用例里顺手把"收拢收没收到"钉成判据：
  `STROKE zoom=0.2819 edges=48 线=1.27设备px 描边=0.85 类别条=2.54 节点=45 视野外=0`。

### 照实记

①**`page.emulateMedia()` 是 browser context 级的**，换页不会自己退回默认——第一版逐页只设 `reduce` 没设回 `no-preference`，
于是第二页起量到的"正常态"其实仍是减动效态，`normal-infinite=1` 让反空跑断言误红（判据没错，是探针自欺）；
②**有限动画不在这条判据内**（真机量到 antd `…-MessageMoveOut` 0.3s `fill=forwards`）：通知条退出挂在 `animationend` 上，
硬关掉会留一条挂不掉的提示，所以只判 `iterations === Infinity`；
③**全量跑第一版 13 红的根因不在代码**：这次是我自己手动起的 preview 少带 `AEGIS_API_TARGET`，
`vite.config.ts:5` 回落到 `127.0.0.1:8000`（没人监听）⇒ 数据类判据全红"在空跑"，
而台账里挂着的「接口调用失败 (HTTP 500)」提示（任务 #37）也是它——**已结案，不是后端的缺陷**；
④两处**探针自欺**：解析定义行节点数读整行 `textContent`，把 "v1" 与 "40 节点" 粘成 **140**（改成只读 `.wf-def__meta`）；
`waitForSelector('.vue-flow__edge-path')` 等的是"可见"，而水平直连线的 SVG path 包围盒**高度为 0**，
Playwright 判它不可见 ⇒ 干等 20 秒超时（改 `waitForFunction(条数 > 3)`，这条判据要量描边，DOM 里有没有才是前提）；
⑤写注释时把参考项目说满了（"NexusMind 从不拿标签文字做脉冲"），回头逐条 grep 才发现它自己有一处正是这么写的——
**对标结论要么给出全部反例，要么不要写成"从不"**。

收尾计数：真机门禁 **96 passed（20.9m）**、单测 **764 passed / 42 个文件**、源码门禁 10 条。

## 还没测到的（诚实清单）

1. **部署形态的并发曲线**：真总线 + 真库那一档已在 2026-10-02 量过（见上文"部署形态复测"，
   拐点同样是 120→200、容量口径仍是 ≤120），但**仍不是生产环境数字**：缺的部分是
   Linux + uvloop + 4 workers 的那条曲线（本机复现不了），以及"按站数梯度"的资源占用
   （CPU/内存/连接数随在册站数怎么涨）——后者连量法都还没定，属于下一批要开口子的地方。
   另外这次的 `delivery_mode` 仍是 mock，演练段里不含真实触达链路。
2. **弱网工况**：Zenoh 链路已在真实运行时上验过互通、请求-响应、边缘存留与按序重放
   （见上表），但还没在真实丢包/高时延链路（4G/卫星回传）上量过；`poc_report.py` 的丢包梯度是
   本机注入的合成时延，不是空口实测。
3. **真实通道对接**（短信/北斗/广播）：`delivery_mode=http` 本轮已接线（适配器、主机白名单闸、
   凭据只进请求头、回执入 `warning_receipts`、状态面 `delivery` 行外显发送/失败计数），
   但**没有任何真实网关地址与凭据**，所以现场触达时延仍未测得：目前 `warning_reach_ms` 里的 1.2s
   是 mock 适配器注入的模拟链路耗时，两种口径在报表里分开标注，mock 数字不得冒充真触达。
   下一步要的是现场 webhook 的形状（路径/字段/回执格式），不是代码。
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

9. **准确率回放：库侧通路与 HTTP 出口都已就位，缺的只是真实标注**：真值表（`004_accuracy_labels.sql`）、
   与落库 `warnings` 的配对 SQL、`GET /api/v1/accuracy/replay` 都在（取证见对应行），判定仍只在
   `persistence/replay.py` 一处。剩下的是数据侧：仓库里没有真实现场标注（也不该造一个），
   所以"≥80%"这项在真实数据上照旧是 `not_measured`。
10. **图谱写路径的"真图"证据仍缺第二档**：`learn()` / `prepare_schema()` 现在都有生产调用方了
    （`POST /api/v1/knowledge/cases` + 启动期索引初始化，见上一节新增行），但"写进去之后真图上
    确实能召回"这件事还是只有替身级证据——`AEGIS_LLM_API_KEY` 为空，`-m slow` 的第二档照旧 skip。
    另外内置 16 条预案模板没有批量回填入口（当前只能逐条 POST），新部署的图仍然是空的。
11. **工程配套**：① 已销项——`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` / `AEGIS_WORKFLOW_HTTP_TIMEOUT_MS`
    连同本轮新增的触达与语义腿共 11 个键已进 `.env.example`，并受
    `tests/unit/test_config_env_surface.py`（键 ↔ `Settings` 字段双向）与
    `tests/unit/test_config_knob_reachability.py`（每个字段都被生产代码读到，`RESERVED` 清单现应为空）
    两条门禁夹住；② Playwright 真机门禁仍不在 CI 与默认测试命令里（见"地图两道门禁"行的照实记②），
    它是唯一能证到 GPU 那一层的证据，静默停跑的风险由人承担而不是由流水线承担。
12. **带 LLM 的两条智能路径本轮没有数字**：任务解析的 LLM 裁决腿、语义交互的措辞润色都写完了并且有
    替身级用例（非法输出弃用、越权动作拒绝、润色编数即整段回退），但 `AEGIS_LLM_API_KEY` 为空时它们按设计
    缺席，所以"三路融合比两路好多少"这句话目前没有实测答案——需要一次带凭据的对照跑
    （`uv run python -m scripts.eval_report_parsing --with-llm`）。
13. **部署形态下「≤3min 预警生成」的合账：2026-10-03 已补上**（本条从"未测得"改判为"已量，剩余部分另记"）。
    环境是 Docker Desktop 4.93.0（WSL2 后端，数据盘 `D:\Docker\data`）+ `aegis-nats`（JetStream）+
    `aegis-postgres`（自建 `aegis-pg:local`：PostgreSQL 17.5 + PostGIS 3.5.2 + pgvector 0.8.6）+ `aegis-neo4j`，
    后端镜像由 `deploy/Dockerfile` 现建（`aegis-backend:d4`），1.1GB 检索权重只读挂载 `/models`。
    接线与否一律读 `/api/v1/integrations`（9 条腿：`store.driver=postgres` 且不再 degraded、
    `retrieval.driver=bge-m3-int8` 且 `dense_leg=true`、`delivery=mock`），不读 `/readyz`。
    数字见上表"部署形态端到端合账"那一行。**这一档收出一条永远绿的假判定**（`warning_generation_ms`
    此前恒为 0.0ms，见上表），所以"≤3min 达标"这句话在修好之前其实没人量过。
    仍未测得的部分照实留着：真实网关凭据（触达仍是 mock 注入的 1.2s）、LLM 凭据（图谱写入与翻译腿）、
    外部智能体（协同成功率/同步时延真样本）、现场标注案例集（准确率）——这四样都不是再写代码能关掉的。
    环境故障的根因也记在这里，免得下次从头猜：`docker-desktop` 发行版起不来是
    `Wsl/Service/CreateInstance/MountDisk/HCS/E_ACCESSDENIED`，指向 `D:\Docker\data\main\ext4.vhdx` 挂载被拒；
    对照可正常启动的 `D:\WSL\Ubuntu-24.04\ext4.vhdx` 后定位到**属主差异**——那份盘属主是当前用户
    （owner 可改 DACL，HCS 挂盘时能把本次会话的 `NT VIRTUAL MACHINE\<guid>` 加进 ACL），
    而 Docker 的三份盘属主是 `BUILTIN\Administrators`、用户只有 `Modify`（不含 WriteAccessControl），
    补不进那条 ACE 于是被拒。给这三份盘加上用户的 `:(F)` 后 `wsl -d docker-desktop` 直接起得来，
    HCS 也如预期自己补了一条本次会话的 ACE。给 `NT VIRTUAL MACHINE\Virtual Machines` 组授权是无效尝试
    （每次开机的 VM SID 是新造的 GUID，预授权那条组并不被这次挂载采用）。
14. **Graphiti 第二档**（16 条内置预案真的进图 + 混合召回）：仓库根 `.env` 里 `AEGIS_LLM_API_KEY` 为空 ⇒ `-m slow` 照旧 skip。本轮能证的是「缺凭据时命令与状态面说的是真话」（`degraded=16`、`schema_error` 点名 llm_api_key），不是写入能力本身。
15. **Zenoh 帧开销的线上量测**：POC 已验过互通、请求-响应、边缘存留与按序重放；「4–6 字节」仍是上游调研期口径，要进报告得靠抓包，本机未做。
16. **链路切换（光纤 / 4G-5G / 北斗短报文备用）**：架构文档 §4.3 的"弱网保障"这一行里，其余四项是真代码
    （边缘本地闭环 `edge/`、压缩与有界缓冲、序号+ACK 按序重放、报警优先的分层出队），**只有"按链路质量自动切换"没有实现**。
    平台侧目前的出口只有三条：MQTT、气象拉取、HTTP 外呼/触达，全部走同一张 IP 网；短报文终端与多运营商出口是硬件与现场条件，
    缺的不是判定逻辑而是可切的目标。本轮（2026-10-03 完成度复核）发现，已从 §4.3 的既成事实列表里移出并就地标注。
17. **数据分级（公开/内部/敏感/机密）**：架构文档 §7.2 原本把它与"血缘、质量旗标"并排写成已交付项——**不成立**。
    仓库里没有任何密级枚举、字段标注或按级裁剪的代码（`grep 数据分级|sensitivity|classification` 在 `backend/src`、
    `frontend/src`、`contracts/` 均无命中）。根因不是漏写一个枚举，而是**平台 HTTP API 没有鉴权模型**
    （部署假设专网/内网，`api/app.py` 全部路由只依赖 `get_container`）：没有调用方身份，就没有"按密级少给几个字段"的落点，
    先摆一个四级枚举只会得到"看起来分了级"。要补这条，先要三个现场决定：谁调这些接口、带什么凭据、
    哪些字段算敏感（眼下已知的候选：`reporter` 上报人姓名、`location` 精确到小数点后 6 位的坐标、人员数量）。
    现状唯一的真实防线是脱敏：凭据只进请求头、外呼与触达目标只报 `scheme://host`、案例出处逐条外显。

上述各项补完后把命令、日期和输出摘要追加到上一节，并删掉对应条目。
