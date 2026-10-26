"""端到端验收：一次跑完五个阶段并输出人类可读报告。

    set PYTHONPATH=src && python scripts/acceptance.py

输出同时写入 docs/ACCEPTANCE.md。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from bi import mcp_server as M  # noqa: E402
from bi.agent import BIAgent  # noqa: E402
from bi.api import app  # noqa: E402
from bi.config import load_dataset_config  # noqa: E402
from bi.diff_engine import diff_dataset_against_snapshot  # noqa: E402
from bi.ingest import ingest  # noqa: E402
from bi.seed import seed_all  # noqa: E402
from bi.settings import get_settings  # noqa: E402
from bi.store import FactStore  # noqa: E402

F, T = "20261012", "20261013"
LINES: list[str] = []


def out(s: str = "") -> None:
    print(s)
    LINES.append(s)


def hr(title: str) -> None:
    out()
    out("=" * 78)
    out(title)
    out("=" * 78)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    settings = get_settings()
    cfg = load_dataset_config(settings.dataset_config_path)
    t_all = time.perf_counter()

    # ================= 阶段一 =================
    hr("阶段一 · Excel 差分原型")
    seed_result = seed_all(F, T)
    d1 = ingest(F, settings=settings, cfg=cfg)
    d2 = ingest(T, settings=settings, cfg=cfg)
    result = diff_dataset_against_snapshot(d1.snapshot, d2.dataset, cfg, F, T)
    gt_path = settings.project_root / "data" / "store" / f"ground_truth_{F}_{T}.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))

    out(f"输入：{Path(d1.source_file).name}（{d1.batch}）  vs  {Path(d2.source_file).name}（{d2.batch}）")
    out(f"主键：{'|'.join(cfg.key_columns)}    追踪字段：{'、'.join(cfg.tracked_fields)}")
    out(f"口径：{d1.notes[0] if d1.notes else '-'}")
    out()
    out(f"  上期 {result.count_from} 人 → 本期 {result.count_to} 人")
    out(f"  新增 {len(result.added)}｜移除 {len(result.removed)}｜"
        f"变更 {len(result.changed)} 人（{result.event_count} 个字段级事件）｜未变 {result.unchanged}")
    out("  字段维度：" + "、".join(f"{f['field']} {f['count']}" for f in result.field_summary))
    ok = (sorted(result.added) == sorted(gt["added"])
          and sorted(result.removed) == sorted(gt["removed"])
          and result.event_count == gt["changed_event_count"]
          and result.unchanged == gt["unchanged"])
    out()
    out(f"  ▶ 与 ground truth 逐条对账：{'完全一致 ✓' if ok else '不一致 ✗'}")
    out(f"  ▶ 数据质量异常：本期 {len(d2.anomalies)} 条"
        + (f"（{'、'.join(f'{k}×{v}' for k, v in d2.anomaly_summary()['by_rule'].items())}）"
           if d2.anomalies else ""))

    # ================= 阶段二 =================
    hr("阶段二 · 事实存储")
    store = FactStore(settings)
    versions = store.list_versions()
    for v in versions:
        out(f"  version#{v['id']}  {v['batch']}  记录 {v['record_count']}  "
            f"剔除 {v['dropped_rows']}  事件 {v['event_count']}  "
            f"sha256 {v['sha256'][:16]}…")
    with store.connect() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("upload_versions", "raw_records", "diff_events",
                            "metric_snapshots", "anomalies", "audit_log")}
    out()
    out("  表行数：" + "、".join(f"{k} {v}" for k, v in counts.items()))
    location = store.backend if store.backend == "postgresql" else str(store.db_path)
    out(f"  数据库：{location}")
    out("  隐私：事实层存明文证件号（精确去重/跨源关联用），输出层一律掩码（前 6 后 4）")

    # ================= 阶段三 =================
    hr("阶段三 · 固定查询 API")
    client = TestClient(app)
    out(f"  GET /health                -> {client.get('/health').json()['status']}")
    mc = client.get("/metrics/compare", params={"from": F, "to": T}).json()
    out("  GET /metrics/compare       ->")
    for i in mc["items"]:
        a = i.get("attribution") or {}
        res = a.get("residual")
        tag = "已完全解释" if res == 0 else (f"残差 {res}" if res is not None else "均值型")
        out(f"      {i['metric_name']:<10} {i['value_from']:>8} → {i['value_to']:<8}"
            f" Δ{i['delta']:>+7}  [{tag}]")
    ds = client.get("/diffs/summary", params={"from": F, "to": T}).json()
    out(f"  GET /diffs/summary         -> {ds['total_events']} 条事件 {ds['by_type']}")
    dr = client.get("/diffs/records", params={"from": F, "to": T, "field_name": "分数", "limit": 3}).json()
    out(f"  GET /diffs/records         -> 分数变更共 {dr['total']} 条，示例：")
    for r in dr["items"][:3]:
        out(f"      {r['name']}：{r['value_from_raw']} → {r['value_to_raw']}  [{r['severity']}]")
    ev = client.get(f"/evidence/{dr['items'][0]['id']}").json()
    before, after = ev["chain"]
    out(f"  GET /evidence/{dr['items'][0]['id']}          -> 证据链：")
    out(f"      {before['batch']} {before.get('path') or before['file']} 第 {before['source_row']} 行"
        f"（{before['id_number_masked']}）")
    out(f"      {after['batch']} {after.get('path') or after['file']} 第 {after['source_row']} 行")
    out(f"      文件 sha256：{before['file_sha256'][:32]}…")
    lim = client.get("/diffs/records", params={"from": F, "to": T, "limit": 9999})
    out(f"  GET /diffs/records?limit=9999 -> HTTP {lim.status_code}（超过 API 上限被拒绝）")

    # ================= 阶段四 =================
    hr("阶段四 · MCP 工具")
    M.assert_readonly()
    out("  只读自检：通过（无任何写工具）")
    out("  已注册工具：" + "、".join(fn.__name__ for fn in M.TOOLS))
    M.set_query_client(M.BIQueryClient(sdk_client=TestClient(app)))
    capped = M.bi_diff_records(F, T, limit=100000)
    out(f"  限额：请求 limit=100000 → 实际 {capped['limit']}（server_max_limit="
        f"{capped['server_max_limit']}，truncated={capped['truncated']}）")
    bad = M.bi_diff_records(F, T, event_type="deleted")
    out(f"  参数白名单：非法 event_type='deleted' → {'已拒绝 ✓' if 'error' in bad else '未拦截 ✗'}")
    logs = FactStore(settings).recent_audit(200)
    mcp_logs = [x for x in logs if x["actor"] == "mcp"]
    out(f"  审计日志：本次共记录 {len(mcp_logs)} 条 MCP 调用"
        f"（工具 {'、'.join(sorted({x['tool'] for x in mcp_logs})[:4])}…）")

    # ================= 阶段五 =================
    hr("阶段五 · 自然语言 Agent 闭环")
    question = "为什么报名人数涨了？本期有哪些异常，证据在哪？"
    out(f"  提问：{question}")
    t0 = time.perf_counter()
    ans = BIAgent().ask(question)
    out(f"  多轮执行：{ans['step_count']} 步，耗时 {int((time.perf_counter()-t0)*1000)} ms"
        f"（intent={ans['intent']}，LLM={'是' if ans['used_llm'] else '否（本地模板）'}）")
    out()
    out("  执行轨迹：")
    for s in ans["steps"]:
        out(f"    [{s['n']:>2}] {s['tool']:<22} {s['summary']}")
    out()
    out("  最终解释：")
    for line in ans["answer"].splitlines():
        out("    " + line)

    hr("汇总")
    out(f"  五阶段全部跑通，用时 {time.perf_counter() - t_all:.1f}s")
    out("  单元测试：请单独运行 pytest -q（本脚本不运行单测）")
    out(f"  可核查：每个结论都能经 API -> MCP -> 证据链 回到原始 Excel 的具体行")
    out(f"  对话页：启动 `python -m bi.cli api` 后访问 http://{settings.api_host}:{settings.api_port}/")

    report = ROOT / "docs" / "ACCEPTANCE.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# 端到端验收报告\n\n> 由 `python scripts/acceptance.py` 自动生成，"
        f"数据批次 {F} → {T}。\n\n```text\n" + "\n".join(LINES) + "\n```\n",
        encoding="utf-8",
    )
    print(f"\n报告已写入: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
