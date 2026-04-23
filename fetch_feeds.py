#!/usr/bin/env python3
"""
fetch_feeds.py — Silicon Signal news aggregator

Fetches RSS/Atom feeds from our tracked sources, categorizes items by keyword,
and emits a JSON blob ready to be embedded into the dashboard HTML.

Usage:
    python3 fetch_feeds.py                    # writes news_data.json
    python3 fetch_feeds.py --inject dashboard.html   # rebuilds HTML in-place

Strategy:
- Sources with a working direct RSS feed (The Register, SemiAnalysis,
  ChipsAndCheese, DataCenterDynamics) are fetched directly with a real
  browser User-Agent.
- Sources fronted by aggressive bot protection (Tom's Hardware, HotHardware,
  TechRadar, CRN, ServeTheHome) are aggregated via Google News with a
  site-restricted query. This is stable, free, and doesn't require a key.

Run on cron (e.g. nightly at 02:00) to keep the dashboard current.
"""

import argparse
import datetime as dt
import gzip
import html
import io
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15")

TIMEOUT = 25
MAX_ITEMS_PER_SOURCE = 8
MAX_TOTAL_ITEMS = 9   # cards on the dashboard
MAX_AGE_DAYS = 60     # drop stale stuff

# Sources with working direct feeds. A real browser UA is enough.
DIRECT_FEEDS = [
    ("The Register",          "https://www.theregister.com/headlines.atom"),
    ("The Register · AI/ML",  "https://www.theregister.com/software/ai_ml/headlines.atom"),
    ("The Register · HPC",    "https://www.theregister.com/on_prem/hpc/headlines.atom"),
    ("SemiAnalysis",          "https://semianalysis.com/feed/"),
    ("ChipsAndCheese",        "https://chipsandcheese.com/feed/"),
    ("DataCenterDynamics",    "https://www.datacenterdynamics.com/en/rss/"),
]

# Sources blocked by Cloudflare-style WAFs. We route through Google News,
# which lets us use `site:` restrictions and returns a clean RSS feed.
GOOGLE_NEWS_SOURCES = [
    ("Tom's Hardware",   "site:tomshardware.com (chip OR GPU OR CPU OR AI OR data center)"),
    ("HotHardware",      "site:hothardware.com (chip OR GPU OR CPU OR AI OR Nvidia OR AMD)"),
    ("TechRadar Pro",    "site:techradar.com/pro (chip OR AI OR GPU OR cloud OR data center)"),
    ("CRN",              "site:crn.com (Nvidia OR AMD OR Intel OR hyperscaler OR AI chip)"),
    ("ServeTheHome",     "site:servethehome.com (GPU OR CPU OR AI OR data center)"),
]

# Keyword → category mapping. First match wins.
# Categories are used for the coloured label on each news card.
CATEGORY_RULES = [
    ("policy", re.compile(r"\b(tariff|export|sanction|chips act|bis |commerce|regulator|ftc|doj|congress)\b", re.I)),
    ("cpu",    re.compile(r"\b(graviton|xeon|epyc|arm neoverse|core ultra|panther lake|snapdragon x|cpu)\b", re.I)),
    ("edge",   re.compile(r"\b(jetson|thor|dragonwing|optimus|humanoid|robot|figure|waymo|aurora|tesla ai|physical ai|edge ai)\b", re.I)),
    ("infra",  re.compile(r"\b(data.?center|hyperscaler|azure|aws|gcp|oracle|neocloud|coreweave|nebius|capex|grid|power)\b", re.I)),
    ("gpu",    re.compile(r"\b(gpu|nvidia|rubin|blackwell|hopper|h100|h200|b100|b200|gb200|gb300|mi300|mi325|mi450|instinct|radeon|cerebras|groq|lpu|tpu|trainium|inferentia|maia|mtia|accelerator|asic)\b", re.I)),
]
DEFAULT_CATEGORY = "gpu"  # reasonable fallback for semi-world news

