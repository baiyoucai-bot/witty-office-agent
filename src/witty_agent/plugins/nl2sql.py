"""自然语言问数（NL2SQL）：模式提取、只读执行、静态检查、候选选择。业务插件，不进内核循环。

四阶段骨架照 DeepEye-SQL，分解链照 SQL-of-Thought，M-Schema 文本格式照 Aix-DB，
只读白名单与强制 LIMIT 照 SQLBot。按用户要求不接向量检索：值落地走 LIKE 采样，
选表走词面打分，业务口径走 llmwiki。
"""

from __future__ import annotations

import csv
import importlib
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from witty_agent import hooks
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.tools.registry import ToolSpec, register_tool

logger = get_logger("nl2sql")

# 只读入口：语句必须以此开头。其余一律拒绝，不做「看起来安全就放行」的判断
_READ_HEAD = re.compile(r"^\s*(?:select|with)\b", re.IGNORECASE)
# 第二道网：CTE 后面挂写操作（Postgres 允许 WITH ... DELETE）也要拦住
_BLOCKED = re.compile(
    r"\b(?:insert|update|delete|drop|alter|truncate|create|grant|revoke|attach|detach"
    r"|pragma|vacuum|reindex|savepoint|commit|rollback|call|execute|exec|merge|copy"
    r"|outfile|dumpfile|load_file)\b",
    re.IGNORECASE,
)
_AGG = re.compile(r"\b(?:count|sum|avg|min|max|group_concat|string_agg|median|stddev)\s*\(", re.IGNORECASE)
# 只有可加总的聚合会被 JOIN 扇出放大；AVG/MIN/MAX 不会，报了是噪声
_FANOUT_AGG = re.compile(r"\b(?:count|sum|group_concat|string_agg)\s*\(", re.IGNORECASE)
_FROM_JOIN = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)", re.IGNORECASE)
_ALIAS = re.compile(
    r"\b(?:from|join)\s+[A-Za-z_][\w$.]*(?:\s+as)?\s+([A-Za-z_][\w$]*)",
    re.IGNORECASE,
)
_QUALIFIED = re.compile(r"\b([A-Za-z_][\w$]*)\.[A-Za-z_*]")
_WORD = re.compile(r"[a-z0-9_]{2,}")
_HAN = re.compile(r"[一-鿿]+")
_IDENT = re.compile(r"^[A-Za-z_][\w$]*$")
_M_TABLE = re.compile(r"^#\s*Table:\s*([^,\s]+)", re.MULTILINE)
_M_COLUMN = re.compile(r"^\(([A-Za-z_][\w$]*):", re.MULTILINE)
_COL_COMMENT = re.compile(r"--\s*(.+?)\s*$")
# 关键字不能当表别名（`FROM t WHERE ...` 里的 WHERE 会被别名正则误吃）
_NOT_ALIAS = frozenset(
    {
        "where", "group", "order", "having", "limit", "offset", "join", "inner", "left",
        "right", "full", "outer", "cross", "on", "using", "union", "except", "intersect",
        "window", "fetch", "and", "or", "as", "when", "then", "else", "end", "select",
        "case", "distinct", "all",
    }
)
# FETCH FIRST 方言：不认 LIMIT
_FETCH_DIALECTS = frozenset({"oracle", "dm", "damengsql", "mssql", "sqlserver", "db2"})
_ROW_CAP = 50
_CELL_CAP = 40


# ---------------------------------------------------------------- 配置与连接


def nl2sql_settings() -> dict[str, Any]:
    """运行时配置。模块级不从 runtime 绑名字，避免旧进程/环状导入把整轮聊天打挂。"""
    from witty_agent import runtime

    loader = getattr(runtime, "nl2sql_settings", None)
    if callable(loader):
        return loader()
    return {
        "enabled": True,
        "default_limit": 1000,
        "max_limit": 10000,
        "max_tables": 12,
        "conf_threshold": 0.6,
        "sources": [],
    }


def _enabled() -> bool:
    return bool(nl2sql_settings().get("enabled", True))


def _workspace() -> Path:
    raw = str(hooks.current_workspace or "").strip()
    return Path(raw).expanduser() if raw else Path.cwd()


def _resolve(path: str) -> Path:
    target = Path(path).expanduser()
    if target.is_absolute() or target.exists():
        return target
    return _workspace() / path


def _sources() -> dict[str, dict[str, str]]:
    table: dict[str, dict[str, str]] = {}
    for item in nl2sql_settings().get("sources") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        table[name] = {
            "name": name,
            "dialect": str(item.get("dialect") or "sqlite").strip().lower(),
            "dsn": str(item.get("dsn") or "").strip(),
            "password_env": str(item.get("password_env") or "").strip(),
            "comment": str(item.get("comment") or "").strip(),
        }
    return table


def _spec(source: str) -> dict[str, str]:
    """source 可以是 [nl2sql.sources] 里登记的名字，也可以直接给 sqlite 文件路径。"""
    asked = str(source or "").strip()
    known = _sources()
    if asked in known:
        return known[asked]
    if not asked:
        if len(known) == 1:
            return next(iter(known.values()))
        raise ValueError(get_prompt("nl2sql_source_ambiguous", names=", ".join(sorted(known)) or "-"))
    return {"name": asked, "dialect": "sqlite", "dsn": asked, "password_env": "", "comment": ""}


@contextmanager
def _connect(source: str) -> Iterator[tuple[Any, str]]:
    spec = _spec(source)
    dialect = spec["dialect"]
    if dialect == "sqlite":
        target = _resolve(spec["dsn"])
        if not target.is_file():
            raise ValueError(get_prompt("nl2sql_db_missing", path=str(target)))
        # mode=ro 让 SQLite 自己也拒绝写，和 SQL 白名单构成两道防线
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    else:
        conn = _connect_driver(spec)
    try:
        yield conn, dialect
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关连接失败不该盖掉真正的错
            logger.warning("关闭连接失败 source=%s", spec["name"])


_DRIVERS = {
    "mysql": ("pymysql", "MySQLdb", "mysql.connector"),
    "postgres": ("psycopg", "psycopg2"),
    "postgresql": ("psycopg", "psycopg2"),
    "oracle": ("oracledb", "cx_Oracle"),
    "dm": ("dmPython",),
    "mssql": ("pymssql", "pyodbc"),
    "sqlserver": ("pymssql", "pyodbc"),
}

# 缺驱动时推荐装哪个包（`pyproject.toml` 的 optional-dependencies 里同名的 extra）。
# 只列有 extra 的方言；没列的走通用文案，让人自己挑驱动
_DRIVER_EXTRA = {
    "mysql": ("mysql", "pymysql"),
    "postgres": ("postgres", "psycopg[binary]"),
    "postgresql": ("postgres", "psycopg[binary]"),
}


def _checkout_root() -> Path | None:
    """源码树的仓库根；装成 wheel 在 site-packages 里跑就是 None。

    判据跟 `http_api.package_version` 一致：往上数到该有 `pyproject.toml` 的那层，
    有就是源码树。site-packages 里对应的那层不会有。
    """
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


