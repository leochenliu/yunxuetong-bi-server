# AGENTS.md — AI Agent 工作指南

> 本文件面向 AI Agent（WorkBuddy / Claude / Cursor 等）与本仓库的人类协作者。
> 目标：让 Agent 在改动代码前快速掌握架构、约定与雷区，避免重复踩坑。
> 业务规则说明见 [`README.md`](README.md)，逐文件说明见 [`docs/FILEMAP.md`](docs/FILEMAP.md)。
> 最后更新：2026-10-26

---

## 一、项目是什么

**yunxuetong-bi-server**：把「云学通」培训业务**每日导出的 Excel 总表**变成一条可追问、可核查的数字链路（Python 3.13 + FastAPI + SQLite/PostgreSQL + FastMCP）。

```
data/inbound/<批次>/(登高)总表.xlsx
   │
   │ ① excel_reader → cleaning（清洗）→ derive（口径过滤+派生列）→ diff_engine / anomaly / metrics
   ▼
 ingest ─► ② FactStore (SQLite/PostgreSQL 单库：7 张事实表 + 3 张实体表)
   │
   ▼
 ③ FastAPI  /metrics/compare · /diffs/summary · /diffs/records · /evidence/{id}
   │
   ▼
 ④ FastMCP   7 个只读工具（限额 + 白名单 + 审计）
   │
   ▼
 ⑤ BIAgent   提问 → 多轮查询 → 发现异常 → 追溯证据 → 生成解释
   │
   ▼
 web/dashboard.html 首页（业务驾驶舱，/ 与 /dashboard 均可达）· web/index.html 对话页（/chat）
```

> 库里**没有外部系统镜像表**（`trainingos_*` 已全部移除）：所有口径只有一个来源
> `raw_records`。需要与发证系统对账时，在库外按证书编号 / 证件号比对。

**多类目**：同一套引擎服务多个统计类目，目前四个——
`denggao`（登高总表）、`personnel_roster`（继续教育人员名单）、
`course_monthly_summary`（课程月份汇总）、`certificates`（证书台账·小黄卡）。
类目之间**不共享字段假设**：各自的契约写在 `config/datasets/<id>.yaml`，
路由写在 `config/import_sources.yaml`，看板注册在 `config/dashboards.yaml`。

**只碰自己的库**：事实库只能是本地 SQLite 或自有 PostgreSQL；线上生产库（Neon PG）不被本项目的任何代码路径访问，`FactStore` 构造时会拒绝指向它的连接串（见约定 19）。

**一句话理解这个项目**：每期 Excel 是「当前全量状态」，上一期的值已被覆盖，所以必须先落快照才能算差分；差分产出**字段级事件**；指标变化用这些事件**完全归因**；任何结论都能沿事件回溯到**原始 Excel 的具体某一行**。

**核心概念（改代码前必须懂）**：

- **主键 = `证件号码|姓名`（复合键）**。不用裸证件号，因为实测表中存在两个不同的人共用同一身份证号（吴明志 / 吴民志 同为 `340221197212154953`），裸证件号做 key 会**静默覆盖掉其中一人**。证书台账更进一步：**一人多证**，主键是 `身份证号码|姓名|证书编号`（三段，缺第三段会把 424 人的多证合并）。
- **口径 = 只统计有证件号码的行**。真实数据 905 行中有 22 行（2.4%）无证件号被剔除 → 883 人。剔除数量必须写进版本元数据与页面说明。
- **报名日期不在原表里**，编码在 `开班编号` 中：`0706782` + 年份位(5=2025/6=2026) + MMDD + 班序号，共 13 位。例 `0706782605115` → 2026-05-11 第 5 班。
- **归因残差**：`residual == 0` 表示指标变化已被差分事件完全解释；`!= 0` 表示存在**未被追踪的变化**，此时必须明确报「建议核查」，不允许给出看似完整实则错误的解释。
- **自然时间 = 配置项，单位是天**：「一周 = 7 天、一个自然月 = 30 天」写在 `config/period.yaml`，引擎不写死自然月。周期分组在**查询时**算（`src/bi/period.py`），因此改配置不落库、不影响差分事件与 ground truth。
- **时点口径默认现算；确需落库必须是「快照列 + 日期」成对出现**：「还有几天到期」随当前日期变化，直接存一个裸数字入库即过时。现在的例外是实体表的 `expiry_days` / `min_expiry_days`——它们与 `days_as_of` 成对，由 `scripts/build_entity_tables.py` 每次重建时整体刷新，查询端要校验 `days_as_of` 是否等于今天。静态口径仍只写进 `config/metrics/*.yaml`。
- **数据现状**：`20261012` 是真实数据；`20261013` 是由 `simulate.py` 从真实 D1 **派生**的（含 ground truth），不是真实数据。证书台账 `certificates` 类目目前只有**一期真实数据**（9085 张证），无第二期，故不产生差分事件。

