from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

SOCIAL_DOMAINS = {
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "x.com": "X (Twitter)",
    "twitter.com": "X (Twitter)",
    "linkedin.com": "LinkedIn",
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "reddit.com": "Reddit",
    "threads.net": "Threads",
    "threads.com": "Threads",
    "bsky.app": "Bluesky",
    "mastodon.social": "Mastodon",
    "tumblr.com": "Tumblr",
    "flickr.com": "Flickr",
    "vk.com": "VK",
    "weibo.com": "Weibo",
    "snapchat.com": "Snapchat",
    "quora.com": "Quora",
    "medium.com": "Medium",
    "substack.com": "Substack",
    "github.com": "GitHub",
    "behance.net": "Behance",
    "dribbble.com": "Dribbble",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
    "imdb.com": "IMDb",
}

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "igsh", "igshid", "fbclid", "gclid", "ref", "ref_src", "s", "t"}


def canonical_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host in ("twitter.com", "mobile.twitter.com"):
            host = "x.com"
        if host.startswith("m.") and host[2:] in SOCIAL_DOMAINS:
            host = host[2:]
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
        path = p.path.rstrip("/") or "/"
        return urlunparse(("https", host, path, "", urlencode(q), ""))
    except Exception:
        return url


def platform_of(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    for i in range(len(parts) - 1):
        d = ".".join(parts[i:])
        if d in SOCIAL_DOMAINS:
            return SOCIAL_DOMAINS[d]
    return None


def is_post_url(url: str, platform: str | None) -> bool:
    """Heuristic: does this look like an individual post (vs a profile / listing)?"""
    path = urlparse(url).path
    if platform == "Instagram":
        return bool(re.search(r"/(p|reel|reels|tv)/[A-Za-z0-9_-]+", path))
    if platform == "X (Twitter)":
        return "/status/" in path
    if platform == "Facebook":
        return any(k in path for k in ("/posts/", "/photo", "/videos/", "/permalink", "story.php", "/reel/")) or "fbid=" in url
    if platform == "LinkedIn":
        return "/posts/" in path or "/feed/update/" in path or "/pulse/" in path
    if platform == "TikTok":
        return "/video/" in path or "/photo/" in path
    if platform == "YouTube":
        return "watch" in url or "youtu.be" in url or "/shorts/" in path
    if platform == "Pinterest":
        return "/pin/" in path
    if platform == "Reddit":
        return "/comments/" in path
    if platform == "Threads":
        return "/post/" in path
    if platform == "Bluesky":
        return "/post/" in path
    if platform == "Mastodon":
        return bool(re.search(r"/@[^/]+/\d+", path))
    return False


def _s(v) -> str:
    """Engine payloads are untrusted: coerce anything to a plain string."""
    if v is None or isinstance(v, (dict, list, tuple)):
        return ""
    return v if isinstance(v, str) else str(v)


@dataclass
class Candidate:
    engine: str
    title: str
    link: str
    source: str = ""
    thumbnail: str = ""
    image: str = ""
    snippet: str = ""
    posted_at: str = ""
    position: int = 0
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.engine = _s(self.engine)
        self.title = _s(self.title)[:400]
        self.link = _s(self.link)
        self.source = _s(self.source)[:200]
        self.thumbnail = _s(self.thumbnail)
        self.image = _s(self.image)
        self.snippet = _s(self.snippet)[:1000]
        self.posted_at = _s(self.posted_at)[:80]
        try:
            self.position = int(self.position)
        except (TypeError, ValueError):
            self.position = 0
        if not isinstance(self.extra, dict):
            self.extra = {}

    @property
    def platform(self) -> str | None:
        return platform_of(self.link)

    @property
    def is_social(self) -> bool:
        return self.platform is not None

    @property
    def is_post(self) -> bool:
        return is_post_url(self.link, self.platform)

    @property
    def key(self) -> str:
        return canonical_url(self.link)


@dataclass
class VerifiedMatch:
    candidate: Candidate
    similarity: float
    band: str
    face_bbox: list[float]
    candidate_faces: int
    image_used: str
    image_sha256: str
    face_crop_b64: str = ""
    metadata: dict = field(default_factory=dict)
    engines: list[str] = field(default_factory=list)

    @property
    def platform(self) -> str:
        return self.candidate.platform or "Web"

    def to_dict(self) -> dict:
        d = {
            "engine": self.candidate.engine,
            "engines": self.engines or [self.candidate.engine],
            "title": self.candidate.title,
            "link": self.candidate.link,
            "canonical_link": self.candidate.key,
            "source": self.candidate.source,
            "platform": self.platform,
            "is_social": self.candidate.is_social,
            "is_post": self.candidate.is_post,
            "thumbnail": self.candidate.thumbnail,
            "image": self.candidate.image,
            "snippet": self.candidate.snippet,
            "posted_at": self.candidate.posted_at,
            "similarity": round(self.similarity, 4),
            "band": self.band,
            "face_bbox": [round(v, 1) for v in self.face_bbox],
            "candidate_faces": self.candidate_faces,
            "image_used": self.image_used,
            "image_sha256": self.image_sha256,
            "face_crop_b64": self.face_crop_b64,
            "metadata": self.metadata,
        }
        return d
