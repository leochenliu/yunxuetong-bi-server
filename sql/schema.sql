-- =============================================================================
-- 事实库 schema（SQLite）—— 本地数据维护的唯一权威 DDL
-- =============================================================================
-- 用途
--   1) 本地事实库（默认 data/store/bi.sqlite3）的建表脚本；
--   2) src/bi/store.py 的 SCHEMA 常量从这里加载 —— **本文件是建表结构的单一事实源**，
--      改表结构只改这里，不要再回头改 Python 里的 DDL（否则两边会漂移）；
--   3) 人工维护数据时的结构参照（DB Browser for SQLite / sqlite3 CLI 均可直接执行）。
--
-- 怎么用
--   建一个空库（幂等，已存在则不改动）：
--     sqlite3 data/store/bi.sqlite3 < sql/schema.sql
--   灌数据（走业务代码，保留口径与派生规则）：
--     python -m bi.cli seed
--   备份后再动手：
--     sqlite3 data/store/bi.sqlite3 ".backup data/store/bi.bak.sqlite3"
--
-- 约束
--   * 本脚本必须与 src/bi/store.py 写库的字段完全一致，tests/test_schema.py 会守住这条；
--   * 证件号码在**事实层落明文**（raw_records.id_number / diff_events.id_number），
--     理由：掩码会把不同证件号折叠成同一串
--     （实测 8634 个不同证件号掩码后只剩 7644 个），无法精确统计「持证人数」「一人多证」；
--     但**输出层一律掩码**（API / 看板 / MCP / 证据链只出掩码值），见 README「隐私说明」；
--   * 主键是复合键「证件号码|姓名|…业务键」，不是裸证件号，raw_records.record_key 已按此口径生成；
--   * certificates / students / companies 是**本地实体表**（不是事实表）：
--     由 scripts/build_entity_tables.py 从 raw_records 投影，ingest/差分都不碰它们，
--     每次构建先 DELETE 再 INSERT，删了也能重建；
--   * 全库只有两层：raw_records（事实快照）+ 三张实体表；另有 7 张事实表。
--     **不镜像外部系统的表，也不建视图**（视图没有代码使用，只是多一层要同步的对象）。
--
-- 版本：user_version = 8（结构有变更时递增，便于人工核对库是否需要重建）
-- =============================================================================

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA user_version=8;

-- -----------------------------------------------------------------------------
-- 表
-- -----------------------------------------------------------------------------

-- 上传版本：一次上传 = 一个版本
CREATE TABLE IF NOT EXISTS upload_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id       TEXT    NOT NULL,
    batch            TEXT    NOT NULL,
    source_file      TEXT    NOT NULL,
    filename         TEXT    NOT NULL,
    sheet            TEXT,
    sha256           TEXT    NOT NULL,
    size_bytes       INTEGER,
    file_modified_at TEXT,
    captured_at      TEXT    NOT NULL,
    row_count        INTEGER NOT NULL,
    record_count     INTEGER NOT NULL,
    dropped_rows     INTEGER NOT NULL DEFAULT 0,
    columns_json     TEXT    NOT NULL,
    notes_json       TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',
    UNIQUE (dataset_id, batch)
);

-- 原始记录：每期每人一行
-- id_number 是**事实层明文**（见文件头「约束」），展示请用 payload_json 里的掩码值
CREATE TABLE IF NOT EXISTS raw_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    record_key      TEXT    NOT NULL,
    id_number       TEXT    NOT NULL,
    name            TEXT,
    source_row      INTEGER,
    payload_json    TEXT    NOT NULL,
    UNIQUE (version_id, record_key)
);
-- version_id 已有 UNIQUE(version_id, record_key) 的自动索引覆盖前缀查询，不再单建
CREATE INDEX IF NOT EXISTS idx_raw_records_id_number ON raw_records(id_number);

-- 差分事件：字段级
CREATE TABLE IF NOT EXISTS diff_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id       TEXT    NOT NULL,
    from_version_id  INTEGER REFERENCES upload_versions(id) ON DELETE CASCADE,
    to_version_id    INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    batch_from       TEXT    NOT NULL,
    batch_to         TEXT    NOT NULL,
    event_type       TEXT    NOT NULL,
    record_key       TEXT    NOT NULL,
    id_number        TEXT,
    name             TEXT,
    field_name       TEXT,
    field_std_name   TEXT,
    value_from       TEXT,
    value_to         TEXT,
    value_from_raw   TEXT,
    value_to_raw     TEXT,
    severity         TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_events_to ON diff_events(to_version_id);
