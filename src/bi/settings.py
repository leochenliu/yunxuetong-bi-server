"""运行配置（环境变量）。

与 config/*.yaml 的区别：
  - config/*.yaml  = 业务契约（主键、字段映射、差分规则、指标口径），会进 git
  - settings       = 运行参数（库路径、端口、隐私开关），来自 .env，不进 git
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根 = 本文件的上上级（src/bi/settings.py -> src/bi -> src -> 项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器：不覆盖已存在的真实环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _resolve(value: str, base: Path) -> Path:
    """相对路径按项目根解析，绝对路径原样返回。"""
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    database_url: str | None
    config_dir: Path
    inbound_dir: Path
    store_pii_plaintext: bool
    api_host: str
    api_port: int
    mcp_max_limit: int
    audit_enabled: bool
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    project_root: Path

    @property
    def dataset_config_path(self) -> Path:
        return self.config_dir / "dataset.yaml"

    @property
    def metrics_config_path(self) -> Path:
        return self.config_dir / "metrics.yaml"

    @property
    def goals_config_path(self) -> Path:
        return self.config_dir / "goals.yaml"

    @property
    def queries_config_path(self) -> Path:
        return self.config_dir / "queries.yaml"


def load_settings(env_file: str | Path | None = None) -> Settings:
    _load_dotenv(Path(env_file) if env_file else PROJECT_ROOT / ".env")
    root = PROJECT_ROOT
    return Settings(
        db_path=_resolve(os.environ.get("BI_DB_PATH", "./data/store/bi.sqlite3"), root),
        database_url=os.environ.get("BI_DATABASE_URL", "").strip() or None,
        config_dir=_resolve(os.environ.get("BI_CONFIG_DIR", "./config"), root),
        inbound_dir=_resolve(os.environ.get("BI_INBOUND_DIR", "./data/inbound"), root),
        store_pii_plaintext=_env_bool("BI_STORE_PII_PLAINTEXT", False),
        api_host=os.environ.get("BI_API_HOST", "127.0.0.1"),
        api_port=_env_int("BI_API_PORT", 8787),
        mcp_max_limit=_env_int("BI_MCP_MAX_LIMIT", 200),
        audit_enabled=_env_bool("BI_AUDIT_ENABLED", True),
        llm_base_url=os.environ.get("BI_LLM_BASE_URL", "").strip(),
        llm_api_key=os.environ.get("BI_LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("BI_LLM_MODEL", "").strip(),
        project_root=root,
    )


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """进程内单例。"""
    global _settings
    if _settings is None or refresh:
        _settings = load_settings()
    return _settings
