# yunxuetong-bi-server

「云学通」培训业务 BI 差分与事实服务 —— 把**每天中午导出的 Excel 总表**变成一条
**可以被追问、被核查的数字链路**。

> 核心承诺：任何一个结论，都能沿着 `自然语言问题 → 查询 → 事件 → 原始 Excel 的具体某一行` 倒推回去。

真实数据：`(登高)总表.xlsx`，905 行 / 883 人（剔除了 22 行无证件号码的记录）；
证书台账 `小黄卡简表…xlsx`，9085 张证 / 8634 人（**有效期字段三种写法，必须先清洗再解析**）。

---

## 一、它解决什么问题

培训业务每天从系统导出一份 Excel 总表，它是**当前全量状态**——只有每个人「今天」的字段值。
于是有三个绕不开的问题：

1. **看不见变化**：上一期某人的状态在下一期文件里已被覆盖，不落盘就算不出一夜之间改了什么。
2. **数字对不上**：为什么报名人数涨了 4 人？「涨了」和「涨在哪」是两件事。
3. **结论不可查**：报表说「已通过 113 人」，没人能说清这 113 是怎么来的。

本服务把这条链路补齐：**差分 → 事实存储 → 可查询 → 可被 AI 调用 → 可被追问**。

---

## 二、架构与五阶段

```
 data/inbound/<批次>/(登高)总表.xlsx
        │
        │ ① Excel 读取 · 口径过滤 · 派生列
        ▼
   ingest ────────────► 数据集 / 快照 / 指标 / 异常
        │
        │ ② 落库
        ▼
   FactStore (SQLite / PostgreSQL)
   upload_versions · raw_records · diff_events · metric_snapshots · metric_buckets · anomalies · audit_log
   建表结构单一事实源：sql/schema.sql
        │
        │ ③ 固定查询
        ▼
   FastAPI  /metrics/compare · /diffs/summary · /diffs/records · /evidence/{id}
        │
        │ ④ 只读包装（限额 + 白名单 + 审计）
        ▼
   FastMCP  7 个只读工具
        │
        │ ⑤ 自然语言
        ▼
   BIAgent  提问 → 多轮查询 → 发现异常 → 追溯证据 → 生成解释
        │
        ▼
   web/index.html  对话页（可展开每一步的原始返回）
```

| 阶段 | 目标 | 主要落点 | 验证方式 |
|---|---|---|---|
| ① Excel 差分原型 | 定义主键、字段映射、差分规则、异常处理 | `excel_reader.py` `derive.py` `diff_engine.py` `anomaly.py` `config/dataset.yaml` | 与生成 D2 时记录的 ground truth **逐条对账** |
| ② 事实存储 | 存上传版本、原始记录、差分事件、指标快照 | `store.py`（7 张事实表 + 3 张本地实体表，全在一个 `bi.sqlite3`；DDL 见 `sql/schema.sql`） | `tests/test_store.py` `tests/test_schema.py` `tests/test_isolation.py` |
| ③ 固定查询 API | 指标对比 / 差分汇总 / 明细下钻 / 证据追溯 | `api.py` `attribution.py` | `tests/test_api.py`（含归因残差为 0） |
| ④ 包装为 MCP | 只读、查询限制、审计日志 | `mcp_server.py` | `tests/test_mcp.py` |
| ⑤ 自然语言 Agent | 一问 → 多轮查询 → 异常 → 证据 → 解释 | `agent.py` `web/index.html` | `tests/test_agent.py` + 验收脚本 |

---

## 三、快速开始

### 创建并激活虚拟环境（Windows）

在项目根目录执行以下命令。项目使用 Python 3.13：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果使用命令提示符（CMD），激活命令为：

```bat
.venv\Scripts\activate.bat
```

完成后可用 `deactivate` 退出虚拟环境。之后重新打开终端时，需要再次执行对应的激活命令。

```powershell
# 设置本次终端会话的模块搜索路径
$env:PYTHONPATH = "src"

# 1) 灌数据：入站 D1、派生 D2、建立差分事件（幂等，可反复跑）
python -m bi.cli seed

# 2) 看差分文字报告
python -m bi.cli diff 20261012 20261013

# 3) 起服务（同时托管首页）
python -m bi.cli api                   # 首页（业务驾驶舱）：http://127.0.0.1:8787/
# 驾驶舱：http://127.0.0.1:8787/dashboard   对话页：http://127.0.0.1:8787/chat

# 4) 单测 / 验收
pytest -q                              # 运行全部测试
python scripts/acceptance.py           # 生成 docs/ACCEPTANCE.md

# 5) 清洗「证书有效期」列（区间 → 到期日），输出到 data/cleaned/
python scripts/clean_cert_validity.py data/inbound/certificates/<台账>.xlsx --dry-run
#    把清洗结果写进事实库（--file 指定任意路径，不必拷进 data/inbound/）
python -m bi.cli ingest certificates --file data/cleaned/certificates/<原名>.cleaned.xlsx

# 6) 从 raw_records 投影出实体表（certificates / students / companies）
python scripts/build_entity_tables.py --dry-run

# 7) 定时任务跑没跑（缺口 = 漏跑，一天一行在 job_runs 里）
python -m bi.cli job-status refresh_entity_tables 14
```

单独入站一个批次（例如 `20261015`）：

```powershell
python -m bi.cli ingest 20261015
```

当批次目录包含 [import_sources.yaml](config/import_sources.yaml) 中定义的全部来源文件时，
命令会按各自配置分别写入数据集；不在当前“登高总表”分析口径内，也不会与其建立差分。