def _install_hint(dialect: str) -> str:
    """给出能照抄的装包命令，并点明是往**哪个**解释器装。

    截图里那次失败的根因不是权限而是回执太空：只说「请自行安装」，模型就会自己发明
    办法——先 pip 进沙箱 venv（那是另一个解释器，且被策略锁死），再想拉 docker 客户端
    容器。这里把解释器路径和命令都写出来，agent 过一次 bash 审批就能装上。

    命令得分源码树和装好的包两种情形：源码树能 `uv add` 进 pyproject，装好的包没有
    自己的 pyproject 可改，只能往当前解释器装。给错命令跟不给命令一样会让模型乱走。
    """
    extra = _DRIVER_EXTRA.get(dialect)
    if not extra:
        return get_prompt("nl2sql_driver_install_plain", python=sys.executable)
    root = _checkout_root()
    if root is None:
        return get_prompt(
            "nl2sql_driver_install_wheel",
            python=sys.executable,
            package=extra[1],
        )
    return get_prompt(
        "nl2sql_driver_install_extra",
        root=root,
        extra=extra[0],
        package=extra[1],
        python=sys.executable,
    )


def _connect_driver(spec: dict[str, str]) -> Any:
    """非 sqlite 方言不带驱动：装了就用，没装就明说，不假装能连。"""
    dialect = spec["dialect"]
    names = _DRIVERS.get(dialect)
    if not names:
        raise ValueError(get_prompt("nl2sql_dialect_unknown", dialect=dialect))
    module = None
    for name in names:
        try:
            module = importlib.import_module(name)
            break
        except ImportError:
            continue
    if module is None:
        raise ValueError(
            get_prompt(
                "nl2sql_driver_missing",
                dialect=dialect,
                names=", ".join(names),
                hint=_install_hint(dialect),
            )
        )
    parsed = urlparse(spec["dsn"])
    if not parsed.hostname:
        raise ValueError(get_prompt("nl2sql_dsn_bad", name=spec["name"]))
    # 口令只从环境变量取，不进配置、不进日志
    password = os.environ.get(spec["password_env"], "") if spec["password_env"] else ""
    kwargs: dict[str, Any] = {
        "host": parsed.hostname,
        "user": parsed.username or "",
        "password": password or (parsed.password or ""),
        "database": (parsed.path or "/").lstrip("/"),
    }
    if parsed.port:
        kwargs["port"] = int(parsed.port)
    logger.info("连接数据源 name=%s dialect=%s host=%s", spec["name"], dialect, parsed.hostname)
    return module.connect(**kwargs)


