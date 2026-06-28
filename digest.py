"""
Daily Tech Digest — fetch 13 RSS feeds, filter 3-day freshness,
generate bilingual digest (EN-first), push to QQ Bot.

GitHub Actions cron: daily at 1:00 UTC (9:00 Beijing time).
"""
import json
import os
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

# ── Config ──────────────────────────────────────────────────
RSS_SOURCES = {
    "AI": [
        ("HuggingFace Blog", "https://huggingface.co/blog/feed.xml"),
        ("Ahead of AI", "https://magazine.sebastianraschka.com/feed"),
        ("Marcus on AI", "https://garymarcus.substack.com/feed"),
        ("Ben's Bites", "https://bensbites.substack.com/feed"),
        ("AI Supremacy", "https://aisupremacy.substack.com/feed"),
    ],
    "Phone": [
        ("GSMArena", "https://www.gsmarena.com/rss-news-reviews.php3"),
        ("Android Authority", "https://www.androidauthority.com/feed"),
        ("9to5Google", "https://9to5google.com/feed"),
        ("XDA Developers", "https://www.xda-developers.com/feed"),
        ("Droid Life", "https://www.droid-life.com/feed"),
    ],
    "Apple": [
        ("9to5Mac", "https://9to5mac.com/feed"),
        ("MacRumors", "https://feeds.macrumors.com/MacRumors-All"),
        ("AppleInsider", "https://appleinsider.com/rss/news/"),
    ],
}

QQ_APP_ID = os.environ["QQ_APP_ID"]
QQ_CLIENT_SECRET = os.environ["QQ_CLIENT_SECRET"]
QQ_OPENID = os.environ["QQ_OPENID"]
CUTOFF_DAYS = 3
MAX_ITEMS = 10

EMOJI_POOL = [
    "🤖", "🔥", "🚀", "💡", "🧠", "🎯", "⚡", "🌟", "💎", "🦄",
    "🎪", "🍜", "🐙", "🦊", "🌵", "🐸", "🦜", "🌈", "🪐", "🏴‍☠️",
    "🪄", "🌋", "🎭", "🔮", "🧩", "🎸", "🕹️", "📱", "🍎", "💰",
    "🏔️", "🦖", "🎨", "💫", "🏄", "🌊", "🦅", "🍄", "🐉", "🔧",
    "📉", "🖥️", "🛸", "🧲", "🎮", "🗿", "🌺", "🦋", "🐬", "🦀",
]

ATOM_NS = "http://www.w3.org/2005/Atom"


# ── Helpers ─────────────────────────────────────────────────
class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_data(self):
        return "".join(self.text)


def strip_html(html):
    if not html:
        return ""
    s = _Stripper()
    s.feed(html)
    return s.get_data()


