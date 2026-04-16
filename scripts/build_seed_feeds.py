#!/usr/bin/env python3
"""
Build the Stirred seed-feeds.json by pulling from iTunes Search, Apple Podcasts
top charts, and optionally Podcast Index, then vetting each candidate feed.

Runs weekly as a GitHub Action. Outputs two files to ./public/:
  - seed-feeds.json       (array of feed records the Stirred iOS app consumes)
  - seed-feeds-meta.json  (build metadata: timestamp, count, tradition breakdown)

Schema for each feed matches the Stirred app's `SeedFeed` Swift decoder
(snake_case keys, optional artwork_url/website_url):

  {
    "feed_url": str,
    "title": str,
    "author": str,
    "speaker_display": str,
    "speaker_slug": str,
    "description": str,
    "artwork_url": str | null,
    "website_url": str | null,
    "tradition": str,
    "theological_lean": str
  }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    "sermon",
    "preaching",
    "bible teaching",
    "expository preaching",
    "sunday sermon",
    "pastor sermon",
    "gospel sermon",
    "scripture teaching",
    "homily",
    "bible sermon",
    "church sermon",
    "christian sermon",
    "reformed preaching",
    "bible exposition",
]

# Apple Podcasts genre IDs:
#   1314 Religion & Spirituality (parent)
#   1439 Christianity
TOP_CHARTS_GENRE_IDS = [1439, 1314]
TOP_CHARTS_REGIONS = ["us", "gb", "ca", "au"]

TRADITION_KEYWORDS: dict[str, list[str]] = {
    "reformed": [
        "reformed", "calvinist", "presbyterian", "sola scriptura",
        "expository preaching", "doctrines of grace", "9marks",
        "redeemer", "gospel coalition", "ligonier", "desiring god",
    ],
    "catholic": [
        "catholic", "diocese", "parish", "mass readings", "homily",
        "vatican", "archdiocese", "roman catholic",
    ],
    "charismatic": [
        "pentecostal", "spirit-filled", "charismatic", "speaking in tongues",
        "bethel", "hillsong", "prophetic",
    ],
    "baptist": [
        "baptist", "southern baptist", "sbc",
    ],
    "methodist": [
        "methodist", "wesleyan", "umc",
    ],
    "episcopal": [
        "episcopal", "anglican", "cathedral", "book of common prayer",
    ],
    "lutheran": [
        "lutheran", "lcms", "missouri synod", "elca",
    ],
    "orthodox": [
        "orthodox church", "eastern orthodox", "greek orthodox",
        "russian orthodox", "ancient faith",
    ],
}

# Must contain at least one of these to pass vetting
POSITIVE_KEYWORDS = [
    "sermon", "preach", "teach", "bible", "gospel", "scripture", "homily",
    "exposit", "pastor", "church", "christ", "jesus", "faith", "worship",
    "devotional", "christian", "ministry", "theology",
]

# Disqualifies if present (obvious non-sermon content)
NEGATIVE_KEYWORDS = [
    "christian music", "worship music playlist", "kids podcast",
    "christian comedy", "politics podcast", "true crime",
    "horoscope", "tarot", "witchcraft",
]

REQUEST_TIMEOUT = 12          # seconds per HTTP request
MAX_CONCURRENT_FETCHES = 24   # parallel feed vets
MIN_EPISODE_COUNT = 5         # a feed with fewer items is probably dead
MIN_AUDIO_RATIO = 0.4         # at least 40% of items must have audio enclosure
USER_AGENT = "Stirred-FeedBuilder/1.0 (+https://github.com/colsonrice/stirred-feeds)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (name or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or "unknown"


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        if not p.scheme or not p.netloc:
            return ""
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/')}"
    except Exception:
        return ""


def classify_tradition(title: str, description: str, author: str) -> str:
    blob = f"{title or ''} {description or ''} {author or ''}".lower()
    for tradition, keywords in TRADITION_KEYWORDS.items():
        if any(k in blob for k in keywords):
            return tradition
    return "evangelical"  # default bucket for generic Christian feeds


def passes_keyword_filter(title: str, description: str) -> bool:
    blob = f"{title or ''} {description or ''}".lower()
    if not any(k in blob for k in POSITIVE_KEYWORDS):
        return False
    if any(k in blob for k in NEGATIVE_KEYWORDS):
        return False
    return True


def guess_theological_lean(tradition: str, title: str, description: str) -> str:
    """Coarse default. Most sermon podcasts skew conservative."""
    return "conservative"


# ---------------------------------------------------------------------------
# iTunes sources
# ---------------------------------------------------------------------------

def itunes_search(term: str, limit: int = 200) -> list[dict]:
    url = "https://itunes.apple.com/search"
    params = {
        "term": term,
        "media": "podcast",
        "entity": "podcast",
        "limit": limit,
        "explicit": "No",
        "country": "US",
    }
    r = requests.get(url, params=params,
                     timeout=REQUEST_TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json().get("results", [])


def itunes_top_charts(region: str, genre_id: int, limit: int = 200) -> list[dict]:
    """Pull the top podcasts RSS feed for a region+genre, then lookup
    each entry by trackId to get the feedUrl."""
    chart_url = (
        f"https://itunes.apple.com/{region}/rss/toppodcasts/"
        f"limit={limit}/genre={genre_id}/json"
    )
    r = requests.get(chart_url, timeout=REQUEST_TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    data = r.json()
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]

    track_ids: list[str] = []
    for entry in entries:
        id_field = entry.get("id", {})
        if isinstance(id_field, dict):
            attrs = id_field.get("attributes", {}) or {}
            tid = attrs.get("im:id")
            if tid:
                track_ids.append(str(tid))

    results: list[dict] = []
    # Apple lookup accepts comma-separated ids, up to ~150 per call
    for i in range(0, len(track_ids), 100):
        batch = ",".join(track_ids[i:i + 100])
        try:
            r2 = requests.get(
                "https://itunes.apple.com/lookup",
                params={"id": batch, "entity": "podcast"},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            if r2.ok:
                results.extend(r2.json().get("results", []))
        except Exception as e:
            print(f"  ! lookup batch failed: {e}", file=sys.stderr)
        time.sleep(0.4)
    return results


# ---------------------------------------------------------------------------
# Podcast Index (optional)
# ---------------------------------------------------------------------------

def fetch_podcast_index(key: str, secret: str,
                        seen_urls: set[str], seen_collections: set[str]) -> list[dict]:
    auth_time = str(int(time.time()))
    signature = hashlib.sha1(
        (key + secret + auth_time).encode("utf-8")
    ).hexdigest()
    headers = {
        "X-Auth-Key": key,
        "X-Auth-Date": auth_time,
        "Authorization": signature,
        "User-Agent": USER_AGENT,
    }

    results: list[dict] = []
    for term in SEARCH_TERMS[:8]:
        try:
            r = requests.get(
                "https://api.podcastindex.org/api/1.0/search/byterm",
                params={"q": term, "max": 200, "fulltext": ""},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! podcast-index {term!r}: {e}", file=sys.stderr)
            continue

        for feed in data.get("feeds", []) or []:
            fu = normalize_url(feed.get("url"))
            cn = feed.get("title") or ""
            if not fu or fu in seen_urls or cn in seen_collections:
                continue
            seen_urls.add(fu)
            seen_collections.add(cn)
            results.append({
                "feedUrl": feed.get("url"),
                "artistName": feed.get("author") or feed.get("ownerName") or cn,
                "collectionName": cn,
                "artworkUrl600": feed.get("artwork") or feed.get("image"),
                "description": feed.get("description") or "",
                "source": "podcast-index",
            })
        time.sleep(0.3)

    return results


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def collect_candidates() -> list[dict]:
    seen_urls: set[str] = set()
    seen_collections: set[str] = set()
    candidates: list[dict] = []

    for term in SEARCH_TERMS:
        print(f"[search] term={term!r}")
        try:
            results = itunes_search(term, limit=200)
        except Exception as e:
            print(f"  ! {e}", file=sys.stderr)
            continue
        added = 0
        for r in results:
            fu = normalize_url(r.get("feedUrl"))
            cn = r.get("collectionName") or ""
            if not fu or fu in seen_urls or cn in seen_collections:
                continue
            seen_urls.add(fu)
            seen_collections.add(cn)
            r["source"] = f"itunes-search:{term}"
            candidates.append(r)
            added += 1
        print(f"    +{added} new")
        time.sleep(0.4)

    for region in TOP_CHARTS_REGIONS:
        for gid in TOP_CHARTS_GENRE_IDS:
            print(f"[chart] region={region} genre={gid}")
            try:
                results = itunes_top_charts(region, gid, limit=200)
            except Exception as e:
                print(f"  ! {e}", file=sys.stderr)
                continue
            added = 0
            for r in results:
                fu = normalize_url(r.get("feedUrl"))
                cn = r.get("collectionName") or ""
                if not fu or fu in seen_urls or cn in seen_collections:
                    continue
                seen_urls.add(fu)
                seen_collections.add(cn)
                r["source"] = f"itunes-chart:{region}:{gid}"
                candidates.append(r)
                added += 1
            print(f"    +{added} new")

    pi_key = os.environ.get("PODCAST_INDEX_KEY")
    pi_secret = os.environ.get("PODCAST_INDEX_SECRET")
    if pi_key and pi_secret:
        print("[podcast-index] enabled")
        before = len(candidates)
        candidates.extend(fetch_podcast_index(pi_key, pi_secret,
                                              seen_urls, seen_collections))
        print(f"    +{len(candidates) - before} new")
    else:
        print("[podcast-index] skipped (no key)")

    print(f"\nCollected {len(candidates)} unique candidates")
    return candidates


# ---------------------------------------------------------------------------
# Vetting (async, parallel)
# ---------------------------------------------------------------------------

async def vet_feed(session: aiohttp.ClientSession,
                   candidate: dict) -> dict | None:
    feed_url = candidate.get("feedUrl") or candidate.get("feed_url")
    if not feed_url:
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(feed_url, timeout=timeout,
                               allow_redirects=True) as r:
            if r.status != 200:
                return None
            text = await r.text(errors="replace")
    except Exception:
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    items = root.findall(".//item")
    if len(items) < MIN_EPISODE_COUNT:
        return None

    audio_items = 0
    for item in items:
        for enc in item.findall("enclosure"):
            if (enc.get("type") or "").lower().startswith("audio/"):
                audio_items += 1
                break
    if audio_items / max(len(items), 1) < MIN_AUDIO_RATIO:
        return None

    last_pub: datetime | None = None
    for item in items:
        pub = item.findtext("pubDate") or ""
        if not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub)
            if dt and (last_pub is None or dt > last_pub):
                last_pub = dt
        except Exception:
            continue

    # Metadata — prefer the feed's own title/description over iTunes blurb.
    feed_title = (root.findtext(".//channel/title") or "").strip()
    feed_desc = (root.findtext(".//channel/description") or "").strip()
    feed_link = (root.findtext(".//channel/link") or "").strip()

    title = feed_title or candidate.get("collectionName", "") or ""
    description = feed_desc or candidate.get("description", "") or ""
    author = candidate.get("artistName") or ""
    if not author:
        # iTunes author namespace
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        author_el = root.find(".//channel/itunes:author", ns)
        if author_el is not None and author_el.text:
            author = author_el.text.strip()
    if not author:
        author = title

    artwork = candidate.get("artworkUrl600") or ""
    if not artwork:
        img = root.find(".//channel/image/url")
        if img is not None and img.text:
            artwork = img.text.strip()
        else:
            ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
            it_img = root.find(".//channel/itunes:image", ns)
            if it_img is not None:
                artwork = (it_img.get("href") or "").strip()

    if not passes_keyword_filter(title, description):
        return None

    tradition = classify_tradition(title, description, author)
    speaker_display = author or title
    speaker_slug = slugify(author) or slugify(title)

    return {
        "feed_url": feed_url,
        "title": title[:300],
        "author": author[:200],
        "speaker_display": speaker_display[:120],
        "speaker_slug": speaker_slug,
        "description": (description or "")[:1200],
        "artwork_url": artwork or None,
        "website_url": feed_link or None,
        "tradition": tradition,
        "theological_lean": guess_theological_lean(tradition, title, description),
        # Internal fields (kept for potential debugging, not used by app):
        "_source": candidate.get("source", "itunes"),
        "_episode_count": len(items),
        "_audio_ratio": round(audio_items / max(len(items), 1), 3),
        "_last_published_at": last_pub.date().isoformat() if last_pub else None,
    }


async def vet_all(candidates: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_FETCHES)
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        connector=connector,
    ) as session:
        async def bounded(c: dict) -> dict | None:
            async with sem:
                return await vet_feed(session, c)

        results = await asyncio.gather(
            *[bounded(c) for c in candidates],
            return_exceptions=False,
        )
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def finalize(vetted: list[dict]) -> list[dict]:
    """Dedup by speaker_slug (keep the one with most episodes), then sort."""
    by_slug: dict[str, dict] = {}
    for feed in vetted:
        slug = feed["speaker_slug"]
        prev = by_slug.get(slug)
        if prev is None or feed["_episode_count"] > prev["_episode_count"]:
            by_slug[slug] = feed

    final = sorted(
        by_slug.values(),
        key=lambda f: (-f["_episode_count"], f["speaker_display"].lower()),
    )
    return final


def write_output(final: list[dict]) -> None:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "public"
    out_dir.mkdir(parents=True, exist_ok=True)

    # App-facing JSON: strip internal fields (leading _)
    app_facing = []
    for f in final:
        app_facing.append({k: v for k, v in f.items() if not k.startswith("_")})

    (out_dir / "seed-feeds.json").write_text(
        json.dumps(app_facing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    tradition_counts: dict[str, int] = {}
    for f in final:
        tradition_counts[f["tradition"]] = tradition_counts.get(f["tradition"], 0) + 1

    source_counts: dict[str, int] = {}
    for f in final:
        src = f.get("_source", "unknown").split(":")[0]
        source_counts[src] = source_counts.get(src, 0) + 1

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed_count": len(final),
        "version": int(time.time()),
        "traditions": dict(sorted(tradition_counts.items(),
                                   key=lambda kv: -kv[1])),
        "sources": dict(sorted(source_counts.items(),
                               key=lambda kv: -kv[1])),
        "schema_version": 1,
    }
    (out_dir / "seed-feeds-meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nWrote {len(app_facing)} feeds -> {out_dir / 'seed-feeds.json'}")
    print(f"Traditions: {meta['traditions']}")
    print(f"Sources:    {meta['sources']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    start = time.time()
    candidates = collect_candidates()
    if not candidates:
        print("No candidates collected; aborting.", file=sys.stderr)
        return 1

    print(f"\nVetting {len(candidates)} candidate feeds ...")
    vetted = asyncio.run(vet_all(candidates))
    print(f"Vetted: {len(vetted)} passed")

    final = finalize(vetted)
    print(f"Final (after slug dedup): {len(final)}")

    write_output(final)
    print(f"\nDone in {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
