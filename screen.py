"""Screen journal RSS feeds with Jev and publish the relevant items as one RSS feed.

Usage: python screen.py [--dry-run]   (needs TYPESAFE_API_KEY)
"""
import asyncio
import calendar
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import feedparser
from typesafe_sdk import AsyncTypeSafeClient, Score, TypeSafeAuthenticationError, TypeSafePermissionDeniedError

ROOT = Path(__file__).parent
# local dev: load KEY=value lines from .env (CI uses repo secrets instead)
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"\''))
SEEN = ROOT / "seen.json"
OUT = ROOT / "docs" / "feed.xml"
PAGE = ROOT / "docs" / "index.html"
CFG = tomllib.loads((ROOT / "config.toml").read_text())
THRESHOLD, KEEP_DAYS, PRUNE_DAYS = CFG["threshold"], CFG["keep_days"], CFG["prune_days"]
NEAR_MISS = CFG["near_miss_margin"]
UA = "Mozilla/5.0 (artparse feed screener)"

NOTICE = re.compile(r"\s*(" + "|".join(map(re.escape, CFG["skip_title_prefixes"])) + r")\b", re.I)

RELEVANCE = Score(instructions=CFG["question"], criteria=CFG["levels"])


def strip_tags(s):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))).strip()


def is_nature(link):
    return re.match(r"https?://(?:www\.)?nature\.com/articles/", link or "") is not None


def nature_meta(link):
    """-> (abstract, article_type). nature.com RSS ships titles only; the article page has both
    in meta tags (Crossref often lacks new abstracts; OpenAlex lags by days)."""
    try:
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception:
        return "", ""
    meta = lambda name: (m := re.search(rf'<meta name="{name}" content="([^"]*)"', page)) and strip_tags(m.group(1)) or ""
    return meta(r"dc\.description"), meta("citation_article_type")


def feed_urls(md):
    """URLs on lines starting with '- ' in feeds.md."""
    return [m.group(0) for line in md.splitlines()
            if line.startswith("- ") and (m := re.search(r"https?://\S+", line))]


def fetch_new(seen):
    items = []
    for url in feed_urls((ROOT / "feeds.md").read_text()):
        for _ in range(3):  # bioRxiv intermittently returns 500
            f = feedparser.parse(url, agent="Mozilla/5.0")
            if f.entries:
                break
        else:
            print(f"no items from {url} (status {f.get('status')})", file=sys.stderr)
        source = f.feed.get("title", url)
        for e in f.entries:
            guid = e.get("id") or e.get("link")
            if not guid or guid in seen or NOTICE.match(e.get("title", "")):
                continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            date = datetime.fromtimestamp(calendar.timegm(t), timezone.utc) if t else datetime.now(timezone.utc)
            items.append({
                "guid": guid, "title": strip_tags(e.get("title")), "link": e.get("link", guid),
                "abstract": strip_tags(e.get("summary")), "source": source, "date": date.isoformat(),
                "type": e.get("prism_section", ""),  # Cell Press feeds: Article, Commentary, Preview...
            })
    # ponytail: dedupe by guid only; same article in two feeds with different guids gets scored twice
    return list({i["guid"]: i for i in items}.values())


async def score_all(items, interests):
    sem = asyncio.Semaphore(10)
    async with AsyncTypeSafeClient() as client:
        async def one(it):
            async with sem:
                if not it["abstract"] and is_nature(it["link"]):
                    it["abstract"], it["type"] = await asyncio.to_thread(nature_meta, it["link"])
                try:
                    r = await client.system_one(
                        state={"my_interests": interests, "title": it["title"],
                               "abstract": it["abstract"], "journal": it["source"],
                               **({"article_type": it["type"]} if it.get("type") else {})},
                        questions={"relevance": RELEVANCE},
                    )
                    it["score"] = round(r.scores["relevance"].score, 2)
                except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError):
                    raise
                except Exception as ex:  # leave unscored -> retried next run
                    print(f"jev error on {it['link']}: {ex}", file=sys.stderr)
        await asyncio.gather(*(one(it) for it in items))
    return [it for it in items if "score" in it]