---

## 二、常用命令

```bash
# 依赖（已装在 managed venv，一般无需重装）
pip install -r requirements.txt

# Windows Git Bash 下每次都要带 PYTHONPATH 与 UTF-8
export PYTHONPATH=src
export PYTHONIOENCODING=utf-8

python -m bi.cli seed                  # 灌数据：派生 D2 → 两期入站 → 建差分事件（幂等）
python -m bi.cli ingest <批次> --file <xlsx>  # 从任意路径入站一个文件（如 data/cleaned 下的清洗产物）
python -m bi.cli diff 20261012 20261013 # 差分文字报告
python -m bi.cli profile 20261012       # 数据摸底：口径备注 / 指标 / 异常 / 高频值
python -m bi.cli metrics 20261013       # 指标快照
python -m bi.cli simulate 20261012 20261013  # 重新派生 D2（会覆盖数据 + 重写 ground truth）
python -m bi.cli refresh-entities --trigger schedule  # 重建实体表并写 job_runs（定时任务入口）
python -m bi.cli job-status refresh_entity_tables 14  # 任务执行日历（缺口 = 漏跑）
python -m bi.cli api                    # 启动服务 + 首页（驾驶舱）：http://127.0.0.1:8787/

pytest -q                               # 全部测试（阶段一~五）
python scripts/acceptance.py            # 端到端验收 → docs/ACCEPTANCE.md
```

**Python 解释器**（managed venv，不要用系统 `py`）：

```bash
C:/Users/leoch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
```

- `pytest.ini` 已配 `pythonpath = src`，跑测试不需要手动设 `PYTHONPATH`。
- `seed` 是**幂等**的：同一批次重复入站会先删旧版本再重建（版本 id 会递增，属正常）。
- 测试夹具在 D2 不存在时**自动派生**，所以全新克隆仓库直接 `pytest` 就能跑通。

---

## 三、目录地图

