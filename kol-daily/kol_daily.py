# -*- coding: utf-8 -*-
"""
kol_daily.py —— 博主每日汇总采集器

做的事：按 FEEDS 里配置的 RSS 地址抓取每位博主的最新内容，
汇总成 data/kol.json，供 App 通过一个网址直接读取。

设计要点（都是踩过坑的地方）：
  1. 一个源抓失败，不影响其他源；失败的作者保留上一次的好数据，不会被清空。
  2. 整体一条都没抓到 → 直接退出，绝不覆盖已有文件（App 继续显示旧数据）。
  3. RSSHub 公开实例经常 429/502，这里会自动轮换镜像重试。
  4. 只用「作者+题目」去重，同一篇文章不会因为重跑而堆积。

依赖：pip install feedparser requests
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import feedparser
import requests

# ---------------------------------------------------------------- 配置区

# 北京时间
BEIJING = timezone(timedelta(hours=8))

# 仓库根目录（脚本放在 kol-daily/ 下，所以上两级才是根）
ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "kol.json"

# RSSHub 公开实例，按顺序尝试。自建实例请填在最前面。
RSSHUB_BASES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com",
]

# 每位博主最新保留多少条
PER_AUTHOR_LIMIT = 10
# 总条数上限
TOTAL_LIMIT = 300

# ====== 在这里填你的博主，一行一个 ======
# url 填真实的 RSS 地址即可；如果填的是 rsshub.app 的地址，
# 脚本会自动尝试上面的镜像。
FEEDS = [
    # --- 示例源，用来验证流程，加上自己的博主后可以删掉 ---
    {"author": "少数派", "platform": "少数派", "url": "https://sspai.com/feed"},

    # --- 知乎：需要博主的 people ID（不是昵称）---
    # 打开博主主页，地址形如 zhihu.com/people/xxxx，把 xxxx 填进去
    # {"author": "水又三人禾", "platform": "知乎",
    #  "url": "https://rsshub.app/zhihu/people/activities/把xxxx换成ID"},

    # --- 雪球：xueqiu.com/u/数字 ---
    # {"author": "某某", "platform": "雪球",
    #  "url": "https://rsshub.app/xueqiu/user/把这里换成数字ID"},

    # --- 微信公众号 / 其他普通 RSS，直接贴地址 ---
    # {"author": "某某公众号", "platform": "公众号",
    #  "url": "https://rsshub.app/wechat/xxx/xxx"},
]

# ---------------------------------------------------------------- 工具函数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    # GitHub Actions 会把这种行渲染成黄色警告
    print(f"::warning::{msg}", flush=True)


def url_candidates(url: str) -> list[str]:
    """把 rsshub.app 的地址扩展成多个镜像，普通地址原样返回。"""
    out = [url]
    if "rsshub.app" in url:
        tail = url.split("rsshub.app", 1)[1]
        for base in RSSHUB_BASES:
            alt = base.rstrip("/") + tail
            if alt not in out:
                out.append(alt)
    return out


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def to_iso(entry) -> str:
    """把 feedparser 的时间转成北京时间 ISO 字符串。"""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return ""
    try:
        dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(BEIJING)
        return dt.isoformat(timespec="seconds")
    except Exception:
        return ""


def norm_key(s: str) -> str:
    """归一化标题：去空白、全角转半角、小写，用于去重。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", "", s)
    return s.lower()[:120]


