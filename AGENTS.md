# AGENTS.md — AI Agent 工作指南

> 本文件面向 AI Agent（WorkBuddy / Claude / Cursor 等）与本仓库的人类协作者。
> 目标：让 Agent 在改动代码前快速掌握架构、约定与雷区，避免重复踩坑。
> 业务规则说明见 [`README.md`](README.md)，逐文件说明见 [`docs/FILEMAP.md`](docs/FILEMAP.md)。
> 最后更新：2026-10-16

---

## 一、项目是什么

**yunxuetong-bi-server**：把「云学通」培训业务**每日导出的 Excel 总表**变成一条可追问、可核查的数字链路（Python 3.13 + FastAPI + SQLite + FastMCP）。

```
data/inbound/<批次>/(登高)总表.xlsx
   │
   │ ① excel_reader → derive（口径过滤+派生列）→ diff_engine / anomaly / metrics
   ▼
 ingest ─► ② FactStore (SQLite 6 张表)
   │
   ▼
 ③ FastAPI  /metrics/compare · /diffs/summary · /diffs/records · /evidence/{id}
   │
   ▼
 ④ FastMCP   6 个只读工具（限额 + 白名单 + 审计）
   │
   ▼
 ⑤ BIAgent   提问 → 多轮查询 → 发现异常 → 追溯证据 → 生成解释
   │
   ▼
 web/index.html 对话页
```

**一句话理解这个项目**：每期 Excel 是「当前全量状态」，上一期的值已被覆盖，所以必须先落快照才能算差分；差分产出**字段级事件**；指标变化用这些事件**完全归因**；任何结论都能沿事件回溯到**原始 Excel 的具体某一行**。

**核心概念（改代码前必须懂）**：

- **主键 = `证件号码|姓名`（复合键）**。不用裸证件号，因为实测表中存在两个不同的人共用同一身份证号（吴明志 / 吴民志 同为 `340221197212154953`），裸证件号做 key 会**静默覆盖掉其中一人**。
- **口径 = 只统计有证件号码的行**。真实数据 905 行中有 22 行（2.4%）无证件号被剔除 → 883 人。剔除数量必须写进版本元数据与页面说明。
- **报名日期不在原表里**，编码在 `开班编号` 中：`0706782` + 年份位(5=2025/6=2026) + MMDD + 班序号，共 13 位。例 `0706782605115` → 2026-05-11 第 5 班。
- **归因残差**：`residual == 0` 表示指标变化已被差分事件完全解释；`!= 0` 表示存在**未被追踪的变化**，此时必须明确报「建议核查」，不允许给出看似完整实则错误的解释。
- **数据现状**：`20261012` 是真实数据；`20261013` 是由 `simulate.py` 从真实 D1 **派生**的（含 ground truth），不是真实数据。

---

## 二、常用命令