| 路径 | 职责 |
|---|---|
| `config/dataset.yaml` | **业务契约**：主键、30 列字段映射、追踪字段+严重度、派生规则、差分归一化规则、9 条异常规则 |
| `config/metrics.yaml` | 5 个核心指标口径 |
| `config/datasets/` | **其它类目的业务契约**（如 `certificates.yaml`：主键三段、cleaning、expiry 派生、6 条异常规则） |
| `config/metrics/` | **其它类目的指标口径**（如 `certificates.yaml`：9 个静态指标） |
| `config/import_sources.yaml` | **入站路由**：文件名片段 → 数据集/指标契约（如 `小黄卡` → certificates） |
| `config/dashboards.yaml` | **看板项目注册**：项目 id / 页面 / 数据集 / KPI 指标 / 配色 |
| `config/period.yaml` | **自然时间周期（单位：天）**：`days=7` 一周、`days=30` 一个自然月；窗口从 `anchor` 起每 N 天切一刀 |
| `src/bi/values.py` | 值归一化（**差分正确性的地基**），含证件号保护 |
| `src/bi/period.py` | 按配置的自然时间周期分组（查询时计算，不落库） |
| `src/bi/cleaning.py` | 字段清洗（配置驱动 `cleaning:` 段）：全角转半角 / 去空白 / 去噪字符 / 末位 x→X，**只做有唯一答案的清洗**，改动留痕 |
| `src/bi/expiry.py` | 过期日期解析：单日期 / 区间 / 年月 / 不可解析，带形态与精度；**区间两端都合法才算数** |
| `src/bi/excel_reader.py` | 读 xlsx，保留 `__row__` 物理行号 |
| `src/bi/derive.py` | 清洗 `clean()` → 口径过滤 `require_identity()` → 派生列 `derive()` / `derive_expiry()` |
| `src/bi/diff_engine.py` | 差分引擎：added / removed / changed / unchanged |
| `src/bi/anomaly.py` | 异常检测（行级 + 跨期） |
| `src/bi/metrics.py` | 指标计算 |
| `src/bi/ingest.py` | 阶段一→二编排，产出 `Ingested` |
| `src/bi/simulate.py` | 从真实 D1 派生 D2 + 记录 ground truth |
| `src/bi/seed.py` | 灌数据入口（**批次常量硬编码在此**，见雷区 5.5） |
| `src/bi/store.py` | 事实存储：写入 + 查询 + 隐私掩码 + **生产库防护硬护栏**（**DDL 不在这里**，从 `sql/schema.sql` 加载） |
| `src/bi/dsn.py` | PostgreSQL 连接串解析（**标准库兜底**），脱敏与「是不是同一个库」判定共用 |
| `src/bi/jobs.py` | **定时任务的期望与漏跑判定**：`JOBS` 注册表（几点该跑 / 时区 / 宽限）、`run_clock()`（run_date 与 as_of 同源）、`job_health()`（日历里 `missed` 即漏跑） |
| `sql/schema.sql` | **事实库 DDL 的单一事实源**：7 张事实表 + 3 张本地实体表（`certificates` / `students` / `companies`）+ 1 张任务执行记录表 `job_runs` + 索引（**无镜像表、无视图**）；`store.py` 的 `SCHEMA` / `POSTGRES_SCHEMA` 都由它派生 |
| `scripts/clean_cert_validity.py` | 「证书有效期」列清洗：区间 → 到期日；解析复用 `expiry.py`，右端非法则不动；输出到 `data/cleaned/`（**不写回 `data/inbound/`**） |
| `scripts/build_entity_tables.py` | `raw_records` → 三张本地实体表 `certificates` / `students` / `companies`（**同一个 `bi.sqlite3`，不单开库**）；脚本不写 DDL，调 `init_schema()` 建表；每次构建整体 `DELETE` + `INSERT`，幂等可重建 |
| `data/cleaned/` | 清洗产物（仍是含姓名/证件号的完整表，**已 gitignore**） |
| `tests/test_isolation.py` | 生产库防护的**机器校验**（Neon 托管域拒绝 / 与生产库连接串同库拒绝） |
| `src/bi/api.py` | FastAPI 端点（只读 + Agent + 托管首页） |
| `src/bi/project_dashboard.py` | 多项目看板聚合（`PANEL_BUILDERS`：denggao / roster / course / certificates） |
| `src/bi/attribution.py` | 指标归因（`VALUE_RULES` / `EMPTY_RULES`） |
| `src/bi/mcp_server.py` | MCP 只读工具 + 限额 + 审计（**不直连数据库，走 HTTP API**） |
| `src/bi/agent.py` | 自然语言闭环（条件驱动，非固定流水线） |
| `src/bi/cli.py` | 命令行总入口 |
| `tests/` | 按阶段划分的测试，`conftest.py` 提供自动派生夹具 |
| `web/dashboard.html` | 首页 / 业务驾驶舱（`/` 与 `/dashboard` 都指向它） |
| `web/dashboard_certificates.html` | 证书台账看板（`/dashboard/certificates`）：到期倒计时（按来源拆分）、形态/精度分布、异常 |
| `web/index.html` | 对话页（`/chat`，单文件、无外部依赖、浅色主题） |
| `data/inbound/` | 原始 Excel（**含 PII，已 gitignore**） |
| `data/store/` | SQLite 事实库（PostgreSQL 可选）+ ground truth JSON（**已 gitignore**） |

---

## 四、必须遵守的约定