# Topics we care about; an item must match at least one to be included.
TOPIC_FILTER = re.compile(
    r"\b("
    r"nvidia|amd|intel|tsmc|arm|qualcomm|broadcom|cerebras|groq|tesla|waymo|aurora|nuro|"
    r"figure|boston dynamics|agility|nebius|coreweave|iren|lambda|crusoe|"
    r"gpu|cpu|chip|silicon|semiconductor|accelerator|asic|tpu|trainium|inferentia|maia|mtia|lpu|"
    r"rubin|blackwell|hopper|h100|h200|b200|gb200|gb300|mi300|mi325|mi450|"
    r"jetson|thor|dragonwing|snapdragon|optimus|humanoid|robotic|robot|autonomous|"
    r"hyperscaler|azure|aws|gcp|oracle|data.?center|neocloud|"
    r"capex|hbm|memory|dram|nand|foundry|cuda|rocm|inference|training|"
    r"tariff|export|chips act|bis "
    r")\b", re.I,
)

# -------------------------------------------------------------------------
# HTTP
# -------------------------------------------------------------------------

def fetch(url: str, retries: int = 3) -> bytes:
    """GET a URL with browser UA and gzip support. Retries 503s with backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            last_err = e
            # 503/429 → probably rate-limited or a bot check. Back off and retry.
            if e.code in (429, 503) and attempt < retries - 1:
                wait = 2 ** (attempt + 2)  # 4, 8, 16 seconds
                print(f"    ↻ {e.code} on {url[:60]}… waiting {wait}s")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


# -------------------------------------------------------------------------
# Feed parsing
# -------------------------------------------------------------------------

NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
}

def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    # Try a handful of common formats. feedparser handles more, but we only need
    # enough to sort and filter.
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            d = dt.datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed(xml_bytes: bytes, source: str) -> list[dict]:
    """Parse RSS 2.0 or Atom and return a list of normalized item dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"    ! parse error for {source}: {e}", file=sys.stderr)
        return []

    items = []
    # RSS 2.0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link") or "").strip()
        date  = it.findtext("pubDate") or it.findtext("{%s}date" % NS["dc"])
        desc  = (it.findtext("description") or
                 it.findtext("{%s}encoded" % NS["content"]) or "")
        items.append({
            "title":   _strip_html(title),
            "link":    link,
            "date":    _parse_date(date),
            "summary": _strip_html(desc)[:400],
            "source":  source,
        })

    # Atom
    for e in root.iter("{%s}entry" % NS["atom"]):
        title = (e.findtext("{%s}title" % NS["atom"]) or "").strip()
        link_el = e.find("{%s}link" % NS["atom"])
        link = link_el.get("href", "") if link_el is not None else ""
        date = (e.findtext("{%s}updated" % NS["atom"]) or
                e.findtext("{%s}published" % NS["atom"]))
        desc = (e.findtext("{%s}summary" % NS["atom"]) or
                e.findtext("{%s}content" % NS["atom"]) or "")
        items.append({
            "title":   _strip_html(title),
            "link":    link,
            "date":    _parse_date(date),
            "summary": _strip_html(desc)[:400],
            "source":  source,
        })

    return [i for i in items if i["title"] and i["link"]]


# -------------------------------------------------------------------------
# Google News fallback
# -------------------------------------------------------------------------

def google_news_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def normalize_google_news(items: list[dict], source_label: str) -> list[dict]:
    """Google News wraps the title with ' - <Publisher>'; strip it and enforce the source."""
    out = []
    for it in items:
        title = re.sub(r"\s+-\s+[^-]+$", "", it["title"]).strip()
        # Google News links are redirects; keep them as-is so the user lands on
        # the original publisher's page.
        out.append({**it, "title": title, "source": source_label})
    return out


# -------------------------------------------------------------------------
# Pipeline
# -------------------------------------------------------------------------

def categorize(text: str) -> str:
    for cat, pat in CATEGORY_RULES:
        if pat.search(text):
            return cat
    return DEFAULT_CATEGORY


def is_on_topic(item: dict) -> bool:
    blob = f"{item['title']} {item['summary']}"
    return bool(TOPIC_FILTER.search(blob))


