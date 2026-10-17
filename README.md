# yunxuetong-bi-server

「云学通」培训业务 BI 差分与事实服务 —— 把**每天中午导出的 Excel 总表**变成一条
**可以被追问、被核查的数字链路**。

> 核心承诺：任何一个结论，都能沿着 `自然语言问题 → 查询 → 事件 → 原始 Excel 的具体某一行` 倒推回去。

真实数据：`(登高)总表.xlsx`，905 行 / 883 人（剔除了 22 行无证件号码的记录）。

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
   upload_versions · raw_records · diff_events · metric_snapshots · anomalies · audit_log
        │
        │ ③ 固定查询
        ▼
   FastAPI  /metrics/compare · /diffs/summary · /diffs/records · /evidence/{id}
        │
        │ ④ 只读包装（限额 + 白名单 + 审计）
        ▼
   FastMCP  6 个只读工具
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
| ② 事实存储 | 存上传版本、原始记录、差分事件、指标快照 | `store.py`（6 张表） | `tests/test_store.py` |
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

# 3) 起服务（同时托管对话页）
python -m bi.cli api                   # http://127.0.0.1:8787/
# 驾驶舱：http://127.0.0.1:8787/dashboard

# 4) 单测 / 验收
pytest -q                              # 运行全部测试
python scripts/acceptance.py           # 生成 docs/ACCEPTANCE.md
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

## 六、查询 API

| 端点 | 作用 |
|---|---|
| `GET /health` `GET /versions` | 服务与批次清单 |
| `GET /metrics/compare?from=&to=` | 指标对比 + 变化归因（`residual` / `fully_explained`） |
| `GET /diffs/summary?from=&to=` | 差分汇总（按类型 / 字段 / 严重度） |
| `GET /diffs/records?from=&to=&event_type=&field_name=&record_key=&limit=&offset=` | 明细下钻 |
| `GET /evidence/{event_id}` | 证据追溯：上下两期原始记录 + 源文件 + sha256 + **Excel 物理行号** |
| `GET /anomalies?batch=` | 异常清单 |
| `POST /agent/ask` | 自然语言闭环 |
| `GET /audit` | 审计日志 |

`limit` 上限 500（超过返回 422）。

---

## 七、MCP 工具与约束

6 个工具，**全部只读**（`assert_readonly()` 会自检工具名不含写关键词）：

`bi_list_batches` · `bi_compare_metrics` · `bi_diff_summary` · `bi_diff_records` ·
`bi_trace_evidence` · `bi_list_anomalies`

- **只读**：不存在任何写工具；MCP 不直接读库，而是调用第三阶段那套**已被 pytest 覆盖的 HTTP API**，
  保证「MCP 暴露的能力」与「已验证的能力」是同一份实现。
- **查询限制**：`limit` 被 `BI_MCP_MAX_LIMIT`（默认 200）硬截断，响应回传 `truncated` / `server_max_limit`；
  参数走白名单（`event_type` 只能是 added/removed/changed）。
- **审计日志**：每次调用（含失败）都写 `audit_log`：工具名、参数、返回行数、耗时、状态、调用方。

---

## 八、隐私

- 证件号码**默认不落明文**（`BI_STORE_PII_PLAINTEXT=false`）：只存 `sha256` 摘要 + 掩码
  （`320826********1638`），payload 中的证件号与手机号同样掩码。证据追溯走摘要与行号，无需回明文。
- `data/inbound/`（原始 Excel）与 `data/store/`（事实库）都已 gitignore。
- 异常与证据输出中的人员标识均为掩码证件号。

---

## 九、关于 D2 数据（重要）

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

## 十、目录结构

```
config/
  dataset.yaml        主键 / 字段映射 / 追踪字段 / 派生规则 / 差分规则 / 异常规则
  metrics.yaml        5 个核心指标口径
src/bi/
  excel_reader.py     ① Excel 读取（保留物理行号）
  derive.py           ① 口径过滤 + 派生列
  diff_engine.py      ① 差分引擎（added/removed/changed/unchanged）
  anomaly.py          ① 异常检测
  metrics.py          ① 指标计算
  simulate.py         D2 派生 + ground truth
  store.py            ② SQLite / PostgreSQL 事实存储
  api.py              ③ FastAPI 固定查询
  attribution.py      ③ 指标归因
  mcp_server.py       ④ MCP 只读工具
  agent.py            ⑤ 自然语言 Agent
  ingest.py / seed.py / cli.py / settings.py
web/index.html        ⑤ 对话页（浅色 · 单文件 · 无外部依赖）
web/dashboard.html    业务驾驶舱（图表优先，聚合展示多数据集指标）
tests/                测试套件（阶段一~五）
scripts/acceptance.py 端到端验收 → docs/ACCEPTANCE.md
```

---

## 十一、已知限制与下一步

- **D2 为派生数据**：收到真实第二期后需重跑 `seed`，并复核「归因残差是否为 0」——
  真实数据大概率会暴露新的未追踪字段，这正是该看的地方。
- **归因只覆盖计数型指标**：平均分为均值型，只能给出升降分布，无法用事件计数完全归因。
- **单表单项目**：目前只处理「登高」一张总表；新增项目只需加一份 `config/*.yaml`。
- **异常规则 9 条**：可按业务继续补充（如「补考次数 > 3」「付款金额与科目不匹配」）。
- **未做鉴权**：当前为本地只读服务，若要对外暴露需加认证与更细的字段级脱敏。
- **业务目标路由器（goals.yaml）**：「潜在流失率」「证书快到期」等业务关注点
  在 `config/goals.yaml` 声明；BIAgent 按关键词/意图匹配后调用 `src/bi/goal_actions.py` 里的
  业务专属钩子。**当前数据下**：潜在流失率因缺「单位-应报课程」表无法可靠回答
  （Agent 会显式说明缺什么）；证书快到期因 D1 开班日期都在近一年命中 0 人
  （逻辑就位，等历史批次即生效）。详见 `config/goals.yaml` 顶部注释。