1. **主键必须是复合键 `证件号码|姓名`**。任何按人聚合、去重、对齐的地方都要用 `make_record_key()` / `ingested_key()`，不要自己拼 `证件号码` 单键——会静默丢人。按**物理行**建键，不要按证件号去重。

2. **证件号码禁止做数值规范化**。18 位数字转 float 会丢精度，导致不同的人撞成同一个键。`canon_key()` 因此只做 strip；`canon_value()` 的数值规范化**仅在字符串长度 ≤ 15 时启用**。新增归一化逻辑时务必保住这条。

3. **口径过滤必须留痕**。`require_identity()` 剔除无证件号的行后，剔除数量要写进 `Dataset.notes`，并随版本落库（`upload_versions.dropped_rows`）。**不允许让分母悄悄变小**。

4. **业务规则只写在 `config/*.yaml`**，引擎代码不得硬编码具体列名之外的业务判断。新增培训项目 = 新增一份 `config/<项目>.yaml`，不改引擎。

5. **归因残差是信号，不是可以抹掉的噪音**。若 `residual != 0`，要么补归因规则（优先），要么在输出里明确说明「存在未被追踪的变化，建议核查」。`tests/test_api.py::test_every_countable_metric_is_fully_explained` 会锁死这一点——**不要为了让它变绿而放宽断言或强行调整算法**。

6. **只读边界**。MCP 只暴露查询工具，`TOOLS` 元组里不得出现任何写操作；`assert_readonly()` 会检查工具名是否命中写关键词。新增工具后记得：
   ① 加入 `TOOLS`；② 走 `_clamp_limit()`；③ 每次调用（含失败）都要 `_audit()`。

7. **依赖方向单向**：`config → values → derive/diff_engine/anomaly → ingest → store → api → mcp_server → agent`。下层不得 import 上层（`api.py` 里 import `agent` 用延迟导入，因为 `agent` 会调 `mcp_server`）。**MCP 层零 SQL**，一律走 HTTP API。

8. **版本写入必须幂等且先清引用**。`save_ingested()` 删版本前要显式删掉引用它的 `diff_events`（`from_version_id` 指向上一期，级联删不到），否则重跑会撞外键约束。

9. **测试纪律**。改行为必须补/改测试；断言失败先怀疑自己的边界理解，**不得为了让测试变绿而删除测试或放宽断言**。测试用真实数据 + ground truth 对账，不要 mock 内部模块。

10. **事实层落明文，输出层一律掩码**。证件号在**事实层**（`raw_records.id_number` / `diff_events.id_number`）存**归一化后的明文**——掩码会把不同证件号折叠成同一串（实测 8634 个不同证件号掩码后只剩 7644 个），无法精确统计「持证人数」「一人多证」。但**输出面一律出掩码**：API / 看板 / MCP / 证据链 / payload_json / 导入日志都不得出现明文证件号（掩码函数 `mask_id_number()`，前 6 后 4）。新增输出面或新增落库的 PII 字段时，同步更新 `mask_id_number()` 的适用范围与 `tests/test_store.py::test_pii_stored_plaintext_masked_on_output`。手机号 / 邮箱只存掩码（`primary_phone_masked` / `primary_email_masked`），不参与任何 join。

11. **Windows 输出编码**。CLI 与脚本开头要 `sys.stdout.reconfigure(encoding="utf-8")`，运行命令带 `PYTHONIOENCODING=utf-8`。中文列名/路径一律用 `encoding="utf-8"` 读写。

12. **不要提交 `data/`、`.env`**。原始 Excel 含姓名/证件号/手机号；事实库含经营数据。

13. **清洗只做「有唯一答案」的事**。`cleaning:` 段里只允许全角转半角、去首尾空白、折叠空白、去噪字符、末位 `x→X` 这类结果唯一的动作。**绝不**补身份证缺失位、改校验位、给非法日期（如 4 月 31 日）找替代值——那三类是**编造**。脏数据只由异常规则上报给人工核对。清洗命中数与样例要写进版本 `notes`，**留痕不留痕等于没做**。

