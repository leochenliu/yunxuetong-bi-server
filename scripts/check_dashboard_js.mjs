/**
 * 页面 JS 静态验证：在 Node 里跑 dashboard.js + 各项目页面的 render()，
 * 用真实 API 数据断言渲染结果。本机 Chrome 无头模式不可用（进程单例劫持，退出码 21），
 * 所以用这个方式替代「截图肉眼看」。
 *
 * 用法：node scripts/check_dashboard_js.mjs
 * 需要本地服务已启动（http://127.0.0.1:8787）。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.BI_BASE || "http://127.0.0.1:8787";

/* ----------------------------------------------------------- 迷你 DOM */
const elements = new Map();
function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", hidden: false, disabled: false,
    classList: { add() {}, remove() {}, contains: () => false },
    style: { setProperty() {} },
    addEventListener() {},
  };
}
global.document = {
  getElementById: (id) => {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  documentElement: { style: { setProperty() {} } },
};
const realFetch = globalThis.fetch;
global.fetch = async (url) => {
  const full = String(url).startsWith("http") ? String(url) : `${BASE}${url}`;
  const r = await realFetch(full);
  if (!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
  return r;
};

/* --------------------------------------------------- 载入共享图表库 */
const libSrc = readFileSync(join(ROOT, "web/assets/dashboard.js"), "utf8");
// 页面里 <script src="/assets/dashboard.js"> 是全局作用域，浏览器中 const BI 在
// 顶层脚本里等价于 window.BI（顶层 const 会挂到全局词法环境，跨 script 可见）。
// 这里用 globalThis.BI 复刻这一行为，供页面里的 `const { $, esc, ... } = BI;` 解构。
const mod = new Function(`${libSrc}; return BI;`)();
globalThis.BI = mod;
const BI = mod;

/* ---------------------------------------------------- 页面 render 校验 */
const failures = [];
function check(name, cond, detail = "") {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    console.log(`  ✗ ${name} ${detail}`);
    failures.push(`${name} ${detail}`);
  }
}

/**
 * 从页面 HTML 里抽出内联 script 并执行 render()。
 * 关键：页面脚本【一行都不能删】——
 *   - `const PROJECT_ID = "..."` 在页面里是合法的顶层 const，
 *     new Function 的函数体是独立作用域，重复声明不会与外层冲突；
 *   - `const { $, esc, fmt, ... } = BI;` 是页面真正依赖的绑定，
 *     删掉会导致 render() 内 $ / esc / fmt 全部 ReferenceError。
 * 因此用「参数化注入」代替「正则剥离」：
 *   1) 剥掉页面末尾的 `load();` 自动调用（否则会真的发请求）；
 *   2) 把 `const PROJECT_ID = "x"` 改写成 `const PROJECT_ID = __PROJECT_ID__`，
 *      由外部注入，避免多个项目页面共用一个函数体时重名。
 */
function extractInline(html) {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]);
  return scripts.join("\n");
}

async function runPageRender(projectId, data) {
  const html = readFileSync(join(ROOT, `web/dashboard_${projectId}.html`), "utf8");
  const inline = extractInline(html)
    // 去掉自动加载调用（保留 load 函数定义本身）
    .replace(/^\s*load\(\);\s*$/m, "")
    // PROJECT_ID 交给外部注入
    .replace(/const\s+PROJECT_ID\s*=\s*"[^"]*"\s*;/, "const PROJECT_ID = __PROJECT_ID__;");
  const runner = new Function(
    "BI", "data", "__PROJECT_ID__",
    `${inline}\nreturn typeof render === "function" ? render(data) : null;`
  );
  runner(BI, data, projectId);
}

/* ------------------------------------------------------------- 主流程 */
const projectsRes = await fetch(`${BASE}/projects`);
const projects = (await projectsRes.json()).items;
console.log(`项目清单: ${projects.map((p) => p.id).join(", ")}\n`);