持续监控新批次并自动入库、分析：

```powershell
python -m bi.cli watch
```

监控器每 5 秒扫描一次 `data\inbound` 的一级子目录；配置清单要求的来源文件齐全且稳定 10 秒后开始处理。
同一数据集存在更早批次时，会保存与最近上一批次的差分和跨期异常；首次出现的数据集只入库。
监控器会跳过文件指纹未变化的已入库批次，处理失败则记录错误并稍后重试。此命令需保持运行；
要登录 Windows 后自动运行，可在任务计划程序中创建登录触发的任务，程序设为当前虚拟环境的
`python.exe`，参数设为 `-m bi.cli watch`，起始位置设为项目根目录。

MCP 接入（stdio）：

```json
{ "mcpServers": { "yunxuetong-bi": {
    "command": "python", "args": ["-m", "bi.mcp_server"],
    "env": { "PYTHONPATH": "src", "BI_MCP_MAX_LIMIT": "200" } } } }
```

### 本地数据维护（`sql/schema.sql`）

事实库的建表结构只维护一份：[`sql/schema.sql`](sql/schema.sql)。
`src/bi/store.py` 从这个文件加载 DDL，**改表结构只改它**；
`tests/test_schema.py` 会守住「文件」与「代码」两边不漂移。

```powershell
# 动手前先备份
sqlite3 data/store/bi.sqlite3 ".backup data/store/bi.bak.sqlite3"

# 建空库 / 补齐缺失的表（幂等，已存在则不改动）
sqlite3 data/store/bi.sqlite3 < sql/schema.sql

# 灌数据（走业务代码，保留口径过滤与派生规则）
python -m bi.cli seed

# 排查：每期口径与产出量（直接查表，库里不建视图）
sqlite3 data/store/bi.sqlite3 "SELECT dataset_id, batch, row_count, record_count, dropped_rows, sha256 FROM upload_versions ORDER BY batch;"

# 排查：差分事件 + 两期文件名 + Excel 物理行号
sqlite3 data/store/bi.sqlite3 "SELECT e.batch_from, e.batch_to, e.event_type, e.record_key, e.field_name, e.value_from, e.value_to, rf.source_row AS row_from, rr.source_row AS row_to FROM diff_events e LEFT JOIN raw_records rf ON rf.version_id=e.from_version_id AND rf.record_key=e.record_key LEFT JOIN raw_records rr ON rr.version_id=e.to_version_id AND rr.record_key=e.record_key WHERE e.record_key LIKE '320826%' LIMIT 20;"
```

> 没装 `sqlite3` 命令行时，可用 DB Browser for SQLite 打开 `data/store/bi.sqlite3`，
> 或用 Python 执行 `sqlite3.connect('data/store/bi.sqlite3').executescript(open('sql/schema.sql',encoding='utf-8').read())`。

文件里除 7 张事实表外，还有 3 张**本地实体表**（`certificates` / `students` / `companies`，
由 `scripts/build_entity_tables.py` 从 `raw_records` 投影，`ingest` / 差分都不碰它们，
可随时整体重建）。另有 1 张**定时任务执行记录表** `job_runs`（见下节）。
库里**不镜像外部系统的表，也不建视图**（视图没有代码使用，只是多一层要跟表结构同步的对象），
口径只有一个来源：`raw_records`。

`PRAGMA user_version` 记录结构版本号：结构不兼容变更时递增，老库应重建而不是硬改。

### PostgreSQL 后端（可选）

默认继续使用本地 SQLite。设置 `BI_DATABASE_URL` 后，API、CLI、watcher 和 MCP
会改用 PostgreSQL，并在 API 启动或首次入库时自动创建所需表；`BI_DATABASE_URL` 优先于 `BI_DB_PATH`。
安装依赖后，在 `.env` 或进程环境中配置连接串：

```dotenv
BI_DATABASE_URL=postgresql://用户名:密码@数据库主机:5432/yunxuetong_bi
```

请将生产连接串存放在部署环境的密钥管理中，不要提交真实密码。切换后 SQLite 中已有数据
不会自动复制到 PostgreSQL。迁移本地 SQLite 中的六张事实表时，先配置目标连接串并运行：

```powershell
$env:BI_DATABASE_URL = "postgresql://用户名:密码@数据库主机:5432/yunxuetong_bi"
python scripts/migrate_sqlite_to_postgres.py
```

脚本保留现有主键与外键关系，要求目标表为空，不会覆盖已有数据；完成后将连接串放入
`.env` 或部署密钥配置，再正常启动服务。健康检查只显示数据库类型，不会返回连接串或密码。

### Docker 部署

镜像默认监听容器内的 `8787` 端口，以非 root 用户运行；SQLite 文件及导入文件位于 `/data`，
生产环境建议挂载持久化卷。构建并运行：

```powershell
docker build -t yunxuetong-bi-server .
docker run --name yunxuetong-bi-server `
  --env-file .env `
  -p 8787:8787 `
  -v yunxuetong-bi-data:/data `
  -e BI_API_HOST=0.0.0.0 `
  -e BI_DB_PATH=/data/store/bi.sqlite3 `
  -e BI_INBOUND_DIR=/data/inbound `
  yunxuetong-bi-server