14. **有效期解析不猜**。`src/bi/expiry.py` 的规则：区间**两端都合法**才算数；右端写了不存在的日期时整段判为不可解析，**绝不拿左端顶替**（否则会算出一批「有效期 0 天」的假数据）。只有年月时按月末取值并把精度降级为「月」，页面必须标出来。实测 9085 张证里 256 张不可解析（全是 `2026/04/17-2028/04/31` 一种写法），集中且易修。

15. **证面原文与推算值必须分开**。证书台账的「日期来源」列区分**证面原文**（3117 张，可直接用于续证提醒）与**按 2 年效期推算**（5968 张，只能回答规模，续证前必须回证面核对）。任何到期提醒类输出都要按来源拆分展示。

16. **时点口径默认现算，落库必须是「快照列 + as_of」**。「还有几天到期」这类值随日期变化，裸存入库即过时。实体表的 `expiry_days` / `min_expiry_days` 是唯一例外：必须与 `days_as_of` 成对、由整体重建刷新、查询端校验日期，且**绝不进 `tracked_fields`**（每天会凭空多出 9000+ 条假差分事件）。`config/metrics/*.yaml` 只放静态口径。

17. **证件号问题只上报不修复**。位数错与校验位错是**两条**规则（修法不同）；实测 18 位号码里校验位本身就有大量错误，任何「智能修复」都是猜。

18. **建表结构只改 `sql/schema.sql`**。它是事实库 DDL 的**单一事实源**，`src/bi/store.py` 的 `SCHEMA` / `POSTGRES_SCHEMA` 都由它派生（PostgreSQL 变体自动去掉 PRAGMA 并把 `AUTOINCREMENT` 换成 `IDENTITY`）。**不要在 Python 里再写一份 DDL**，两边一定会漂移；`tests/test_schema.py` 会比对「文件重建的库」与「`init_schema()` 建的库」是否完全一致，并顺带检查本地在用的 SQLite 库有没有漂移，还会断言 `scripts/` 下没有任何 `CREATE TABLE`。
    **全部对象都在一个 `bi.sqlite3` 里**（事实表 + 本地实体表）。投影出来的实体表（`certificates` / `students` / `companies`）也走这条规则：DDL 写进 `schema.sql`，脚本只负责「调 `init_schema()` 建表 → 整体 `DELETE` + `INSERT`」，不要为了一张派生表再开第二个库文件。开库的成本是跨库 join 用不上索引 + 备份要两份。

19. **不碰线上生产库，也不镜像外部系统的表**。本项目的事实库只能是本地 SQLite 或自有 PostgreSQL。护栏一道，但必须过：`FactStore` 构造时若发现连接串指向 Neon 托管域（`*.neon.tech` / `*.neon.build`）或与 `TRAININGOS_DATABASE_URL` 同库，直接抛错（`store.py::_assert_not_source_db`，`tests/test_isolation.py` 守着）。**不得为了让某个环境跑起来而放宽这条**——一旦把线上库当事实库，`init_schema()` 会在生产库里建表、每次入站都会写入。
    **不得再建 `trainingos_*` 之类的镜像表**（`tests/test_schema.py::test_no_external_mirror_tables` 守着）：外部快照会让同一个问题有两套答案（源库 UUID 口径 vs 台账公司名口径）、必然过时，还会把 PII 面积翻倍。要与发证系统对账，就在库外按证书编号 / 证件号比对。
    连接串脱敏统一走 `src/bi/dsn.py`（**标准库兜底**，不依赖 psycopg 是否安装——护栏不能因驱动缺失而静默失效）。

20. **定时任务必须写 `job_runs`，漏跑由「缺行」表达**。每次执行（成功/失败）都要 `record_job_run()`；**不写 `missed` 记录**，否则两套真相会打架。一天一 job 一行（`UNIQUE (job_id, run_date)`，重跑 UPSERT 覆盖、不累加计数）；`run_date` 用任务声明的时区算，不能拿 UTC 日期。跨进程用文件锁（`exclusive_lock`），拿不到锁**不写任何表**、退出码 2。幂等靠 `content_hash` 自证：同一 `as_of` 连跑两次指纹必须相同（`tests/test_build_entity_tables.py::test_content_hash_is_stable_across_rebuilds` 守着）。

