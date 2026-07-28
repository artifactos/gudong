#!/usr/bin/env python3
"""从 Umami 把当天的成绩拉下来,分档写成 dist/<日期>.json。

    UMAMI_API_KEY=xxx UMAMI_WEBSITE_ID=xxx python3 tools/pull_dist.py

为什么不另起一个后端:分数已经在往 Umami 送了(round 事件带 score)。
再搭一套 Worker + 数据库,等于把同一份数据存两遍,还多一个账号、多一处会挂的东西。

这里跑在 CI 上,不在浏览器里 —— API key 只有服务端见得到。前端读的是一个
静态 JSON,走 CDN,读多少次都不要钱,也没有接口能被刷。

代价是分布不是实时的,跟着定时任务走。对这个东西无所谓:你比的是「今天到目前
为止的人」,晚一小时和早一小时看不出区别。
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("UMAMI_API_BASE", "https://api.umami.is/v1")
KEY = os.environ.get("UMAMI_API_KEY", "")
SITE = os.environ.get("UMAMI_WEBSITE_ID", "")
OUT = pathlib.Path(os.environ.get("DIST_DIR", "dist"))

BINS = 22            # 和前端直方图的档数一致,改了两边一起改
MAX_SCORE = 400      # 一组四件 × 100
CST = timezone(timedelta(hours=8))
KEEP_DAYS = 45       # 只留最近这些天的文件,别让目录无限长


def api(path: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{BASE}{path}?{q}", headers={"x-umami-api-key": KEY,
                                                              "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def day_bounds(day: str) -> tuple[int, int]:
    """北京时间那一天的起止,毫秒。日期一律按 UTC+8 算,和前端同一套。"""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CST)
    return int(d.timestamp() * 1000), int((d + timedelta(days=1)).timestamp() * 1000)


def buckets_for(day: str) -> tuple[list[int], int]:
    start, end = day_bounds(day)
    rows = api(f"/websites/{SITE}/event-data/values",
               {"startAt": start, "endAt": end, "event": "round", "propertyName": "score"})
    buckets = [0] * BINS
    total = 0
    for row in (rows if isinstance(rows, list) else rows.get("data", [])):
        try:
            score = float(row.get("value"))
            n = int(row.get("total") or row.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if not (0 <= score <= MAX_SCORE) or n <= 0:
            continue
        buckets[min(BINS - 1, int(score / MAX_SCORE * BINS))] += n
        total += n
    return buckets, total


def main() -> int:
    if not KEY or not SITE:
        print("缺 UMAMI_API_KEY / UMAMI_WEBSITE_ID", file=sys.stderr)
        return 1
    today = datetime.now(CST).date()
    # 今天和昨天都刷一遍:跨零点交的那批要补进昨天,今天的要更新到最新
    days = [str(today), str(today - timedelta(days=1))]
    OUT.mkdir(parents=True, exist_ok=True)

    # 基线取最近三十天已经落盘的文件,不再去打接口 —— 一次拉一天就够了
    base = [0] * BINS
    base_n = 0
    for f in sorted(OUT.glob("*.json"))[-30:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                            # noqa: BLE001 —— 坏文件跳过就行
            continue
        for i, v in enumerate(d.get("buckets", [])[:BINS]):
            base[i] += v
        base_n += d.get("totalPlayers", 0)

    for day in days:
        try:
            buckets, total = buckets_for(day)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  ! {day}: {e}", file=sys.stderr)
            continue
        (OUT / f"{day}.json").write_text(json.dumps(
            {"day": day, "buckets": buckets, "totalPlayers": total,
             "baseline": base, "baselinePlayers": base_n},
            ensure_ascii=False), encoding="utf-8")
        print(f"  ✓ {day}  {total} 人  {buckets}")

    old = sorted(OUT.glob("*.json"))[:-KEEP_DAYS]
    for f in old:
        f.unlink()
    if old:
        print(f"  · 清掉 {len(old)} 个过期文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