```

`.env` 中放置 `BI_DATABASE_URL`，或在部署平台配置该密钥；不要将密码写在命令行或镜像里。
使用 PostgreSQL 时，`/data` 卷主要用于放置待导入的 Excel 文件。`/health`
容器健康检查端点会检查应用和数据库是否可用。容器只运行 API；需要自动监控新批次时，
应另行运行 `python -m bi.cli watch` worker，并让 worker 与 API 使用相同数据库和入站文件卷。

---

## 四、第一阶段的三件定义（本项目的地基）

### 1. 主键：`证件号码` + `姓名`（复合键）

**不能用裸证件号**。实测表中存在两个不同的人共用同一身份证号
（吴明志 / 吴民志 同为 `340221197212154953`、同属一个班），
裸证件号做键会**静默覆盖掉其中一人**。

无证件号码的行不参与统计（22 行 / 2.4%），剔除数量写入版本元数据与页面口径说明——
**分母变小这件事必须永远有痕迹**。

### 2. 字段映射与追踪字段

30 列通过 `config/dataset.yaml` 映射为 snake_case 标准名（`证件号码 → id_number` 等）。

参与逐字段比对的只有 6 个**状态字段**：

| 追踪字段 | 严重度 | 为什么追踪 |
|---|---|---|
| 开班编号 | high | 决定所属批次，变动意味着归属被改 |
| 考试状态 | medium | 业务主状态机 |
| 考试日期 | low | 反映排考进展 |
| 分数 | high | 考核结果 |
| 是否付款 | medium | 财务口径 |
| 备注1 | info | 运营备注 |

刻意**不追踪**姓名 / 电话 / 单位等文本列：它们不反映业务状态，却会因「同一人表述不一致」
制造大量假变更，同时扩大个人信息留存面。

### 3. 派生列：`报名日期` 不在原表里

原表没有报名日期，报名/编班时间编码在 `开班编号` 中：

```
0706782 + 年份位(5=2025, 6=2026) + MMDD(4) + 班序号(1)     共 13 位
0706782605115  →  2026-05-11 第 5 班
0706782509141  →  2025-09-14 第 1 班
```

该解读经一致性校验：**报名月份从未晚于考试月份（违例 0 条）**。
派生列还包括 `人数 / 已考试人数 / 未考试人数 / 已通过人数 / 已付款人数 / 延期换证人数` 等 0/1 标记列。

### 异常规则（9 条，声明在 `config/dataset.yaml`）

`duplicate_record_key`（复合主键重复）、`invalid_id_number`（校验位不合法）、
`score_out_of_range`、`passed_but_no_exam_date`、`passed_but_low_score`、
`passed_but_no_score`、`paid_but_zero_amount`、`class_code_unparsable`、
`exam_status_regressed`（跨期状态回退）。

> 真实数据首轮检出 **3 人「考试状态=已通过」但没有分数**（卢华波、黄诗淇、刘雨欣）——
> 这是原始数据就存在的业务异常，正是这条规则被加进来的原因。

### 4. 自然时间周期：一个配置项，单位是「天」

「自然时间」不是写死的自然月，而是 `config/period.yaml` 里的**配置项**，单位统一为**天**：

```yaml
period:
  unit: day              # 只支持「天」
  days: 30               # 周期长度：7 = 一周，30 = 一个自然月
  anchor: "2025-01-01"   # 窗口对齐锚点（设为某个周一即可对齐自然周）
  label: ""              # 展示名；留空按 days 推断（7→周，30→月）
```

- 切法：从 `anchor` 起每 `days` 天切一刀，**固定长度窗口**（不是日历月）。
  `days=30` 时窗口相对日历月会有漂移（日历月 28~31 天），这是「以天为单位」的口径。
- 周期标签取窗口**起始日**，窗口结束日随数据一起下发，便于核对。
- 统计在**查询时**计算（`src/bi/period.py`），不落库、不改派生列，
  因此改配置只需刷新页面，不会影响差分事件、指标快照与 ground truth。
- 驱动驾驶舱「报名人数周期趋势」图；页面同时显示当前口径（周期天数 + 锚点），
  取不到日期的记录会显式提示「未计入周期分布」，不让分母悄悄变小。
- 源数据粒度是下限：登高表能解析到「日」（报名日期），
  课程月份汇总表只有「年+月」，那部分仍按源表的月维度统计。

---

## 五、指标与「归因」——本项目最有价值的一层

5 个核心指标（`config/metrics.yaml`）：

| 指标 | 口径 | D1 → D2 |
|---|---|---|
| 报名总人数 | 参与统计的学员数 | 883 → 887（+4） |
| 已安排考试人数 | 考试日期非空 | 747 → 738（−9） |
| 已通过人数 | 考试状态=已通过 | 74 → 113（+39） |
| 平均分 | 有分数者的均值 | 53.83 → 57.42（+3.59） |
| 已付款人数 | 是否付款=已付款 | 17 → 32（+15） |

**归因**不只是「涨了 4 人」，而是把变化拆成可核查的事件计数，并给出**残差**：

```
报名总人数 +4   = 新录入报名 +12 + 未再出现(退费/剔除/漏报) −8     残差 0 ✔
已通过人数 +39  = 考试状态变为「已通过」+40 + 从「已通过」变为其他 −1  残差 0 ✔
已安排考试 +(−9) = 清空考试日期 −1 + 移除了原本含考试日期的记录 −8     残差 0 ✔
```

`残差 ≠ 0` 是**有意义的信号**：说明存在未被追踪的变化，此时系统会明确说「建议核查」，
而不是给一个看起来完整、实则错误的解释。

---

## 六、证书台账（小黄卡）：清洗与「过期日期」口径

第二个统计类目。源表完全不同于登高总表：一张**证书清单**，一行一张证，
核心问题只有一个——**这张证什么时候过期**。它的契约在
`config/datasets/certificates.yaml` 与 `config/metrics/certificates.yaml`，
引擎复用同一套（读表 → 清洗 → 派生 → 落库 → 看板），**不写第二套管线**。

### 1. 原始数据的真实形态

| 项 | 实测值 |
|---|---|
| 台账行数 | 9085（一张证一行） |
| 不同身份证号 | 8634 → **424 人持有多张证**（多出 451 张） |
| 涉及公司 | 1319 家 |
| 「证书有效期」写法 | **三种**，外加一类解析失败 |

「证书有效期」列写法不统一，正是**必须先做数据清洗**的原因：

| 原始写法 | 例子 | 含义 |
|---|---|---|
| 单日期 | `2026/10/01` | 就是过期日期 |
| 区间 | `2024/10/01-2026/10/01` | 右端是过期日期 |
| 只有年月 | `2027年1月` | 取该月最后一天，**精度降级为「月」** |
| 解析失败 | `2026/04/17-2028/04/31` | 区间右端写了不存在的日期 → 整段不可解析 |

解析在 `src/bi/expiry.py`，每个结果都带**形态标签**与**精度标记**。

### 2. 两条不能越界的线

1. **区间两端都必须合法**。右端写了「4 月 31 日」时整段判为不可解析，
   **绝不拿左端顶替**——否则会凭空算出一批「有效期 0 天」的假数据。
2. **日期来源必须分开看**。源表的「日期来源」列标明该日期是**证面原文**
   （证上真实写的到期日）还是**按 2 年效期推算**。实测 9085 张里
   **证面原文只有 3117 张**，其余 5968 张是推算值。
   推算值能回答「规模」，**续证提醒必须回证面核对**——所以看板强制按来源拆分展示。

### 3. 清洗：只做有唯一答案的事

规则声明在 `config/datasets/certificates.yaml` 的 `cleaning:` 段，引擎在 `src/bi/cleaning.py`：

| 做什么 | 为什么安全 |
|---|---|
| 全角转半角、去首尾空白、折叠空白 | 结果唯一 |
| 去噪字符（`‘’'’。` 等，实测命中 2 条） | 结果唯一 |
| 末位小写 `x` → `X` | 身份证末位字母本就大小写等价 |