CREATE INDEX IF NOT EXISTS idx_diff_events_type ON diff_events(event_type);
CREATE INDEX IF NOT EXISTS idx_diff_events_key ON diff_events(record_key);

-- 指标快照
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    batch        TEXT    NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_name  TEXT,
    value        REAL,
    unit         TEXT,
    decimals     INTEGER,
    descr        TEXT,
    computed_at  TEXT    NOT NULL,
    UNIQUE (version_id, metric_key)
);

-- 异常
CREATE TABLE IF NOT EXISTS anomalies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER REFERENCES upload_versions(id) ON DELETE CASCADE,
    scope        TEXT    NOT NULL DEFAULT 'row',
    rule_id      TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    record_key   TEXT,
    field_name   TEXT,
    value        TEXT,
    message      TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_version ON anomalies(version_id);

-- 分桶指标：需要「按维度切分」的指标（metric_snapshots 只能存单标量）
-- 例：按月 × 报名门数的人数分布 —— 「2026年3月 报了2门课的人数 = 12」
-- 维度值（dimension / bucket）都是业务语义标签，不含任何个人标识。
CREATE TABLE IF NOT EXISTS metric_buckets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES upload_versions(id) ON DELETE CASCADE,
    batch        TEXT    NOT NULL,
    dataset_id   TEXT    NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_name  TEXT,
    dimension    TEXT    NOT NULL,      -- 维度名，如 "月份"
    bucket       TEXT    NOT NULL,      -- 维度取值，如 "3月"
    group_key    TEXT    NOT NULL,      -- 组内分档，如 "2门"
    value        REAL    NOT NULL,
    unit         TEXT,
    descr        TEXT,
    computed_at  TEXT    NOT NULL,
    UNIQUE (version_id, metric_key, dimension, bucket, group_key)
);
CREATE INDEX IF NOT EXISTS idx_metric_buckets_lookup
    ON metric_buckets(dataset_id, metric_key, batch);

-- 审计日志（阶段四）
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    actor        TEXT,
    tool         TEXT    NOT NULL,
    params_json  TEXT,
    result_rows  INTEGER,
    duration_ms  INTEGER,
    status       TEXT    NOT NULL,
    message      TEXT
);
-- 审计只按 id 倒序读取（recent_audit），没有按 ts 的查询，故不建 ts 索引

-- 定时任务执行记录：每次跑完（成功 / 部分成功 / 失败）写一行
--
-- 怎么「一眼看出某天没执行」：
--   漏跑**不写 miss 行**，而是「这一天没有这一行」。判定只有一个来源（有 / 没有），
--   不需要跟补写的 missed 记录对账，也就不会出现两套互相打架的真相。
--   最近 N 天日历：src/bi/jobs.py::job_health() → CLI `job-status` / GET /jobs/status。
--
-- 几个刻意的设计：
--   * UNIQUE (job_id, run_date)：一个任务一天只留一行，重跑走 UPSERT **覆盖**为最后
--     一次运行，所以跑 1 次和跑 10 次的库内容一致（幂等）；因此刻意不存 attempts；
--   * run_date 是**业务日期**（按任务声明的时区算），不是 started_at 的 UTC 日期 ——
--     任务 00:05 跑，按 UTC 记会落到前一天，明明跑了却显示没跑；
--   * status 只有三种且不含 'missed'：failed 表示「跑了但没成功」，与「没跑」必须能区分；
--     被锁跳过的那次没有真正执行，不占一行（写 audit_log）；
--   * content_hash 是业务内容指纹：同一 as_of、同一输入版本，跑多少次都必须相同 ——
--     幂等性由它自证，不靠人眼比对；
--   * 与 audit_log 的分工：audit_log 记「谁查了什么」（量大、无期望频率），
--     本表记「该跑的任务跑了没有」（一天一行、有期望），两者不合并。
CREATE TABLE IF NOT EXISTS job_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT    NOT NULL,
    run_date     TEXT    NOT NULL,                     -- YYYY-MM-DD（业务日期）
    status       TEXT    NOT NULL
        CHECK (status IN ('success','partial','failed')),
    trigger      TEXT    NOT NULL DEFAULT 'schedule',  -- schedule / startup / manual
    scheduled_at TEXT    NOT NULL,                     -- 计划时刻（同一天固定，可复现）
    started_at   TEXT    NOT NULL,                     -- 实际开始（运行元数据）
    finished_at  TEXT,
    duration_ms  INTEGER,
    result_rows  INTEGER,
    content_hash TEXT,                                 -- 业务内容指纹（幂等自证）
    detail_json  TEXT,
    UNIQUE (job_id, run_date)
);
CREATE INDEX IF NOT EXISTS ix_job_runs_date ON job_runs (run_date);

