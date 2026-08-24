"""Bluesky (AT Protocol) direct social-network engine - keyless, public API.

Given a name hint we search actors, then pull their media posts.  Every image
found is biometrically re-verified downstream, so a wrong actor is harmless.
"""
from __future__ import annotations

import httpx

from ..types import Candidate

API = "https://public.api.bsky.app/xrpc"
UA = "verified-pipeline/1.0 (+https://github.com)"


def _rkey(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def search(name: str, max_actors: int = 5, max_posts: int = 25) -> tuple[list[Candidate], dict]:
    raw: dict = {"actors": [], "feeds": {}}
    out: list[Candidate] = []
    with httpx.Client(timeout=30, headers={"User-Agent": UA}) as c:
        r = c.get(f"{API}/app.bsky.actor.searchActors", params={"q": name, "limit": max_actors})
        r.raise_for_status()
        actors = r.json().get("actors", [])
        raw["actors"] = actors
        for a in actors:
            handle = a.get("handle", "")
            did = a.get("did", "")
            profile_url = f"https://bsky.app/profile/{handle}"
            if a.get("avatar"):
                out.append(Candidate(engine="bluesky_profile", title=f"{a.get('displayName') or handle} (@{handle})", link=profile_url, source="bsky.app", thumbnail=a["avatar"], image=a["avatar"], snippet=a.get("description", "") or "", extra={"did": did, "handle": handle}))
            try:
                f = c.get(f"{API}/app.bsky.feed.getAuthorFeed", params={"actor": did or handle, "filter": "posts_with_media", "limit": max_posts})
                f.raise_for_status()
                feed = f.json().get("feed", [])
            except Exception:  # noqa: BLE001
                feed = []
            raw["feeds"][handle] = feed
            for item in feed:
                post = item.get("post", {})
                embed = post.get("embed", {}) or {}
                images = embed.get("images") or (embed.get("media", {}) or {}).get("images") or []
                if not images:
                    continue
                record = post.get("record", {}) or {}
                url = f"https://bsky.app/profile/{post.get('author', {}).get('handle', handle)}/post/{_rkey(post.get('uri', ''))}"
                for img in images[:4]:
                    out.append(
                        Candidate(
                            engine="bluesky",
                            title=(record.get("text") or "")[:140] or f"Post by @{handle}",
                            link=url,
                            source="bsky.app",
                            thumbnail=img.get("thumb", ""),
                            image=img.get("fullsize", "") or img.get("thumb", ""),
                            snippet=record.get("text", "") or "",
                            posted_at=record.get("createdAt", "") or "",
                            extra={"handle": handle, "did": did, "alt": img.get("alt", ""), "likes": post.get("likeCount"), "reposts": post.get("repostCount")},
                        )
                    )
    return out, raw