**不做**：补身份证缺失位、改校验位、给非法日期找替代值。
这三类是**编造**，只由异常规则报出来给人工核对。
清洗前后的差异（命中数 + 样例）写进版本 `notes`——**每次清洗都留痕**。

### 4. 派生列与异常

派生：`开始日期` / `过期日期` / `过期日期精度`（日·月） / `有效期天数` / `过期月份` / `有效期形态`。

异常 6 条，实测 6622 条：

| 规则 | 严重度 | 条数 | 说明 |
|---|---|---|---|
| `expiry_unresolved` | high | 256 | 过期日期解析不出（续证提醒覆盖不到） |
| `id_number_length_invalid` | high | 13 | 证件号不是 18 位 |
| `invalid_id_number` | high | 60 | 证件号校验位不合法 |
| `expiry_month_precision` | low | 325 | 只有年月，按月末取值（估计值） |
| `expiry_estimated_source` | info | 5968 | 过期日期来自推算，非证面原文 |

位数错与校验位错**拆成两条**：修法完全不同（前者补录/核对，后者可能是录入笔误）。
所有证件号问题**只上报不修复**——实测 18 位号码里校验位本身就有大量错误，
任何「智能修复」都是猜。

### 5. 主键：一人多证

主键是 `身份证号码|姓名|证书编号`（**三段**）。只用两段会把 424 人的多证静默合并成一行——
正是本项目复合主键约定要防的那类静默丢人。证书编号本身唯一（9085 行 9085 个编号），
带上它才是一张证一个身份。

### 6. 指标：静态入库，时点现算

`config/metrics/certificates.yaml` 里 9 个指标**全部是静态口径**（与日期无关）。
「还有几天到期」「已过期多少人」这类随当前日期变化的口径**不入库**
（入库即过时），由看板在查询时按当天现算。

看板页面 `web/dashboard_certificates.html`（`/dashboard/certificates`）：
6 张 KPI、到期倒计时分桶（**按来源拆分**）、来源/形态/精度分布、按月到期趋势、
周期分布、持证人结构、待核对原文、异常清单（按严重度排序）。

### 7. 清洗任务：把「证书有效期」的区间折叠成到期日

**任务定义**：把 `证书有效期` 列里的**区间**清洗成它的**右端（到期日）**，
产出一份可直接交付的 Excel。脚本：`scripts/clean_cert_validity.py`（`pytest` 覆盖：
`tests/test_clean_cert_validity.py`）。

```
"2026/09/18-2028/09/30"   ->   "2028/09/30"
```

为什么需要它：上面的派生列（`过期日期`）是**入库后**的结果，只在库里和看板上可见。
一线同事拿到的仍是原始 Excel，字段写法混排（区间 / 单日期 / 只有年月），
直接在 Excel 里筛「什么时候到期」是做不到的。

#### 7.1 输入与输出

| | 位置 |
|---|---|
| 输入 | `data/inbound/certificates/<台账>.xlsx`（工作表 `小黄卡清单`，目标列 `证书有效期`） |
| 输出 | `data/cleaned/certificates/<原名>.cleaned.xlsx` |
| 明细报告 | `data/cleaned/certificates/<原名>.cleaned.report.csv` |

