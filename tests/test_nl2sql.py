"""nl2sql 插件与技能的验收测试。口径见 docs/change_maintenance/nl2sql/README.md 第六节。"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from witty_agent.approval import DANGEROUS_TOOLS
from witty_agent.kernel_surface import KERNEL_TOOLS
from witty_agent.loop import READONLY_TOOLS
from witty_agent.plugins import nl2sql as sql
from witty_agent.prompts import get_prompt
from witty_agent.runtime import clear_runtime_cache
from witty_agent.skills import list_system_skills, load_skill, match_relevant_skills
from witty_agent.tools import list_tools

_TOOLS = (
    "sql_sources",
    "sql_schema",
    "sql_tables",
    "sql_values",
    "sql_run",
    "sql_export",
    "sql_check",
    "sql_pick",
)

# 建表语句里的行尾 `-- 说明` 就是 sqlite 唯一的列注释来源，测试要覆盖到
_FIXTURE = """
CREATE TABLE station (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,     -- 变电站名称
  region TEXT,            -- 所属供电区域
  status INTEGER          -- 1=在运 0=停运
);
CREATE TABLE load_record (
  id INTEGER PRIMARY KEY,
  station_id INTEGER,     -- 关联变电站
  stat_month TEXT,        -- 统计月份 YYYY-MM
  load_mw REAL,           -- 有功负荷 兆瓦
  FOREIGN KEY (station_id) REFERENCES station(id)
);
INSERT INTO station VALUES (1, '江北新区变', '江北新区', 1);
INSERT INTO station VALUES (2, '城南变', '市区', 1);
INSERT INTO station VALUES (3, '试运行变', '市区', 0);
INSERT INTO load_record VALUES (1, 1, '2026-01', 120.5);
INSERT INTO load_record VALUES (2, 1, '2026-02', 131.0);
INSERT INTO load_record VALUES (3, 2, '2026-01', 88.0);
INSERT INTO load_record VALUES (4, 2, '2026-02', 91.5);
"""


def _make_db(directory: Path) -> str:
    path = directory / "demo.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_FIXTURE)
        conn.commit()
    finally:
        conn.close()
    return str(path)


class Nl2sqlFixtureCase(unittest.TestCase):
    """每个用例一份临时库。source 直接传文件路径，不依赖 runtime 里登记的数据源。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = _make_db(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        clear_runtime_cache()


class SchemaTests(Nl2sqlFixtureCase):
    def test_m_schema_carries_comments_pk_and_foreign_keys(self) -> None:
        body = sql.sql_schema(self.db)
        self.assertIn("【DB_ID】 demo", body)
        self.assertIn("【Schema】", body)
        self.assertIn("# Table: station", body)
        self.assertIn("# Table: load_record", body)
        # 注释、类型、主键标记都要在，模型靠这些判断口径
        self.assertIn("(name:TEXT, 变电站名称", body)
        self.assertIn("(load_mw:REAL, 有功负荷 兆瓦", body)
        self.assertIn("(id:INTEGER" + get_prompt("nl2sql_schema_pk"), body)
        self.assertIn("【Foreign keys】", body)
        self.assertIn("load_record.station_id=station.id", body)
        # 不给 samples 就不该带取值，省 token
        self.assertNotIn("取值示例", body)

    def test_table_filter_and_samples(self) -> None:
        body = sql.sql_schema(self.db, tables="station", samples=3)
        self.assertIn("# Table: station", body)
        self.assertNotIn("# Table: load_record", body)
        self.assertIn("江北新区", body)
        self.assertIn("取值示例", body)
        self.assertIn(get_prompt("nl2sql_no_tables", tables="没这张表"), sql.sql_schema(self.db, tables="没这张表"))

    def test_missing_database_is_reported_not_raised(self) -> None:
        missing = str(self.root / "nope.db")
        self.assertEqual(sql.sql_schema(missing), get_prompt("nl2sql_db_missing", path=missing))

    def test_tables_ranked_by_surface_tokens_with_reasons(self) -> None:
        report = sql.sql_tables("各区变电站的负荷趋势", source=self.db)
        self.assertIn("station", report)
        self.assertIn("load_record", report)
        # 命中理由必须列出来，否则模型无法判断这是词面碰巧还是真相关
        self.assertRegex(report, r"←\s*命中")
        blank = sql.sql_tables("zzz 完全无关的词", source=self.db)
        self.assertIn(get_prompt("nl2sql_tables_empty", count="2"), blank)
        self.assertEqual(sql.sql_tables("   ", source=self.db), get_prompt("nl2sql_question_empty"))

    def test_value_sampling_aligns_wording(self) -> None:
        # 用户说「江北」，库里写「江北新区」——值采样是把两者对上的唯一正当手段
        hit = sql.sql_values("station", "region", source=self.db, keyword="江北")
        self.assertIn("江北新区", hit)
        miss = sql.sql_values("station", "region", source=self.db, keyword="西湖区")
        self.assertIn(get_prompt("nl2sql_values_empty", table="station", column="region", keyword="西湖区"), miss)
        # 表名列名直接进 SQL，必须先过标识符白名单
        bad = sql.sql_values('station"; DROP TABLE station; --', "region", source=self.db)
        self.assertIn(get_prompt("nl2sql_ident_bad", table='station"; DROP TABLE station; --', column="region"), bad)


class ReadOnlyTests(Nl2sqlFixtureCase):
    def test_write_statements_are_rejected(self) -> None:
        cases = (
            "INSERT INTO station VALUES (9, 'x', 'y', 1)",
            "UPDATE station SET status = 0",
            "DELETE FROM station",
            "DROP TABLE station",
            "ALTER TABLE station ADD COLUMN note TEXT",
            "CREATE TABLE t (a INT)",
            "TRUNCATE TABLE station",
            "ATTACH DATABASE '/tmp/other.db' AS o",
            "PRAGMA table_info(station)",
            "VACUUM",
            "GRANT SELECT ON station TO reader",
        )
        for statement in cases:
            with self.subTest(sql=statement):
                out = sql.sql_run(statement, source=self.db)
                self.assertNotIn("| id |", out)
                self.assertTrue(
                    out.startswith("拒绝执行"),
                    f"没拦住：{statement} → {out[:80]}",
                )
        # CTE 后面挂写操作（Postgres 允许）也要拦
        cte = sql.sql_run("WITH x AS (SELECT 1) DELETE FROM station", source=self.db)
        self.assertEqual(cte, get_prompt("nl2sql_blocked_keyword", word="DELETE"))
        # 多条语句、括号不配平
        self.assertEqual(
            sql.sql_run("SELECT 1; SELECT 2", source=self.db),
            get_prompt("nl2sql_multi_statement"),
        )
        self.assertEqual(
            sql.sql_run("SELECT count(id FROM station", source=self.db),
            get_prompt("nl2sql_unbalanced"),
        )
        self.assertEqual(sql.sql_run("   ", source=self.db), get_prompt("nl2sql_sql_empty"))

    def test_write_keyword_inside_string_literal_passes(self) -> None:
        # 字面量里的 DROP 不是写操作，抹字面量后再判是为了不误伤正常查询
        out = sql.sql_run("SELECT name FROM station WHERE name = 'DROP TABLE station'", source=self.db)
        self.assertIn("0 行", out)
        self.assertNotIn("拒绝执行", out)
        commented = sql.sql_run("SELECT name FROM station -- delete 掉停运的\nLIMIT 2", source=self.db)
        self.assertIn("2 行", commented)

    def test_database_is_opened_read_only(self) -> None:
        # SQL 白名单之外的第二道防线：连接本身是 mode=ro
        with sql._connect(self.db) as (conn, dialect):
            self.assertEqual(dialect, "sqlite")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE station SET status = 0")


class LimitTests(Nl2sqlFixtureCase):
    def test_limit_is_forced_even_when_user_wants_everything(self) -> None:
        out = sql.sql_run("SELECT id, name FROM station", source=self.db)
        self.assertIn("LIMIT 1000", out)
        self.assertIn(get_prompt("nl2sql_limit_added", now="1000"), out)

    def test_oversized_limit_is_lowered(self) -> None:
        out = sql.sql_run("SELECT id FROM station LIMIT 999999", source=self.db)
        executed = out.split("```sql", 1)[1].split("```", 1)[0]
        self.assertIn("LIMIT 1000", executed)
        # 改写过的上限不许留在实际执行的 SQL 里，但提示要说清改了什么
        self.assertNotIn("999999", executed)
        self.assertIn(get_prompt("nl2sql_limit_lowered", was="999999", now="1000"), out)

    def test_small_limit_is_left_alone(self) -> None:
        out = sql.sql_run("SELECT id FROM station LIMIT 2", source=self.db)
        self.assertIn("2 行", out)
        self.assertEqual(out.count("LIMIT"), 1)

    def test_subquery_limit_does_not_count_as_top_level(self) -> None:
        body, note = sql._cap_limit(
            "SELECT id FROM (SELECT id FROM station LIMIT 1) t",
            "sqlite",
            50,
        )
        self.assertTrue(body.endswith("LIMIT 50"))
        self.assertEqual(note, get_prompt("nl2sql_limit_added", now="50"))

    def test_fetch_first_dialects_do_not_get_limit(self) -> None:
        for dialect in ("oracle", "dm", "mssql"):
            with self.subTest(dialect=dialect):
                body, _ = sql._cap_limit("SELECT id FROM station", dialect, 100)
                self.assertTrue(body.endswith("FETCH FIRST 100 ROWS ONLY"))
                self.assertNotIn("LIMIT", body.upper())
        # 已经带 FETCH FIRST 的不重复加
        body, note = sql._cap_limit("SELECT id FROM station FETCH FIRST 10 ROWS ONLY", "oracle", 100)
        self.assertEqual(note, "")
        self.assertEqual(body.upper().count("FETCH"), 1)

    def test_commented_out_limit_is_not_a_limit(self) -> None:
        self.assertEqual(sql._limit_of(sql._scrub("SELECT id FROM station -- LIMIT 5")), 0)
        self.assertEqual(sql._limit_of(sql._scrub("SELECT id FROM station LIMIT 5")), 5)

    def test_max_limit_caps_the_requested_limit(self) -> None:
        out = sql.sql_run("SELECT id FROM station", source=self.db, limit=999999)
        self.assertIn("LIMIT 10000", out)


class CheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = "【Schema】\n# Table: station\n[\n(id:INTEGER),\n(name:TEXT),\n]"

    def _codes(self, text: str) -> set[str]:
        return set(re.findall(r"- \[(?:FAIL|WARN)\] (\w+)", text))

    def test_clean_sql_passes_all_three_stages(self) -> None:
        out = sql.sql_check("SELECT id, name FROM station WHERE status = 1 LIMIT 10", self.schema)
        self.assertEqual(out, get_prompt("nl2sql_check_ok"))

    def test_unknown_table_is_a_fail(self) -> None:
        out = sql.sql_check("SELECT id FROM transformer LIMIT 10", self.schema)
        self.assertIn("S3", self._codes(out))
        self.assertIn(get_prompt("nl2sql_check_block"), out)

    def test_alias_is_not_mistaken_for_unknown_table(self) -> None:
        out = sql.sql_check("SELECT s.id FROM station s WHERE s.status = 1 LIMIT 10", self.schema)
        self.assertNotIn("S3", self._codes(out))
        self.assertNotIn("L6", self._codes(out))

    def test_logic_stage_catches_cross_join_group_by_and_having(self) -> None:
        cross = sql.sql_check("SELECT a.id FROM station a JOIN load_record b LIMIT 10")
        self.assertIn("L1", self._codes(cross))
        group = sql.sql_check("SELECT region, count(*) FROM station LIMIT 10")
        self.assertIn("L2", self._codes(group))
        self.assertIn("region", group)
        where_agg = sql.sql_check("SELECT region FROM station WHERE count(*) > 1 LIMIT 10")
        self.assertIn("L3", self._codes(where_agg))

    def test_l2_catches_column_missing_from_group_by(self) -> None:
        # sqlite/mysql 不报错，会从组里随便挑一行的值 —— 静默出错，比语法错更危险
        out = sql.sql_check(
            "SELECT s.region, r.stat_month, sum(r.load_mw) FROM station s "
            "JOIN load_record r ON r.station_id = s.id GROUP BY s.region LIMIT 10"
        )
        self.assertIn("L2", self._codes(out))
        self.assertIn("stat_month", out)
        self.assertNotIn("region", out.split("stat_month")[0].split("[FAIL] L2")[-1])

    def test_l2_silent_when_group_by_covers_every_bare_column(self) -> None:
        for sql_text in (
            "SELECT s.region, r.stat_month, sum(r.load_mw) FROM station s "
            "JOIN load_record r ON r.station_id = s.id GROUP BY s.region, r.stat_month LIMIT 10",
            # 序号引用 SELECT 位置，映射不可靠：宁可漏报也不误报
            "SELECT region, stat_month, count(*) FROM load_record GROUP BY 1, 2 LIMIT 10",
            # 表达式分组，裸列提取本就不认表达式
            "SELECT substr(stat_month, 1, 4) AS y, count(*) FROM load_record "
            "GROUP BY substr(stat_month, 1, 4) LIMIT 10",
            # HAVING / ORDER BY 不该被当成分组列
            "SELECT region, count(*) AS n FROM station GROUP BY region HAVING count(*) > 2 "
            "ORDER BY n DESC LIMIT 10",
        ):
            with self.subTest(sql=sql_text[:48]):
                self.assertNotIn("L2", self._codes(sql.sql_check(sql_text)))

    def test_l2_does_not_mistake_keywords_for_bare_columns(self) -> None:
        # DISTINCT / CASE 是首词但不是列名，报出来就是噪声（会误判成阻断级 FAIL）
        distinct = sql.sql_check(
            "SELECT DISTINCT region, count(*) FROM station GROUP BY region LIMIT 10"
        )
        self.assertNotIn("L2", self._codes(distinct))  # Q5 里提到 DISTINCT 是另一回事，别一起断言
        case_when = sql.sql_check(
            "SELECT CASE WHEN status = 1 THEN '在运' ELSE '停运' END AS 状态, count(*) "
            "FROM station GROUP BY status LIMIT 10"
        )
        self.assertNotIn("L2", self._codes(case_when))
        self.assertNotIn("CASE", case_when)


    def test_fanout_warns_on_additive_aggregates_only(self) -> None:        # SUM/COUNT 会被一对多连接放大；AVG 不会，报了就是噪声
        additive = sql.sql_check(
            "SELECT s.region, sum(r.load_mw) FROM station s JOIN load_record r ON r.station_id = s.id "
            "GROUP BY s.region LIMIT 10"
        )
        self.assertIn("L5", self._codes(additive))
        averaged = sql.sql_check(
            "SELECT s.region, avg(r.load_mw) FROM station s JOIN load_record r ON r.station_id = s.id "
            "GROUP BY s.region LIMIT 10"
        )
        self.assertNotIn("L5", self._codes(averaged))

    def test_quality_stage_warns_without_blocking(self) -> None:
        out = sql.sql_check("SELECT * FROM station ORDER BY name")
        codes = self._codes(out)
        self.assertIn("Q1", codes)
        self.assertIn("Q2", codes)
        self.assertIn("Q3", codes)
        # 只有 WARN 时结论是「可以执行但要说明」，不是拦下
        self.assertIn(get_prompt("nl2sql_check_pass"), out)
        self.assertNotIn(get_prompt("nl2sql_check_block"), out)

    def test_write_sql_fails_the_syntax_stage(self) -> None:
        out = sql.sql_check("DELETE FROM station")
        self.assertIn("S1", self._codes(out))
        self.assertIn(get_prompt("nl2sql_check_block"), out)
        self.assertEqual(sql.sql_check(""), get_prompt("nl2sql_sql_empty"))


class PaperCheckerTests(unittest.TestCase):
    """DeepEye Table 2 里第一轮没实现的 4 个检查器 + JOIN/ORDER-BY 补到论文口径。"""

    def setUp(self) -> None:
        # 可空标记是 NULL 检查器唯一的证据来源，没有它这几项一律不报
        self.schema = (
            "【Schema】\n# Table: station\n[\n"
            "(id:INTEGER, 主键),\n"
            "(name:TEXT),\n"
            "(region:TEXT, 可空),\n"
            "(status:INTEGER, 可空),\n"
            "(built_at:DATETIME, 可空),\n]"
        )

    def _codes(self, text: str) -> set[str]:
        return set(re.findall(r"- \[(?:FAIL|WARN)\] (\w+)", text))

    def _check(self, text: str, dialect: str = "sqlite") -> set[str]:
        return self._codes(sql.sql_check(text, self.schema, dialect=dialect))

    def test_join_checker_flags_the_two_patterns_the_paper_names(self) -> None:
        # ON a = b OR …：合法 SQL，但引擎退化成近似笛卡尔积再过滤
        with_or = self._check(
            "SELECT s.id FROM station s JOIN station t ON s.id = t.id OR s.name = t.name LIMIT 10"
        )
        self.assertIn("L7", with_or)
        # ON col IN (SELECT …)：连接被写成半连接，粒度变了
        with_sub = self._check(
            "SELECT s.id FROM station s JOIN station t ON t.id IN (SELECT id FROM station) LIMIT 10"
        )
        self.assertIn("L8", with_sub)
        plain = self._check("SELECT s.id FROM station s JOIN station t ON s.id = t.id LIMIT 10")
        self.assertNotIn("L7", plain)
        self.assertNotIn("L8", plain)

    def test_order_by_checker_separates_dead_sort_from_tie_risk(self) -> None:
        # 没有 GROUP BY：聚合把全表压成一行，ORDER BY 和 LIMIT 都是摆设 —— 确定的错
        dead = sql.sql_check("SELECT region FROM station ORDER BY count(*) DESC LIMIT 1", self.schema)
        self.assertIn("L9", self._codes(dead))
        self.assertIn(get_prompt("nl2sql_check_block"), dead)
        # 有 GROUP BY 只是并列风险，要问用户，不该拦
        tie = sql.sql_check(
            "SELECT region, count(*) AS n FROM station GROUP BY region ORDER BY count(*) DESC LIMIT 1",
            self.schema,
        )
        self.assertIn("L10", self._codes(tie))
        self.assertNotIn("L9", self._codes(tie))
        self.assertNotIn(get_prompt("nl2sql_check_block"), tie)
        # 取前 10 名不存在「被 LIMIT 1 随机砍掉」的问题
        many = self._check(
            "SELECT region, count(*) AS n FROM station GROUP BY region ORDER BY count(*) DESC LIMIT 10"
        )
        self.assertNotIn("L10", many)

    def test_aggregate_inside_subquery_is_not_an_error(self) -> None:
        # 阻断性 FAIL 打在正确的 SQL 上比漏报贵：这两条都合法，一个都不许报
        legal = self._check("SELECT id FROM station WHERE id = (SELECT max(id) FROM station) LIMIT 10")
        self.assertNotIn("L3", legal)
        ordered = self._check(
            "SELECT id FROM station ORDER BY (SELECT count(*) FROM station) LIMIT 10"
        )
        self.assertNotIn("L9", ordered)
        # 真的把聚合写进 WHERE 仍然要拦
        self.assertIn("L3", self._check("SELECT region FROM station WHERE count(*) > 1 LIMIT 10"))

    def test_time_checker_is_dialect_aware(self) -> None:
        # 同一条 SQL 在 mysql 上对、在 sqlite/postgres 上直接报错
        self.assertIn("T1", self._check("SELECT YEAR(built_at) FROM station LIMIT 10", "sqlite"))
        self.assertIn("T1", self._check("SELECT YEAR(built_at) FROM station LIMIT 10", "postgres"))
        self.assertNotIn("T1", self._check("SELECT YEAR(built_at) AS y FROM station LIMIT 10", "mysql"))
        self.assertIn(
            "T1", self._check("SELECT STRFTIME('%Y', built_at) FROM station LIMIT 10", "postgres")
        )
        self.assertNotIn(
            "T1", self._check("SELECT STRFTIME('%Y', built_at) FROM station LIMIT 10", "sqlite")
        )
        # 方言未知时整条跳过：猜方言会对着正确的 SQL 报 FAIL
        self.assertNotIn("T1", self._check("SELECT YEAR(built_at) FROM station LIMIT 10", ""))

    def test_time_checker_catches_text_compared_with_number(self) -> None:
        # sqlite 里 strftime 返回文本，和数字比永远不成立 —— 静默 0 行
        hit = self._check(
            "SELECT id FROM station WHERE STRFTIME('%Y', built_at) = 2025 LIMIT 10", "sqlite"
        )
        self.assertIn("T2", hit)
        ok = self._check(
            "SELECT id FROM station WHERE STRFTIME('%Y', built_at) = '2025' LIMIT 10", "sqlite"
        )
        self.assertNotIn("T2", ok)
        cast = self._check(
            "SELECT id FROM station WHERE CAST(STRFTIME('%Y', built_at) AS INTEGER) = 2025 LIMIT 10",
            "sqlite",
        )
        self.assertNotIn("T2", cast)

    def test_time_checker_flags_date_only_bound_on_datetime_column(self) -> None:
        # built_at 带时分秒，右端点写日期等于只取到当天 00:00:00
        self.assertIn(
            "T3",
            self._check(
                "SELECT id FROM station WHERE built_at BETWEEN '2025-01-01' AND '2025-06-30' LIMIT 10"
            ),
        )
        self.assertIn(
            "T3", self._check("SELECT id FROM station WHERE built_at <= '2025-06-30' LIMIT 10")
        )
        # name 不是时间类型，同样的写法不该报
        self.assertNotIn(
            "T3",
            self._check("SELECT id FROM station WHERE name BETWEEN '2025-01-01' AND '2025-06-30' LIMIT 10"),
        )
        # 模式文本没给（拿不到列类型）就不报
        self.assertNotIn(
            "T3",
            self._codes(
                sql.sql_check(
                    "SELECT id FROM station WHERE built_at <= '2025-06-30' LIMIT 10", dialect="sqlite"
                )
            ),
        )

    def test_maxmin_checker_warns_without_ordering_a_blind_rewrite(self) -> None:
        out = sql.sql_check(
            "SELECT name FROM station WHERE id = (SELECT MAX(id) FROM station) LIMIT 10", self.schema
        )
        self.assertIn("M1", self._codes(out))
        # 论文只说这是效率优化；改写会改并列时的行数，文案必须写清代价
        self.assertIn("并列", out)
        self.assertNotIn(get_prompt("nl2sql_check_block"), out)

    def test_maxmin_checker_blocks_nested_aggregates(self) -> None:
        nested = sql.sql_check("SELECT MAX(count(*)) FROM station GROUP BY region LIMIT 10", self.schema)
        self.assertIn("M2", self._codes(nested))
        self.assertIn(get_prompt("nl2sql_check_block"), nested)
        # 窗口函数里的嵌套是另一回事，不许一起拦
        self.assertNotIn(
            "M2",
            self._check("SELECT MAX(count(*)) OVER () FROM station GROUP BY region LIMIT 10"),
        )
        self.assertNotIn("M2", self._check("SELECT max(load_mw) FROM station LIMIT 10"))

    def test_null_checker_needs_evidence_from_the_schema(self) -> None:
        # region 标了可空 → 排序取头尾会先拿到一堆 NULL
        self.assertIn("N1", self._check("SELECT name, region FROM station ORDER BY region DESC LIMIT 10"))
        # 有守卫或显式 NULLS LAST 就不报
        self.assertNotIn(
            "N1",
            self._check(
                "SELECT name FROM station WHERE region IS NOT NULL ORDER BY region DESC LIMIT 10"
            ),
        )
        self.assertNotIn(
            "N1", self._check("SELECT name FROM station ORDER BY region DESC NULLS LAST LIMIT 10")
        )
        # name 没标可空 → 没有证据，不报
        self.assertNotIn("N1", self._check("SELECT name FROM station ORDER BY name LIMIT 10"))
        # 手工粘贴的 schema 拿不到可空信息，整组 NULL 检查沉默（有意的取舍）
        self.assertNotIn(
            "N1",
            self._codes(
                sql.sql_check("SELECT name, region FROM station ORDER BY region DESC LIMIT 10")
            ),
        )

    def test_null_checker_flags_count_on_nullable_column(self) -> None:
        self.assertIn("N2", self._check("SELECT count(region) FROM station LIMIT 10"))
        self.assertNotIn("N2", self._check("SELECT count(*) FROM station LIMIT 10"))
        self.assertNotIn("N2", self._check("SELECT count(name) FROM station LIMIT 10"))

    def test_not_in_with_nullable_subquery_column_is_a_fail(self) -> None:
        # 子查询含一个 NULL 就整条恒为未知、静默返回 0 行 —— 比语法错危险
        hit = sql.sql_check(
            "SELECT name FROM station WHERE id NOT IN (SELECT status FROM station) LIMIT 10", self.schema
        )
        self.assertIn("N3", self._codes(hit))
        self.assertIn(get_prompt("nl2sql_check_block"), hit)
        # 可空性未知时降级成 WARN，不拦
        unknown = sql.sql_check(
            "SELECT name FROM station WHERE id NOT IN (SELECT id FROM station) LIMIT 10", self.schema
        )
        self.assertIn("N3", self._codes(unknown))
        self.assertNotIn(get_prompt("nl2sql_check_block"), unknown)
        self.assertNotIn(
            "N3",
            self._check("SELECT name FROM station WHERE id IN (SELECT status FROM station) LIMIT 10"),
        )

    def test_dialect_comes_from_a_registered_source_but_is_never_guessed(self) -> None:
        registered = {
            "enabled": True,
            "default_limit": 1000,
            "max_limit": 10000,
            "max_tables": 12,
            "conf_threshold": 0.6,
            "sources": [{"name": "ops", "dialect": "postgres", "dsn": "postgres://h/db"}],
        }
        with patch.object(sql, "nl2sql_settings", return_value=registered):
            self.assertEqual(sql._known_dialect("ops", ""), "postgres")
            # 未登记的源名不猜方言：猜错就会对正确的 SQL 报 FAIL
            self.assertEqual(sql._known_dialect("unknown-source", ""), "")
            # 显式传的方言优先
            self.assertEqual(sql._known_dialect("ops", "mysql"), "mysql")


class ResultCheckerTests(Nl2sqlFixtureCase):
    """DeepEye Table 2 的 Result 检查器：0 行要触发约束复查，不是答案。"""

    def test_empty_result_lists_the_constraints_to_recheck(self) -> None:
        out = sql.sql_run(
            "SELECT s.name FROM station s JOIN load_record r ON r.station_id = s.id "
            "WHERE s.region = '江北' AND r.stat_month BETWEEN '2026-01' AND '2026-06' AND s.status = 1",
            source=self.db,
        )
        self.assertIn("0 行", out)
        # 字面量必须原样出现：'江北' 对不上 '江北新区' 是最常见的一条
        self.assertIn("s.region = '江北'", out)
        # BETWEEN 自带的 AND 不是分隔符，整条要留完整
        self.assertIn("r.stat_month BETWEEN '2026-01' AND '2026-06'", out)
        self.assertIn("r.station_id = s.id", out)

    def test_literal_and_is_not_mistaken_for_a_separator(self) -> None:
        out = sql.sql_run("SELECT id FROM station WHERE name = 'A AND B'", source=self.db)
        self.assertIn("name = 'A AND B'", out)

    def test_non_empty_result_carries_no_result_checker_block(self) -> None:
        out = sql.sql_run("SELECT count(*) AS n FROM station", source=self.db)
        self.assertNotIn("Result", out)

    def test_unanimous_empty_result_loses_the_high_confidence_shortcut(self) -> None:
        # 多个写法一致地查空，通常是共用了同一个错约束，不是真没数据
        out = sql.sql_pick(
            "SELECT name FROM station WHERE region = '江北'"
            ";;"
            "SELECT s.name FROM station s WHERE s.region LIKE '江北'",
            source=self.db,
        )
        self.assertIn("置信度 1.00", out)
        self.assertIn(get_prompt("nl2sql_pick_empty"), out)
        self.assertNotIn(get_prompt("nl2sql_pick_high"), out)

    def test_nullable_columns_are_marked_in_the_schema_text(self) -> None:
        out = sql.sql_schema(source=self.db, tables="station")
        marker = get_prompt("nl2sql_schema_nullable")
        # name 是 NOT NULL、id 是主键，两者都不该带标记；region 可空要带
        self.assertIn(f"(region:TEXT, 所属供电区域{marker})", out)
        self.assertNotIn(f"(name:TEXT, 变电站名称{marker})", out)
        self.assertNotIn(f"主键{marker}", out)
        facts = sql._schema_facts(out)
        self.assertTrue(facts["region"]["nullable"])
        self.assertFalse(facts["name"]["nullable"])
        self.assertFalse(facts["id"]["nullable"])


class PickTests(Nl2sqlFixtureCase):
    def test_same_result_different_writing_forms_one_cluster(self) -> None:
        out = sql.sql_pick(
            "SELECT count(*) AS n FROM station WHERE status = 1"
            ";;"
            "SELECT count(id) AS n FROM station WHERE status <> 0",
            source=self.db,
        )
        self.assertIn("结果簇 1 个", out)
        self.assertIn("置信度 1.00", out)
        self.assertIn(get_prompt("nl2sql_pick_high"), out)

    def test_different_results_split_and_refuse_to_vote(self) -> None:
        # 第三条漏了 status 过滤，把停运站算进来了——多簇必须要求人复核，不能按票数选
        out = sql.sql_pick(
            "SELECT count(*) AS n FROM station WHERE status = 1"
            ";;"
            "SELECT count(id) AS n FROM station WHERE status <> 0"
            ";;"
            "SELECT count(*) AS n FROM station",
            source=self.db,
        )
        self.assertIn("结果簇 2 个", out)
        self.assertIn("置信度 0.67", out)
        self.assertIn(get_prompt("nl2sql_pick_review"), out)
        self.assertNotIn(get_prompt("nl2sql_pick_high"), out)

    def test_join_fanout_candidate_lands_in_its_own_cluster(self) -> None:
        out = sql.sql_pick(
            "SELECT count(DISTINCT s.id) AS n FROM station s "
            "JOIN load_record r ON r.station_id = s.id"
            ";;"
            "SELECT count(s.id) AS n FROM station s JOIN load_record r ON r.station_id = s.id",
            source=self.db,
        )
        self.assertIn("结果簇 2 个", out)
        self.assertIn(get_prompt("nl2sql_pick_review"), out)

    def test_all_failed_sends_you_back_to_phase_one(self) -> None:
        out = sql.sql_pick("SELECT x FROM nope;;SELECT y FROM nope2", source=self.db)
        self.assertIn("全部执行失败", out)
        self.assertIn("回去重看 schema", out)

    def test_single_candidate_is_refused(self) -> None:
        self.assertEqual(
            sql.sql_pick("SELECT 1", source=self.db),
            get_prompt("nl2sql_pick_need_two"),
        )

    def test_failed_candidate_is_listed_beside_the_clusters(self) -> None:
        out = sql.sql_pick(
            "SELECT count(*) AS n FROM station"
            ";;"
            "SELECT count(*) AS n FROM station"
            ";;"
            "SELECT n FROM nope",
            source=self.db,
        )
        self.assertIn("结果簇 1 个", out)
        self.assertIn("#3 失败", out)
        # 失败的候选不算进分母，置信度是「成功候选里的一致比例」
        self.assertIn("执行成功 2 个", out)
        self.assertIn("置信度 1.00", out)


class ExportTests(Nl2sqlFixtureCase):
    def test_csv_lands_with_header_and_rows(self) -> None:
        target = self.root / "out" / "result.csv"
        out = sql.sql_export("SELECT stat_month, load_mw FROM load_record", str(target), source=self.db)
        self.assertIn("已导出", out)
        self.assertTrue(target.is_file())
        text = target.read_text(encoding="utf-8-sig")
        self.assertEqual(text.splitlines()[0], "stat_month,load_mw")
        self.assertEqual(len(text.strip().splitlines()), 5)
        self.assertIn("LIMIT 1000", out)

    def test_non_csv_suffix_is_refused(self) -> None:
        target = self.root / "result.png"
        out = sql.sql_export("SELECT id FROM station", str(target), source=self.db)
        self.assertEqual(out, get_prompt("nl2sql_export_suffix", path=str(target)))
        self.assertFalse(target.exists())

    def test_write_sql_cannot_sneak_through_export(self) -> None:
        target = self.root / "evil.csv"
        out = sql.sql_export("DELETE FROM station", str(target), source=self.db)
        self.assertEqual(out, get_prompt("nl2sql_not_readonly", head="DELETE"))
        self.assertFalse(target.exists())

    def test_relative_path_lands_in_the_workspace(self) -> None:
        with patch.object(sql.hooks, "current_workspace", str(self.root)):
            out = sql.sql_export("SELECT id FROM station", "rel.csv", source=self.db)
        self.assertIn("已导出", out)
        self.assertTrue((self.root / "rel.csv").is_file())


class SourceTests(Nl2sqlFixtureCase):
    def test_registered_sources_are_listed_without_passwords(self) -> None:
        sources = [
            {"name": "demo", "dialect": "sqlite", "dsn": self.db, "comment": "样例库"},
            {
                "name": "ops",
                "dialect": "mysql",
                "dsn": "mysql://reader@10.0.0.9:3306/ops",
                "password_env": "WITTY_TEST_OPS_PASSWORD",
            },
        ]
        with patch.object(sql, "nl2sql_settings", return_value={"enabled": True, "sources": sources}):
            out = sql.sql_sources()
        self.assertIn("demo", out)
        self.assertIn("ops", out)
        self.assertIn("样例库", out)
        # 口令只存在于环境变量里，列表里连变量名带的值都不该出现
        self.assertNotIn("WITTY_TEST_OPS_PASSWORD", out)

    def test_empty_registry_tells_you_how_to_add_one(self) -> None:
        with patch.object(sql, "nl2sql_settings", return_value={"enabled": True, "sources": []}):
            self.assertEqual(sql.sql_sources(), get_prompt("nl2sql_sources_empty"))

    def test_multiple_sources_without_a_pick_is_ambiguous(self) -> None:
        sources = [
            {"name": "a", "dialect": "sqlite", "dsn": self.db},
            {"name": "b", "dialect": "sqlite", "dsn": self.db},
        ]
        with patch.object(sql, "nl2sql_settings", return_value={"enabled": True, "sources": sources}):
            self.assertEqual(
                sql.sql_schema(""),
                get_prompt("nl2sql_source_ambiguous", names="a, b"),
            )

    def test_unknown_dialect_and_missing_driver_are_honest(self) -> None:
        weird = [{"name": "w", "dialect": "cassandra", "dsn": "x"}]
        with patch.object(sql, "nl2sql_settings", return_value={"enabled": True, "sources": weird}):
            self.assertEqual(
                sql.sql_schema("w"),
                get_prompt("nl2sql_dialect_unknown", dialect="cassandra"),
            )

    def test_disabled_switch_turns_every_tool_into_a_notice(self) -> None:
        with patch.dict(os.environ, {"WITTY_NL2SQL_ENABLED": "0"}):
            clear_runtime_cache()
            notice = get_prompt("nl2sql_disabled")
            self.assertEqual(sql.sql_sources(), notice)
            self.assertEqual(sql.sql_schema(self.db), notice)
            self.assertEqual(sql.sql_tables("负荷", source=self.db), notice)
            self.assertEqual(sql.sql_values("station", "region", source=self.db), notice)
            self.assertEqual(sql.sql_run("SELECT 1", source=self.db), notice)
            self.assertEqual(sql.sql_export("SELECT 1", str(self.root / "x.csv"), source=self.db), notice)
            self.assertEqual(sql.sql_check("SELECT 1"), notice)
            self.assertEqual(sql.sql_pick("SELECT 1;;SELECT 2", source=self.db), notice)


class DriverHintTests(unittest.TestCase):
    """缺驱动的回执必须能照抄。

    空洞的「请自行安装」会让模型自己发明装法：先 pip 进沙箱 venv（那是另一个解释器，
    只跑出图脚本，而且目录被策略锁死），再想拉 docker 数据库客户端容器。所以回执要写清
    两件事——装到哪个解释器、执行哪条命令——并把这两条岔路点名劝阻。
    """

    def test_source_checkout_hint_uses_uv_add_and_names_the_interpreter(self) -> None:
        with patch.object(sql, "_checkout_root", return_value=Path("/repo/witty_agent")):
            hint = sql._install_hint("mysql")
        self.assertIn(sys.executable, hint)
        self.assertIn("uv add", hint)
        self.assertIn("--project '/repo/witty_agent'", hint)
        self.assertIn("--optional mysql", hint)
        self.assertIn("pymysql", hint)
        self.assertIn("沙箱 venv", hint)
        self.assertIn("docker", hint)

    def test_installed_wheel_hint_never_tells_you_to_edit_pyproject(self) -> None:
        with patch.object(sql, "_checkout_root", return_value=None):
            hint = sql._install_hint("postgres")
        self.assertIn(f"--python '{sys.executable}'", hint)
        self.assertIn("psycopg[binary]", hint)
        # 装好的包没有自己的 pyproject 可改，给 `uv add` 等于给一条跑不通的命令
        self.assertNotIn("uv add", hint)

    def test_dialect_without_an_extra_still_gets_a_target_interpreter(self) -> None:
        # oracle/dm/mssql 没登记 extra：不硬推包名，但解释器和劝阻照旧
        hint = sql._install_hint("oracle")
        self.assertIn(sys.executable, hint)
        self.assertIn("沙箱 venv", hint)
        self.assertNotIn("pymysql", hint)
        self.assertNotIn("uv add", hint)

    def test_missing_driver_error_carries_the_tried_names_and_the_command(self) -> None:
        sources = [{"name": "ops", "dialect": "mysql", "dsn": "mysql://reader@10.0.0.9:3306/ops"}]
        def no_driver(name: str) -> None:
            raise ImportError(name)

        with patch.object(sql, "nl2sql_settings", return_value={"enabled": True, "sources": sources}):
            with patch.object(sql, "importlib", SimpleNamespace(import_module=no_driver)):
                out = sql.sql_schema("ops")
        self.assertIn("pymysql", out)  # 试过哪些驱动
        self.assertIn(sys.executable, out)  # 装到哪个解释器
        self.assertIn("```bash", out)  # 一条能照抄的命令


class SurfaceTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_cache()

    def test_tools_are_registered_as_plugins_not_kernel(self) -> None:
        names = {item.name for item in list_tools()}
        for name in _TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, names)
                self.assertNotIn(name, KERNEL_TOOLS)

    def test_read_tools_are_readonly_and_export_needs_approval(self) -> None:
        for name in ("sql_sources", "sql_schema", "sql_tables", "sql_values", "sql_run", "sql_check"):
            with self.subTest(tool=name):
                self.assertIn(name, READONLY_TOOLS)
                self.assertNotIn(name, DANGEROUS_TOOLS)
        # 只有写文件的 sql_export 要审批；sql_pick 会跑多条 SQL，不进并行读集合
        self.assertIn("sql_export", DANGEROUS_TOOLS)
        self.assertNotIn("sql_export", READONLY_TOOLS)
        self.assertNotIn("sql_pick", READONLY_TOOLS)

    def test_descriptions_and_params_all_come_from_prompts(self) -> None:
        specs = {item.name: item for item in list_tools()}
        for name in _TOOLS:
            with self.subTest(tool=name):
                spec = specs[name]
                self.assertEqual(spec.description, get_prompt(f"tool_desc_{name}"))
                self.assertTrue(get_prompt(f"tool_snippet_{name}"))
                for key, field in spec.parameters["properties"].items():
                    self.assertTrue(field["description"], f"{name}.{key} 缺参数说明")

    def test_plugin_has_no_third_party_imports(self) -> None:
        tree = ast.parse(Path(sql.__file__).read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        # 出图靠沙箱脚本，插件里不许出现 matplotlib / pandas / 数据库驱动
        for banned in ("matplotlib", "pandas", "numpy", "pymysql", "psycopg", "sqlalchemy"):
            self.assertNotIn(banned, roots)
        allowed = {
            "__future__",
            "collections",
            "contextlib",
            "csv",
            "importlib",
            "os",
            "pathlib",
            "re",
            "sqlite3",
            "sys",
            "typing",
            "urllib",
            "witty_agent",
        }
        self.assertEqual(roots - allowed, set())

    def test_no_hardcoded_model_facing_chinese_strings(self) -> None:
        """AGENTS.md 提示词规则第 1 条：模型可见文案一律走 get_prompt，不写死在 .py 里。"""
        source = Path(sql.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        skip: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if ast.get_docstring(node, clean=False) is not None:
                    skip.add(id(node.body[0].value))
            # 日志格式串不是模型可见文案，另有「不打敏感正文」的规则管
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = node.func.value
                if isinstance(target, ast.Name) and target.id == "logger":
                    skip.update(id(item) for item in node.args)
        han = re.compile(r"[一-鿿]")
        offenders = [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and han.search(node.value)
            and (len(node.value) > 16 or re.search(r"[。：，；！？]", node.value))
        ]
        self.assertEqual(offenders, [])

    def test_get_prompt_keys_used_by_the_plugin_all_exist(self) -> None:
        tree = ast.parse(Path(sql.__file__).read_text(encoding="utf-8"))
        keys = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_prompt"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertGreater(len(keys), 40)
        for key in sorted(set(keys)):
            with self.subTest(key=key):
                self.assertTrue(get_prompt(key))


class SkillTests(unittest.TestCase):
    def test_four_skills_are_discovered_with_matching_names(self) -> None:
        found = {item.name: item for item in list_system_skills()}
        for name in ("nl2sql", "nl2sql-schema", "nl2sql-sql", "nl2sql-deliver"):
            with self.subTest(skill=name):
                self.assertIn(name, found)
                # 技能名必须等于目录名，否则 load_skill 找不到 scripts/references
                self.assertEqual(found[name].path.name, name)
                self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertTrue(found[name].description.strip())

    def test_orchestrator_skill_carries_scripts_and_references(self) -> None:
        skill = load_skill("nl2sql")
        self.assertTrue(skill.body.strip())
        self.assertIsNotNone(skill.scripts_dir)
        self.assertIsNotNone(skill.references_dir)
        assert skill.references_dir is not None and skill.scripts_dir is not None
        names = {item.name for item in skill.references_dir.iterdir()}
        self.assertEqual(
            names,
            {"analysis-depth.md", "chart-decision.md", "checkers.md", "error-taxonomy.md"},
        )
        plot = skill.scripts_dir / "plot_result.py"
        self.assertTrue(plot.is_file())
        # 出图脚本跑在沙箱解释器里，所以它可以 import matplotlib；插件不行
        self.assertIn("matplotlib", plot.read_text(encoding="utf-8"))
        compile(plot.read_text(encoding="utf-8"), str(plot), "exec")

    def test_stage_skills_are_reachable_from_the_orchestrator(self) -> None:
        body = load_skill("nl2sql").body
        for name in ("nl2sql-schema", "nl2sql-sql", "nl2sql-deliver"):
            with self.subTest(skill=name):
                self.assertIn(name, body)
                self.assertTrue(load_skill(name).body.strip())

    def test_question_routing_does_not_steal_other_skills(self) -> None:
        names = [item.name for item in match_relevant_skills("帮我查一下各区上半年的负荷趋势")]
        self.assertEqual(names[:1], ["nl2sql"])
        csv_names = [item.name for item in match_relevant_skills("把这个 CSV 分析一下")]
        self.assertNotIn("nl2sql", csv_names)


if __name__ == "__main__":
    unittest.main()
