"""生成 nl2sql 端到端演示用的 sqlite 库（配电网月度负荷 + 停电事件）。

    uv run python examples/nl2sql_demo.py /tmp/grid_demo.db

数据是固定种子伪随机，每次生成结果一致，便于回归对照。表结构刻意留了几个
真实场景里常见的坑，用来验收 nl2sql 的各级分析：

- 列注释只写在建表语句行尾（sqlite 没有 COMMENT 语法），考验 M-Schema 提取
- 有两座变电站中途退运，退运前的历史负荷仍在库里：漏掉 `status = 1` 过滤时，
  只有退运前那几个月的合计会偏高，考验「按执行结果聚类」能不能真把错的甩出去
- `load_record` 一对多，直接 JOIN 再 SUM 会扇出放大，考验 L5 检查器
- 2026-07 高新园区负荷异常抬升（数据中心投产），给 L4 归因留了因果线索
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE region (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,   -- 供电区域名称
  city          TEXT,            -- 所属地市
  manager       TEXT             -- 区域负责人
);

CREATE TABLE substation (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL,  -- 变电站名称
  region_id        INTEGER,        -- 所属供电区域
  voltage_kv       INTEGER,        -- 电压等级 千伏
  commission_date  TEXT,           -- 投运日期 YYYY-MM-DD
  status           INTEGER,        -- 运行状态 1=在运 0=停运
  FOREIGN KEY (region_id) REFERENCES region(id)
);

CREATE TABLE load_record (
  id             INTEGER PRIMARY KEY,
  substation_id  INTEGER,        -- 关联变电站
  stat_month     TEXT,           -- 统计月份 YYYY-MM
  peak_load_mw   REAL,           -- 月最大负荷 兆瓦
  avg_load_mw    REAL,           -- 月平均负荷 兆瓦
  energy_gwh     REAL,           -- 月供电量 亿千瓦时
  FOREIGN KEY (substation_id) REFERENCES substation(id)
);

CREATE TABLE outage (
  id              INTEGER PRIMARY KEY,
  substation_id   INTEGER,       -- 关联变电站
  start_time      TEXT,          -- 停电开始时间 YYYY-MM-DD HH:MM
  minutes         INTEGER,       -- 停电时长 分钟
  cause           TEXT,          -- 停电原因 设备故障/外力破坏/计划检修/天气
  affected_users  INTEGER,       -- 影响用户数
  FOREIGN KEY (substation_id) REFERENCES substation(id)
);
"""

REGIONS = [
    (1, "江北新区", "南京", "王建国"),
    (2, "城东片区", "南京", "李秀兰"),
    (3, "城西片区", "南京", "张伟"),
    (4, "高新园区", "南京", "陈明"),
    (5, "沿江工业区", "南京", "刘芳"),
]

# (名称, 区域, 电压等级, 投运日, 状态, 负荷基准, 退运月份)
SUBSTATIONS = [
    ("江北新区变", 1, 220, "2015-06-01", 1, 180.0, None),
    ("浦口变", 1, 110, "2011-09-15", 1, 95.0, None),
    ("桥林变", 1, 110, "2019-03-20", 1, 72.0, None),
    ("城东变", 2, 220, "2008-11-01", 1, 210.0, None),
    ("紫金变", 2, 110, "2016-07-10", 1, 88.0, None),
    ("孝陵卫变", 2, 110, "2005-04-18", 0, 64.0, "2025-12"),  # 2025-12 退运，之前的负荷仍在库里
    ("城西变", 3, 220, "2010-05-06", 1, 165.0, None),
    ("莫愁变", 3, 110, "2013-12-01", 1, 78.0, None),
    ("河西变", 3, 220, "2020-08-08", 1, 142.0, None),
    ("高新变", 4, 220, "2017-02-14", 1, 155.0, None),
    ("软件园变", 4, 110, "2021-05-30", 1, 105.0, None),
    ("生物医药变", 4, 110, "2022-10-12", 1, 68.0, None),
    ("沿江变", 5, 220, "2009-03-01", 1, 245.0, None),
    ("化工园变", 5, 110, "2014-06-25", 1, 132.0, None),
    ("龙潭变", 5, 110, "2018-11-11", 0, 90.0, "2026-02"),  # 2026-02 退运
]

MONTHS = [f"2025-{m:02d}" for m in range(8, 13)] + [f"2026-{m:02d}" for m in range(1, 8)]

# 月份季节系数：7-8 月迎峰度夏最高，1 月冬季次高峰，4-5 / 10-11 月为低谷
SEASON = {
    "01": 1.04, "02": 0.88, "03": 0.85, "04": 0.79, "05": 0.86, "06": 0.97,
    "07": 1.18, "08": 1.15, "09": 0.99, "10": 0.82, "11": 0.87, "12": 0.98,
}

CAUSES = ["设备故障", "外力破坏", "计划检修", "天气影响"]


def build(path: Path) -> dict[str, int]:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.executemany("INSERT INTO region VALUES (?,?,?,?)", REGIONS)
    rows = [
        (i, name, region, kv, date, status)
        for i, (name, region, kv, date, status, _base, _off) in enumerate(SUBSTATIONS, start=1)
    ]
    conn.executemany("INSERT INTO substation VALUES (?,?,?,?,?,?)", rows)

    loads: list[tuple] = []
    rid = 0
    for sid, (_n, region, _kv, _d, _status, base, retired) in enumerate(SUBSTATIONS, start=1):
        # 逐年负荷自然增长，按站给一个 1%-4% 的年增速
        growth = 1.0 + rng.uniform(0.01, 0.04) / 12
        for idx, month in enumerate(MONTHS):
            if retired and month >= retired:
                continue  # 退运之后不再有负荷记录，退运之前的历史保留
            factor = SEASON[month[-2:]] * (growth**idx)
            peak = base * factor * rng.uniform(0.97, 1.03)
            # 高新园区 2026-07 数据中心投产，负荷阶跃抬升
            if region == 4 and month == "2026-07":
                peak *= 1.34
            avg = peak * rng.uniform(0.62, 0.71)
            energy = avg * 24 * 30 / 10000
            rid += 1
            loads.append((rid, sid, month, round(peak, 1), round(avg, 1), round(energy, 4)))
    conn.executemany("INSERT INTO load_record VALUES (?,?,?,?,?,?)", loads)

    live = [i for i, s in enumerate(SUBSTATIONS, start=1) if s[4] == 1]
    outages: list[tuple] = []
    for oid in range(1, 41):
        sid = rng.choice(live)
        month = rng.choice(MONTHS)
        day = rng.randint(1, 28)
        cause = rng.choices(CAUSES, weights=[4, 2, 3, 2])[0]
        # 天气影响的停电时长明显更长，给 L4 归因留出可解释的差异
        span = rng.randint(180, 720) if cause == "天气影响" else rng.randint(25, 240)
        outages.append(
            (
                oid,
                sid,
                f"{month}-{day:02d} {rng.randint(0, 23):02d}:{rng.choice(['00', '15', '30', '45'])}",
                span,
                cause,
                rng.randint(120, 9800),
            )
        )
    conn.executemany("INSERT INTO outage VALUES (?,?,?,?,?,?)", outages)

    conn.commit()
    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("region", "substation", "load_record", "outage")
    }
    conn.close()
    return counts


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/grid_demo.db").expanduser()
    counts = build(target)
    print(f"已生成 {target}")
    for table, n in counts.items():
        print(f"  {table}: {n} 行")
    print(f"  月份范围: {MONTHS[0]} ~ {MONTHS[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