**输出不落在 `data/inbound/` 里**——入站路由是按文件名片段匹配的（`小黄卡`），
同目录出现两份都含该片段的 xlsx，入站会把它们当成**两份台账互相覆盖**。
`data/cleaned/` 已加入 `.gitignore`（产物仍是含姓名 / 身份证号的完整表）。

#### 7.2 清洗口径

| 原始值 | 默认（`--mode range`） | 加 `--mode all` |
|---|---|---|
| `2026/09/18-2028/09/30`（区间） | `2028/09/30` | `2028/09/30` |
| `2027/03/31`（单日期） | 不动 | `2027/03/31`（仅规范化格式） |
| `2027年1月`（只有年月） | 不动（按月末取值是估计值） | `2027/01/31`（报告标「估计值」） |
| `2026/04/17-2028/04/31`（右端非法） | **不动** | **不动** |

默认只动区间，是因为区间折叠有**唯一正确答案**；把「只有年月」补成月末属于**估计**，
必须显式开启，且报告里会写明「续证前需回证面核对」。

#### 7.3 命令与参数

```powershell
# 先看统计，不落盘
python scripts/clean_cert_validity.py data/inbound/certificates/<台账>.xlsx --dry-run

# 实跑（只折叠区间）
python scripts/clean_cert_validity.py data/inbound/certificates/<台账>.xlsx

# 连「只有年月」的也按月末折叠（得到估计值）
python scripts/clean_cert_validity.py data/inbound/certificates/<台账>.xlsx --mode all
```

| 参数 | 默认 | 作用 |
|---|---|---|
| `source` | 必填 | 输入 `.xlsx` 路径 |
| `-o, --output` | `data/cleaned/<批次>/<原名>.cleaned.xlsx` | 输出路径；指到输入文件或 `data/inbound/` 内会被**拒绝** |
| `-s, --sheet` | `小黄卡清单` | 工作表名（取不到则用第一个） |
| `-c, --column` | `证书有效期` | 目标列名 |
| `-m, --mode` | `range` | `range`=只折叠区间；`all`=另外处理只有年月的（估计值） |
| `--date-format` | `%Y/%m/%d` | 输出日期格式 |
| `--report` | 与输出同目录的 `*.cleaned.report.csv` | 明细报告路径 |
| `--no-report` | 关 | 不写明细报告 |
| `--dry-run` | 关 | 只统计，不写 xlsx（也不写报告） |

#### 7.4 三条硬约束

1. **解析复用 `src/bi/expiry.py`**——脚本改出来的值与库里算出的 `过期日期` 完全一致，
   不会出现「脚本一套、入库一套」。改 `expiry.py` 等于同时改脚本。
2. **右端不合法就整行不动**，绝不拿左端顶替（否则会造出一批「有效期 0 天」的假数据）。
   实测 256 行属于这种，全部保持原值并写进报告交人工核对。
3. **只改目标列**。其他列、其他工作表、行数与表头一律不动 —— 测试里逐列比对过。

#### 7.5 实测结果（20261026 批次，9085 行）

| 形态 | 行数 | 默认改动 | `--mode all` 改动 |
|---|---|---|---|
| 区间 | 2861 | 2861 | 2861 |
| 单日期 | 5643 | 0 | 0（原本已规范） |
| 只有年月 | 325 | 0 | 325 |
| 无法解析 | 256 | 0 | 0 |
| **合计被改行** | | **2861** | **3186** |

明细报告 9085 行，字段只有 `Excel行号 / 原值 / 清洗后 / 形态 / 状态 / 说明`，
**不含姓名与身份证号**。脚本幂等：重复执行第二次改动数为 0。

#### 7.6 把清洗结果入站事实库（`ingest --file`）

清洗产物在 `data/cleaned/`，而入站只扫 `data/inbound/<批次>/`。要把清洗后的文件
写进事实库，用 `--file` 显式指定（仍按文件名片段路由到数据集配置，只是不扫目录）：

```powershell
python -m bi.cli ingest certificates --file "data/cleaned/certificates/<原名>.cleaned.xlsx"
```

**不要**把文件拷进 `data/inbound/certificates/` 绕过这个问题：同目录里会出现两份都含
`小黄卡` 片段的 xlsx，`match_sources_for_batch` 会把它们当成同一数据集的两份文件，
最终留下哪份取决于排序（`UNIQUE (dataset_id, batch)` 决定互相覆盖）。

覆盖同一批次后能看到的变化（`certificates` 只有一期，无差分）：

| 项 | 变化 |
|---|---|
| `upload_versions.source_file` / `sha256` / `filename` | 指向清洗产物 |
| `payload_json.证书有效期` | 2861 行从区间变成单日期 |
| `有效期形态` 分布 | 区间 2861 → **0**；单日期 5643 → **8504** |
| `过期日期` / `有效期天数` / 指标 / 异常 | **完全不变**（都走 `expiry.py`） |

入站时还会按 `config/datasets/certificates.yaml` 的 `cleaning:` 段继续清洗
`身份证号码` 与 `证书编号`（本次分别为 2 行、8278 行），这是入站既有行为，
与清洗脚本无关 —— 所以库里看到的 `证书编号` 是规范成大写后的值。

### 8. 实体表：从 `raw_records` 投影出证 / 人 / 公司

台账「一证一行」塞在 `payload_json` 里，直接查要 `json_extract` 且没索引。业务更常问
「这个人有几张证」「哪家公司持证人多」「谁最快要续证」——那需要**有主键、有类型、有索引**
的实体表。脚本：`scripts/build_entity_tables.py`（`tests/test_build_entity_tables.py`）。

