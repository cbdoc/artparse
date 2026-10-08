from pathlib import Path
import tempfile

import feedparser

from screen import NOTICE, build_page, build_rss, feed_urls, is_nature, strip_tags

assert is_nature("https://www.nature.com/articles/s41467-026-78309-y")
assert not is_nature("https://www.cell.com/cancer-cell/fulltext/S1535")
assert strip_tags("<jats:p>HR+/HER2&#8722; <i>cancer</i></jats:p>") == "HR+/HER2− cancer"
assert NOTICE.match("Correction:  EZH2-Deficient T-Cell ...") and not NOTICE.match("Correcting KRAS signaling")
assert feed_urls("# Feeds\nSee https://x.org\n- A — http://a.org/rss\nB — http://b.org/rss\n- no url") == ["http://a.org/rss"]
assert len(feed_urls((Path(__file__).parent / "feeds.md").read_text())) == 11

item = {"guid": "g1", "title": "A & B <study>", "link": "https://x.org/1", "abstract": "abs",
        "source": "Nature", "date": "2026-10-05T00:00:00+00:00", "score": 2.71}
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "feed.xml"
    build_rss([item], p)
    f = feedparser.parse(str(p))
    assert not f.bozo, f.bozo_exception
    assert f.entries[0].title == "[2.7] A & B <study>"
    assert f.entries[0].link == "https://x.org/1"
    page = Path(d) / "index.html"
    build_page([item], [], {"at": "2026-10-08 12:00", "new": 1}, page)
    assert "A &amp; B &lt;study&gt;" in page.read_text() and "None." in page.read_text()
print("ok")
