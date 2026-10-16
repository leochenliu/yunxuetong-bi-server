# 文件地图（FILEMAP）

> 每个文件**为什么存在**、**对外暴露什么**、**谁在用它**。
> 业务规则与五阶段设计见 [`../README.md`](../README.md)。

---

## 一、总览

```
yunxuetong-bi-server/
├── README.md                    业务与五阶段设计说明
├── requirements.txt             运行依赖
├── pytest.ini                   pytest 配置（自动把 src 加入 pythonpath）
├── .env.example                 运行参数模板（拷贝成 .env 生效）
├── .gitignore                   排除 .env / data/inbound / data/store
│
├── config/                      ★ 业务契约（改规则只改这里）
│   ├── dataset.yaml
│   ├── metrics.yaml
│   └── goals.yaml
│
├── src/bi/                      ★ 引擎（22 个模块）
│   ├── settings.py              运行参数
│   ├── config.py                契约加载
│   ├── values.py                值归一化
│   ├── excel_reader.py          ① 读 Excel
│   ├── derive.py                ① 口径过滤 + 派生列
│   ├── diff_engine.py           ① 差分引擎
│   ├── anomaly.py               ① 异常检测
│   ├── metrics.py               ① 指标计算
│   ├── ingest.py                ①→② 编排
│   ├── simulate.py              D2 派生 + ground truth
│   ├── seed.py                  ② 灌数据入口
│   ├── store.py                 ② 事实存储
│   ├── api.py                   ③ 查询 API
│   ├── attribution.py           ③ 指标归因
│   ├── mcp_server.py            ④ MCP 工具
│   ├── agent.py                 ⑤ 自然语言 Agent（含目标路由器）
│   ├── goal_actions.py          ⑤ 业务目标钩子（missing_data / screen_due_cert）
│   ├── cli.py                   命令行总入口
│   └── __init__.py
│
├── tests/                       61 个测试，按阶段划分
├── scripts/acceptance.py        端到端验收 → docs/ACCEPTANCE.md
├── web/index.html               ⑤ 对话页（单文件）
├── docs/                        ACCEPTANCE.md / FILEMAP.md
└── data/                        运行期数据（不入库）
    ├── inbound/<批次>/*.xlsx     原始 Excel（含 PII）
    └── store/                    SQLite 事实库 + ground truth
```

---

## 二、根目录

| 文件 | 作用 |
|---|---|
| `README.md` | 项目定位、五阶段架构、主键/字段/派生规则、指标与归因说明、API 与 MCP 清单、隐私说明、真实 D2 接入方式。**接手项目先读它。** |
| `requirements.txt` | 依赖分四组：运行核心（fastapi/uvicorn/pydantic/PyYAML）、阶段一（openpyxl）、阶段四（fastmcp）、阶段五（httpx）、测试（pytest）。 |
| `pytest.ini` | `pythonpath = src`，让测试无需手动设 PYTHONPATH；`--tb=short`。 |
| `.env.example` | 全部运行参数及默认值：库路径、配置目录、入站目录、**隐私开关** `BI_STORE_PII_PLAINTEXT`、API host/port、**MCP 限额** `BI_MCP_MAX_LIMIT`、审计开关、可选 LLM 配置。 |
| `.gitignore` | 关键作用是**排除 `data/inbound/`（原始 Excel 含姓名/证件号/手机号）与 `data/store/`**，防止个人信息入库。 |

---

## 三、config/ — 业务契约（第一阶段的三件定义）

| 文件 | 作用 |
|---|---|
| `dataset.yaml` | 整个项目**最该先读的文件**。定义：① 主键 `证件号码\|姓名`（附「为何不用裸证件号」的实测依据）；② 30 列字段映射（中文列名 → snake_case）；③ 6 个追踪字段 + 3 个辅助字段 + 严重度分级；④ 派生规则（开班编号 `0706782`+年份位+MMDD+班序号）；⑤ 差分值的归一化规则；⑥ **9 条异常规则**。 |
| `metrics.yaml` | 5 个核心指标口径：报名总人数（行数）、已安排考试、已通过、平均分、已付款——每个都注明聚合方式与来源列。 |
| `goals.yaml` | **业务关注点 / 工作改进目标**。BIAgent 按这里匹配每个问题，再调 `goal_actions.py` 里的业务专属钩子。**改业务关注点只动这里**。 |