def clean_text(t):
    if not t:
        return ""
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.DOTALL)
    t = strip_html(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_date(s):
    s = s.strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── RSS Fetch ───────────────────────────────────────────────
def _find_el(item, tag):
    """Find element in both RSS and Atom namespaces."""
    el = item.find(tag)
    if el is None:
        el = item.find(f"{{{ATOM_NS}}}{tag}")
    return el


def _fetch_substack(source_name, subdomain, cutoff):
    """Fetch latest post from a Substack via their JSON API."""
    api_url = f"https://{subdomain}.substack.com/api/v1/posts?limit=5"
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as r:
            data = json.loads(r.read())
    except Exception as e:
        # Fall back to RSS feed if API fails
        return _fetch_rss(source_name, f"https://{subdomain}.substack.com/feed", cutoff)

    posts = data.get("posts", [])
    for post in posts:
        pub_date_str = post.get("post_date") or post.get("published_at", "")
        if not pub_date_str:
            continue
        dt = parse_date(pub_date_str)
        if dt and dt >= cutoff:
            title = clean_text(post.get("title", ""))
            desc = clean_text(post.get("subtitle", "") or post.get("description", ""))
            link = post.get("canonical_url", "")
            return {
                "title": title,
                "link": link,
                "date": dt,
                "source": source_name,
                "description": desc[:500] if desc else title,
            }
    return None


def _fetch_rss(source_name, url, cutoff):
    """Fallback RSS fetch with Referer header for stubborn feeds."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Referer": f"https://{url.split('/feed')[0]}/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as r:
            content = r.read()
    except Exception as e:
        print(f"  --  [{source_name}] fetch error: {e}")
        return None
    return _parse_feed(source_name, content, cutoff)


def fetch_feed(source_name, url, cutoff):
    """Return newest item within cutoff, or None."""
    # Detect Substack URLs — use JSON API which is less likely to be blocked
    substack_match = re.match(r"https?://([^.]+)\.substack\.com/feed", url)
    if substack_match:
        return _fetch_substack(source_name, substack_match.group(1), cutoff)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as r:
            content = r.read()
    except Exception as e:
        print(f"  --  [{source_name}] fetch error: {e}")
        return None

    return _parse_feed(source_name, content, cutoff)


def _parse_feed(source_name, content, cutoff):
    """Parse RSS/Atom XML and return newest item within cutoff (handled by caller)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    items = root.findall(".//item") or root.findall(f".//{{{ATOM_NS}}}entry")
    best = None
    best_dt = None

    for item in items:
        title = clean_text(
            (_find_el(item, "title").text if _find_el(item, "title") is not None else "")
        )
        if not title:
            continue

        # Link
        link_el = _find_el(item, "link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "") or (link_el.text or "")
        link = link.strip()

        # Description
        desc_el = (
            _find_el(item, "description")
            or _find_el(item, "summary")
            or _find_el(item, "content")
        )
        desc = clean_text(desc_el.text if desc_el is not None else "")

        # PubDate
        pub_date_str = ""
        for tag in ("pubDate", "published", "updated"):
            el = _find_el(item, tag)
            if el is not None and el.text:
                pub_date_str = el.text.strip()
                break

        if not pub_date_str:
            continue

        dt = parse_date(pub_date_str)
        if dt is None:
            continue

        if dt >= cutoff:
            if best_dt is None or dt > best_dt:
                best_dt = dt
                best = {
                    "title": title,
                    "link": link,
                    "date": dt,
                    "source": source_name,
                    "description": desc[:500] if desc else title,
                }

    return best


# ── QQ Bot API ──────────────────────────────────────────────
def qq_get_token():
    data = json.dumps(
        {"appId": QQ_APP_ID, "clientSecret": QQ_CLIENT_SECRET}
    ).encode()
    req = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    return body["access_token"]


def qq_send_dm(token, content):
    data = json.dumps({"content": content, "msg_type": 0}).encode()
    req = urllib.request.Request(
        f"https://api.sgroup.qq.com/v2/users/{QQ_OPENID}/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Digest formatting ───────────────────────────────────────
def build_digest(articles):
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    )
    mmdd = today.strftime("%m%d")

    week = today.isocalendar()[1]
    offset = (week * 7) % len(EMOJI_POOL)
    emojis = EMOJI_POOL[offset:] + EMOJI_POOL[:offset]

    lines = [f"# Daily Updates · {mmdd}", ""]

    for i, a in enumerate(articles):
        emoji = emojis[i % len(emojis)]
        cat = a["category"]
        title = a["title"]
        desc = a.get("description", title)

        lines.append(f"{emoji} [{cat}] {title}")
        lines.append("")
        lines.append(f"{desc}")
        lines.append("")

    return "\n".join(lines).strip()


# ── Main ────────────────────────────────────────────────────
def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)

    all_articles = []
    for category, sources in RSS_SOURCES.items():
        for name, url in sources:
            result = fetch_feed(name, url, cutoff)
            if result:
                result["category"] = category
                all_articles.append(result)
                print(f"  OK  [{category}] {name}: {result['title'][:60]}")
            # failures already printed inside fetch_feed

    all_articles.sort(key=lambda x: x["date"], reverse=True)
    top = all_articles[:MAX_ITEMS]

    if not top:
        print("No articles in window — exiting silently.")
        return

    digest = build_digest(top)

    print(f"\n=== Digest ({len(top)} items) ===\n")
    print(digest)
    print(f"\n=== Sending to QQ ... ===")

    token = qq_get_token()
    result = qq_send_dm(token, digest)
    print(f"QQ response: {json.dumps(result, ensure_ascii=False)}")
    print("Done!")


if __name__ == "__main__":
    main()
