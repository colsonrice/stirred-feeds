# stirred-feeds

Automated, vetted list of Christian sermon RSS feeds for the [Stirred](https://github.com/colsonrice/) iOS app.

## What this repo does

A GitHub Action runs **weekly** (plus on-demand), pulling candidate feeds from:

- **iTunes Search API** — multiple sermon-related queries (public, no key)
- **Apple Podcasts top charts** — Christianity genre, multiple regions (public, no key)
- **Podcast Index API** — broader coverage (optional, requires free API key)

Each candidate is **vetted**:

1. `HTTP 200` on the feed URL, valid XML
2. At least 5 `<item>` elements
3. At least 40% of items have `<enclosure type="audio/*">`
4. Keyword filter (must contain sermon/preach/bible/gospel/… and must **not** contain obvious non-sermon markers)
5. Deduped by feed URL, collection title, and speaker slug

Surviving feeds are tagged by tradition (reformed / evangelical / catholic / charismatic / baptist / methodist / episcopal / lutheran) via a keyword classifier, then committed to `public/seed-feeds.json`.

## Output

Consumed by the Stirred iOS app via jsDelivr CDN:

```
https://cdn.jsdelivr.net/gh/colsonrice/stirred-feeds@main/public/seed-feeds.json
```

Schema matches the Stirred app's `SeedFeed` decoder:

```json
{
  "feed_url": "https://...",
  "title": "...",
  "author": "...",
  "speaker_display": "...",
  "speaker_slug": "...",
  "description": "...",
  "artwork_url": "...",
  "website_url": null,
  "tradition": "reformed",
  "theological_lean": "conservative"
}
```

Additional metadata about each build is in `public/seed-feeds-meta.json`.

## Running locally

```bash
pip install -r scripts/requirements.txt
python scripts/build_seed_feeds.py
# Output written to public/seed-feeds.json
```

Optional environment variables:

- `PODCAST_INDEX_KEY` / `PODCAST_INDEX_SECRET` — enables the Podcast Index source for broader coverage

## Scheduled runs

See `.github/workflows/build-feeds.yml`. Runs at 06:17 UTC every Monday, and can be triggered manually from the Actions tab.