```powershell
python scripts/build_entity_tables.py                # 从库里最新一期 certificates 重建
python scripts/build_entity_tables.py --dry-run      # 只看统计不落库
python scripts/build_entity_tables.py --batch 20261026
python scripts/build_entity_tables.py --as-of 2026-10-24   # 按指定日期算到期天数（回填/复现）
```

写进**事实库本身** `data/store/bi.sqlite3`（不单开第二个库文件）：

| 表 | 一行是什么 | 主键 |
|---|---|---|
| `certificates` | 一张证（9085 行） | `cert_number`（缺失时回退 `record_key`） |
| `students` | 一个人（8634 行，跨公司只占一行） | `id_number` |
| `companies` | 一家公司（1319 行） | `company_name` |

这三张表是**本地实体表，不是事实表**：`ingest` / `seed` / 差分 / 指标都不碰它们，
每次构建先 `DELETE` 再 `INSERT`（一个事务，幂等），删了也能重建。

> 重新入站 `certificates` 时要注意：`save_ingested()` 会先删旧版本，而 `certificates.version_id`
> 是 `ON DELETE CASCADE`，实体表里的证书行会被一起删掉。`bi.cli ingest` 已自动调
> `refresh-entities` 兜住；若你自己直接调 `store.save_ingested()`，记得随后重建。
建表 DDL 仍然只写在 `sql/schema.sql`（单一事实源），脚本里不重复定义结构——
`tests/test_schema.py` 会断言脚本里没有 `CREATE TABLE`。

两层别混：`raw_records`（事实快照，版本化、不可改）→ 这三张实体表（当前状态，可重建）。
外部系统（发证系统）的数据**不进这个库**：对账在库外按证书编号 / 证件号比对。
让同一个问题在同一张表里只有一种答案。

公司级列的口径：

| 列 | 含义 |
|---|---|
| `cert_count` | 证书张数 |
| `person_count` | 该公司下**不同证件号**数（公司内去重） |
| `cross_company_person_count` | 该公司里有多少人**同时还在别的公司**持证 |
| `start_date_min` / `start_date_max` | 最早 / 最晚开证日 |
| `expiry_date_min` | **最早到期日**（续证提醒看这个） |
| `expiry_date_max` | 最晚到期日（这家公司的手持证书整体何时全部过期） |
| `expiry_unresolved_count` | 到期日解析不出的张数（清洗后仍有 256 张） |
| `expiry_month_precision_count` | 只有年月、按月末取值（**估计值**）的张数 |
| `expiry_from_cert_count` / `expiry_estimated_count` | 证面原文 / 按 2 年效期推算 的张数 |
| `min_expiry_days` / `days_as_of` | 最早到期还有几天 / **这列算的是哪一天**（快照列，见下） |

**到期天数（`expiry_days` / `min_expiry_days`）是带日期的快照列**：
它随「今天」变化，所以入库时必须同时写 `days_as_of`（这一列算的是哪天），
由 `scripts/build_entity_tables.py` 每次重建时整体刷新。查询前要校验 `days_as_of`
是否等于今天——不等于就说明快照陈旧，应重建而不是直接用旧天数。

```sql
-- 已过期（走 ix_certificates_days 索引）
SELECT cert_number, company_name, expiry_date, expiry_source, expiry_days
  FROM certificates WHERE expiry_days < 0 ORDER BY expiry_days;

-- 30 天内到期
SELECT * FROM certificates WHERE expiry_days BETWEEN 0 AND 30;
```

`expiry_days IS NULL` 表示**到期日解析不出**（256 张），不是「已过期 0 天」；
`expiry_days < 0` 才是已过期。

三个必须注意的口径陷阱：

1. **各公司 `person_count` 之和 ≠ 全量持证人数**。实测 223 个证件号跨多家公司，
   相加得 8868，而实际只有 8634 人。要总人数请查 `students` 的行数。
2. **`expiry_date_min` 与 `expiry_date_max` 别看反**。问「谁最快要续证」用前者；
   后者回答「整体覆盖何时结束」。天数也可以用现算（不依赖快照）：

```sql
SELECT company_name, cert_count, expiry_date_min,
       CAST(julianday(expiry_date_min) - julianday(date('now')) AS INTEGER) AS days_to_next_expiry
  FROM companies
 WHERE expiry_date_min IS NOT NULL
 ORDER BY days_to_next_expiry LIMIT 20;
```

3. **解析不出的到期日留空**，由 `validity_shape='无法解析'` 标出来，不猜、不兜底
   （那 256 张全是 `2026/04/17-2028/04/31` 一种写法）。

**为什么放在同一个库里**：一张表一个库会让「查明细」和「查汇总」必须 `ATTACH`，
而 SQLite 对 attached 库用不上索引，备份也要两份。同库之后可以直接 join：

```sql
-- 某公司下每个人的证书，直接用 id_number 关联明细与汇总
SELECT c.company_name, c.id_number, c.person_name,
       c.cert_number, c.expiry_date, c.expiry_source
  FROM certificates c
  JOIN students s ON s.id_number = c.id_number
 WHERE c.company_name = '上海仲诚通信设备有限公司'
 ORDER BY c.expiry_date LIMIT 20;
```

### 9. 定时任务：跑了没有，一眼能看出

实体表的到期天数是「当天快照」，所以需要每天重建一次。执行结果写在 `job_runs`：