def fetch_all() -> list[dict]:
    all_items: list[dict] = []

    print("▸ Direct feeds:")
    for name, url in DIRECT_FEEDS:
        try:
            xml = fetch(url)
            items = parse_feed(xml, name)[:MAX_ITEMS_PER_SOURCE]
            print(f"    {name:<28} {len(items):>3} items")
            all_items.extend(items)
        except Exception as e:
            print(f"    {name:<28} FAIL: {type(e).__name__}: {e}")
        time.sleep(2.0)  # polite

    print("▸ Google News aggregation:")
    for label, query in GOOGLE_NEWS_SOURCES:
        try:
            xml = fetch(google_news_url(query))
            items = parse_feed(xml, label)[:MAX_ITEMS_PER_SOURCE]
            items = normalize_google_news(items, label)
            print(f"    {label:<28} {len(items):>3} items")
            all_items.extend(items)
        except Exception as e:
            print(f"    {label:<28} FAIL: {type(e).__name__}: {e}")
        time.sleep(2.0)

    return all_items


def dedupe(items: list[dict]) -> list[dict]:
    """Drop near-duplicate titles (the same story often runs on several sources)."""
    seen = set()
    out = []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:50]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def human_age(when: dt.datetime | None) -> str:
    if when is None:
        return "recent"
    delta = dt.datetime.now(dt.timezone.utc) - when
    hrs = delta.total_seconds() / 3600
    if hrs < 1:    return "just now"
    if hrs < 24:   return f"{int(hrs)}h ago"
    days = hrs / 24
    if days < 14:  return f"{int(days)}d ago"
    return f"{int(days/7)}w ago"


def build_payload(items: list[dict]) -> dict:
    # Filter: on-topic + fresh
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAX_AGE_DAYS)
    keep = [
        i for i in items
        if is_on_topic(i) and (i["date"] is None or i["date"] >= cutoff)
    ]
    keep = dedupe(keep)

    # Sort: items with dates first, newest first; undated items after.
    keep.sort(key=lambda x: (x["date"] is None, -(x["date"].timestamp() if x["date"] else 0)))

    # Trim summary to ~250 chars for card display.
    cards = []
    for it in keep[:MAX_TOTAL_ITEMS]:
        summary = it["summary"]
        if len(summary) > 260:
            summary = summary[:257].rsplit(" ", 1)[0] + "…"
        cards.append({
            "category": categorize(it["title"] + " " + it["summary"]),
            "title":    it["title"],
            "summary":  summary or "(No summary available — click through for the full story.)",
            "source":   it["source"],
            "link":     it["link"],
            "age":      human_age(it["date"]),
            "date_iso": it["date"].isoformat() if it["date"] else None,
        })

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "item_count":   len(cards),
        "items":        cards,
    }


def inject_into_html(html_path: Path, payload: dict) -> None:
    """Replace the JSON between the sentinel markers in the HTML file."""
    text = html_path.read_text(encoding="utf-8")
    marker_open  = '<script id="news-data" type="application/json">'
    marker_close = "</script>"
    start = text.find(marker_open)
    if start < 0:
        raise SystemExit(f"No <script id='news-data'> block found in {html_path}")
    end = text.find(marker_close, start)
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
    new_text = (text[:start + len(marker_open)]
                + "\n" + payload_json + "\n    "
                + text[end:])
    html_path.write_text(new_text, encoding="utf-8")
    print(f"▸ Injected {payload['item_count']} items into {html_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="news_data.json",
                    help="Where to write the JSON payload")
    ap.add_argument("--inject", default=None,
                    help="If set, also rewrite this HTML file's <script id='news-data'> block")
    args = ap.parse_args()

    raw = fetch_all()
    print(f"▸ Collected {len(raw)} raw items")
    payload = build_payload(raw)
    print(f"▸ Kept {payload['item_count']} after filtering/dedup")

    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    print(f"▸ Wrote {args.out}")

    if args.inject:
        inject_into_html(Path(args.inject), payload)


if __name__ == "__main__":
    main()