---

## 五、环境与本地雷区

1. **openpyxl `read_only=True` 不支持随机单元格访问**（`ws.cell()` 会报错）。
   表头必须与数据走**同一次 `iter_rows` 遍历**取得；工作表对象用 `ws.title` 而非 `ws.name`（只读模式没有 `.name`）。

2. **Excel 值形态多样**：日期可能是 `datetime` / `"2026-09-07 00:00:00"` / `"2026/9/7"` / `"2026年9月7日"`；数字可能是 `80` / `"80"` / `80.0`；空可能是 `None` / `""` / `"-"` / `"N/A"`。
   一律先过 `values.py` 的归一化，不要在别处自己 `str()` 比较。

3. **开班编号可能被 Excel 存成数字**（丢前导 0）或浮点（带 `.0`）。`parse_class_code()` 已处理这两种形态，改这里时要保住。

4. **别把 `(登高)总表 (1).xlsx` 当第二期数据**——它只有 5 行，是碎片。真实第二期到手前，第二期一律由 `simulate.py` 派生。

5. **`seed.py` 里的批次常量是硬编码的**（`BATCH_1 = "20261012"` / `BATCH_2 = "20261013"`）。换数据集或接真实新批次时，除了放文件，也要改这两个常量（或后续重构为从配置读取）。

6. **`data/inbound/` 下的 `~$xxx.xlsx` / `.~lock.xxx` 是 Excel/LibreOffice 临时文件**，`ingest.resolve_input_file()` 已忽略；多文件时优先取文件名含「总表」的。

7. **`seed` 重复执行会新建 version 行**（先删后插，id 递增）。这是设计如此，不是 bug；但如果看到 `version_id=17` 之类的大数字，说明库被反复重建过。

8. **本机 Chrome 无头模式不可用**（退出码 21，被进程单例劫持），不要尝试用它做页面渲染验证。验证前端请直接用 `present_files` 打开 `http://127.0.0.1:8787/`。

9. **掩码证件号不是唯一标识**。`mask_id_number()` 只保留前 6 后 4 位：证书台账 8634 个不同身份证号，掩码后只剩 **7644 个**（约 990 个不同的人撞成同一个掩码串）。所以统计「持证人数」「一人多证」必须用**明文证件号**（`raw_records.id_number` / `record_key` 首段，**仅服务端内部使用**），**不要**拿掩码串去重或当主键。

10. **同一批次目录里有多份 Excel 时按文件名路由**。`config/import_sources.yaml` 用**文件名片段**匹配数据集（子串匹配，非精确相等）。新增类目要挑一个足够长、不与其它类目冲突的片段（如 `小黄卡`），否则两份文件会落进同一个数据集。

11. **重新入站 `certificates` 会级联清空实体表**。`save_ingested()` 先删旧版本，而 `certificates.version_id` 是 `ON DELETE CASCADE`，三张实体表里只剩 `students` / `companies`（它们不带 version 外键），于是「台账明明导完了，实体表却是空的」。`bi.cli ingest` 已在入站后自动调 `refresh-entities` 兜住；**直接调 `store.save_ingested()` 的代码必须自己重建**，否则会读到空的 `certificates`。

12. **`content_hash` 不含 `version_id`**。同一份台账重导后版本 id 必然变（先删后插），把它算进指纹会让「内容没变但指纹变了」。指纹只覆盖业务列 + `source_row` + `expiry_days` / `days_as_of`，**不要**把 `updated_at` 或 `version_id` 加回去。

---

## 六、安全边界

- **只读服务**：`GET` 类端点 + `/agent/ask`（只读的问答）。不提供任何写数据的能力。
- **未做鉴权**：当前设计为**本地**服务（`127.0.0.1`）。若要对外暴露，必须先加认证与更细的字段级脱敏。
- **个人信息**：事实层存明文证件号（为了精确去重与跨源关联），**输出层一律掩码**（`320826********1638`）；姓名保留（业务核对必需）。新增输出面时请沿用 `mask_id_number()`。
- **不碰生产库**：事实库只能是本地 SQLite / 自有 PostgreSQL；连接串指向 Neon 托管域会被 `FactStore` 直接拒绝（见约定 19）。库里也不存任何外部系统镜像表。
- **审计不可绕过**：任何新增的查询入口都要写 `audit_log`（工具名、参数、返回行数、耗时、状态、调用方）。

