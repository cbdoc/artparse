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
THRESHOLD = float(os.environ.get("THRESHOLD", "1.8"))
KEEP_DAYS, PRUNE_DAYS = 30, 90
UA = "Mozilla/5.0 (artparse feed screener)"

NOTICE = re.compile(r"\s*(Correction|Erratum|Corrigendum)\b", re.I)

RELEVANCE = Score(
    instructions="How relevant is this article to the reader's stated interests?",
    criteria=[
        "Unrelated to my interests",
        "Same broad field but not my topics",
        "Touches one of my topics",
        "Squarely on my topics; must read",
    ],
)


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
    for k, v in [("title", "Cancer Research (Jev-screened)"), ("link", "https://typesafe.ai"),
                 ("description", f"Items scoring >= {THRESHOLD} on interests.md")]:
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
    keep = [i for i in seen.values()
            if i["score"] >= THRESHOLD and datetime.fromisoformat(i["date"]) > now - timedelta(days=KEEP_DAYS)]
    build_rss(sorted(keep, key=lambda i: i["date"], reverse=True), OUT)
    print(f"wrote {len(keep)} items to {OUT}")


if __name__ == "__main__":
    main()