def _fetch(conn: Any, sql: str, params: Sequence[Any] = ()) -> tuple[list[str], list[tuple[Any, ...]]]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, tuple(params)) if params else cursor.execute(sql)
        columns = [str(item[0]) for item in (cursor.description or [])]
        rows = [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()
    return columns, rows


# ---------------------------------------------------------------- SQL 静态处理


def _scrub(sql: str) -> str:
    """把字符串字面量和注释换成空格，后面所有正则都跑在这份上。

    否则 `WHERE name = 'DROP TABLE x'` 会被当成写操作，`-- limit 10` 会被当成有 LIMIT。
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in {"'", '"', "`"}:
            quote = char
            out.append(" ")
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        out.append(" ")
                        index += 2
                        continue
                    index += 1
                    break
                out.append(" " if sql[index] != "\n" else "\n")
                index += 1
            out.append(" ")
            continue
        if char == "-" and sql.startswith("--", index):
            while index < length and sql[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            stop = length if end < 0 else end + 2
            out.append("".join(" " if ch != "\n" else "\n" for ch in sql[index:stop]))
            index = stop
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _depth_zero(scrubbed: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """只要顶层（括号深度 0）的匹配，子查询里的不算。"""
    depth = 0
    edges: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(scrubbed):
        if char == "(":
            if depth == 0:
                edges.append((start, index))
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                start = index + 1
    edges.append((start, len(scrubbed)))
    hits: list[re.Match[str]] = []
    for left, right in edges:
        hits.extend(pattern.finditer(scrubbed, left, right))
    return hits


_LIMIT = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
_FETCH = re.compile(r"\bfetch\s+(?:first|next)\s+(\d+)\s+rows?\s+only", re.IGNORECASE)
_TOP = re.compile(r"\bselect\s+(?:distinct\s+)?top\s*\(?\s*(\d+)", re.IGNORECASE)


def _limit_of(scrubbed: str) -> int:
    """顶层行数上限；没有就返回 0。"""
    for pattern in (_LIMIT, _FETCH, _TOP):
        hits = _depth_zero(scrubbed, pattern)
        if hits:
            return int(hits[-1].group(1))
    return 0


def _reject(sql: str) -> str:
    """只读体检。通过返回空串，否则返回给模型看的拒绝原因。"""
    text = str(sql or "").strip()
    if not text:
        return get_prompt("nl2sql_sql_empty")
    scrubbed = _scrub(text)
    if scrubbed.count("(") != scrubbed.count(")"):
        return get_prompt("nl2sql_unbalanced")
    body = scrubbed.rstrip().rstrip(";")
    if ";" in body:
        return get_prompt("nl2sql_multi_statement")
    if not _READ_HEAD.match(scrubbed):
        head = (scrubbed.strip().split() or ["?"])[0]
        return get_prompt("nl2sql_not_readonly", head=head.upper())
    hit = _BLOCKED.search(scrubbed)
    if hit:
        return get_prompt("nl2sql_blocked_keyword", word=hit.group(0).upper())
    return ""


def _cap_limit(sql: str, dialect: str, want: int) -> tuple[str, str]:
    """SQLBot 的零容忍规则：用户说「全部数据」也照样带上限。返回 (SQL, 提示)。"""
    scrubbed = _scrub(sql)
    body = sql.rstrip().rstrip(";").rstrip()
    current = _limit_of(scrubbed)
    if current and current <= want:
        return body, ""
    if current > want:
        hits = _depth_zero(_scrub(body), _LIMIT)
        if hits:
            last = hits[-1]
            patched = body[: last.start()] + f"LIMIT {want}" + body[last.end() :]
            return patched, get_prompt("nl2sql_limit_lowered", was=str(current), now=str(want))
        return body, get_prompt("nl2sql_limit_over", was=str(current), now=str(want))
    if dialect in _FETCH_DIALECTS:
        return f"{body} FETCH FIRST {want} ROWS ONLY", get_prompt("nl2sql_limit_added", now=str(want))
    return f"{body} LIMIT {want}", get_prompt("nl2sql_limit_added", now=str(want))


# ---------------------------------------------------------------- 词面切分


def _tokens(text: str) -> set[str]:
    """词面切分。中文切双字，和 plugins/llmwiki 的口径一致，不引入 jieba。"""
    lowered = str(text or "").lower()
    words = set(_WORD.findall(lowered))
    for run in _HAN.findall(str(text or "")):
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words


# ---------------------------------------------------------------- M-Schema


def _sqlite_tables(conn: Any) -> list[tuple[str, str]]:
    _, rows = _fetch(
        conn,
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def _sqlite_comments(create_sql: str) -> dict[str, str]:
    """SQLite 没有列注释，但建表语句里的行尾 `-- 说明` 是现成的业务口径。"""
    found: dict[str, str] = {}
    for line in create_sql.splitlines():
        note = _COL_COMMENT.search(line)
        if not note:
            continue
        head = line[: note.start()].strip().strip(",").strip()
        name = (head.split() or [""])[0].strip('"`[]')
        if _IDENT.match(name):
            found[name.lower()] = note.group(1).strip()
    return found


def _sqlite_columns(conn: Any, table: str, create_sql: str) -> list[dict[str, str]]:
    notes = _sqlite_comments(create_sql)
    _, rows = _fetch(conn, f'PRAGMA table_info("{table}")')
    columns: list[dict[str, str]] = []
    for row in rows:
        name = str(row[1])
        columns.append(
            {
                "name": name,
                "type": str(row[2] or "TEXT").upper(),
                "comment": notes.get(name.lower(), ""),
                "pk": "1" if row[5] else "",
                "notnull": "1" if row[3] else "",
            }
        )
    return columns


def _sqlite_keys(conn: Any, table: str) -> list[str]:
    _, rows = _fetch(conn, f'PRAGMA foreign_key_list("{table}")')
    return [f"{table}.{row[3]}={row[2]}.{row[4]}" for row in rows]


_GENERIC_TABLES = (
    "SELECT table_name, COALESCE(table_comment, '') FROM information_schema.tables "
    "WHERE table_schema = DATABASE() ORDER BY table_name"
)
_GENERIC_COLUMNS = (
    "SELECT column_name, data_type, COALESCE(column_comment, ''), column_key, is_nullable "
    "FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = %s "
    "ORDER BY ordinal_position"
)


def _describe(conn: Any, dialect: str, only: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """统一成 [{name, comment, columns:[...]}] + 外键行。"""
    tables: list[dict[str, Any]] = []
    keys: list[str] = []
    if dialect == "sqlite":
        for name, create_sql in _sqlite_tables(conn):
            if only and name.lower() not in only:
                continue
            tables.append(
                {
                    "name": name,
                    "comment": "",
                    "columns": _sqlite_columns(conn, name, create_sql),
                }
            )
            keys.extend(_sqlite_keys(conn, name))
        return tables, keys
    _, rows = _fetch(conn, _GENERIC_TABLES)
    for row in rows:
        name = str(row[0])
        if only and name.lower() not in only:
            continue
        _, cols = _fetch(conn, _GENERIC_COLUMNS, (name,))
        tables.append(
            {
                "name": name,
                "comment": str(row[1] or ""),
                "columns": [
                    {
                        "name": str(col[0]),
                        "type": str(col[1] or "").upper(),
                        "comment": str(col[2] or ""),
                        "pk": "1" if str(col[3] or "").upper() == "PRI" else "",
                        "notnull": "" if str(col[4] or "").upper() == "YES" else "1",
                    }
                    for col in cols
                ],
            }
        )
    return tables, keys


def _samples(conn: Any, table: str, column: str, count: int) -> list[str]:
    if count <= 0:
        return []
    try:
        _, rows = _fetch(
            conn,
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT {count}',
        )
    except Exception:  # noqa: BLE001 - 采样失败不该让整张模式表挂掉
        return []
    return [_cell(row[0], _CELL_CAP) for row in rows]


def _m_schema(name: str, tables: list[dict[str, Any]], keys: list[str], conn: Any, samples: int) -> str:
    blocks: list[str] = []
    for table in tables:
        lines: list[str] = []
        for column in table["columns"]:
            parts = [f"({column['name']}:{column['type']}"]
            if column["comment"]:
                parts.append(f", {column['comment']}")
            if column["pk"]:
                parts.append(get_prompt("nl2sql_schema_pk"))
            elif not column["notnull"]:
                # 可空要写进模式文本：NULL 检查器只有拿到这条证据才敢报，否则一律不报
                parts.append(get_prompt("nl2sql_schema_nullable"))
            values = _samples(conn, table["name"], column["name"], samples)
            if values:
                parts.append(get_prompt("nl2sql_schema_examples", values=", ".join(values)))
            lines.append("".join(parts) + "),")
        head = f"# Table: {table['name']}"
        if table["comment"]:
            head += f", {table['comment']}"
        blocks.append(head + "\n[\n" + "\n".join(lines) + "\n]")
    body = get_prompt("nl2sql_schema_body", db_id=name, tables="\n".join(blocks))
    if keys:
        body += "\n" + get_prompt("nl2sql_schema_keys", keys="\n".join(keys))
    return body


# ---------------------------------------------------------------- 渲染


def _cell(value: Any, cap: int) -> str:
    if value is None:
        return get_prompt("nl2sql_null")
    text = " ".join(str(value).split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _markdown(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not columns:
        return get_prompt("nl2sql_no_columns")
    head = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_cell(value, _CELL_CAP) for value in row) + " |"
        for row in rows[:_ROW_CAP]
    ]
    table = "\n".join([head, rule, *body])
    if len(rows) > _ROW_CAP:
        table += "\n" + get_prompt("nl2sql_rows_truncated", shown=str(_ROW_CAP), total=str(len(rows)))
    return table


# ---------------------------------------------------------------- 工具：模式


def sql_sources() -> str:
    """列出已登记的数据源。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    rows = _sources()
    if not rows:
        return get_prompt("nl2sql_sources_empty")
    lines = [
        get_prompt(
            "nl2sql_sources_item",
            name=item["name"],
            dialect=item["dialect"],
            dsn=item["dsn"],
            comment=item["comment"] or "-",
        )
        for item in sorted(rows.values(), key=lambda item: item["name"])
    ]
    return get_prompt("nl2sql_sources_report", count=str(len(lines)), rows="\n".join(lines))


def sql_schema(source: str = "", tables: str = "", samples: int = 0) -> str:
    """按 M-Schema 格式导出库结构，可只导指定表、可带样例值。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    only = {part.strip().lower() for part in str(tables or "").split(",") if part.strip()}
    cap = max(0, min(int(samples or 0), 10))
    try:
        with _connect(source) as (conn, dialect):
            found, keys = _describe(conn, dialect, only)
            if not found:
                return get_prompt("nl2sql_no_tables", tables=tables or "-")
            name = _spec(source)["name"]
            body = _m_schema(Path(name).stem or name, found, keys, conn, cap)
    except (ValueError, sqlite3.Error) as exc:
        return str(exc)
    logger.info("导出模式 source=%s tables=%s", _spec(source)["name"], len(found))
    return body


def sql_tables(question: str, source: str = "", limit: int = 10) -> str:
    """按问句词面打分挑候选表，替代向量召回。返回命中理由，不猜列。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    needles = _tokens(question)
    if not needles:
        return get_prompt("nl2sql_question_empty")
    cap = max(1, min(int(limit or 10), int(nl2sql_settings().get("max_tables", 12))))
    try:
        with _connect(source) as (conn, dialect):
            found, _ = _describe(conn, dialect, set())
    except (ValueError, sqlite3.Error) as exc:
        return str(exc)
    ranked: list[tuple[int, str, str]] = []
    for table in found:
        # 表名和表注释权重高于列，照 Aix-DB 给表注释加权的思路
        hits: dict[str, int] = {}
        for weight, text in ((3, table["name"]), (2, table["comment"])):
            for token in needles & _tokens(text):
                hits[token] = max(hits.get(token, 0), weight)
        for column in table["columns"]:
            for token in needles & _tokens(f"{column['name']} {column['comment']}"):
                hits.setdefault(token, 1)
        score = sum(hits.values())
        if score > 0:
            ranked.append((score, table["name"], "、".join(sorted(hits))))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return get_prompt("nl2sql_tables_empty", count=str(len(found)))
    rows = "\n".join(
        get_prompt("nl2sql_tables_item", score=str(score), table=name, why=why)
        for score, name, why in ranked[:cap]
    )
    return get_prompt("nl2sql_tables_report", count=str(min(cap, len(ranked))), total=str(len(found)), rows=rows)


def sql_values(table: str, column: str, source: str = "", keyword: str = "", limit: int = 8) -> str:
    """采样某列的真实取值，把问句里的说法对到库里的写法上。不建向量索引。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    if not _IDENT.match(str(table or "")) or not _IDENT.match(str(column or "")):
        return get_prompt("nl2sql_ident_bad", table=str(table), column=str(column))
    cap = max(1, min(int(limit or 8), 50))
    needle = str(keyword or "").strip()
    sql = f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
    params: list[Any] = []
    if needle:
        sql += f' AND CAST("{column}" AS CHAR) LIKE ?'
        params.append(f"%{needle}%")
    sql += f" LIMIT {cap}"
    try:
        with _connect(source) as (conn, dialect):
            if dialect != "sqlite":
                sql = sql.replace("?", "%s").replace("CHAR", "TEXT" if "postgres" in dialect else "CHAR")
            _, rows = _fetch(conn, sql, params)
    except (ValueError, sqlite3.Error, Exception) as exc:  # noqa: BLE001 - 驱动异常种类不统一
        return get_prompt("nl2sql_values_failed", table=table, column=column, error=_brief(exc))
    if not rows:
        return get_prompt("nl2sql_values_empty", table=table, column=column, keyword=needle or "-")
    values = ", ".join(_cell(row[0], _CELL_CAP) for row in rows)
    return get_prompt(
        "nl2sql_values_report",
        table=table,
        column=column,
        keyword=needle or "-",
        count=str(len(rows)),
        values=values,
    )


def _brief(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= 200 else text[:199] + "…"


# ---------------------------------------------------------------- 工具：执行


def _run(source: str, sql: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]], str, str]:
    """跑一条只读 SQL。返回 (列, 行, 实际执行的 SQL, 提示)。失败抛 ValueError。"""
    refuse = _reject(sql)
    if refuse:
        raise ValueError(refuse)
    settings = nl2sql_settings()
    want = int(limit or 0) or int(settings.get("default_limit", 1000))
    want = max(1, min(want, int(settings.get("max_limit", 10000))))
    with _connect(source) as (conn, dialect):
        final, note = _cap_limit(str(sql).strip(), dialect, want)
        columns, rows = _fetch(conn, final)
    return columns, rows, final, note


_AND_OR = re.compile(r"\b(and|or)\b", re.IGNORECASE)
_BETWEEN = re.compile(r"\bbetween\b", re.IGNORECASE)


def _split_predicates(sql: str, scrubbed: str, left: int, right: int) -> list[str]:
    """按顶层 AND/OR 把一段条件切成单条。

    切点在 scrubbed 上找（字面量已抹掉，不会把 `'a AND b'` 当连接词），
    正文从原文取（要让人看见 `'江北'` 到底写成了什么）。
    `BETWEEN x AND y` 自带的那个 AND 不是分隔符，要跳过。
    """
    out: list[str] = []
    depth = 0
    start = left
    index = left
    skip_and = False
    while index < right:
        char = scrubbed[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            found = _BETWEEN.match(scrubbed, index, right)
            if found:
                skip_and = True
                index = found.end()
                continue
            found = _AND_OR.match(scrubbed, index, right)
            if found:
                if skip_and and found.group(1).lower() == "and":
                    skip_and = False
                else:
                    out.append(sql[start : found.start()])
                    start = found.end()
                index = found.end()
                continue
        index += 1
    out.append(sql[start:right])
    return out


def _constraints(sql: str) -> list[str]:
    """把 SQL 里的过滤条件逐条拆出来，给 Result 检查器点名复查。"""
    scrubbed = _scrub(sql)
    items: list[str] = []
    for head, tail in (
        (r"\bwhere\b", r"\b(?:group\s+by|having|order\s+by|limit|offset|fetch|window|union)\b"),
        (r"\bhaving\b", r"\b(?:order\s+by|limit|offset|fetch|window|union)\b"),
    ):
        start = re.search(head, scrubbed, re.IGNORECASE)
        if not start:
            continue
        rest = scrubbed[start.end() :]
        stop = re.search(tail, rest, re.IGNORECASE)
        end = start.end() + (stop.start() if stop else len(rest))
        items.extend(_split_predicates(sql, scrubbed, start.end(), end))
    for body in _on_clauses(scrubbed):
        offset = scrubbed.find(body)
        if offset >= 0:
            items.append(sql[offset : offset + len(body)])
    out: list[str] = []
    for item in items:
        text = " ".join(item.split())
        if text and text not in out:
            out.append(text if len(text) <= 90 else text[:89] + "…")
    return out[:8]


def sql_run(sql: str, source: str = "", limit: int = 0) -> str:
    """只读执行一条 SELECT，强制带行数上限，返回 markdown 表。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    try:
        columns, rows, final, note = _run(source, sql, limit)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - 驱动异常种类不统一，交回模型改 SQL
        return get_prompt("nl2sql_run_failed", error=_brief(exc))
    logger.info("执行只读 SQL source=%s rows=%s", _spec(source)["name"], len(rows))
    report = get_prompt(
        "nl2sql_run_report",
        sql=final,
        rows=str(len(rows)),
        cols=str(len(columns)),
        note=note or "-",
        table=_markdown(columns, rows),
    )
    if not rows:
        # DeepEye Table 2 的 Result 检查器：0 行不是答案，要回去复查约束
        found = _constraints(final)
        report += "\n" + get_prompt(
            "nl2sql_run_empty",
            items="\n".join(f"- {item}" for item in found) if found else get_prompt("nl2sql_no_filter"),
        )
    return report


def sql_export(sql: str, path: str, source: str = "", limit: int = 0) -> str:
    """只读执行后把结果写成 CSV，给沙箱出图用。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    target = _resolve(str(path or "").strip() or "nl2sql_result.csv")
    if target.suffix.lower() != ".csv":
        return get_prompt("nl2sql_export_suffix", path=str(target))
    try:
        columns, rows, final, note = _run(source, sql, limit)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return get_prompt("nl2sql_run_failed", error=_brief(exc))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    logger.info("导出结果 path=%s rows=%s", target.name, len(rows))
    return get_prompt(
        "nl2sql_export_report",
        path=str(target),
        rows=str(len(rows)),
        cols=str(len(columns)),
        sql=final,
        note=note or "-",
    )


# ---------------------------------------------------------------- 工具：检查器链


def _schema_names(schema: str) -> tuple[set[str], set[str]]:
    tables = {name.split(".")[-1].lower() for name in _M_TABLE.findall(schema or "")}
    columns = {name.lower() for name in _M_COLUMN.findall(schema or "")}
    return tables, columns


_M_COL_LINE = re.compile(r"^\((?P<name>[A-Za-z_][\w$]*):(?P<rest>.*)$", re.MULTILINE)
_TEMPORAL_TYPE = re.compile(r"\b(?:datetime|timestamp|timestamptz)\b", re.IGNORECASE)


def _schema_facts(schema: str) -> dict[str, dict[str, bool]]:
    """从 M-Schema 文本读回「列 → 是否可空 / 是否带时分秒」，给 NULL 与时间检查器当证据。

    同名列出现在多张表里且性质不一致时，那一项整个丢掉——两张表的 `status` 一个可空
    一个不可空，报出来必然有一半是错的，不如不报。
    """
    cut = get_prompt("nl2sql_schema_examples", values="\x00").split("\x00")[0]
    marker = get_prompt("nl2sql_schema_nullable").strip().strip(",").strip()
    seen: dict[str, list[tuple[bool, bool]]] = {}
    for match in _M_COL_LINE.finditer(schema or ""):
        rest = match.group("rest")
        if cut:
            rest = rest.split(cut)[0]
        parts = [item.strip().rstrip("),").strip() for item in rest.split(",")]
        nullable = bool(marker) and marker in parts
        temporal = bool(parts and _TEMPORAL_TYPE.search(parts[0]))
        seen.setdefault(match.group("name").lower(), []).append((nullable, temporal))
    facts: dict[str, dict[str, bool]] = {}
    for name, rows in seen.items():
        entry: dict[str, bool] = {}
        if len({row[0] for row in rows}) == 1:
            entry["nullable"] = rows[0][0]
        if len({row[1] for row in rows}) == 1:
            entry["temporal"] = rows[0][1]
        facts[name] = entry
    return facts


def _known_dialect(source: str, dialect: str) -> str:
    """检查器要用的方言。只认三种来源：显式传的、登记过的源、存在的 sqlite 文件。

    未登记的源名不猜方言——猜错会让时间检查器对着正确的 SQL 报 FAIL。
    """
    asked = str(dialect or "").strip()
    if asked:
        return asked
    name = str(source or "").strip()
    known = _sources()
    if name and name in known:
        return known[name]["dialect"]
    if not name and len(known) == 1:
        return next(iter(known.values()))["dialect"]
    if name and _resolve(name).is_file():
        return "sqlite"
    return ""


def _aliases(scrubbed: str) -> set[str]:
    found: set[str] = set()
    for match in _ALIAS.finditer(scrubbed):
        name = match.group(1).lower()
        if name not in _NOT_ALIAS:
            found.add(name)
    return found


def _syntax_findings(sql: str, scrubbed: str, schema: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    refuse = _reject(sql)
    if refuse:
        out.append(("FAIL", "S1", refuse))
    if not re.search(r"\bfrom\b", scrubbed, re.IGNORECASE):
        out.append(("WARN", "S2", get_prompt("nl2sql_check_no_from")))
    if schema:
        known, _ = _schema_names(schema)
        if known:
            used = {name.split(".")[-1].lower() for name in _FROM_JOIN.findall(scrubbed)}
            unknown = sorted(used - known - _aliases(scrubbed))
            if unknown:
                out.append(("FAIL", "S3", get_prompt("nl2sql_check_unknown_table", names="、".join(unknown))))
    return out


def _outer_only(text: str) -> str:
    """抹掉括号里的内容，括号本身留下——用来区分「顶层出现」和「子查询里出现」。

    括号要留：`count(*)` 抹成 `count( )` 还认得出是聚合，而
    `id = (SELECT MAX(id) FROM t)` 抹成 `id = ( )` 就不会再被当成「WHERE 里有聚合」。
    """
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            out.append(char)
        elif depth:
            out.append("\n" if char == "\n" else " ")
        else:
            out.append(char)
    return "".join(out)


def _logic_findings(scrubbed: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    joins = len(re.findall(r"\bjoin\b", scrubbed, re.IGNORECASE))
    links = len(re.findall(r"\b(?:on|using)\b", scrubbed, re.IGNORECASE))
    if joins and links < joins:
        out.append(("FAIL", "L1", get_prompt("nl2sql_check_cross_join", joins=str(joins), links=str(links))))
    select_part = _select_clause(scrubbed)
    has_agg = bool(_AGG.search(select_part))
    has_group = bool(re.search(r"\bgroup\s+by\b", scrubbed, re.IGNORECASE))
    bare = _bare_columns(select_part)
    if has_agg and not has_group and bare:
        out.append(("FAIL", "L2", get_prompt("nl2sql_check_group_by", cols="、".join(bare))))
    elif has_agg and has_group and bare:
        missing = _ungrouped(bare, scrubbed)
        if missing:
            out.append(("FAIL", "L2", get_prompt("nl2sql_check_group_by_missing", cols="、".join(missing))))
    where_part = _clause(scrubbed, r"\bwhere\b", r"\b(?:group\s+by|having|order\s+by|limit|window)\b")
    # 只看顶层：`WHERE id = (SELECT MAX(id) FROM t)` 是合法的，聚合在子查询里不算错
    if where_part and _AGG.search(_outer_only(where_part)):
        out.append(("FAIL", "L3", get_prompt("nl2sql_check_agg_in_where")))
    if re.search(r"/\s*(?!\*)", scrubbed) and not re.search(r"\bnullif\b", scrubbed, re.IGNORECASE):
        out.append(("WARN", "L4", get_prompt("nl2sql_check_div_zero")))
    if joins and _FANOUT_AGG.search(select_part) and not re.search(r"\bdistinct\b", scrubbed, re.IGNORECASE):
        out.append(("WARN", "L5", get_prompt("nl2sql_check_join_fanout")))
    prefixes = {name.lower() for name in _QUALIFIED.findall(scrubbed)}
    declared = _aliases(scrubbed) | {name.split(".")[-1].lower() for name in _FROM_JOIN.findall(scrubbed)}
    stray = sorted(prefixes - declared - _NOT_ALIAS)
    if stray:
        out.append(("WARN", "L6", get_prompt("nl2sql_check_alias", names="、".join(stray))))
    out.extend(_join_findings(scrubbed))
    out.extend(_order_findings(scrubbed))
    return out


def _on_clauses(scrubbed: str) -> list[str]:
    """每个 ON 条件的正文：从 ON 到下一个 JOIN/WHERE/GROUP/ORDER/HAVING/LIMIT 之前。"""
    out: list[str] = []
    stop = re.compile(
        r"\b(?:inner|left|right|full|outer|cross|join|where|group\s+by|having|order\s+by"
        r"|limit|window|union|except|intersect)\b",
        re.IGNORECASE,
    )
    for match in re.finditer(r"\bon\b", scrubbed, re.IGNORECASE):
        rest = scrubbed[match.end() :]
        end = stop.search(rest)
        out.append(rest[: end.start()] if end else rest)
    return out


def _join_findings(scrubbed: str) -> list[tuple[str, str, str]]:
    """JOIN 检查器：论文 Table 2 点名的两种非标准连接条件。

    `ON a = b OR c = d` 和 `ON col IN (SELECT …)` 都是合法 SQL，引擎不会报错，
    但前者会让行数爆掉、后者把连接写成了半连接，两种都是「跑得动但算错」。
    """
    out: list[tuple[str, str, str]] = []
    bodies = _on_clauses(scrubbed)
    if any(re.search(r"\bor\b", body, re.IGNORECASE) for body in bodies):
        out.append(("WARN", "L7", get_prompt("nl2sql_check_join_or")))
    if any(re.search(r"\bselect\b", body, re.IGNORECASE) for body in bodies):
        out.append(("WARN", "L8", get_prompt("nl2sql_check_join_subquery")))
    return out


def _order_findings(scrubbed: str) -> list[tuple[str, str, str]]:
    """ORDER-BY 检查器：论文 Table 2 的「排序与 LIMIT 的逻辑冲突」。

    `ORDER BY COUNT(*) DESC LIMIT 1` 这个模式有两个坑：没有 GROUP BY 时聚合把
    整张表压成一行，排序和 LIMIT 都成了摆设；有 GROUP BY 时并列第一会被 LIMIT 1
    随机砍掉一个。前者是确定的错，后者要问用户。
    """
    out: list[tuple[str, str, str]] = []
    order_part = _clause(scrubbed, r"\border\s+by\b", r"\b(?:limit|offset|fetch|window)\b")
    # 同样只看顶层：`ORDER BY (SELECT count(*) …)` 是合法的相关子查询排序
    if not order_part.strip() or not _AGG.search(_outer_only(order_part)):
        return out
    if not re.search(r"\bgroup\s+by\b", scrubbed, re.IGNORECASE):
        out.append(("FAIL", "L9", get_prompt("nl2sql_check_order_agg_no_group")))
    elif _limit_of(scrubbed) == 1:
        out.append(("WARN", "L10", get_prompt("nl2sql_check_order_agg_tie")))
    return out


# ---------------------------------------------------------------- 检查器：时间


def _dialect_key(dialect: str) -> str:
    """把方言名归一到检查器认识的键；不认识就返回空串（宁可不查，不乱报）。"""
    name = str(dialect or "").strip().lower()
    if name.startswith("postgres"):
        return "postgres"
    if name in {"mysql", "mariadb"}:
        return "mysql"
    if name == "sqlite":
        return "sqlite"
    return ""


# 各方言「没有这个时间函数」的清单。只列确定不存在的，拿不准的一律不列——
# 误报一个 FAIL 会让模型去改本来正确的 SQL，比漏报更贵。
_TIME_ABSENT: dict[str, frozenset[str]] = {
    "sqlite": frozenset(
        {
            "year", "month", "day", "hour", "minute", "date_format", "datediff", "dateadd",
            "date_add", "date_sub", "adddate", "subdate", "extract", "date_trunc", "date_part",
            "to_char", "to_date", "curdate", "now", "sysdate", "timestampdiff", "last_day",
            "age", "months_between", "convert_tz",
        }
    ),
    "mysql": frozenset(
        {"strftime", "julianday", "unixepoch", "date_trunc", "date_part", "age", "to_date"}
    ),
    "postgres": frozenset(
        {
            "strftime", "julianday", "unixepoch", "date_format", "datediff", "dateadd",
            "year", "month", "day", "hour", "minute", "curdate", "sysdate", "adddate",
            "subdate", "date_add", "date_sub", "timestampdiff", "last_day", "convert_tz",
        }
    ),
}
_CALL = re.compile(r"\b([A-Za-z_][\w$]*)\s*\(")
# 抽年月的函数在 sqlite 里返回文本，和数字比永远不成立
_TEXT_DATE_FUNCS = re.compile(r"\b(strftime|date_format|to_char)\s*\(", re.IGNORECASE)
_CMP_NUMBER = re.compile(r"\s*(?:=|<>|!=|>=|<=|>|<)\s*(\d+)")
_DATE_ONLY = re.compile(
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s+between\s+'\d{4}-\d{2}-\d{2}'\s+and\s+'(\d{4}-\d{2}-\d{2})'",
    re.IGNORECASE,
)
_DATE_UPPER = re.compile(
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*<=\s*'(\d{4}-\d{2}-\d{2})'",
    re.IGNORECASE,
)


def _call_end(scrubbed: str, open_paren: int) -> int:
    """从左括号处走到配对的右括号之后；括号不配对返回 -1。"""
    depth = 0
    for index in range(open_paren, len(scrubbed)):
        if scrubbed[index] == "(":
            depth += 1
        elif scrubbed[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _time_findings(
    sql: str, scrubbed: str, dialect: str, facts: dict[str, dict[str, bool]]
) -> list[tuple[str, str, str]]:
    """时间检查器：论文 Table 2 的 STRFTIME vs. DATETIME 与日期格式比较。

    T1 靠方言清单，方言未知就整条跳过。T3 要看原文（字面量没被抹掉），
    而且只在模式文本说了这列是 datetime/timestamp 时才报。
    """
    out: list[tuple[str, str, str]] = []
    key = _dialect_key(dialect)
    absent = _TIME_ABSENT.get(key, frozenset())
    if absent:
        used = {match.group(1).lower() for match in _CALL.finditer(scrubbed)}
        hits = sorted(used & absent)
        if hits:
            out.append(
                (
                    "FAIL",
                    "T1",
                    get_prompt(
                        "nl2sql_check_time_func",
                        dialect=key,
                        names="、".join(name.upper() + "()" for name in hits),
                    ),
                )
            )
    for match in _TEXT_DATE_FUNCS.finditer(scrubbed):
        end = _call_end(scrubbed, match.end() - 1)
        if end < 0:
            continue
        follow = _CMP_NUMBER.match(scrubbed[end:])
        if not follow:
            continue
        level = "FAIL" if key == "sqlite" else "WARN"
        out.append(
            (
                level,
                "T2",
                get_prompt(
                    "nl2sql_check_time_text_number",
                    func=match.group(1).upper(),
                    number=follow.group(1),
                ),
            )
        )
        break
    for pattern in (_DATE_ONLY, _DATE_UPPER):
        for match in pattern.finditer(sql):
            column = match.group(1).split(".")[-1].lower()
            if facts.get(column, {}).get("temporal"):
                out.append(
                    (
                        "WARN",
                        "T3",
                        get_prompt(
                            "nl2sql_check_time_open_end",
                            column=match.group(1),
                            bound=match.group(2),
                        ),
                    )
                )
                return out
    return out


# ---------------------------------------------------------------- 检查器：最值与 NULL


_MAXMIN_SUBQUERY = re.compile(r"(?:=|>=|<=)\s*\(\s*select\s+(max|min)\s*\(", re.IGNORECASE)
_MINMAX_CALL = re.compile(r"\b(?:max|min)\s*\(", re.IGNORECASE)


def _maxmin_findings(scrubbed: str) -> list[tuple[str, str, str]]:
    """MaxMin 检查器。论文只说这是效率优化，但改写会改语义——并列时行数不同，
    所以这里报 WARN 并写清代价，不给「照改就行」的指令。"""
    out: list[tuple[str, str, str]] = []
    hit = _MAXMIN_SUBQUERY.search(scrubbed)
    if hit:
        out.append(("WARN", "M1", get_prompt("nl2sql_check_maxmin_subquery", func=hit.group(1).upper())))
    if not re.search(r"\bover\b", scrubbed, re.IGNORECASE):
        for match in _MINMAX_CALL.finditer(scrubbed):
            end = _call_end(scrubbed, match.end() - 1)
            if end < 0:
                continue
            if _AGG.search(scrubbed[match.end() : end - 1]):
                out.append(("FAIL", "M2", get_prompt("nl2sql_check_nested_agg")))
                break
    return out


_COUNT_COL = re.compile(r"\bcount\s*\(\s*(?:distinct\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*\)", re.IGNORECASE)
_NOT_IN_SUB = re.compile(
    r"\bnot\s+in\s*\(\s*select\s+(?:distinct\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    re.IGNORECASE,
)


def _null_findings(scrubbed: str, facts: dict[str, dict[str, bool]]) -> list[tuple[str, str, str]]:
    """NULL 检查器。只在模式文本明确标了可空时才报——没有证据就不报，
    手工粘贴的 schema 因此拿不到 NULL 保护，这是有意的取舍。"""
    out: list[tuple[str, str, str]] = []

    def nullable(raw: str) -> bool:
        return bool(facts.get(raw.split(".")[-1].lower(), {}).get("nullable"))

    order_part = _clause(scrubbed, r"\border\s+by\b", r"\b(?:limit|offset|fetch|window|union)\b")
    if order_part.strip() and not re.search(r"\bnulls\s+(?:first|last)\b", order_part, re.IGNORECASE):
        risky: list[str] = []
        for item in _split_top_level(order_part):
            head = re.split(r"\s+", item.strip())[0] if item.strip() else ""
            if not head or not nullable(head):
                continue
            guard = re.compile(rf"{re.escape(head)}\s+is\s+not\s+null", re.IGNORECASE)
            if not guard.search(scrubbed):
                risky.append(head)
        if risky:
            out.append(("WARN", "N1", get_prompt("nl2sql_check_null_order", cols="、".join(risky))))
    counted = sorted({match.group(1) for match in _COUNT_COL.finditer(scrubbed) if nullable(match.group(1))})
    if counted:
        out.append(("WARN", "N2", get_prompt("nl2sql_check_null_count", cols="、".join(counted))))
    hit = _NOT_IN_SUB.search(scrubbed)
    if hit:
        if nullable(hit.group(1)):
            out.append(("FAIL", "N3", get_prompt("nl2sql_check_not_in_null", col=hit.group(1))))
        else:
            out.append(("WARN", "N3", get_prompt("nl2sql_check_not_in_check", col=hit.group(1))))
    return out


def _quality_findings(scrubbed: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    has_limit = _limit_of(scrubbed) > 0
    if not has_limit:
        out.append(("WARN", "Q1", get_prompt("nl2sql_check_no_limit")))
    if re.search(r"\bselect\s+\*", scrubbed, re.IGNORECASE):
        out.append(("WARN", "Q2", get_prompt("nl2sql_check_select_star")))
    if re.search(r"\border\s+by\b", scrubbed, re.IGNORECASE) and not has_limit:
        out.append(("WARN", "Q3", get_prompt("nl2sql_check_order_no_limit")))
    for match in re.finditer(r"\blike\b([^)]*)", scrubbed, re.IGNORECASE):
        # 字面量已被 _scrub 抹掉，所以这里看的是「LIKE 后面还剩不剩通配符」
        if "%" not in match.group(1) and "_" not in match.group(1):
            out.append(("WARN", "Q4", get_prompt("nl2sql_check_like")))
            break
    if re.search(r"\bdistinct\b", scrubbed, re.IGNORECASE) and re.search(
        r"\bgroup\s+by\b", scrubbed, re.IGNORECASE
    ):
        out.append(("WARN", "Q5", get_prompt("nl2sql_check_distinct_group")))
    return out


def _select_clause(scrubbed: str) -> str:
    return _clause(scrubbed, r"\bselect\b", r"\bfrom\b")


def _clause(scrubbed: str, head: str, tail: str) -> str:
    start = re.search(head, scrubbed, re.IGNORECASE)
    if not start:
        return ""
    rest = scrubbed[start.end() :]
    stop = re.search(tail, rest, re.IGNORECASE)
    return rest[: stop.start()] if stop else rest


def _bare_columns(select_part: str) -> list[str]:
    """SELECT 里的裸列（非聚合项）。GROUP BY 漏列是 BIRD 上的高频错。"""
    bare: list[str] = []
    for item in _split_top_level(select_part):
        if _AGG.search(item):
            continue
        # `SELECT DISTINCT r.name` / `CASE WHEN … END` 的首词是关键字不是列名，剥掉再看
        stripped = re.sub(r"^\s*distinct\s+", "", item, flags=re.IGNORECASE)
        head = re.split(r"\s+as\s+|\s+", stripped.strip(), flags=re.IGNORECASE)[0]
        name = head.split(".")[-1].strip('"`[]')
        if _IDENT.match(name) and name.lower() not in _NOT_ALIAS:
            bare.append(name)
    return bare


def _ungrouped(bare: Sequence[str], scrubbed: str) -> list[str]:
    """裸列里哪些没进 GROUP BY。

    sqlite 和 mysql 对此不报错，直接从组里挑任意一行的值，结果静默出错——比语法错更危险。
    GROUP BY 用序号（`GROUP BY 1, 2`）时放弃判断，宁可漏报也不误报。
    """
    group_part = _clause(scrubbed, r"\bgroup\s+by\b", r"\b(?:having|order\s+by|limit|window|union)\b")
    if not group_part.strip():
        return []
    grouped: set[str] = set()
    for item in _split_top_level(group_part):
        text = item.strip()
        if not text:
            continue
        if text.isdigit():
            return []  # 序号引用 SELECT 位置，映射不可靠，整条放弃
        grouped.add(text.split(".")[-1].strip('"`[]').lower())
    return [name for name in bare if name.lower() not in grouped]


def _split_top_level(text: str) -> list[str]:
    """按括号深度为 0 的逗号切分，函数调用里的逗号不算分隔。"""
    parts: list[str] = []
    depth = 0
    buffer: list[str] = []
    for char in text + ",":
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    return parts


def sql_check(sql: str, schema: str = "", source: str = "", dialect: str = "") -> str:
    """静态过一遍语法/逻辑/质量三段检查器，出可执行的修正指令。不连库、不执行。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    text = str(sql or "").strip()
    if not text:
        return get_prompt("nl2sql_sql_empty")
    scrubbed = _scrub(text)
    facts = _schema_facts(schema)
    picked = _known_dialect(source, dialect)
    stages = (
        (get_prompt("nl2sql_stage_syntax"), _syntax_findings(text, scrubbed, schema)),
        (
            get_prompt("nl2sql_stage_logic"),
            _logic_findings(scrubbed) + _time_findings(text, scrubbed, picked, facts),
        ),
        (
            get_prompt("nl2sql_stage_quality"),
            _quality_findings(scrubbed) + _maxmin_findings(scrubbed) + _null_findings(scrubbed, facts),
        ),
    )
    blocks: list[str] = []
    fails = 0
    warns = 0
    for label, findings in stages:
        if not findings:
            continue
        rows = []
        for level, code, message in findings:
            fails += level == "FAIL"
            warns += level == "WARN"
            rows.append(get_prompt("nl2sql_check_item", level=level, code=code, issue=message))
        blocks.append(get_prompt("nl2sql_check_stage", stage=label, rows="\n".join(rows)))
    if not blocks:
        return get_prompt("nl2sql_check_ok")
    return get_prompt(
        "nl2sql_check_report",
        fails=str(fails),
        warns=str(warns),
        blocks="\n".join(blocks),
        verdict=get_prompt("nl2sql_check_block" if fails else "nl2sql_check_pass"),
    )


# ---------------------------------------------------------------- 工具：置信选择


def _fingerprint(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """按执行结果聚类，不按 SQL 文本。别名不同但结果一致的候选应该归一类。"""
    body = sorted("\x1f".join(_cell(value, 200) for value in row) for row in rows)
    return f"{len(columns)}\x1e" + "\x1e".join(body)


def sql_pick(sqls: str, source: str = "", limit: int = 0) -> str:
    """跑多个候选 SQL，按执行结果聚类算置信度，给出该选哪个和为什么。"""
    if not _enabled():
        return get_prompt("nl2sql_disabled")
    parts = [item.strip() for item in str(sqls or "").split(";;") if item.strip()]
    if len(parts) < 2:
        return get_prompt("nl2sql_pick_need_two")
    clusters: dict[str, dict[str, Any]] = {}
    failed: list[tuple[int, str]] = []
    for index, candidate in enumerate(parts, start=1):
        try:
            columns, rows, _, _ = _run(source, candidate, limit)
        except ValueError as exc:
            failed.append((index, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            failed.append((index, _brief(exc)))
            continue
        key = _fingerprint(columns, rows)
        bucket = clusters.setdefault(key, {"members": [], "columns": columns, "rows": rows})
        bucket["members"].append(index)
    if not clusters:
        rows = "\n".join(
            get_prompt("nl2sql_pick_failed_item", index=str(index), error=error) for index, error in failed
        )
        return get_prompt("nl2sql_pick_all_failed", count=str(len(parts)), rows=rows)
    ordered = sorted(clusters.values(), key=lambda item: (-len(item["members"]), item["members"][0]))
    total = sum(len(item["members"]) for item in ordered)
    threshold = float(nl2sql_settings().get("conf_threshold", 0.6))
    top = ordered[0]
    conf = len(top["members"]) / total
    rows = "\n".join(
        get_prompt(
            "nl2sql_pick_cluster",
            members=", ".join(f"#{index}" for index in item["members"]),
            conf=f"{len(item['members']) / total:.2f}",
            rows=str(len(item["rows"])),
            preview=_cell(item["rows"][0] if item["rows"] else "", 120),
            flag="" if item["rows"] else get_prompt("nl2sql_pick_empty_flag"),
        )
        for item in ordered
    )
    if failed:
        rows += "\n" + "\n".join(
            get_prompt("nl2sql_pick_failed_item", index=str(index), error=error) for index, error in failed
        )
    if not top["rows"]:
        # Result 检查器：多个写法一致地查空，通常是共用了同一个错约束，不是真的没数据
        verdict = get_prompt("nl2sql_pick_empty")
    elif conf >= threshold and len(ordered) == 1:
        verdict = get_prompt("nl2sql_pick_high")
    else:
        verdict = get_prompt("nl2sql_pick_review")
    logger.info("候选聚类 source=%s clusters=%s conf=%.2f", _spec(source)["name"], len(ordered), conf)
    return get_prompt(
        "nl2sql_pick_report",
        total=str(len(parts)),
        ok=str(total),
        clusters=str(len(ordered)),
        pick=f"#{top['members'][0]}",
        conf=f"{conf:.2f}",
        rows=rows,
        verdict=verdict,
        table=_markdown(top["columns"], top["rows"]),
    )


# ---------------------------------------------------------------- 注册


_SOURCE = {"source": {"type": "string", "description": get_prompt("nl2sql_param_source")}}
_LIMIT_ARG = {"limit": {"type": "integer", "description": get_prompt("nl2sql_param_limit")}}


def _register(name: str, func: Any, properties: dict[str, Any], required: list[str] | None = None) -> None:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    register_tool(
        ToolSpec(
            name=name,
            description=get_prompt(f"tool_desc_{name}"),
            parameters=parameters,
            func=func,
        )
    )


_register("sql_sources", sql_sources, {})
_register(
    "sql_schema",
    sql_schema,
    {
        **_SOURCE,
        "tables": {"type": "string", "description": get_prompt("nl2sql_param_tables")},
        "samples": {"type": "integer", "description": get_prompt("nl2sql_param_samples")},
    },
)
_register(
    "sql_tables",
    sql_tables,
    {
        **_SOURCE,
        **_LIMIT_ARG,
        "question": {"type": "string", "description": get_prompt("nl2sql_param_question")},
    },
    ["question"],
)
_register(
    "sql_values",
    sql_values,
    {
        **_SOURCE,
        **_LIMIT_ARG,
        "table": {"type": "string", "description": get_prompt("nl2sql_param_table")},
        "column": {"type": "string", "description": get_prompt("nl2sql_param_column")},
        "keyword": {"type": "string", "description": get_prompt("nl2sql_param_keyword")},
    },
    ["table", "column"],
)
_register(
    "sql_run",
    sql_run,
    {
        **_SOURCE,
        **_LIMIT_ARG,
        "sql": {"type": "string", "description": get_prompt("nl2sql_param_sql")},
    },
    ["sql"],
)
_register(
    "sql_export",
    sql_export,
    {
        **_SOURCE,
        **_LIMIT_ARG,
        "sql": {"type": "string", "description": get_prompt("nl2sql_param_sql")},
        "path": {"type": "string", "description": get_prompt("nl2sql_param_path")},
    },
    ["sql", "path"],
)
_register(
    "sql_check",
    sql_check,
    {
        **_SOURCE,
        "sql": {"type": "string", "description": get_prompt("nl2sql_param_sql")},
        "schema": {"type": "string", "description": get_prompt("nl2sql_param_schema")},
        "dialect": {"type": "string", "description": get_prompt("nl2sql_param_dialect")},
    },
    ["sql"],
)
_register(
    "sql_pick",
    sql_pick,
    {
        **_SOURCE,
        **_LIMIT_ARG,
        "sqls": {"type": "string", "description": get_prompt("nl2sql_param_sqls")},
    },
    ["sqls"],
)