> 引擎不硬编码任何字段名。新增一个培训项目（如「低压电工总表」）只需再写一份 `*.yaml`。

---

## 四、src/bi/ — 引擎

### 4.1 基础设施（先于五阶段）

| 文件 | 作用 | 关键导出 | 谁在用 |
|---|---|---|---|
| `settings.py` | 运行参数：读 `.env`（自带极简 dotenv，不引入额外依赖），相对路径按项目根解析，进程内单例。与应用 yaml 契约分离（前者进 git，后者不进）。 | `Settings`、`get_settings()` | 几乎所有模块 |
| `config.py` | 把 yaml 读成强类型对象，提供 `standard_name()`（中文列→标准名）、`severity_of()`、`anomaly_rule()` 等便捷访问。 | `DatasetConfig`、`MetricDef`、`load_dataset_config()`、`load_metrics_config()` | 除 values 外全部 |
| `values.py` | **整个差分正确性的地基**。同一业务含义在 Excel 有数字/文本/日期/空占位多种形态，这里统一成可稳定比较的规范字符串。含两条关键约束：`canon_key()` 对证件号**不做数值规范化**（18 位 float 会丢精度）；`canon_value()` 的数值规范化仅对长度 ≤ 15 的字符串生效。附宽松 `to_number()` / `to_date()`。 | `canon_key`、`canon_value`、`display_value` | diff_engine、derive、store、attribution |

### 4.2 阶段一 · Excel 差分

| 文件 | 作用 | 关键导出 |
|---|---|---|
| `excel_reader.py` | 把 xlsx 读成内存 `Dataset`，只负责「读进来 + 保真」：**保留每行 Excel 物理行号**（`__row__`，证据追溯要用）、重复列名自动去重、跳全空行、忽略临时文件。用 openpyxl 只读模式，表头与数据走**同一次** `iter_rows` 遍历。 | `Dataset`、`read_primary()`、`file_fingerprint()`（sha256） |
| `derive.py` | ①**口径过滤**：剔除无证件号的行（22 行 / 2.4%），剔除数写入 `notes` 保证「分母变小」有痕迹；②**派生列**：从开班编号解析出 `报名日期/报名月份/开班日期/班序号`，并生成 `人数/已考试人数/未考试人数/已通过人数/已付款人数/延期换证人数` 等 0/1 标记列（聚合引擎无法条件计数，只能靠标记列求和）。 | `parse_class_code()`、`require_identity()`、`derive()`、`DERIVED_COLUMNS` |
| `diff_engine.py` | **核心**。两期快照逐人逐字段比对，产出四类判定 `added / removed / changed / unchanged`；changed 记录到字段级 `from → to`（归一化值 + 原始展示值）；输出字段维度汇总与严重度。按**物理行**建键而非去重，避免重复证件号被静默合并。 | `Snapshot`、`build_snapshot()`、`diff_snapshots()`、`DiffResult` |
| `anomaly.py` | 执行 yaml 里的 9 条规则：18 位身份证校验位（ISO 7064 MOD 11-2）、分数越界、已通过但低分/无分数/无考试日期、已付款但金额 0、开班编号不可解析、复合主键重复，以及**跨期**的状态回退。 | `check_row_anomalies()`、`check_cross_period_anomalies()`、`summarize()` |
| `metrics.py` | 按 `metrics.yaml` 口径计算 5 个指标（支持 count_rows / sum / avg / count_distinct）。 | `compute_metrics()`、`MetricValue` |

### 4.3 阶段一→二 编排与造数