---

## 七、常见任务怎么做

| 任务 | 步骤 |
|---|---|
| **加一个核心指标** | ① `config/metrics.yaml` 加一条（`agg` 支持 `count_rows`/`sum`/`avg`/`count_distinct`）；② 若需归因，在 `attribution.py` 的 `VALUE_RULES` 或 `EMPTY_RULES` 加规则；③ 跑 `test_api.py::test_every_countable_metric_is_fully_explained` 确认残差为 0 |
| **改统计周期（周 / 月 / 任意天数）** | 改 `config/period.yaml` 的 `days`（单位：天）与 `anchor`；改完刷新页面即可。周期标签取窗口起始日，取不到日期的记录必须显式提示「未计入」 |
| **加一条业务目标** | ① `config/goals.yaml` 加一条（`match` 支持关键词/意图/规则 id/字段名）；② 若需专属逻辑在 `src/bi/goal_actions.py` 加函数并注册到 `ACTIONS`；③ 在 `tests/test_goals.py` 加断言（路由器 + action 钩子）；**当原数据缺字段时优先用 `action: missing_data` 让 Agent 显式说明缺什么，不要编造数字** |
| **加一条异常规则** | ① `config/dataset.yaml` 的 `anomaly_rules` 声明；② `anomaly.py` 里实现检测并写入对应 `version_id`；③ 在 `test_anomaly.py` 里**用注入法**验证能检出（不要只依赖真实数据） |
| **加追踪字段** | 改 `dataset.yaml` 的 `tracked_fields` + `severity`；注意加完会改变差分事件数量，需同步更新 ground truth（跑 `bi.cli simulate`）与相关测试断言 |
| **加一张表 / 改字段** | 改 `sql/schema.sql`（DDL 单一事实源）；同步改 `store.py` 的写入与查询；补 `tests/test_schema.py`。老库结构不兼容时**重建**而不是硬改（`PRAGMA user_version` 是结构版本号） |
| **加一个 API 端点** | `api.py` 加路由 + `_audit()`；若涉及分页，`limit` 必须带 `le=MAX_LIMIT`；补 `test_api.py` |
| **加一个 MCP 工具** | `mcp_server.py` 加纯函数（不直连 DB，走 `_client().get(...)`）+ `_clamp_limit()` + `_audit()`；加入 `TOOLS` 元组；补 `test_mcp.py` |
| **接真实的第二期数据** | 把文件放到 `data/inbound/<新批次>/`（文件名含「总表」），改 `seed.py` 的 `BATCH_2`，跑 `python -m bi.cli seed`。**重点复核归因残差是否仍为 0**——真实数据大概率会暴露新的未追踪字段，那里就是要补规则的地方 |
| **支持第二个培训项目** | 复制一份 `config/<项目>.yaml` 并调整字段映射/派生规则；`DatasetConfig.id` 即数据集标识 |
| **接入一个新的统计类目** | ① `config/datasets/<id>.yaml` 写契约（主键/字段映射/清洗/派生/追踪/异常规则）；② `config/metrics/<id>.yaml` 写**静态**指标；③ `config/import_sources.yaml` 加一条文件名片段路由；④ `config/dashboards.yaml` 注册看板项目；⑤ `src/bi/project_dashboard.py` 的 `PANEL_BUILDERS` 注册面板；⑥ 写 `web/dashboard_<id>.html`；⑦ `tests/test_<id>.py` 加断言（**注入法**验证异常规则）；⑧ `python -m bi.cli ingest <批次目录>` 入库 |
| **改证书有效期口径** | 改 `src/bi/expiry.py`（引擎）+ `config/datasets/certificates.yaml` 的 `derive.expiry`（来源列、精度、推算来源取值）；改完重跑 ingest 并复核「不可解析条数」。**注意** `scripts/clean_cert_validity.py` 也用 `expiry.py`，改引擎等于同时改脚本，两边结果必须继续一致 |
| **导出一份列已清洗的 Excel** | `python scripts/clean_cert_validity.py <台账>.xlsx`（默认只折叠区间；`--dry-run` 先看统计）。输出到 `data/cleaned/`，**不要**写回 `data/inbound/`（会被入站路由当成第二份台账） |
| **把清洗产物写进事实库** | `python -m bi.cli ingest <batch> --file data/cleaned/<批次>/<原名>.cleaned.xlsx`。`--file` 仍按文件名片段路由到数据集配置，只是不扫批次目录。**不要**把文件拷进 `data/inbound/` 绕过（同目录两份同片段文件会互相覆盖） |
| **按人 / 按公司看台账** | `python scripts/build_entity_tables.py` → 写进事实库的 `certificates`（一证一行）/ `students`（一人一行）/ `companies`（一家一行），与明细同库可直接 join。注意各公司 `person_count` 相加 ≠ 全量人数（有人跨公司），「谁最快要续证」用 `expiry_date_min` 而不是 `expiry_date_max` |
| **加一个定时任务** | ① `src/bi/jobs.py` 的 `JOBS` 注册 `JobSpec`（`daily_at` / `timezone` / `grace_minutes`）；② 执行完调 `store.record_job_run()`（失败也要调，`status='failed'`）；③ 用 `run_clock()` 取 `run_date`/`as_of`，**不要**自己 `date.today()`；④ 调度器调 `python -m bi.cli <命令>`，退出码非 0 即告警；⑤ 漏跑判定交给 `job_health()`，不要在任务里补写 `missed` 行 |
| **改前端** | 只改 `web/` 下的单文件页面（首页 `dashboard.html`、对话页 `index.html`，无构建步骤）；API 同源托管，无需处理 CORS |
| **与发证系统对账** | 库里不再有镜像表，对账在库外做：导出 `certificates`（证书编号 / 到期日 / `expiry_source`）与对方报表按证书编号比对。**不要**为此再把外部表镜像进事实库（会让同一个问题有两套口径，`test_no_external_mirror_tables` 会红） |

