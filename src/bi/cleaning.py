"""字段清洗：配置驱动的、可追溯的清洗层。

设计原则（与项目既有约定一致）
------------------------------
1. **只做可验证的清洗**：全角转半角、去首尾空白、去噪字符、末位小写 x→X。
   这些改动有唯一正确答案，不引入任何猜测。
2. **不猜、不补、不替**：
   - 不补身份证缺失位（少一位可能是出生年月的任意一位，补出来就是编造）；
   - 不改身份证校验位（校验位错说明源表录错，改了等于伪造）；
   - 不给非法日期（4 月 31 日）找替代值（4 月 30 日是推测，不是事实）。
   这三类只**标注**，由异常规则报出来给人工核对。
3. **每次改动都留痕**：命中数量、原始值、清洗后值写进版本元数据（`Dataset.notes`）
   与异常记录，不允许静默改数。
4. **业务规则写在 `config/datasets/*.yaml`**，引擎只实现通用动作。

实测（20261026 批次，9085 行）需要清洗的只有身份证列的 2 条尾部噪字符
（`310229199403034414‘`、`310229198510301632.`），
以及 13 条位数异常、60 条校验位错误——后两类按原则 2 **只标注不修改**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 全角 → 半角（数字、字母、常见标点与空格）
_FULL_TO_HALF = {
    **{chr(code): chr(code - 0xFEE0) for code in range(0xFF10, 0xFF1A)},  # ０-９
    **{chr(code): chr(code - 0xFEE0) for code in range(0xFF21, 0xFF3B)},  # Ａ-Ｚ
    **{chr(code): chr(code - 0xFEE0) for code in range(0xFF41, 0xFF5B)},  # ａ-ｚ
    "　": " ",
    "：": ":",
    "（": "(",
    "）": ")",
    "，": ",",
    "、": ",",
    "。": ".",
}


@dataclass
class CleaningRule:
    """一列的清洗规则。全部动作都是幂等的。"""

    column: str
    trim: bool = True                      # 去首尾空白（含全角空格）
    full_to_half: bool = False             # 全角字符转半角
    remove_chars: str = ""                 # 直接删除的噪字符（全局删除）
    collapse_whitespace: bool = False      # 内部连续空白压成一个空格
    uppercase: bool = False                # 整列转大写
    uppercase_x: bool = False              # 末位小写 x → X（身份证末位校验位）


@dataclass
class CleaningReport:
    """一列的清洗留痕。"""

    column: str
    total: int = 0
    changed: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)
    max_samples: int = 5

    def record(self, before: str, after: str) -> None:
        self.changed += 1
        if len(self.samples) < self.max_samples:
            self.samples.append((before, after))

    def as_note(self) -> str:
        if not self.changed:
            return ""
        samples = "；".join(f"{before!r}→{after!r}" for before, after in self.samples)
        more = "…" if self.changed > len(self.samples) else ""
        return (
            f"清洗列「{self.column}」：{self.changed}/{self.total} 行被修改"
            f"（样例 {samples}{more}）"
        )


def clean_value(value: Any, rule: CleaningRule) -> tuple[Any, bool]:
    """按规则清洗单个值，返回 (新值, 是否发生变化)。非字符串原样返回。"""
    if not isinstance(value, str):
        return value, False
    original = value
    text = value
    if rule.full_to_half:
        text = text.translate(_FULL_TO_HALF)
    if rule.trim:
        text = text.strip()
    if rule.collapse_whitespace:
        text = " ".join(text.split())
    if rule.remove_chars:
        noise = set(rule.remove_chars)
        text = "".join(char for char in text if char not in noise)
    if rule.uppercase:
        text = text.upper()
    if rule.uppercase_x and len(text) == 18 and text[-1] == "x":
        text = text[:-1] + "X"
    if rule.trim:
        text = text.strip()
    return (text, text != original)


def clean_rows(
    rows: list[dict[str, Any]],
    rules: list[CleaningRule],
) -> list[CleaningReport]:
    """就地清洗 rows（只动规则声明的列），返回每列的清洗留痕。"""
    reports: list[CleaningReport] = []
    for rule in rules:
        report = CleaningReport(column=rule.column)
        for row in rows:
            if rule.column not in row:
                continue
            report.total += 1
            before = row.get(rule.column)
            new_value, changed = clean_value(before, rule)
            if changed:
                row[rule.column] = new_value
                report.record(str(before), str(new_value))
        reports.append(report)
    return reports