| 列 | 作用 |
|---|---|
| `job_id` / `run_date` | 哪个任务、哪一天（**业务日期**，按任务时区算；`UNIQUE` 保证一天一行） |
| `status` | `success` / `partial` / `failed`（**没有 `missed`**） |
| `scheduled_at` / `started_at` / `duration_ms` | 计划时刻、实际开始、耗时 |
| `content_hash` | 业务内容指纹：同 `as_of` 重跑必须相同（幂等自证） |
| `detail_json` | 行数、`as_of`、校验结果、错误信息 |

**漏跑的表达方式就是「这一天没有行」**，不写 `missed` 记录，避免两套真相。
判定逻辑在 `src/bi/jobs.py::job_health()`，三种看法：

```powershell
python -m bi.cli job-status refresh_entity_tables 14
# [refresh_entity_tables] 刷新实体表快照 -> **有缺口**
#   计划 07:00 Asia/Shanghai｜最近一次 2026-10-26｜缺口 2 天
#   2026-10-26  OK   success
#   2026-10-25  ---- missed      ← 一眼看出漏跑
#   2026-10-24  OK   success
```

- `GET /jobs/status?days=14`：同样的日历（JSON），`healthy` 字段可直接接监控；
- `GET /health`：只把「最近一个应跑日期缺记录」记为 `degraded`，历史缺口不影响。

调度侧（任选其一，推荐外部调度 + 启动补跑）：

```powershell
# Windows 任务计划：每天 07:00，程序用 venv 的 python.exe，参数如下，起始位置 = 项目根
-m bi.cli refresh-entities
```

任务本身是**幂等**的：同一天跑 1 次和跑 10 次，三张实体表与 `job_runs` 的内容完全一致
（`job_runs` 按 `(job_id, run_date)` UPSERT 覆盖，不累加计数）。判断是否真幂等，
看 `content_hash` 是否相同，而不是靠人眼比对：

```powershell
python scripts/build_entity_tables.py --as-of 2026-10-26
python scripts/build_entity_tables.py --as-of 2026-10-26   # 指纹必须完全相同
```

---

## 七、查询 API

| 端点 | 作用 |
|---|---|
| `GET /health` `GET /versions` | 服务与批次清单 |
| `GET /metrics/compare?from=&to=` | 指标对比 + 变化归因（`residual` / `fully_explained`） |
| `GET /diffs/summary?from=&to=` | 差分汇总（按类型 / 字段 / 严重度） |
| `GET /diffs/records?from=&to=&event_type=&field_name=&record_key=&limit=&offset=` | 明细下钻 |
| `GET /courses/enrollments?batch=&min_courses=&limit=&offset=` | 按不同课程数统计学员并返回名单；省略批次时查询最新课程明细批次 |
| `GET /evidence/{event_id}` | 证据追溯：上下两期原始记录 + 源文件 + sha256 + **Excel 物理行号** |
| `GET /anomalies?batch=` | 异常清单 |
| `GET /projects` | 看板项目清单（id / 标题 / 页面） |
| `GET /dashboard/{project_id}/data` | 单个项目的看板聚合数据（**只含聚合值，不含个人标识**） |
| `GET /dashboard/all/data` | 所有项目的看板数据 |
| `POST /agent/ask` | 自然语言闭环 |
| `GET /audit` | 审计日志 |

`limit` 上限 500（超过返回 422）。

---

## 八、MCP 工具与约束

7 个工具，**全部只读**（`assert_readonly()` 会自检工具名不含写关键词）：

`bi_list_batches` · `bi_list_course_enrollments` · `bi_compare_metrics` · `bi_diff_summary` · `bi_diff_records` ·
`bi_trace_evidence` · `bi_list_anomalies`

- **只读**：不存在任何写工具；MCP 不直接读库，而是调用第三阶段那套**已被 pytest 覆盖的 HTTP API**，
  保证「MCP 暴露的能力」与「已验证的能力」是同一份实现。
- **查询限制**：`limit` 被 `BI_MCP_MAX_LIMIT`（默认 200）硬截断，响应回传 `truncated` / `server_max_limit`；
  参数走白名单（`event_type` 只能是 added/removed/changed）。
- **审计日志**：每次调用（含失败）都写 `audit_log`：工具名、参数、返回行数、耗时、状态、调用方。

---

## 九、隐私

**事实层落明文，输出层一律掩码**——两层口径不同，刻意如此：

- **事实层（本地库内）**：证件号存**归一化后的明文**（`raw_records.id_number` / `diff_events.id_number`）。
  理由是掩码会**把不同的人折叠成同一串**：实测台账 8634 个不同
  身份证号，掩码后只剩 7644 个——约 990 个不同的人撞成同一个掩码串。拿掩码串去重会静默丢人，
  「持证人数 / 一人多证」全都算不准。
  存 sha256 摘要同样不行：摘要**不可逆**，无法做精确去重。
- **输出层（对外可见的一切）**：API / 看板 / MCP / 证据链 / `payload_json` / 导入日志**一律出掩码**
  （`320826********1638`），姓名保留（业务核对必需）。手机号 / 邮箱只存掩码
  （`primary_phone_masked` / `primary_email_masked`），不参与任何 join。
- `data/inbound/`（原始 Excel）与 `data/store/`（事实库）都已 gitignore。
- 异常与证据输出中的人员标识均为掩码证件号。

**不碰生产库**：本项目的事实库只能是本地 SQLite 或自有 PostgreSQL；
`BI_DATABASE_URL` 若指向 Neon 托管域（生产发证库所在域），`FactStore` 构造时直接拒绝
（`_assert_not_source_db`），避免误在生产库上建表、写入。

---

## 十、关于 D2 数据（重要）