def build_rss(items, path):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    for k, v in [("title", CFG["feed_title"]), ("link", "https://typesafe.ai"),
                 ("description", f"Items scoring >= {THRESHOLD} against interests.md")]:
        ET.SubElement(ch, k).text = v
    for it in items:
        el = ET.SubElement(ch, "item")
        ET.SubElement(el, "title").text = f"[{it['score']:.1f}] {it['title']}"
        ET.SubElement(el, "link").text = it["link"]
        ET.SubElement(el, "guid", isPermaLink="false").text = it["guid"]
        ET.SubElement(el, "pubDate").text = format_datetime(datetime.fromisoformat(it["date"]))
        ET.SubElement(el, "description").text = f"{it['abstract']}\n\n— Source: {it['source']}"
    path.parent.mkdir(exist_ok=True)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


PAGE_HEAD = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e5e5e5;--accent:#0b62d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#e8e8e8;--muted:#999;--line:#2c2c2c;--accent:#6aa8ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px}}
h1{{font-size:1.4em;margin:.2em 0}} h2{{font-size:1.1em;margin:1.6em 0 .4em}}
.meta,.src{{color:var(--muted);font-size:.85em}} a{{color:var(--accent);text-decoration:none}}
.item{{border-top:1px solid var(--line);padding:.6em 0;display:flex;gap:.8em}}
.score{{font-variant-numeric:tabular-nums;font-weight:600;min-width:2.4em}}
details{{font-size:.9em;color:var(--muted)}} summary{{cursor:pointer}}
</style>"""


def build_page(keep, near, run, path):
    e = html.escape
    def rows(items):
        return "".join(
            f'<div class="item"><div class="score">{i["score"]:.2f}</div><div>'
            f'<a href="{e(i["link"])}">{e(i["title"])}</a>'
            f'<div class="src">{e(i["source"])} · {i["date"][:10]}{" · " + e(i["type"]) if i.get("type") else ""}</div>'
            + (f'<details><summary>abstract</summary>{e(i["abstract"])}</details>' if i["abstract"] else "")
            + "</div></div>" for i in items) or '<p class="meta">None.</p>'
    path.write_text(
        PAGE_HEAD.format(title=e(CFG["feed_title"]))
        + f'<h1>{e(CFG["feed_title"])}</h1>'
        + f'<p class="meta">Last run {run["at"]} UTC · {run["new"]} new articles scored · '
        + f'threshold {THRESHOLD} · <a href="feed.xml">RSS feed</a></p>'
        + f"<h2>In the feed ({len(keep)}, last {KEEP_DAYS} days)</h2>" + rows(keep)
        + f"<h2>Near misses ({len(near)}, scored {THRESHOLD - NEAR_MISS:.2f}–{THRESHOLD})</h2>" + rows(near),
        encoding="utf-8")


def main():
    dry = "--dry-run" in sys.argv
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    new = fetch_new(seen)
    print(f"{len(new)} new items")
    t0 = time.time()
    try:
        scored = asyncio.run(score_all(new, (ROOT / "interests.md").read_text()))
    except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as ex:
        sys.exit(f"Jev rejected the API key: {ex}")
    print(f"scored {len(scored)} in {time.time() - t0:.1f}s")
    if dry:
        for it in sorted(scored, key=lambda i: -i["score"]):
            print(f"{it['score']:.2f}  {'KEEP' if it['score'] >= THRESHOLD else '    '}  {it.get('type', '')[:12]:12}  {it['title'][:100]}")
        return
    seen.update({it["guid"]: it for it in scored})
    now = datetime.now(timezone.utc)
    seen = {g: i for g, i in seen.items() if datetime.fromisoformat(i["date"]) > now - timedelta(days=PRUNE_DAYS)}
    SEEN.write_text(json.dumps(seen, indent=1))
    recent = sorted((i for i in seen.values() if datetime.fromisoformat(i["date"]) > now - timedelta(days=KEEP_DAYS)),
                    key=lambda i: i["date"], reverse=True)
    keep = [i for i in recent if i["score"] >= THRESHOLD]
    near = sorted((i for i in recent if THRESHOLD - NEAR_MISS <= i["score"] < THRESHOLD), key=lambda i: -i["score"])
    build_rss(keep, OUT)
    build_page(keep, near, {"at": now.strftime("%Y-%m-%d %H:%M"), "new": len(scored)}, PAGE)
    print(f"wrote {len(keep)} items to {OUT}, {len(near)} near misses to {PAGE}")


if __name__ == "__main__":
    main()
