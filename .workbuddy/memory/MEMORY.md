# MEMORY.md — yunxuetong-bi-server 项目长期笔记

## 项目定位
「云学通」培训业务 BI 差分与事实服务：把每日导出的 Excel 总表变成**可追问、可核查**的数字链路。
承诺：任何结论都能沿 `自然语言问题 → 查询 → 差分事件 → 原始 Excel 物理行号` 倒推回去。

上游参考：`C:/Users/leoch/mycode/trainingos-webhook-events/excel-cockpit`（TypeScript 先行原型，
差分口径 / 字段选择 / 开班编号解析规则均源自它，**其注释里的业务结论是本项目的权威依据**）。

## 技术栈与运行
- Python 3.13 + FastAPI + SQLite + FastMCP。包目录 `src/bi`，`PYTHONPATH=src`。
- 常用命令：`python -m bi.cli seed | diff <from> <to> | api | profile <batch> | metrics <batch>`
- 验收：`pytest -q`（215 tests）、`python scripts/acceptance.py`（→ `docs/ACCEPTANCE.md`）
- 依赖装在 `C:/Users/leoch/.workbuddy/binaries/python/envs/default`。

## 必须遵守的项目约定
1. **主键 = `证件号码|姓名`（复合键）**。裸证件号会静默覆盖同号不同名的两个人（实测存在）。
2. **口径：只统计有证件号码的行**（占 2.4% 的无证件号行被剔除），剔除数量必须写进版本元数据与页面说明，
   不允许「分母悄悄变小」。
3. **证件号码不做数值规范化**（18 位数字，float 会丢精度）；数值规范化仅对长度 ≤ 15 的字符串启用。
4. **隐私两层口径**：**事实层存明文**证件号（`raw_records.id_number` / `diff_events.id_number` /
   `trainingos_students.id_number`），因为掩码会把不同的人折叠成同一串（8634 → 7644），无法精确去重与跨源关联；
   **输出层一律掩码**（API / 看板 / MCP / 证据链 / `payload_json` 前 6 后 4）。手机号/邮箱只存掩码。
5. **归因残差语义**：`residual == 0` 表示指标变化已被差分事件完全解释；`residual != 0` 必须明确报「建议核查」，
   不允许给出看似完整实则错误的解释。有测试锁定所有可计数指标残差为 0。
6. **只读边界**：MCP 不提供任何写工具，`assert_readonly()` 自检；查询 limit 由 `BI_MCP_MAX_LIMIT` 硬截断。
7. Excel 读取用 openpyxl `read_only`，**表头与数据必须走同一次 `iter_rows` 遍历**（只读模式不支持 `ws.cell()`）。
8. **两库完全隔离**：TrainingOS（Neon PG）是另一个系统的库，本项目只**单向只读**镜像到本地
   （`scripts/import_trainingos.py`，四层只读护栏）。不跨库 join、不回写、**不得把源库当事实库**
   （`BI_DATABASE_URL` 指向 Neon 或与 `TRAININGOS_DATABASE_URL` 同库时 `FactStore` 构造即拒）。

## 数据现状
- `20261012` = 真实数据（905 行 / 883 人）。
- `20261013` = 由 `src/bi/simulate.py` 从真实 D1 派生（含 ground truth JSON），**非真实数据**。
- 真实 D2 到手后放入 `data/inbound/20261013/` 覆盖同名文件，重跑 `seed` 即可，无需改代码。
- `data/inbound/`（原始 Excel，含 PII）与 `data/store/` 均在 .gitignore 中。

## 扩展方式
新增项目（如「低压电工总表」）只需新增一份 `config/<项目>.yaml`，引擎不硬编码任何字段名。

## 业务目标路由（goals.yaml）
- 业务关注点不再写死在代码里，统一在 `config/goals.yaml` 声明：
  - `match`（意图 / 关键词 / 规则 id / 字段名，可只填部分）
  - `priority`（high/medium/low）`focus`（查询策略）`output`（解释深度）
  - `action`（业务专属钩子，对应 `src/bi/goal_actions.py` 里的函数）
- 路由器入口：`src/bi/agent.py::select_goal(question, intent, goals)`。
- 命中目标时：`max_steps` 取 `min(self.max_steps, goal.output.max_steps)`，防止目标过度展开。
- **重要诚实选项**：当原数据缺字段时，`action: missing_data` 让 Agent 显式列出需补的字段/表，**不允许编造数字**。
- 新增业务关注点：① 写 yaml ② 若需专属逻辑就加 `goal_actions.py` 函数并注册到 `ACTIONS` ③ 在 `tests/test_goals.py` 加断言。

## 当前已落地的目标
- 潜在流失率（应报未报）—— 走 `missing_data`：原表无「应报课程清单」字段，**目前无法可靠回答**。
- 证书快到期（应复查）—— 走 `screen_due_cert`：原表无「取证日期」字段，用「开班日期+3年」做粗筛代理，**当前数据命中 0 人**（开班都在近一年内），逻辑就位等历史数据生效。
- 通用概览（兜底）—— `match: {}` 全命中，行为与升级前完全一致。