本次交付时**只有一份真实文件**：`Downloads/(登高)总表.xlsx`（= `20261012` 基线，905 行）。
`(登高)总表 (1).xlsx` 只有 5 行，是零碎片段，不能作为第二期。

因此 `20261013` 由 `src/bi/simulate.py` 从**真实 D1 派生**：在真实 883 人上注入确定性变更
（40 人出分通过 / 20 人状态推进 / 15 人付款 / 25 人备注 / 10 人分数修正 / 新增 12 人 / 移除 8 人）
与 5 类已知异常，并在生成时记录 **ground truth**（`data/store/ground_truth_20261012_20261013.json`）。

这样做的价值：差分引擎的每一条判定都能被断言——**比拿两份来历不明的表更能证明规则正确**。

**接入真实 D2**：把文件放到 `data/inbound/20261013/` 覆盖同名文件，然后

```bash
python -m bi.cli seed        # 幂等重建，无需改代码
```

---

## 十一、目录结构

```
config/
  dataset.yaml        登高类目：主键 / 字段映射 / 追踪字段 / 派生规则 / 差分规则 / 异常规则
  metrics.yaml        登高类目：5 个核心指标口径
  goals.yaml          业务关注点 / 目标（Agent 路由）
  period.yaml         自然时间周期（单位：天）
  import_sources.yaml 文件名 → 数据集配置的路由（登高 / 培训人员 / 合并汇总 / 小黄卡）
  dashboards.yaml     看板项目注册（页面 / KPI / 配色）
  datasets/           其它类目的契约（certificates.yaml …）
  metrics/            其它类目的指标口径（certificates.yaml …）
src/bi/
  excel_reader.py     ① Excel 读取（保留物理行号）
  values.py           值归一化（差分的地基）
  cleaning.py         ① 字段清洗（配置驱动，可追溯）
  expiry.py           ① 过期日期解析（多写法 + 精度标记）
  derive.py           ① 清洗 + 口径过滤 + 派生列
  diff_engine.py      ① 差分引擎（added/removed/changed/unchanged）
  anomaly.py          ① 异常检测
  metrics.py          ① 指标计算
  simulate.py         D2 派生 + ground truth
  store.py            ② SQLite / PostgreSQL 事实存储（DDL 从 sql/schema.sql 加载）+ 生产库防护护栏
  dsn.py              PG 连接串解析 / 脱敏（标准库兜底）
  api.py              ③ FastAPI 固定查询
  dashboard.py        ③ 驾驶舱聚合
  project_dashboard.py ③ 多项目看板聚合（含证书台账面板）
  attribution.py      ③ 指标归因
  mcp_server.py       ④ MCP 只读工具
  agent.py            ⑤ 自然语言 Agent
  ingest.py / seed.py / cli.py / settings.py
web/index.html        ⑤ 对话页（浅色 · 单文件 · 无外部依赖）
web/dashboard.html    业务驾驶舱（图表优先，聚合展示多数据集指标）
web/dashboard_certificates.html  证书台账看板（到期倒计时 + 数据质量）
tests/                测试套件（阶段一~五）
scripts/acceptance.py 端到端验收 → docs/ACCEPTANCE.md
scripts/clean_cert_validity.py 证书有效期列清洗（区间 → 到期日）→ data/cleaned/
scripts/build_entity_tables.py 从 raw_records 投影 → bi.sqlite3 的 certificates/students/companies
sql/schema.sql        事实库 DDL（本地数据维护的唯一权威建表脚本）
data/cleaned/         清洗产物（含 PII，已 gitignore）
```

---

## 十二、已知限制与下一步

- **D2 为派生数据**：收到真实第二期后需重跑 `seed`，并复核「归因残差是否为 0」——
  真实数据大概率会暴露新的未追踪字段，这正是该看的地方。
- **归因只覆盖计数型指标**：平均分为均值型，只能给出升降分布，无法用事件计数完全归因。
- **多类目已落地**：登高总表 / 继续教育人员名单 / 课程月份汇总 / 证书台账（小黄卡）四个类目，
  共用同一套引擎（读表 → 清洗 → 派生 → 落库 → 看板 → Agent）；新增项目只需加一份
  `config/datasets/<id>.yaml` + `config/metrics/<id>.yaml` + 一条路由 + 一张看板页。
- **证书台账的数据质量**：9085 张证里 **256 张过期日期解析不出**（全部是
  `2026/04/17-2028/04/31` 这一种写法）、**325 张只有年月**（按月末取值）、
  **5968 张的过期日期是推算值而非证面原文**。
  这三类都不是系统能替业务方决定的，只能上报并回证面核对；
  续证提醒若直接用推算值，会误打扰一批人。
- **异常规则 9 条**：可按业务继续补充（如「补考次数 > 3」「付款金额与科目不匹配」）。
- **未做鉴权**：当前为本地只读服务，若要对外暴露需加认证与更细的字段级脱敏。
- **业务目标路由器（goals.yaml）**：「潜在流失率」「证书快到期」等业务关注点
  在 `config/goals.yaml` 声明；BIAgent 按关键词/意图匹配后调用 `src/bi/goal_actions.py` 里的
  业务专属钩子。**当前数据下**：潜在流失率因缺「单位-应报课程」表无法可靠回答
  （Agent 会显式说明缺什么）；证书快到期仍走「开班日期 + 3 年」的粗筛代理
  （命中 0 人，逻辑就位）——**证书台账里已有真实过期日期**，
  但该目标钩子尚未切换到新类目，看板 `/dashboard/certificates` 已可直接查看。