-- -----------------------------------------------------------------------------
-- 本地实体表（由 scripts/build_entity_tables.py 从 raw_records 投影，可随时重建）
-- -----------------------------------------------------------------------------
-- 两层关系（别把这两层混在一起）：
--   ① raw_records           事实快照层：版本化 + payload_json，差分与证据链依赖它，**不可改**；
--   ② certificates/students/companies   本地实体表：台账的**当前状态**，有类型/索引，直接 SELECT。
-- 本组表属于 ②，全部从 ① 重算：每次构建先 DELETE 再 INSERT，删了也能重建，**不是事实数据**。
-- ingest / seed / 差分 / 指标都不碰它们。
--
-- 口径陷阱（务必先读）：
--   * companies.person_count 相加 **不等于** 全量持证人数 —— 有人跨多家公司
--     （实测 223 个证件号跨公司，相加 8868 vs 实际 8634）；
--   * students.cert_count 相加 **等于** 证书总数（一张证只属于一个人），可以加；
--   * 时点口径（「还有几天到期」）以**快照列**形式落库：`expiry_days` / `min_expiry_days`
--     必须与 `days_as_of` **成对**出现，由 scripts/build_entity_tables.py 每次重建时整体刷新；
--     查询端必须校验 `days_as_of` 是否等于今天，不等就提示「快照非当日」。
--     这两列**绝不能**纳入差分的 `tracked_fields` —— 它们每天都变，一旦进差分，
--     第二天就会凭空多出 9000+ 条假变化事件，把归因残差打爆。