def fetch_feed(url: str) -> tuple[bool, list[dict], str]:
    """抓一个源，返回 (是否成功, 条目列表, 错误信息)。"""
    last_err = ""
    for candidate in url_candidates(url):
        for attempt in range(2):
            try:
                resp = requests.get(candidate, headers=HEADERS, timeout=25)
                if resp.status_code == 200 and resp.content:
                    parsed = feedparser.parse(resp.content)
                    if parsed.entries:
                        items = []
                        for e in parsed.entries:
                            title = strip_html(e.get("title", "")).strip()
                            link = (e.get("link") or "").strip()
                            summary = strip_html(
                                e.get("summary") or e.get("description") or ""
                            )[:280]
                            if not title:
                                continue
                            items.append(
                                {
                                    "title": title,
                                    "url": link,
                                    "published": to_iso(e),
                                    "summary": summary,
                                }
                            )
                        if items:
                            return True, items, ""
                    last_err = f"{candidate} 解析后没有条目（可能不是 RSS）"
                elif resp.status_code in (429, 403, 502, 503):
                    last_err = f"{candidate} 返回 {resp.status_code}（限流或被拦）"
                    time.sleep(1.5)
                else:
                    last_err = f"{candidate} 返回 {resp.status_code}"
                    break
            except Exception as exc:  # 网络超时、DNS 失败等
                last_err = f"{candidate} 请求异常：{type(exc).__name__}"
                time.sleep(1.0)
    return False, [], last_err


def load_old() -> dict:
    if not OUT_FILE.exists():
        return {}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        warn("旧 kol.json 无法解析，本次当作全新生成")
        return {}


def sort_key(item: dict) -> str:
    return item.get("published") or ""


# ---------------------------------------------------------------- 主流程


def main() -> int:
    if not FEEDS:
        warn("FEEDS 是空的，先填至少一个博主再跑")
        return 1

    old = load_old()
    old_items = old.get("items") or []
    # 按作者分组旧数据，用于「某位博主这轮挂了，继续用上一份」
    old_by_author: dict[str, list[dict]] = {}
    for it in old_items:
        old_by_author.setdefault(it.get("author", ""), []).append(it)

    seen: set[str] = set()
    results: list[dict] = []
    report: list[dict] = []
    ok_count = 0

    for feed in FEEDS:
        author = (feed.get("author") or "").strip()
        platform = (feed.get("platform") or "").strip()
        url = (feed.get("url") or "").strip()
        if not author or not url:
            warn(f"跳过配置不完整的条目：{feed}")
            continue
        if "把" in url and ("ID" in url or "换成" in url):
            warn(f"[{author}] URL 还是占位符，先填真实 ID：{url}")
            report.append(
                {"author": author, "platform": platform, "ok": False,
                 "items": 0, "error": "URL 仍是占位符"}
            )
            continue

        log(f"→ 抓取 {author}（{platform}）")
        ok, items, err = fetch_feed(url)

        if ok:
            ok_count += 1
            got = 0
            for it in items[:PER_AUTHOR_LIMIT]:
                key = f"{author}|{norm_key(it['title'])}"
                if key in seen:
                    continue
                seen.add(key)
                it.update({"author": author, "platform": platform})
                results.append(it)
                got += 1
            log(f"  成功，{len(items)} 条，新增 {got} 条")
            report.append(
                {"author": author, "platform": platform, "ok": True,
                 "items": len(items), "error": ""}
            )
        else:
            warn(f"[{author}] 抓取失败：{err}")
            carried = 0
            for it in old_by_author.get(author, [])[:PER_AUTHOR_LIMIT]:
                key = f"{author}|{norm_key(it.get('title', ''))}"
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
                carried += 1
            if carried:
                log(f"  沿用上一份数据 {carried} 条，不会清空")
            report.append(
                {"author": author, "platform": platform, "ok": False,
                 "items": carried, "error": err}
            )

    # 排序、截断
    results.sort(key=sort_key, reverse=True)
    results = results[:TOTAL_LIMIT]

    now = datetime.now(BEIJING)
    if not results:
        warn("一条都没抓到，保留旧文件不覆盖，App 会继续显示上一次的数据")
        return 0

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "count": len(results),
        "feeds": report,
        "items": results,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        f"完成：{len(results)} 条，来自 {ok_count}/{len(report)} 个源 "
        f"→ {OUT_FILE.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
