"""PostgreSQL 连接串解析（**只依赖标准库**）。

为什么不用 `psycopg.conninfo.conninfo_to_dict`
--------------------------------------------
那是最准的解析器，但它属于可选依赖（`psycopg` 装没装取决于环境）。
而本模块的用途是**安全护栏**——判定「这个连接串是不是 Neon 源库」。
护栏不能因为「驱动没装」就静默失效，所以这里自带一个标准库解析器兜底。

解析器优先级：
  1. `psycopg.conninfo.conninfo_to_dict`（装了才用，最准）
  2. `psycopg2.extensions.parse_dsn`（装了 v2 就用）
  3. 本模块的 URL / 关键字形式解析（永远可用）

无论走哪条路径，返回值都至少包含 `host` / `port` / `dbname` / `user` 四个键
（取不到就是空串），**绝不返回 password 给调用方**（调用方要展示时自己脱敏）。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

__all__ = ["parse_dsn", "redact_dsn"]


def _parse_url_form(dsn: str) -> dict[str, str]:
    """`postgresql://user:pass@host:port/dbname?opt=v` → 字典。"""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return {}
    if not parts.scheme:
        return {}
    netloc = parts.netloc
    user = ""
    password = ""
    if "@" in netloc:
        userinfo, _, netloc = netloc.rpartition("@")
        user, _, password = userinfo.partition(":")
    host = netloc
    port = ""
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        host = host.strip("[]")
    return {
        "user": unquote(user),
        "password": unquote(password),
        "host": unquote(host).lower(),
        "port": port,
        "dbname": unquote(parts.path.lstrip("/")),
    }


def _parse_keyword_form(dsn: str) -> dict[str, str]:
    """`host=... port=... dbname=... user=...` → 字典。"""
    out: dict[str, str] = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip().lower()] = value.strip().strip("'")
    return out


def parse_dsn(dsn: str | None) -> dict[str, str]:
    """解析 PostgreSQL 连接串。**任何情况下都返回字典**（解析不出来就是空值键）。

    返回键：`user` / `password` / `host` / `port` / `dbname`。
    """
    if not dsn:
        return {"user": "", "password": "", "host": "", "port": "", "dbname": ""}

    text = dsn.strip()

    # 1) psycopg v3（最准）
    try:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(text)
        if info:
            return {
                "user": str(info.get("user", "")),
                "password": str(info.get("password", "")),
                "host": str(info.get("host", "")).lower(),
                "port": str(info.get("port", "") or "5432"),
                "dbname": str(info.get("dbname", "")),
            }
    except Exception:
        pass

    # 2) psycopg2 v2
    try:
        from psycopg2.extensions import parse_dsn as _pg2_parse

        info = _pg2_parse(text)
        if info:
            return {
                "user": str(info.get("user", "")),
                "password": str(info.get("password", "")),
                "host": str(info.get("host", "")).lower(),
                "port": str(info.get("port", "") or "5432"),
                "dbname": str(info.get("dbname", "")),
            }
    except Exception:
        pass

    # 3) 标准库兜底
    if "://" in text:
        parsed = _parse_url_form(text)
    else:
        parsed = _parse_keyword_form(text)
    parsed.setdefault("user", "")
    parsed.setdefault("password", "")
    parsed.setdefault("host", "")
    parsed.setdefault("port", "")
    parsed.setdefault("dbname", "")
    parsed["host"] = parsed["host"].lower()
    return parsed


def redact_dsn(dsn: str | None) -> str:
    """脱敏后的连接串（供日志）：永远不含 password。"""
    info = parse_dsn(dsn)
    if not info.get("host"):
        return "<unparsable dsn>"
    return "postgresql://{}@{}:{}/{}".format(
        info.get("user") or "?",
        info.get("host") or "?",
        info.get("port") or "5432",
        info.get("dbname") or "?",
    )
