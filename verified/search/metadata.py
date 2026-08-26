"""Post metadata extraction: oEmbed (official, no auth) first, then OpenGraph."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

OEMBED = {
    "x.com": "https://publish.twitter.com/oembed",
    "twitter.com": "https://publish.twitter.com/oembed",
    "youtube.com": "https://www.youtube.com/oembed",
    "youtu.be": "https://www.youtube.com/oembed",
    "tiktok.com": "https://www.tiktok.com/oembed",
    "bsky.app": "https://embed.bsky.app/oembed",
    "reddit.com": "https://www.reddit.com/oembed",
    "pinterest.com": "https://www.pinterest.com/oembed.json",
    "flickr.com": "https://www.flickr.com/services/oembed",
    "vimeo.com": "https://vimeo.com/api/oembed.json",
    "tumblr.com": "https://www.tumblr.com/oembed/1.0",
}


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _oembed_endpoint(url: str) -> str | None:
    h = _host(url)
    for dom, ep in OEMBED.items():
        if h == dom or h.endswith("." + dom):
            return ep
    return None


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(s or "", "lxml").get_text(" ")).strip()


MAX_HTML = 3 * 1024 * 1024


def _get_html(client: httpx.Client, url: str, ua: str) -> str | None:
    """Fetch an HTML page with a hard byte cap (pages can be enormous)."""
    with client.stream("GET", url, headers={"User-Agent": ua, "Accept-Language": "en"}) as r:
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return None
        buf = bytearray()
        for chunk in r.iter_bytes(65536):
            buf.extend(chunk)
            if len(buf) > MAX_HTML:
                break
        return buf.decode(r.encoding or "utf-8", errors="replace")


def fetch_metadata(url: str, timeout: float = 12) -> dict:
    from .verify import _ALLOW_LOCAL, _is_private_host, safe_client

    meta: dict = {"url": url, "method": None}
    if not _ALLOW_LOCAL and _is_private_host(url):
        return meta
    ep = _oembed_endpoint(url)
    with safe_client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as c:
        if ep:
            try:
                r = c.get(ep, params={"url": url, "format": "json", "omit_script": "true"})
                if r.status_code == 200:
                    o = r.json()
                    meta.update(
                        {
                            "method": "oembed",
                            "author": o.get("author_name"),
                            "author_url": o.get("author_url"),
                            "title": o.get("title"),
                            "provider": o.get("provider_name"),
                            "text": _strip_html(o.get("html", "")) if o.get("html") else o.get("title"),
                            "thumbnail": o.get("thumbnail_url"),
                        }
                    )
                    return {k: v for k, v in meta.items() if v not in (None, "")}
            except Exception:  # noqa: BLE001
                pass
        # OpenGraph / HTML fallback (crawler UA gets richer tags on most socials)
        for ua in (BOT_UA, UA):
            try:
                html = _get_html(c, url, ua)
                if html is None:
                    continue
                soup = BeautifulSoup(html, "lxml")
                og = {}
                for tag in soup.find_all("meta"):
                    k = tag.get("property") or tag.get("name")
                    v = tag.get("content")
                    if k and v:
                        og[k.lower()] = v
                title = og.get("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
                if title and "log in" in title.lower() and not og.get("og:description"):
                    continue  # login wall - try next UA
                meta.update(
                    {
                        "method": "opengraph",
                        "title": title,
                        "text": og.get("og:description") or og.get("description") or og.get("twitter:description"),
                        "image": og.get("og:image") or og.get("twitter:image"),
                        "site_name": og.get("og:site_name"),
                        "type": og.get("og:type"),
                        "published": og.get("article:published_time") or og.get("og:updated_time") or og.get("datepublished"),
                        "author": og.get("article:author") or og.get("author") or og.get("twitter:creator"),
                        "canonical": og.get("og:url"),
                    }
                )
                # JSON-LD often carries the author / date for Instagram, LinkedIn, YouTube
                for s in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(s.string or "")
                        ld = ld[0] if isinstance(ld, list) and ld else ld
                        if isinstance(ld, dict):
                            a = ld.get("author")
                            if isinstance(a, dict) and a.get("name"):
                                meta.setdefault("author", a.get("name"))
                            if ld.get("uploadDate") or ld.get("datePublished"):
                                meta.setdefault("published", ld.get("uploadDate") or ld.get("datePublished"))
                            if ld.get("articleBody") or ld.get("caption"):
                                meta.setdefault("text", ld.get("articleBody") or ld.get("caption"))
                    except Exception:  # noqa: BLE001
                        continue
                break
            except Exception:  # noqa: BLE001
                continue
    return {k: v for k, v in meta.items() if v not in (None, "")}