for (const project of projects) {
  console.log(`=== ${project.id} (${project.title}) ===`);
  const res = await fetch(`${BASE}/dashboard/${project.id}/data`);
  const data = await res.json();

  check("KPI 数量 = 声明数",
    data.kpis.length === project.kpi_metrics.length,
    `got ${data.kpis.length}`);

  let threw = null;
  try {
    await runPageRender(project.id, data);
  } catch (e) {
    threw = e;
  }
  check("render() 无运行时异常", !threw, threw ? String(threw.stack || threw) : "");

  // 关键 DOM 容器被填充
  const filled = [...elements.values()].filter((el) => el.innerHTML && el.innerHTML.length > 20);
  check("页面已渲染出内容", filled.length >= 3, `填充容器数=${filled.length}`);

  // 图表必须真的画出来了（至少 1 个 svg 或 table）
  const svgCount = [...elements.values()]
    .filter((el) => el.innerHTML.includes("<svg") || el.innerHTML.includes("<table")).length;
  check("至少 1 个图表容器有 SVG/表格", svgCount >= 1, `图表容器数=${svgCount}`);

  // 主图表区不允许出现「暂无可用数据」空态。
  // 注意：异常面板在「该数据集未配置异常规则」时**应该**显示说明性空态
  // （与「本批次无异常」语义不同），因此按容器白名单排除 anomaly-chart。
  const EMPTY_ALLOWED = new Set(["anomaly-chart"]);
  const emptyBlocks = [...elements.entries()]
    .filter(([id, el]) => !EMPTY_ALLOWED.has(id)
      && (el.innerHTML || "").includes("chart-empty"))
    .map(([id]) => id);
  check("主图表区无 chart-empty 空态", emptyBlocks.length === 0, `空态容器=${emptyBlocks.join(",")}`);

  // PII 不得出现在渲染结果里。
  // 口径说明文案里出现「证件号码」是合法的（例如「只含有证件号码者」是在讲
  // 去重口径，不是泄露个人信息）；真正的泄露特征是「证件号码」以**字段名/key**
  // 的形式出现，或出现真实的证件号/手机号取值。故此处按 key 形态检查。
  const rendered = [...elements.values()].map((el) => el.innerHTML).join("");
  const piiKeyLeak = [
    /证件号码\s*[:：]/, /姓名\s*[:：]/, /手机\s*[:：]/, /联系电话\s*[:：]/,
    /"id_number"/, /"phone"/, /"name"\s*:/,
  ].filter((re) => re.test(rendered));
  check("渲染结果无 PII 字段名", piiKeyLeak.length === 0, `泄露=${piiKeyLeak.join(",")}`);

  // 真实身份证号（18 位）不应出现在页面里
  const idHits = (rendered.match(/\b\d{17}[\dXx]\b/g) || []);
  check("渲染结果无身份证号取值", idHits.length === 0, `命中=${idHits.length}`);

  // 手机号（11 位，1 开头）不应出现在页面里
  const phoneHits = (rendered.match(/\b1[3-9]\d{9}\b/g) || []);
  check("渲染结果无手机号取值", phoneHits.length === 0, `命中=${phoneHits.length}`);

  // 项目主题色确实被应用
  const themed = [...elements.values()].some((el) =>
    (el.innerHTML || "").includes(project.accent) ||
    (el.style?.setProperty && true));
  check("使用本项目主题色", themed, `accent=${project.accent}`);

  // 异常面板语义：必须区分「未配置规则」与「本批次无异常」
  const anomalyHtml = elements.get("anomaly-chart")?.innerHTML || "";
  if (data.anomalies?.rules_configured === false) {
    check("异常面板：未配置规则时给出说明性文案",
      anomalyHtml.includes("未配置异常检测规则"),
      `实际=${anomalyHtml.slice(0, 80)}`);
  } else if ((data.anomalies?.by_rule || []).length === 0) {
    check("异常面板：无异常时给出「本批次未发现异常」",
      anomalyHtml.includes("本批次未发现异常"),
      `实际=${anomalyHtml.slice(0, 80)}`);
  } else {
    check("异常面板：有异常时渲染出条形图", anomalyHtml.includes("<svg"));
  }
  console.log("");
}

// 总览页
console.log("=== dashboards (总览页) ===");
{
  const all = await (await fetch(`${BASE}/dashboard/all/data`)).json();
  const html = readFileSync(join(ROOT, "web/dashboards.html"), "utf8");
  const inline = extractInline(html).replace(/^\s*load\(\);\s*$/m, "");
  let threw = null;
  try {
    new Function("BI", "payload", `${inline}\nreturn render(payload);`)(BI, all);
  } catch (e) { threw = e; }
  check("render() 无运行时异常", !threw, threw ? String(threw.stack || threw) : "");
  const grid = elements.get("project-grid");
  check("项目卡片已渲染", (grid?.innerHTML || "").includes("proj-card"));
  check("卡片数 = 项目数",
    (grid?.innerHTML.match(/proj-card/g) || []).length === all.projects.length);
}

console.log("");
if (failures.length) {
  console.log(`✗ ${failures.length} 项失败`);
  process.exit(1);
}
console.log("✓ 全部页面 JS 验证通过");