-- 证书明细：一行一张证（主键是证书编号，实测 9085 个无重复）
CREATE TABLE IF NOT EXISTS certificates (
    cert_number       TEXT    PRIMARY KEY,
    person_name       TEXT,
    id_number         TEXT,                          -- 事实层明文（与 raw_records 同口径）
    company_name      TEXT,
    start_date        TEXT,                          -- 开证日 YYYY-MM-DD
    expiry_date       TEXT,                          -- 到期日 YYYY-MM-DD（可空：256 张解析不出）
    expiry_precision  TEXT,                          -- 日 / 月（月=按月末取值的估计值）
    validity_shape    TEXT,                          -- 单日期 / 区间 / 年月 / 无法解析
    validity_raw      TEXT,                          -- 原始写法，如 2026/09/18-2028/09/30
    expiry_source     TEXT,                          -- 证面原文 / 按2年效期推算
    version_id        INTEGER REFERENCES upload_versions(id) ON DELETE CASCADE,
    source_row        INTEGER,                       -- Excel 物理行号（证据链）
    expiry_days       INTEGER,                       -- 快照：距到期天数（负数=已过期，NULL=算不出）
    days_as_of        TEXT,                          -- 这列数字算的是哪一天（YYYY-MM-DD）
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_certificates_days
    ON certificates (expiry_days);
CREATE INDEX IF NOT EXISTS ix_certificates_id_number
    ON certificates (id_number);
CREATE INDEX IF NOT EXISTS ix_certificates_company
    ON certificates (company_name);
CREATE INDEX IF NOT EXISTS ix_certificates_expiry
    ON certificates (expiry_date);

-- 人员：一行一个人（主键是证件号；跨公司的人只算一行，company_names 里列出全部）
CREATE TABLE IF NOT EXISTS students (
    id_number        TEXT    PRIMARY KEY,
    person_name      TEXT,
    cert_count       INTEGER NOT NULL,
    company_count    INTEGER NOT NULL,
    company_names    TEXT,                           -- 逗号分隔（展示用；只有 223 人跨公司）
    first_start_date TEXT,
    last_expiry_date TEXT,
    min_expiry_days  INTEGER,                        -- 快照：最早到期那张证还有几天
    days_as_of       TEXT,                           -- 这一列算的是哪一天
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_students_expiry
    ON students (last_expiry_date);

-- 公司：一行一家公司
CREATE TABLE IF NOT EXISTS companies (
    company_name                  TEXT    PRIMARY KEY,
    cert_count                    INTEGER NOT NULL,
    person_count                  INTEGER NOT NULL,
    cross_company_person_count    INTEGER NOT NULL DEFAULT 0,
    start_date_min                TEXT,
    start_date_max                TEXT,
    expiry_date_min               TEXT,              -- 最早到期日（续证提醒看这个）
    expiry_date_max               TEXT,              -- 最晚到期日（整体覆盖何时结束）
    expiry_unresolved_count       INTEGER NOT NULL DEFAULT 0,
    expiry_month_precision_count  INTEGER NOT NULL DEFAULT 0,
    expiry_from_cert_count        INTEGER NOT NULL DEFAULT 0,
    expiry_estimated_count        INTEGER NOT NULL DEFAULT 0,
    min_expiry_days               INTEGER,           -- 快照：最早到期那张证还有几天（续证提醒看这个）
    days_as_of                    TEXT,              -- 这一列算的是哪一天
    updated_at                    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_companies_cert_count
    ON companies (cert_count DESC);
CREATE INDEX IF NOT EXISTS ix_companies_expiry
    ON companies (expiry_date_min);

-- 本项目**不镜像外部系统的表**（原 trainingos_companies / _students / _certificates 已全部移除）：
-- 台账与发证系统的对账改为「按证书编号 / 证件号在离线报表里比对」，
-- 不再往事实库里落第二份来源不明的快照（口径冲突、PII 面积翻倍、且快照必然过时）。

-- -----------------------------------------------------------------------------
-- 不建视图
-- -----------------------------------------------------------------------------
-- 曾经的 4 个辅助视图（v_version_overview / v_diff_event_detail / v_batch_metrics /
-- v_company_expiry_overview）已删除：代码里没有一处查询它们，只有文档与人工排查在用，
-- 而视图会让 schema 多一层需要跟着表结构同步的对象。
--   * 版本总览 / 指标快照 / 差分明细 → 直接查 upload_versions / metric_snapshots /
--     diff_events，或走 API（/versions · /metrics/compare · /diffs/records）；
--   * 证据链（两期文件 + Excel 物理行号）→ GET /evidence/{event_id}（含掩码证件号）；
--   * 「还有几天到期」是**时点口径**，由看板按当天现算，不入库、也不建视图。

-- =============================================================================
-- 常用维护 SQL（按需手动执行，默认注释掉）
-- =============================================================================
-- 完整性检查：
--   PRAGMA integrity_check;
--
-- 压缩（删掉空闲页，会减小文件体积）：
--   VACUUM;
--
-- 某个批次重算前清干净（注意：会同时删掉以它为上期/下期的差分事件）：
--   DELETE FROM diff_events  WHERE from_version_id = (SELECT id FROM upload_versions WHERE dataset_id='denggao' AND batch='20261013')
--                              OR to_version_id   = (SELECT id FROM upload_versions WHERE dataset_id='denggao' AND batch='20261013');
--   DELETE FROM upload_versions WHERE dataset_id='denggao' AND batch='20261013';
--   -- 然后重新入库：python -m bi.cli ingest 20261013
--
-- 只看某人的差分（复合键 = 证件号码|姓名|…业务键；证件号是明文，粘贴时注意别外传）：
--   SELECT e.*, vf.filename AS file_from, vr.filename AS file_to,
--          rf.source_row AS row_from, rr.source_row AS row_to
--     FROM diff_events e
--     LEFT JOIN upload_versions vf ON vf.id = e.from_version_id
--     LEFT JOIN upload_versions vr ON vr.id = e.to_version_id
--     LEFT JOIN raw_records rf ON rf.version_id = e.from_version_id AND rf.record_key = e.record_key
--     LEFT JOIN raw_records rr ON rr.version_id = e.to_version_id   AND rr.record_key = e.record_key
--    WHERE e.record_key LIKE '320826197306181638|张三%';
--
-- 审计流水：
--   SELECT ts, actor, tool, result_rows, duration_ms, status FROM audit_log ORDER BY id DESC LIMIT 50;
