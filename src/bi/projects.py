"""看板项目注册表：把「一个培训项目 = 一个 dataset_id」映射成一张独立页面。

设计要点
--------
1. **项目清单在配置里**（`config/dashboards.yaml`），不在代码里硬编码。
   新增项目 = 加一条 yaml + 写一个页面文件，引擎不用改。
2. **每个项目用自己的 dataset_config / metrics_config**。
   登高有 5 个核心指标，继续教育人员名单有 5 个（缴费/单位/职称），
   课程月份汇总是人次口径 —— 指标口径不同，KPI 卡片自然不同。
3. **页面文件按项目命名**（`web/dashboard_<id>.html`），各页面布局可完全不同，
   但共享同一套设计令牌与图表函数（见 web/assets/dashboard.css / dashboard.js）。
4. **不返回任何个人标识**（姓名/证件号/手机等）——与既有看板一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import Settings


@dataclass(frozen=True)
class Project:
    """一个培训项目（= 一张独立看板页面）。"""

    id: str
    dataset_id: str
    title: str
    subtitle: str
    dataset_config: str
    metrics_config: str
    page: str
    accent: str
    accent_soft: str
    kpi_metrics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "page": self.page,
            "accent": self.accent,
            "accent_soft": self.accent_soft,
            "kpi_metrics": list(self.kpi_metrics),
        }


def load_projects(settings: Settings) -> list[Project]:
    """读取 config/dashboards.yaml，返回项目列表（按文件内顺序）。"""
    path = settings.dashboards_config_path
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("projects", []) or []
    out: list[Project] = []
    for item in raw:
        out.append(Project(
            id=str(item["id"]),
            dataset_id=str(item.get("dataset_id", item["id"])),
            title=str(item.get("title", item["id"])),
            subtitle=str(item.get("subtitle", "")),
            dataset_config=str(item.get("dataset_config", "dataset.yaml")),
            metrics_config=str(item.get("metrics_config", "metrics.yaml")),
            page=str(item.get("page", f"dashboard_{item['id']}.html")),
            accent=str(item.get("accent", "#5578f6")),
            accent_soft=str(item.get("accent_soft", "#eef1fe")),
            kpi_metrics=tuple(str(k) for k in (item.get("kpi_metrics") or [])),
        ))
    return out


def find_project(settings: Settings, project_id: str) -> Project | None:
    for project in load_projects(settings):
        if project.id == project_id:
            return project
    return None


def project_page_path(settings: Settings, project: Project) -> Path:
    return settings.project_root / "web" / project.page
