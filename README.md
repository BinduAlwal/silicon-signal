# Silicon Signal — Live News Aggregator

A static-build news aggregator that pulls RSS/Atom feeds from tracked
semiconductor/cloud-infra sources and injects them into the dashboard HTML.
No runtime server, no external API keys, no database.

## Files

| File | Purpose |
|---|---|
| `silicon-signal.html` | The dashboard. Contains an embedded `<script id="news-data">` block that the renderer reads at page load. |
| `fetch_feeds.py` | Script that fetches all tracked feeds, filters and deduplicates them, writes `news_data.json`, and optionally rewrites the HTML in place. |
| `news_data.json` | Latest curated payload. Kept next to the HTML so you can diff what changed between runs. |

## Install once

```bash
python3 -m pip install --user feedparser
```

(The script actually only uses `urllib` and stdlib `xml.etree` for parsing, so
`feedparser` is optional — it's just handy to have installed for ad-hoc
experimentation.)

## Update the dashboard

```bash
python3 fetch_feeds.py --inject silicon-signal.html
```

That single command fetches every feed, filters items for chip/cloud/edge-AI
topics, dedupes near-identical headlines, and rewrites the `news-data` block in
the HTML. Open `silicon-signal.html` in any browser and you'll see the new cards.

## Schedule it

On a Mac/Linux box, cron works fine. Every weekday at 7 AM:

```cron
0 7 * * 1-5  cd /path/to/dashboard && /usr/bin/python3 fetch_feeds.py --inject silicon-signal.html >> fetch.log 2>&1
```

## Tracked sources

**Direct RSS/Atom feeds** (fetched with a browser User-Agent):

- The Register — main, AI/ML, HPC sections
- SemiAnalysis
- ChipsAndCheese
- DataCenterDynamics

**Google News aggregation** (for sources behind aggressive bot protection):

- Tom's Hardware
- HotHardware
- TechRadar Pro
- CRN
- ServeTheHome

Google News returns RSS for any query, which lets us use `site:tomshardware.com`
etc. and get the same items Cloudflare would otherwise block. The tradeoff is
that links are redirects through `news.google.com`; the user still lands on the
original publisher's page.

## How items are filtered

1. **Topic whitelist** — An item must match at least one of our chip/cloud/edge
   keywords to be kept. Edit `TOPIC_FILTER` in `fetch_feeds.py` to adjust.
2. **Freshness** — Items older than `MAX_AGE_DAYS` (default 60) are dropped.
3. **Dedup** — Near-identical titles (same story on multiple sources) collapse
   to one card.
4. **Category assignment** — Each item is labeled `gpu`, `infra`, `cpu`,
   `policy`, or `edge` based on keyword matches. The dashboard uses the label
   to color-code the card badge. First rule to match wins; edit
   `CATEGORY_RULES` to reorder.
5. **Top N** — The final payload keeps the 9 most recent items so the card
   grid stays clean.

## Extending

- **Add a source with a working RSS feed:** append to `DIRECT_FEEDS`.
- **Add a source fronted by a bot WAF:** append to `GOOGLE_NEWS_SOURCES` with
  a `site:` query. Optionally include keywords (e.g. `site:foo.com (GPU OR AI)`)
  to narrow the result set before filtering.
- **Change what counts as on-topic:** edit `TOPIC_FILTER`.
- **Change how items are categorized:** edit `CATEGORY_RULES`.
- **Show more/fewer cards:** change `MAX_TOTAL_ITEMS`.

## Known behavior

- Cold runs (first fetch after a long idle) sometimes trip Cloudflare 503s.
  The script has built-in exponential backoff (4s → 8s → 16s, then gives up)
  and tries again on the next cron tick, which is usually enough. If a
  specific source fails repeatedly, check its URL hasn't changed.
- Google News occasionally rate-limits too. Same story: wait, retry later.
- The HTML file works standalone — if the feed happens to be empty, the
  dashboard shows a friendly "run fetch_feeds.py to populate" placeholder in
  place of the cards.