| 文件 | 作用 |
|---|---|
| `ingest.py` | **阶段一与阶段二的接缝**。一次调用产出：数据集 + 快照 + 指标 + 异常 + 文件指纹，打包成 `Ingested` 交给 store。含批次目录定位（忽略 `~$` 临时文件，多文件时优先「总表」）。 |
| `simulate.py` | 因**没有真实第二期**，从真实 D1 派生 D2：注入确定性变更（40 人出分通过 / 20 人状态推进 / 15 人付款 / 25 人备注 / 10 人分数修正 / 新增 12 人 / 移除 8 人）+ 5 类已知异常，并**在生成时记录 ground truth**（改了什么、从什么变成什么）。含合法身份证号生成器（自动算校验位）。 |
| `seed.py` | 一键灌数据：D2 不存在则派生 → 两期入站 → 建立差分事件 → 跑跨期异常。幂等，可重复执行。 |
| `cli.py` | 命令行总入口：`profile`（数据摸底）/ `ingest` / `seed` / `diff` / `metrics` / `simulate` / `api`。 |

### 4.4 阶段二 · 事实存储

| 文件 | 作用 |
|---|---|
| `store.py` | **项目最大的模块（约 540 行）**。含 ① 6 张表的 `SCHEMA`：`upload_versions`（上传版本+sha256+口径）、`raw_records`（每期每人一行+物理行号）、`diff_events`（字段级事件）、`metric_snapshots`、`anomalies`、`audit_log`；② 写入（幂等重建，删除前先显式清理 `diff_events` 避免外键冲突）；③ 查询方法（版本列表、指标快照、差分汇总/明细分页、**证据追溯**、异常清单、审计）；④ 隐私处理 `hash_id_number()` / `mask_id_number()`。 |

### 4.5 阶段三 · 固定查询 API

| 文件 | 作用 |
|---|---|
| `api.py` | 8 个只读端点 + 1 个 Agent 端点 + 首页。关键设计：`limit` 上限 500（超出返回 422）；每次调用写审计；`from` 是 Python 关键字故用 `alias`；同源托管 `web/index.html`，避免 CORS。 |
| `attribution.py` | **本项目最有价值的一层**。把「指标涨了 4 人」拆成可核查的事件计数，并计算**残差**：`residual == 0` 表示已被差分事件完全解释；`!= 0` 表示存在未被追踪的变化，必须报「建议核查」。三种归因模式：差集型（报名总人数 = 新增 − 移除）、字段值型（已通过 / 已付款，含新增移除直接命中）、空值型（已安排考试，含字段填入/清空 + 移除者原本含值）；均值型（平均分）只能给升降分布故标记不可计数归因。 |

### 4.6 阶段四 · MCP

| 文件 | 作用 |
|---|---|
| `mcp_server.py` | 6 个**只读**工具的注册与约束。三项硬约束：① 只读——`TOOLS` 只含查询函数，`assert_readonly()` 自检工具名不含写关键词；② 限额——`_clamp_limit()` 把 `limit` 硬截断到 `BI_MCP_MAX_LIMIT` 并回传 `truncated`，参数走白名单；③ 审计——每次调用（含失败）写 `audit_log`。`BIQueryClient` 支持注入 ASGI 客户端，测试无需起服务。fastmcp 未安装时工具函数仍可被调用与测试。 |

### 4.7 阶段五 · 自然语言 Agent

| 文件 | 作用 |
|---|---|
| `agent.py` | **条件驱动的多轮闭环**（非固定流水线）：列批次 → 指标对比+归因 → 差分汇总 → **仅当有变化时**下钻最活跃字段/新增/移除 → 异常清单 → **仅当有高严重度异常时**按主键回查事件并追溯证据 → 生成解释。**业务目标路由器**：`select_goal(question, intent, goals)` 按 `goals.yaml` 匹配；命中时目标名写进解释头部、`output.max_steps` 限制推理步数；命中专属 `action` 时跳到 `goal_actions.py` 优先输出。 |
| `goal_actions.py` | 业务目标专属钩子：`missing_data`（缺数据时显式回绝，列需补字段） / `screen_due_cert`（证书应复查粗筛）。新增业务关注点 = 加函数 + 注册到 `ACTIONS`。 |

---

## 五、tests/ — 61 个测试