```bash
# 依赖（已装在 managed venv，一般无需重装）
pip install -r requirements.txt

# Windows Git Bash 下每次都要带 PYTHONPATH 与 UTF-8
export PYTHONPATH=src
export PYTHONIOENCODING=utf-8

python -m bi.cli seed                  # 灌数据：派生 D2 → 两期入站 → 建差分事件（幂等）
python -m bi.cli diff 20261012 20261013 # 差分文字报告
python -m bi.cli profile 20261012       # 数据摸底：口径备注 / 指标 / 异常 / 高频值
python -m bi.cli metrics 20261013       # 指标快照
python -m bi.cli simulate 20261012 20261013  # 重新派生 D2（会覆盖数据 + 重写 ground truth）
python -m bi.cli api                    # 启动服务 + 对话页：http://127.0.0.1:8787/

pytest -q                               # 61 个测试（阶段一~五）
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
| `src/bi/values.py` | 值归一化（**差分正确性的地基**），含证件号保护 |
| `src/bi/excel_reader.py` | 读 xlsx，保留 `__row__` 物理行号 |
| `src/bi/derive.py` | 口径过滤 `require_identity()` + 派生列 `derive()` |
| `src/bi/diff_engine.py` | 差分引擎：added / removed / changed / unchanged |
| `src/bi/anomaly.py` | 异常检测（行级 + 跨期） |
| `src/bi/metrics.py` | 指标计算 |
| `src/bi/ingest.py` | 阶段一→二编排，产出 `Ingested` |
| `src/bi/simulate.py` | 从真实 D1 派生 D2 + 记录 ground truth |
| `src/bi/seed.py` | 灌数据入口（**批次常量硬编码在此**，见雷区 5.5） |
| `src/bi/store.py` | 事实存储：6 张表 schema + 写入 + 查询 + 隐私掩码 |
| `src/bi/api.py` | FastAPI 端点（只读 + Agent + 托管首页） |
| `src/bi/attribution.py` | 指标归因（`VALUE_RULES` / `EMPTY_RULES`） |
| `src/bi/mcp_server.py` | MCP 只读工具 + 限额 + 审计（**不直连数据库，走 HTTP API**） |
| `src/bi/agent.py` | 自然语言闭环（条件驱动，非固定流水线） |
| `src/bi/cli.py` | 命令行总入口 |
| `tests/` | 按阶段划分的 61 个测试，`conftest.py` 提供自动派生夹具 |
| `web/index.html` | 对话页（单文件、无外部依赖、浅色主题） |
| `data/inbound/` | 原始 Excel（**含 PII，已 gitignore**） |
| `data/store/` | SQLite 事实库 + ground truth JSON（**已 gitignore**） |

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

10. **隐私默认关闭**。证件号默认不落明文（`BI_STORE_PII_PLAINTEXT=false`），只存 sha256 + 掩码。新增涉及个人信息落库的字段时，同步更新 `mask_id_number()` 的适用范围与 `test_store.py::test_pii_not_stored_in_plaintext`。

11. **Windows 输出编码**。CLI 与脚本开头要 `sys.stdout.reconfigure(encoding="utf-8")`，运行命令带 `PYTHONIOENCODING=utf-8`。中文列名/路径一律用 `encoding="utf-8"` 读写。

12. **不要提交 `data/`、`.env`**。原始 Excel 含姓名/证件号/手机号；事实库含经营数据。

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

---

## 六、安全边界

- **只读服务**：`GET` 类端点 + `/agent/ask`（只读的问答）。不提供任何写数据的能力。
- **未做鉴权**：当前设计为**本地**服务（`127.0.0.1`）。若要对外暴露，必须先加认证与更细的字段级脱敏。
- **个人信息**：异常与证据输出中的人员标识一律用掩码证件号（`320826********1638`），姓名保留（业务核对必需）。新增输出面时请沿用。
- **审计不可绕过**：任何新增的查询入口都要写 `audit_log`（工具名、参数、返回行数、耗时、状态、调用方）。

---

## 七、常见任务怎么做

| 任务 | 步骤 |
|---|---|
| **加一个核心指标** | ① `config/metrics.yaml` 加一条（`agg` 支持 `count_rows`/`sum`/`avg`/`count_distinct`）；② 若需归因，在 `attribution.py` 的 `VALUE_RULES` 或 `EMPTY_RULES` 加规则；③ 跑 `test_api.py::test_every_countable_metric_is_fully_explained` 确认残差为 0 |
| **加一条业务目标** | ① `config/goals.yaml` 加一条（`match` 支持关键词/意图/规则 id/字段名）；② 若需专属逻辑在 `src/bi/goal_actions.py` 加函数并注册到 `ACTIONS`；③ 在 `tests/test_goals.py` 加断言（路由器 + action 钩子）；**当原数据缺字段时优先用 `action: missing_data` 让 Agent 显式说明缺什么，不要编造数字** |
| **加一条异常规则** | ① `config/dataset.yaml` 的 `anomaly_rules` 声明；② `anomaly.py` 里实现检测并写入对应 `version_id`；③ 在 `test_anomaly.py` 里**用注入法**验证能检出（不要只依赖真实数据） |
| **加追踪字段** | 改 `dataset.yaml` 的 `tracked_fields` + `severity`；注意加完会改变差分事件数量，需同步更新 ground truth（跑 `bi.cli simulate`）与相关测试断言 |
| **加一个 API 端点** | `api.py` 加路由 + `_audit()`；若涉及分页，`limit` 必须带 `le=MAX_LIMIT`；补 `test_api.py` |
| **加一个 MCP 工具** | `mcp_server.py` 加纯函数（不直连 DB，走 `_client().get(...)`）+ `_clamp_limit()` + `_audit()`；加入 `TOOLS` 元组；补 `test_mcp.py` |
| **接真实的第二期数据** | 把文件放到 `data/inbound/<新批次>/`（文件名含「总表」），改 `seed.py` 的 `BATCH_2`，跑 `python -m bi.cli seed`。**重点复核归因残差是否仍为 0**——真实数据大概率会暴露新的未追踪字段，那里就是要补规则的地方 |
| **支持第二个培训项目** | 复制一份 `config/<项目>.yaml` 并调整字段映射/派生规则；`DatasetConfig.id` 即数据集标识 |
| **改前端** | 只改 `web/index.html`（单文件，无构建步骤）；API 同源托管，无需处理 CORS |

---

## 八、改动后的自检清单

```bash
pytest -q                       # 必须全绿（当前 61 passed）
python scripts/acceptance.py    # 五阶段端到端必须跑通，且与 ground truth 逐条对账一致
```

肉眼确认三点：

1. 归因里所有可计数指标的 `residual == 0`（或明确标了「建议核查」）；
2. 证据链能指回**具体的文件 + Excel 行号 + sha256**；
3. 输出里没有明文证件号。

---

## 九、文档索引

| 文档 | 内容 |
|---|---|
| [`README.md`](README.md) | 项目定位、五阶段架构、主键/字段/派生规则、指标与归因语义、API 与 MCP 清单、隐私说明、真实数据接入方式 |
| [`docs/FILEMAP.md`](docs/FILEMAP.md) | 逐文件说明作用 / 关键导出 / 调用方，附「想改 X → 改哪里」对照表 |
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | 由 `scripts/acceptance.py` 生成的实测验收报告（含真实数字） |
| [`.workbuddy/memory/`](.workbuddy/memory/) | 工作日志（按日，append-only）与项目长期约定 `MEMORY.md`（含踩坑记录） |
