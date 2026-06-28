"""
Daily Tech Digest — fetch 13 RSS feeds, filter 3-day freshness,
generate bilingual digest (EN-first) with DeepSeek AI summaries, push to QQ Bot.

GitHub Actions cron: daily at 1:00 UTC (9:00 Beijing time).

Usage:
    python digest.py          # full run: fetch + summarize + send to QQ
    python digest.py --preview # fetch + summarize, print only (no QQ send)
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
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
CUTOFF_DAYS = 3
MAX_ITEMS = 6
SUMMARY_MAX_TOKENS = 300       # ~2 EN sentences + ~2 CN sentences
DEEPSEEK_TIMEOUT = 30          # seconds

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


def fetch_feed(source_name, url, cutoff):
    """Return newest item within cutoff, or None."""
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
    """Parse RSS/Atom XML and return newest item within cutoff."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    items = root.findall(".//item") or root.findall(f".//{{{ATOM_NS}}}entry")
    best = None
    best_dt = None

    for item in items:
        title_el = _find_el(item, "title")
        title = clean_text(title_el.text if title_el is not None else "")
        if not title:
            continue

        link_el = _find_el(item, "link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "") or (link_el.text or "")
        link = link.strip()

        desc_el = (
            _find_el(item, "description")
            or _find_el(item, "summary")
            or _find_el(item, "content")
        )
        desc = clean_text(desc_el.text if desc_el is not None else "")

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


def _fetch_article_text(url, max_chars=500):
    """Fetch and extract the first substantive paragraph from an article page."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx()) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    html = re.sub(
        r"<(script|style|noscript|iframe|nav|footer|header)[^>]*>.*?</\1>",
        "", html, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    sentences = re.split(r"(?<=[.!?])\s+", text)
    result = []
    length = 0
    for s in sentences:
        s = s.strip()
        if len(s) < 20:
            continue
        result.append(s)
        length += len(s)
        if length >= max_chars:
            break

    return " ".join(result) if result else None


def _is_bad_description(desc, title):
    """Check if description is empty, identical to title, or too short."""
    if not desc or desc == title:
        return True
    clean_desc = re.sub(
        r"^(Read more|Continue reading|Click here)[:.]?\s*", "",
        desc, flags=re.IGNORECASE,
    ).strip()
    if clean_desc == title or len(clean_desc) < 30:
        return True
    return False


# ── DeepSeek AI Summarization ───────────────────────────────
def summarize_with_deepseek(title, description):
    """
    Use DeepSeek to generate ~2 English sentences + ~2 Chinese sentences.
    Returns formatted string, or falls back to raw description on failure.
    
    Cost control: max_tokens=300, timeout=30s, single attempt (no retry).
    """
    if not DEEPSEEK_API_KEY:
        print("  [DeepSeek] No API key — using raw description")
        return f"*{description}*"

    prompt = (
        f"Summarize this tech article in exactly 2 short, informative English sentences, "
        f"then translate those same 2 sentences into simplified Chinese.\n\n"
        f"Title: {title}\n"
        f"Content: {description}\n\n"
        f"Reply in this EXACT format with no extra commentary:\n"
        f"EN: [2 English sentences]\n"
        f"CN: [2 Chinese sentences]"
    )

    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": SUMMARY_MAX_TOKENS,
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=DEEPSEEK_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        print(f"  [DeepSeek] API call failed: {e} — falling back to raw description")
        return f"*{description}*"

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        return f"*{description}*"

    # Parse EN: / CN: sections
    en_match = re.search(r"EN:\s*(.+?)(?=\nCN:|\Z)", content, re.DOTALL)
    cn_match = re.search(r"CN:\s*(.+)", content, re.DOTALL)

    en_text = en_match.group(1).strip() if en_match else ""
    cn_text = cn_match.group(1).strip() if cn_match else ""

    if not en_text and not cn_text:
        # Parse failed — use raw content
        return f"*{content.strip()[:500]}*"

    parts = []
    if en_text:
        parts.append(f"*{en_text}*")
    if cn_text:
        parts.append(cn_text)
    return "\n".join(parts)


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

    for i, a in enumerate(articles, 1):
        emoji = emojis[(i - 1) % len(emojis)]
        num = f"{i:02d}"
        cat = a["category"]
        title = a["title"]
        summary = a.get("ai_summary", "")

        lines.append(f"{emoji} {num} [{cat}] {title}")
        lines.append("")
        if summary:
            lines.append(summary)
        else:
            desc = a.get("description", title)
            lines.append(f"*{desc}*")
        lines.append("")

    return "\n".join(lines).strip()


# ── Main ────────────────────────────────────────────────────
def main():
    preview = "--preview" in sys.argv

    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)

    print("=== Fetching RSS feeds ===")
    all_articles = []
    for category, sources in RSS_SOURCES.items():
        for name, url in sources:
            result = fetch_feed(name, url, cutoff)
            if result:
                result["category"] = category
                all_articles.append(result)
                print(f"  OK  [{category}] {name}: {result['title'][:60]}")

    all_articles.sort(key=lambda x: x["date"], reverse=True)
    top = all_articles[:MAX_ITEMS]

    # Enrich thin descriptions by fetching article text
    for a in top:
        if _is_bad_description(a.get("description", ""), a.get("title", "")):
            link = a.get("link", "")
            if link:
                better = _fetch_article_text(link)
                if better and len(better) > len(a.get("description", "")):
                    a["description"] = better

    if not top:
        print("No articles in window — exiting silently.")
        return

    # AI Summarization via DeepSeek
    print(f"\n=== Summarizing {len(top)} articles with DeepSeek ===")
    for a in top:
        title = a["title"]
        desc = a.get("description", title)
        print(f"  Summarizing: {title[:60]}...")
        a["ai_summary"] = summarize_with_deepseek(title, desc)

    digest = build_digest(top)

    print(f"\n=== Digest ({len(top)} items) ===\n")
    print(digest)

    if preview:
        print("\n=== PREVIEW MODE — not sent to QQ ===")
        return

    print(f"\n=== Sending to QQ ... ===")
    token = qq_get_token()
    result = qq_send_dm(token, digest)
    print(f"QQ response: {json.dumps(result, ensure_ascii=False)}")
    print("Done!")


if __name__ == "__main__":
    main()