| 文件 | 覆盖 |
|---|---|
| `conftest.py` | 共享夹具：**D2 不存在时自动派生**，所以全新克隆仓库直接 `pytest` 就能跑通。 |
| `test_values.py` | 归一化边界：数字/日期/空占位、**证件号不被数值规范化**、开班编号解析（含 Excel 丢前导 0 与浮点形态）。 |
| `test_diff.py` | 阶段一验收：与 ground truth **逐条对账**（人员集合、字段集合、from/to 值、字段汇总、注入异常可指认）、口径过滤幂等。 |
| `test_anomaly.py` | 真实数据异常（3 人已通过无分数）+ **注入 6 类坏数据反证检测器不是「永远返回 0」**。 |
| `test_store.py` | 阶段二验收：版本/指标/事件落库、分页与过滤、严重度排序、证据链完整性、**PII 未落明文**。 |
| `test_api.py` | 阶段三验收：四类查询、分页不重叠、`limit` 超限被拒、证据链到行号，以及**所有可计数指标残差必须为 0**。 |
| `test_mcp.py` | 阶段四验收：只读自检、能力一致、限额硬截断、参数白名单、每次调用（含失败）都有审计。 |
| `test_agent.py` | 阶段五验收：闭环结构（步数、必需工具、解释分段）、证据确实被追溯、**查询是依赖而非固定**（下钻字段来自上一部结果）、解释数字与 API 一致。 |

---

## 六、其它

| 文件 | 作用 |
|---|---|
| `scripts/acceptance.py` | 端到端验收：一次跑完五阶段，打印人类可读报告并写入 `docs/ACCEPTANCE.md`。 |
| `web/index.html` | 对话页（单文件、无外部依赖、浅色主题）。展示指标卡（含归因驱动项与「已完全解释」徽标）、结论、证据追溯（文件名/行号/掩码证件号/sha256）、可展开的执行轨迹。涨红跌绿符合国内习惯。 |
| `docs/ACCEPTANCE.md` | 由验收脚本生成的实测报告（含真实数字）。 |
| `data/inbound/<批次>/(登高)总表.xlsx` | 原始数据。`20261012` 为真实数据，`20261013` 为派生数据。 |
| `data/store/bi.sqlite3` | SQLite 事实库。 |
| `data/store/ground_truth_*.json` | D2 生成时记录的正确答案，供 `test_diff.py` 对账。 |
| `.workbuddy/memory/*.md` | 项目工作日志与长期约定（含踩坑记录）。 |

---

## 七、想改什么，改哪里

| 你的需求 | 改这里 |
|---|---|
| 换主键 / 加追踪字段 / 调严重度 / 加异常规则 / 改派生规则 | `config/dataset.yaml` |
| 加指标 / 改指标口径 | `config/metrics.yaml` |
| 调查询上限、隐私开关、端口 | `.env`（参照 `.env.example`） |
| 改指标变化怎么归因 | `src/bi/attribution.py` 的 `VALUE_RULES` / `EMPTY_RULES` |
| 加一个新的 API 端点 | `src/bi/api.py` |
| 加一个新的 MCP 工具 | `src/bi/mcp_server.py`（记得加入 `TOOLS` 元组） |
| 改 Agent 的查询策略 / 解释模板 | `src/bi/agent.py` |
| 加 / 改业务关注点 | `config/goals.yaml`（声明）+ `src/bi/goal_actions.py`（专属逻辑） |
| 换数据库 / 加表 | `src/bi/store.py` 的 `SCHEMA` |
| 换前端样式 | `web/index.html` |
| 支持第二个培训项目 | 新增一份 `config/<项目>.yaml`，几乎无需改代码 |

---

## 八、依赖规则（改代码前请遵守）

1. **方向单向**：`config → values → derive/diff_engine/anomaly → ingest → store → api → mcp_server → agent`。
   下层不知道上层的存在。`store` 不 import `api`，`api` 不 import `agent`（`agent` 端点内做延迟导入）。
2. **业务规则只写在 yaml**，引擎代码不出现具体列名之外的业务判断。
3. **归一化只走 `values.py`**，不要在别处自己 `str()`。
4. **MCP 不直连数据库**，而是调用第三阶段那套已被测试覆盖的 HTTP API——保证「MCP 暴露的能力」与「已验证的能力」是同一份实现。
5. 新增写操作 = 违反只读约束，会被 `assert_readonly()` 与 `test_mcp.py` 拦下。