---

## 八、改动后的自检清单

```bash
pytest -q                       # 必须全绿
python scripts/acceptance.py    # 五阶段端到端必须跑通，且与 ground truth 逐条对账一致
```

肉眼确认四点：

1. 归因里所有可计数指标的 `residual == 0`（或明确标了「建议核查」）；
2. 证据链能指回**具体的文件 + Excel 行号 + sha256**；
3. 输出里没有明文证件号（事实层存明文是刻意的，**输出层必须是掩码**）；
4. 生产库防护未破：`BI_DATABASE_URL` 不是 Neon 托管域，且库里没有任何 `trainingos_*` 镜像表。

涉及证书台账时再看两点：

4. **不可解析条数**是否与预期一致（当前 256 张），精度分布里「月」是估计值；
5. **证面原文与推算值是否仍分开展示**（合并展示会导致误提醒）。

涉及定时任务时看三点：

1. `python -m bi.cli job-status refresh_entity_tables 14` 的日历里**没有被误报的缺口**（今天没到点不应出现 `missed`）；
2. `job_runs` 里当天只有**一行**（重跑是覆盖，不是追加）；
3. 同一 `--as-of` 连跑两次，`content_hash` 完全相同。

---

## 九、文档索引

| 文档 | 内容 |
|---|---|
| [`README.md`](README.md) | 项目定位、五阶段架构、主键/字段/派生规则、指标与归因语义、**证书台账的清洗与有效期口径**、API 与 MCP 清单、隐私说明、真实数据接入方式 |
| [`docs/FILEMAP.md`](docs/FILEMAP.md) | 逐文件说明作用 / 关键导出 / 调用方，附「想改 X → 改哪里」对照表 |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | 由 `scripts/acceptance.py` 生成的实测验收报告（含真实数字） |
| [`.workbuddy/memory/`](.workbuddy/memory/) | 工作日志（按日，append-only）与项目长期约定 `MEMORY.md`（含踩坑记录） |
