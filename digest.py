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

# Emoji pool — rotated weekly
EMOJI_POOL = [
    "🤖", "🔥", "🚀", "💡", "🧠", "🎯", "⚡", "🌟", "💎", "🦄",
    "🎪", "🍜", "🐙", "🦊", "🌵", "🐸", "🦜", "🌈", "🪐", "🏴‍☠️",
    "🪄", "🌋", "🎭", "🔮", "🧩", "🎸", "🕹️", "📱", "🍎", "💰",
    "🏔️", "🦖", "🎨", "💫", "🏄", "🌊", "🦅", "🍄", "🐉", "🔧",
    "📉", "🖥️", "🛸", "🧲", "🎮", "🗿", "🌺", "🦋", "🐬", "🦀",
]


# ── HTML / text helpers ─────────────────────────────────────
class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_data(self):
        return "".join(self.text)


def strip_html(html):
    s = _Stripper()
    s.feed(html)
    return s.get_data()


def clean_title(t):
    """Remove CDATA wrappers, extra whitespace, leading source prefixes."""
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.DOTALL)
    t = strip_html(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── Date parsing ────────────────────────────────────────────
def parse_date(s):
    """Try every RSS/Atom date format we've seen in the wild."""
    s = s.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


# ── RSS Fetch ───────────────────────────────────────────────
def fetch_feed(source_name, url, cutoff):
    """Return newest item within cutoff, or None."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DailyDigest/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            content = resp.read()
    except Exception:
        return None

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    # Atom namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # Try RSS 2.0 <item> first, then Atom <entry>
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//atom:entry", ns)

    best = None
    best_dt = None

    for item in items:
        # Title
        title_el = item.find("title")
        if title_el is None:
            title_el = item.find("atom:title", ns) or item.find(
                "{http://www.w3.org/2005/Atom}title"
            )
        if title_el is None:
            continue
        title = clean_title(title_el.text or "")

        # Link
        link_el = item.find("link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "") or (link_el.text or "")
        if not link:
            link_el = item.find("atom:link", ns) or item.find(
                "{http://www.w3.org/2005/Atom}link"
            )
            if link_el is not None:
                link = link_el.get("href", "") or (link_el.text or "")
        link = link.strip()

        # PubDate
        pub_date_str = ""
        for tag in ("pubDate", "published", "updated"):
            el = item.find(tag)
            if el is None:
                el = item.find(f"atom:{tag}", ns) or item.find(
                    f"{{http://www.w3.org/2005/Atom}}{tag}"
                )
            if el is not None and el.text:
                pub_date_str = el.text.strip()
                break

        if not pub_date_str or not title:
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
                }

    return best


# ── QQ Bot API ──────────────────────────────────────────────
def qq_get_token():
    """Get QQ Bot access token."""
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
    """Send a DM via QQ Bot API."""
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
    """Build the bilingual digest text. EN first, CN second, no URLs in body."""
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    )
    mmdd = today.strftime("%m%d")

    # Pick emojis — use mmdd to seed a deterministic pick
    week = today.isocalendar()[1]
    offset = (week * 7) % len(EMOJI_POOL)
    emojis = EMOJI_POOL[offset:] + EMOJI_POOL[:offset]

    lines = [f"# Daily Updates · {mmdd}", ""]

    for i, a in enumerate(articles):
        emoji = emojis[i % len(emojis)]
        cat = a["category"]
        title = a["title"]

        lines.append(f"{emoji} [{cat}] {title}")
        lines.append("")
        lines.append(
            f"*{a.get('en_summary', 'Summary unavailable.')}*"
        )
        lines.append("")
        lines.append(a.get("cn_summary", "摘要不可用。"))
        lines.append("")

    return "\n".join(lines).strip()


def summarize(title, source, category):
    """Generate placeholder summaries. In production, an LLM would do this.
    For now we use the title as summary since we can't call an LLM in pure Python."""
    # GitHub Actions free tier has no LLM — use source + title as context.
    # The digest is still useful as a headline roundup.
    en = f"{title} — via {source}."
    cn = f"{title} — 来源：{source}。"
    return en, cn


# ── Main ────────────────────────────────────────────────────
def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)

    all_articles = []
    for category, sources in RSS_SOURCES.items():
        for name, url in sources:
            result = fetch_feed(name, url, cutoff)
            if result:
                result["category"] = category
                en_sum, cn_sum = summarize(
                    result["title"], result["source"], category
                )
                result["en_summary"] = en_sum
                result["cn_summary"] = cn_sum
                all_articles.append(result)
                print(
                    f"  OK  [{category}] {name}: {result['title'][:60]}"
                )
            else:
                print(f"  --  [{category}] {name}: no recent items")

    # Sort by date descending, take top MAX_ITEMS
    all_articles.sort(key=lambda x: x["date"], reverse=True)
    top = all_articles[:MAX_ITEMS]

    if not top:
        print("No articles in window — exiting silently.")
        return

    digest = build_digest(top)

    print(f"\n=== Digest ({len(top)} items) ===\n")
    print(digest)
    print(f"\n=== Sending to QQ {QQ_OPENID[:8]}... ===")

    token = qq_get_token()
    result = qq_send_dm(token, digest)
    print(f"QQ response: {json.dumps(result, ensure_ascii=False)}")

    print("Done!")


if __name__ == "__main__":
    main()
