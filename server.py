from __future__ import annotations

import html
import base64
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from flask import Flask, Response, jsonify, request, send_from_directory


app = Flask(__name__)
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "ahead_history.db"
KOKORO_DIR = APP_DIR / "models" / "kokoro"
KOKORO_MODEL_PATH = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES_PATH = KOKORO_DIR / "voices-v1.0.bin"
RUNTIME_KEYS: dict[str, str] = {}

REQUEST_TIMEOUT_SECONDS = 14
ARTICLE_FETCH_TIMEOUT_SECONDS = 8
MAX_ARTICLE_TEXT_CHARS = 6500
MAX_ITEMS_PER_FEED = 100
RETURN_ARTICLE_COUNT = 10
NEWS_WINDOW_HOURS = 48
LOCKED_TOP_STORY_COUNT = 0
CANDIDATE_POOL_SIZE = 80
STRICT_RELEVANCE_FLOOR = 58
RELAXED_RELEVANCE_FLOOR = 48
MAX_ARTICLES_PER_SOURCE = 4
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE = "Kore"
KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_history (
            article_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT NOT NULL,
            published_at TEXT NOT NULL,
            first_shown_at TEXT NOT NULL,
            last_shown_at TEXT NOT NULL,
            shown_count INTEGER NOT NULL DEFAULT 0,
            read_at TEXT
        )
        """
    )
    return conn


def history_counts() -> dict[str, int]:
    with db_connect() as conn:
        rows = conn.execute("SELECT article_id, shown_count FROM article_history").fetchall()
    return {row["article_id"]: int(row["shown_count"] or 0) for row in rows}


def save_shown_articles(articles: list["Article"]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        for article in articles:
            conn.execute(
                """
                INSERT INTO article_history (
                    article_id, title, url, source, published_at,
                    first_shown_at, last_shown_at, shown_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(article_id) DO UPDATE SET
                    title=excluded.title,
                    url=excluded.url,
                    source=excluded.source,
                    published_at=excluded.published_at,
                    last_shown_at=excluded.last_shown_at,
                    shown_count=article_history.shown_count + 1
                """,
                (
                    article.article_id,
                    article.title,
                    article.url,
                    article.source,
                    article.published_at.isoformat(),
                    now,
                    now,
                ),
            )
        conn.commit()


def mark_articles_read(article_ids: list[str]) -> int:
    if not article_ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    valid_ids = [item for item in article_ids if re.fullmatch(r"[a-f0-9]{8,64}", item)]
    if not valid_ids:
        return 0
    with db_connect() as conn:
        conn.executemany("UPDATE article_history SET read_at = ? WHERE article_id = ?", [(now, item) for item in valid_ids])
        conn.commit()
        return conn.total_changes


PROMOTIONAL_RE = re.compile(
    r"\b("
    r"course|masterclass|webinar|bootcamp|certification|deal|discount|price\s*drop|"
    r"how\s+to\s+buy|buy\s+now|sale|coupon|promo|voucher|cashback|best\s+price|"
    r"limited\s+time|affiliate|sponsored|shopping|shop\s+now|subscribe\s+and\s+save|"
    r"giveaway|black\s+friday|cyber\s+monday|early\s+bird|enroll|registration\s+open|"
    r"clearance|markdown|flash\s+sale|doorbuster|gift\s+card|refurbished"
    r")\b",
    re.IGNORECASE,
)

PROMOTIONAL_PRICE_RE = re.compile(
    r"(?:^|\s)(?:\$|rs\.?|inr)\s?\d{1,6}\b(?!\s?(million|billion|trillion|crore|lakh))",
    re.IGNORECASE,
)

LOW_VALUE_RE = re.compile(
    r"\b("
    r"celebrity|movie|trailer|box\s+office|gaming|gameplay|smartphone|phone\s+review|"
    r"laptop\s+review|camera\s+review|unboxing|rumour|rumor|leak|tips|tricks|"
    r"best\s+apps|recipe|fashion|sports|football|cricket|horoscope|cannes|cinematic|film|"
    r"bollywood|hollywood|ott|web\s+series|awards?|gossip|"
    r"lottery|quiz|viral|meme|local\s+crime|murder|robbery|pope|church|religion|religious|encyclical|sermon|festival|"
    r"weather\s+forecast|rainfall|heatwave|state\s+politics|political\s+row|"
    r"congress'? dig|dig\s+at|political\s+attack|why\s+.+\s+thinks|"
    r"green\s+cards?|visa\s+backlog|immigration\s+paperwork|citizenship\s+applications?|"
    r"family-based\s+applications?|employment-based\s+green\s+card|uscis|h-?1b\s+lottery|"
    r"starship\s+launch|rocket\s+launch|space\s+launch|why\s+is\s+the\s+indian\s+rupee\s+falling|"
    r"nurseries?|childcare|school\s+fees?|campaigners\s+say|time-?travellers?|vlogging|"
    r"opinion|editorial|column|commentary|press\s+release|sponsored\s+content|market\s+to\s+reach|market\s+size|"
    r"funding\s+grabbed|student-led\s+startup|"
    r"according\s+to\s+the\s+report|research\s+and\s+markets|globenewswire|openpr"
    r")\b",
    re.IGNORECASE,
)

BLOCKED_SOURCE_RE = re.compile(
    r"\b("
    r"latestly|vocal\.media|openpr|globenewswire|pr\s*newswire|ein\s*news|"
    r"analytics\s+insight|coingape|benzinga|ambcrypto|cryptopolitan|startup\s+fortune|"
    r"hindustan\s+times\s+tech|gizmodo|screenrant|pinkvilla"
    r")\b",
    re.IGNORECASE,
)

TRUSTED_SOURCE_RE = re.compile(
    r"\b("
    r"reuters|bloomberg|associated\s+press|ap\s+news|bbc|financial\s+times|"
    r"wall\s+street\s+journal|cnbc|marketwatch|fortune|forbes|axios|"
    r"economic\s+times|livemint|mint|business\s+standard|the\s+hindu|"
    r"moneycontrol|times\s+of\s+india|ndtv\s+profit|inc42|techcrunch|"
    r"the\s+guardian|npr|federal\s+reserve|sec|imf|world\s+bank"
    r")\b",
    re.IGNORECASE,
)

MACRO_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets", "https://feeds.reuters.com/reuters/marketsNews"),
    ("Reuters Technology", "https://feeds.reuters.com/reuters/technologyNews"),
    ("AP Business", "https://apnews.com/hub/business?output=rss"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("The Guardian Business", "https://www.theguardian.com/business/rss"),
    ("The Guardian Technology", "https://www.theguardian.com/technology/rss"),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedsdefault.cms"),
    ("Economic Times Tech", "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms"),
    ("Livemint", "https://www.livemint.com/rss/news"),
    ("The Hindu Business", "https://www.thehindu.com/business/feeder/default.rss"),
    ("NPR Business", "https://feeds.npr.org/1006/rss.xml"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss"),
    ("IMF", "https://www.imf.org/en/News/RSS"),
    ("World Bank", "https://www.worldbank.org/en/news/all?format=rss"),
]

GOOGLE_NEWS_QUERIES = [
    "artificial intelligence automation business investment",
    "AI jobs workplace automation skills",
    "OpenAI Anthropic Google AI enterprise business",
    "technology funding startups India AI",
    "finance markets central bank interest rates business",
    "semiconductor chips data centers cloud AI",
    "India business economy investment funding",
    "global business regulation AI policy",
    "stock market AI chips data center investment",
    "layoffs hiring workforce automation companies",
    "venture funding artificial intelligence startups",
    "RBI SEBI Federal Reserve markets economy",
]

SOURCE_WEIGHTS = {
    "Federal Reserve": 14,
    "SEC": 12,
    "Reuters": 12,
    "IMF": 11,
    "World Bank": 11,
    "AP": 9,
    "BBC": 8,
    "Economic Times": 9,
    "Livemint": 8,
    "The Hindu": 8,
    "The Guardian": 6,
    "NPR": 5,
    "Bloomberg": 12,
    "Financial Times": 11,
    "CNBC": 9,
    "Business Standard": 8,
    "Hindustan Times": 6,
    "Times of India": 6,
    "Forbes": 5,
    "TechCrunch": 5,
}

HIGH_IMPACT_TERMS = {
    "artificial intelligence": 24,
    "generative ai": 24,
    "ai infrastructure": 26,
    "foundation model": 24,
    "model release": 22,
    "openai": 22,
    "anthropic": 22,
    "google deepmind": 20,
    "automation": 22,
    "agent": 14,
    "enterprise ai": 22,
    "investment": 20,
    "invest": 12,
    "funding": 16,
    "financing": 16,
    "capital expenditure": 20,
    "capex": 20,
    "billion": 18,
    "trillion": 24,
    "data center": 20,
    "semiconductor": 20,
    "chip": 14,
    "nvidia": 18,
    "microsoft": 14,
    "reliance": 18,
    "tata": 16,
    "infosys": 12,
    "wipro": 12,
    "regulation": 18,
    "regulator": 16,
    "policy": 14,
    "antitrust": 16,
    "tariff": 16,
    "sanctions": 16,
    "central bank": 16,
    "interest rate": 14,
    "inflation": 14,
    "gdp": 14,
    "jobs": 18,
    "layoffs": 18,
    "hiring": 12,
    "workforce": 20,
    "workplace": 16,
    "productivity": 16,
    "supply chain": 16,
    "merger": 14,
    "acquisition": 14,
    "ipo": 12,
    "bankruptcy": 16,
    "restructuring": 16,
}

LOW_IMPACT_TERMS = {
    "review": -20,
    "tips": -18,
    "guide": -12,
    "opinion": -28,
    "editorial": -28,
    "view": -18,
    "rumor": -12,
    "leak": -12,
    "gadget": -16,
    "smartphone": -14,
    "gaming": -18,
    "celebrity": -22,
    "movie": -20,
    "green card": -55,
    "visa": -35,
    "immigration": -45,
    "citizenship": -45,
    "uscis": -55,
    "launch": -22,
    "politics": -22,
    "trump": -16,
}

CORE_RELEVANCE_RE = re.compile(
    r"\b("
    r"ai|artificial intelligence|automation|robotics|agent|model|openai|anthropic|gemini|"
    r"investment|funding|financing|capital|billion|trillion|markets?|stocks?|bank|"
    r"finance|economy|gdp|inflation|interest rate|central bank|rbi|sebi|sec|"
    r"business|enterprise|workforce|jobs?|hiring|layoffs?|skills?|productivity|"
    r"semiconductor|chips?|data center|cloud|supply chain|regulation|policy|tariff|"
    r"merger|acquisition|ipo|restructuring|earnings|revenue"
    r")\b",
    re.IGNORECASE,
)

VERY_STRONG_RELEVANCE_RE = re.compile(
    r"\b("
    r"openai|anthropic|artificial intelligence|automation|funding|investment|billion|"
    r"workforce|jobs?|hiring|layoffs?|semiconductor|chips?|data center|central bank|"
    r"regulation|policy|ipo|merger|acquisition|restructuring|inflation|gdp"
    r")\b",
    re.IGNORECASE,
)

ALLOWED_TOPIC_RE = re.compile(
    r"\b("
    r"ai|artificial intelligence|automation|robotics|openai|anthropic|gemini|nvidia|"
    r"google|microsoft|apple|meta|amazon|cybersecurity|chips?|semiconductor|cloud|data\s*cent(?:er|re)s?|"
    r"jobs?|hiring|layoffs?|skills?|upskilling|freshers?|gcc|workforce|"
    r"startup|funding|ipo|shutdown|investment|rbi|sebi|inflation|gdp|tax|upi|fintech|"
    r"sensex|nifty|nasdaq|crypto|banking|insurance|epf|loan|isro|education|neet|jee|upsc|"
    r"india|indian|economy|business|markets?|stocks?"
    r")\b",
    re.IGNORECASE,
)

MASS_IMPACT_RE = re.compile(
    r"\b(million|crore|lakh|nationwide|india|indian|rbi|sebi|sensex|nifty|upi|jobs?|workers?|students?|consumers?|users?|taxpayers?)\b",
    re.IGNORECASE,
)

READER_CARE_RE = re.compile(
    r"\b(ai|automation|jobs?|hiring|layoffs?|skills?|money|finance|investment|funding|startup|markets?|tax|upi|rbi|sebi|education|technology|cybersecurity|chips?|data\s*cent(?:er|re)s?)\b",
    re.IGNORECASE,
)


@dataclass
class Article:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str
    impact_score: int = 1
    curation_score: int = 0
    priority_group: int = 4
    full_context: str = ""
    context: str = ""
    background: str = ""
    what_happened: str = ""
    short_explanation: str = ""
    why_it_matters: str = ""
    hinglish_summary: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def metadata(self) -> str:
        return f"{self.source} \u2022 {self.published_at.day} {self.published_at.strftime('%B')} {self.published_at.year}"

    @property
    def article_id(self) -> str:
        return sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "id": self.article_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "metadata": self.metadata,
            "summary": self.summary,
            "impact_score": self.impact_score,
            "curation_score": self.curation_score,
            "priority_group": self.priority_group,
            "full_context": self.full_context,
            "context": self.context,
            "background": self.background,
            "what_happened": self.what_happened,
            "short_explanation": self.short_explanation,
            "why_it_matters": self.why_it_matters,
            "hinglish_summary": self.hinglish_summary,
            "tags": self.tags,
        }


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    decoded = html.unescape(without_tags)
    return re.sub(r"\s+", " ", decoded).strip()


class ReadableTextParser(HTMLParser):
    BLOCK_TAGS = {"p", "li", "h1", "h2", "h3"}
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.article_depth = 0
        self.current_tag: str | None = None
        self.current_parts: list[str] = []
        self.article_paragraphs: list[str] = []
        self.other_paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "article":
            self.article_depth += 1
        if tag in self.BLOCK_TAGS:
            self.current_tag = tag
            self.current_parts = []

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not self.current_tag:
            return
        if data and data.strip():
            self.current_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == self.current_tag:
            text = clean_text(" ".join(self.current_parts))
            if len(text) >= 45:
                target = self.article_paragraphs if self.article_depth else self.other_paragraphs
                target.append(text)
            self.current_tag = None
            self.current_parts = []
        if tag == "article" and self.article_depth:
            self.article_depth -= 1

    def readable_text(self) -> str:
        paragraphs = self.article_paragraphs if len(self.article_paragraphs) >= 2 else self.other_paragraphs
        seen: set[str] = set()
        selected: list[str] = []
        total = 0
        for paragraph in paragraphs:
            key = re.sub(r"[^a-z0-9]+", " ", paragraph.lower())[:120]
            if key in seen:
                continue
            seen.add(key)
            selected.append(paragraph)
            total += len(paragraph)
            if total >= MAX_ARTICLE_TEXT_CHARS:
                break
        return clean_text(" ".join(selected))[:MAX_ARTICLE_TEXT_CHARS]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None

    if parsed is None:
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def first_text(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return clean_text(found.text)
    return ""


def first_child(element: ET.Element, names: Iterable[str]) -> ET.Element | None:
    for name in names:
        found = element.find(name)
        if found is not None:
            return found
    return None


def first_link(element: ET.Element) -> str:
    link = first_text(element, ["link", "{http://www.w3.org/2005/Atom}link"])
    if link:
        return link
    atom_link = element.find("{http://www.w3.org/2005/Atom}link")
    if atom_link is not None:
        return atom_link.attrib.get("href", "")
    return ""


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AHEAD-AI-Macro-News/2.0",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def extract_article_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AHEAD-AI-NewsReader/2.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=ARTICLE_FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return ""
            charset = response.headers.get_content_charset() or "utf-8"
            html_text = response.read(900_000).decode(charset, errors="ignore")
    except Exception:
        return ""

    parser = ReadableTextParser()
    try:
        parser.feed(html_text)
    except Exception:
        return ""
    return plain_terms(parser.readable_text())


def normalize_source(source_name: str) -> str:
    source_name = clean_text(source_name)
    if source_name.startswith("Reuters"):
        return "Reuters"
    if source_name.startswith("BBC"):
        return "BBC"
    if source_name.startswith("The Guardian"):
        return "The Guardian"
    if source_name.startswith("Economic Times"):
        return "Economic Times"
    return source_name


def google_news_feeds() -> list[tuple[str, str]]:
    feeds: list[tuple[str, str]] = []
    for query in GOOGLE_NEWS_QUERIES:
        encoded = urllib.parse.quote_plus(f"({query}) when:2d")
        feeds.append(
            (
                "Google News",
                f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en",
            )
        )
    return feeds


def all_discovery_feeds() -> list[tuple[str, str]]:
    return MACRO_FEEDS + google_news_feeds()


def parse_feed(source_name: str, url: str) -> list[Article]:
    try:
        root = ET.fromstring(fetch_url(url))
    except Exception:
        return []

    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    articles: list[Article] = []
    for item in items[:MAX_ITEMS_PER_FEED]:
        title = first_text(item, ["title", "{http://www.w3.org/2005/Atom}title"])
        article_url = first_link(item)
        google_source = first_child(item, ["source"])
        item_source = source_name
        if source_name == "Google News" and google_source is not None and google_source.text:
            item_source = clean_text(google_source.text)
        summary = first_text(
            item,
            [
                "description",
                "summary",
                "{http://www.w3.org/2005/Atom}summary",
                "{http://purl.org/rss/1.0/modules/content/}encoded",
            ],
        )
        published = parse_date(
            first_text(
                item,
                [
                    "pubDate",
                    "published",
                    "updated",
                    "{http://www.w3.org/2005/Atom}published",
                    "{http://www.w3.org/2005/Atom}updated",
                ],
            )
        )

        if title and article_url and published:
            if source_name == "Google News":
                title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
            articles.append(
                Article(
                    title=title,
                    url=article_url,
                    source=normalize_source(item_source),
                    published_at=published,
                    summary=summary or title,
                )
            )
    return articles


def is_junk(article: Article) -> bool:
    haystack = f"{article.title} {article.summary} {article.url}"
    return bool(
        BLOCKED_SOURCE_RE.search(article.source)
        or PROMOTIONAL_RE.search(haystack)
        or PROMOTIONAL_PRICE_RE.search(haystack)
        or LOW_VALUE_RE.search(haystack)
    )


def relevance_signal_count(article: Article) -> int:
    text = f"{article.title} {article.summary}"
    return len(set(match.group(0).lower() for match in CORE_RELEVANCE_RE.finditer(text)))


def trusted_source(article: Article) -> bool:
    return bool(TRUSTED_SOURCE_RE.search(article.source))


def curation_score_5(article: Article, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    text = f"{article.title} {article.summary} {article.source}"
    score = 0
    if MASS_IMPACT_RE.search(text) or re.search(r"\b\d+(\.\d+)?\s?(million|billion|trillion|crore|lakh)\b", text, re.IGNORECASE):
        score += 1
    if READER_CARE_RE.search(text):
        score += 1
    if trusted_source(article):
        score += 1
    age_hours = max(0.0, (now - article.published_at.astimezone(timezone.utc)).total_seconds() / 3600)
    if age_hours <= 48:
        score += 1
    if re.search(r"\b(student|professional|employee|worker|job|skill|startup|business|investor|market|money|india|indian|consumer|user)\b", text, re.IGNORECASE):
        score += 1
    return score


def public_tags(article: Article) -> list[str]:
    text = f"{article.title} {article.summary}".lower()
    tags: list[str] = []
    if re.search(r"\b(ai|artificial intelligence|openai|anthropic|gemini|model|agent)\b", text):
        tags.append("AI")
    if re.search(r"\b(automation|robotics|productivity)\b", text):
        tags.append("Automation")
    if re.search(r"\b(technology|chips?|semiconductor|cloud|data\s*cent(?:er|re)s?|nvidia|microsoft|google|apple|meta|amazon)\b", text):
        tags.append("Technology")
    if re.search(r"\b(jobs?|hiring|layoffs?|skills?|freshers?|workforce|gcc)\b", text):
        tags.append("Jobs")
    if re.search(r"\b(funding|startup|ipo|valuation|venture|business|company|companies)\b", text):
        tags.append("Startup" if "startup" in text else "Business")
    if re.search(r"\b(rbi|sebi|inflation|gdp|economy|tax|policy|government|isro|education|neet|jee|upsc)\b", text):
        tags.append("India Policy" if re.search(r"\b(policy|government|rbi|sebi|tax|education|neet|jee|upsc|isro)\b", text) else "Economy")
    if re.search(r"\b(markets?|stocks?|sensex|nifty|nasdaq|bank|upi|fintech|crypto|loan|insurance|epf)\b", text):
        tags.append("Finance")
    if re.search(r"\b(cybersecurity|cyber attack|data breach|ransomware)\b", text):
        tags.append("Cybersecurity")
    if re.search(r"\b(global|china|us|europe|japan|sri lanka|federal reserve|nasdaq)\b", text):
        tags.append("Global Markets")
    return list(dict.fromkeys(tags))[:4] or ["Business"]


def priority_group(article: Article) -> int:
    text = f"{article.title} {article.summary}".lower()
    area = story_area(article)
    if area in {"ai", "jobs"}:
        return 1
    if area == "compute" and re.search(r"\b(ai|artificial intelligence|nvidia|chips?|semiconductor|data\s*cent(?:er|re)s?)\b", text):
        return 1
    if area in {"investment", "finance", "business"} or re.search(r"\b(startup|funding|investment|ipo|markets?|stocks?|rbi|sebi|inflation|gdp|bank|finance|sensex|nifty)\b", text):
        return 2
    if re.search(r"\b(india policy|education|neet|jee|upsc|isro|government|policy|global|china|us|europe|federal reserve)\b", text):
        return 3
    return 4


def is_relevant(article: Article) -> bool:
    text = f"{article.title} {article.summary}"
    if not ALLOWED_TOPIC_RE.search(text):
        return False
    if curation_score_5(article) < 3:
        return False
    if not CORE_RELEVANCE_RE.search(text):
        return False
    must_have_core = bool(
        re.search(
            r"\b("
            r"ai|artificial intelligence|automation|funding|investment|financing|billion|"
            r"markets?|stocks?|central bank|rbi|sebi|sec|inflation|interest rate|"
            r"hiring|layoffs?|skills?|semiconductor|chips?|data centers?|cloud|"
            r"merger|acquisition|ipo|earnings|revenue|restructuring|nvidia|openai|anthropic|"
            r"reliance|tata|infosys|wipro|startup"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )
    if not must_have_core:
        return False
    practical_business_impact = bool(
        re.search(
            r"\b("
            r"startup|company|companies|firms?|funds?|investors?|investment|funding|financing|"
            r"markets?|stocks?|bank|central bank|rbi|sebi|sec|earnings|revenue|"
            r"hiring|layoffs?|workforce|skills?|automation|ai cloud|ai infrastructure|"
            r"chips?|semiconductor|data centers?|cloud|merger|acquisition|ipo|restructuring"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )
    if not practical_business_impact:
        return False
    personal_policy_story = bool(
        re.search(
            r"\b(green\s+cards?|visa\s+backlog|immigration|citizenship|uscis|family-based|employment-based\s+green\s+card)\b",
            text,
            re.IGNORECASE,
        )
    )
    if personal_policy_story and not re.search(
        r"\b(ai|automation|layoffs?|hiring|skills?|workforce\s+planning|company|companies|tech\s+sector|investment|funding|markets?)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    has_business_use = bool(
        re.search(
            r"\b("
            r"investment|funding|financing|markets?|stocks?|bank|economy|inflation|interest rate|"
            r"central bank|rbi|sebi|sec|hiring|layoffs?|skills?|workplace|"
            r"semiconductor|chips?|data centers?|cloud|ai regulation|business regulation|tariff|merger|"
            r"acquisition|ipo|earnings|revenue|data center|automation|productivity|restructuring"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )
    generic_policy_only = bool(re.search(r"\b(policy|law|rules?|legal|court|lawsuit|probe|investigation)\b", text, re.IGNORECASE)) and not re.search(
        r"\b(ai|automation|business|company|companies|market|bank|central bank|investment|funding|chips?|data center|hiring|layoffs?|tariff|rbi|sebi|sec)\b",
        text,
        re.IGNORECASE,
    )
    if generic_policy_only:
        return False
    weak_ai_only = bool(re.search(r"\b(ai|artificial intelligence)\b", text, re.IGNORECASE)) and not has_business_use
    if weak_ai_only:
        return False
    return has_business_use and (relevance_signal_count(article) >= 2 or bool(VERY_STRONG_RELEVANCE_RE.search(text)))


def in_window(article: Article, start: datetime, end: datetime) -> bool:
    return start <= article.published_at <= end


def deduplicate(articles: Iterable[Article]) -> list[Article]:
    seen: set[str] = set()
    unique: list[Article] = []
    for article in articles:
        key_text = re.sub(
            r"\b(reuters|bloomberg|ap news|bbc|economic times|the hindu|guardian|cnbc|financial times|exclusive|update)\b",
            " ",
            article.title.lower(),
        )
        key_text = re.sub(r"[^a-z0-9]+", " ", key_text).strip()
        if len(key_text) < 12:
            continue
        key = sha256(key_text.encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(article)
    return unique


def topic_key(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    groups = [
        ("openai_jobs", ("openai", "jobs")),
        ("ai_hiring", ("ai", "hiring")),
        ("ai_datacenter", ("ai", "data center")),
        ("ai_markets", ("ai", "stock")),
        ("funding", ("funding",)),
        ("rates", ("rate", "central bank")),
        ("chips", ("chip", "semiconductor")),
    ]
    for key, terms in groups:
        if all(term in text for term in terms):
            return key
    words = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).split()
    useful = [w for w in words if len(w) > 4][:4]
    return "_".join(useful) or article.url


def score_article(article: Article) -> tuple[int, list[str]]:
    text = f"{article.title} {article.summary}".lower()
    score = 16
    tags: list[str] = []

    for source, weight in SOURCE_WEIGHTS.items():
        if article.source.startswith(source):
            score += weight
            tags.append("Trusted Source")
            break

    for term, weight in HIGH_IMPACT_TERMS.items():
        if term in text:
            score += weight
            tags.append(term.title())

    for term, weight in LOW_IMPACT_TERMS.items():
        if term in text:
            score += weight

    if re.search(r"\b(view|editorial|opinion|commentary|analysis\s+piece|column)\b", text):
        score -= 24

    if re.search(r"\b(pope|church|religion|religious|encyclical|celebrity|film|sports)\b", text):
        score -= 34

    if re.search(r"\b(green\s+cards?|visa\s+backlog|immigration|citizenship|uscis|family-based)\b", text):
        score -= 60

    if re.search(r"\b(trump|election|politics|rocket launch|space launch|starship launch)\b", text) and not re.search(
        r"\b(ai|automation|investment|funding|markets?|stocks?|chips?|data center|central bank|company|business)\b",
        text,
    ):
        score -= 35

    if re.search(r"\b(ai|artificial intelligence|automation|funding|investment|financing|billion|chips?|semiconductor|data center|nvidia|openai|anthropic)\b", text):
        score += 18

    if re.search(r"\b(\d+(\.\d+)?)\s?(billion|trillion|crore|lakh)\b", text, re.IGNORECASE):
        score += 20
        tags.append("Material Scale")

    if re.search(r"\b(india|indian|rbi|sebi|reliance|tata|infosys|wipro|nasscom)\b", text):
        score += 12
        tags.append("India Lens")

    if re.search(r"\b(law|bill|rules?|policy|ban|approved|approval|probe|investigation|compliance)\b", text):
        score += 14
        tags.append("Policy Shift")

    if re.search(r"\b(ai regulation|business regulation|central bank|rbi|sebi|sec|tariff|market rules)\b", text):
        score += 14

    if re.search(r"\b(workers?|employees?|jobs?|layoffs?|hiring|wages?|workplace|skills?|reskilling)\b", text):
        score += 14
        tags.append("Workforce")

    if re.search(r"\b(ai|artificial intelligence|automation|robotics|agents?|models?|compute)\b", text):
        score += 16
        tags.append("Automation")

    if re.search(r"\b(global|china|us|europe|eu|japan|supply chain|exports?|imports?|geopolitical)\b", text):
        score += 8
        tags.append("Global Macro")

    if len(article.summary) > 180:
        score += 4

    return max(1, min(100, score)), public_tags(article)


def freshness_mix_value(article: Article, refresh_seed: str) -> float:
    digest = sha256(f"{refresh_seed}:{article.url}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def mixed_rank_score(article: Article, refresh_seed: str, shown_counts: dict[str, int], seen_ids: set[str]) -> float:
    shown_penalty = min(60, shown_counts.get(article.article_id, 0) * 22)
    session_penalty = 18 if article.article_id in seen_ids else 0
    return article.impact_score - shown_penalty - session_penalty + (freshness_mix_value(article, refresh_seed) * 10)


def select_fresh_top_mix(
    ranked: list[Article],
    refresh_seed: str,
    seen_ids: set[str],
    shown_counts: dict[str, int],
) -> tuple[list[Article], int, int]:
    pool = ranked[: max(CANDIDATE_POOL_SIZE, RETURN_ARTICLE_COUNT)]
    unseen_pool = [article for article in pool if shown_counts.get(article.article_id, 0) == 0 and article.article_id not in seen_ids]
    selection_pool = unseen_pool if len(unseen_pool) >= RETURN_ARTICLE_COUNT else pool
    locked = [] if shown_counts else ranked[: min(LOCKED_TOP_STORY_COUNT, len(ranked))]
    selected: list[Article] = []
    selected_urls: set[str] = set()
    per_source: dict[str, int] = {}
    per_topic: set[str] = set()

    def add(article: Article, enforce_diversity: bool = True) -> bool:
        if article.url in selected_urls:
            return False
        if enforce_diversity and per_source.get(article.source, 0) >= MAX_ARTICLES_PER_SOURCE:
            return False
        key = topic_key(article)
        if enforce_diversity and key in per_topic:
            return False
        selected.append(article)
        selected_urls.add(article.url)
        per_source[article.source] = per_source.get(article.source, 0) + 1
        per_topic.add(key)
        return True

    for article in locked:
        add(article, enforce_diversity=False)
        if len(selected) == RETURN_ARTICLE_COUNT:
            return selected, len(pool), len(unseen_pool)

    rotating_pool = sorted(
        [article for article in selection_pool if article.url not in selected_urls],
        key=lambda item: (priority_group(item), -mixed_rank_score(item, refresh_seed, shown_counts, seen_ids)),
    )

    def add_from_group(group: int, target_count: int) -> None:
        group_pool = sorted(
            [item for item in rotating_pool if priority_group(item) == group and item.url not in selected_urls],
            key=lambda item: (mixed_rank_score(item, refresh_seed, shown_counts, seen_ids), item.published_at),
            reverse=True,
        )
        for article in group_pool:
            add(article, enforce_diversity=True)
            if len(selected) >= target_count or len(selected) == RETURN_ARTICLE_COUNT:
                break

    add_from_group(1, min(3, RETURN_ARTICLE_COUNT))
    add_from_group(2, min(6, RETURN_ARTICLE_COUNT))
    add_from_group(3, min(9, RETURN_ARTICLE_COUNT))

    supplemental_pool = sorted(
        [article for article in selection_pool if article.url not in selected_urls],
        key=lambda item: (
            {3: 4, 2: 3, 1: 2, 4: 1}.get(priority_group(item), 0),
            mixed_rank_score(item, refresh_seed, shown_counts, seen_ids),
            item.published_at,
        ),
        reverse=True,
    )
    for article in supplemental_pool:
        add(article, enforce_diversity=True)
        if len(selected) >= min(9, RETURN_ARTICLE_COUNT):
            break

    wildcard_pool = sorted(
        [article for article in ranked if article.url not in selected_urls],
        key=lambda item: (mixed_rank_score(item, refresh_seed, shown_counts, seen_ids), item.published_at),
        reverse=True,
    )
    for article in wildcard_pool:
        add(article, enforce_diversity=False)
        if len(selected) == RETURN_ARTICLE_COUNT:
            break
    return selected, len(pool), len(unseen_pool)


def context_focus(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    if any(term in text for term in ("ai", "automation", "model", "openai", "anthropic", "agent", "compute")):
        return (
            "This matters because AI is becoming part of everyday business work. When AI changes, companies may change "
            "their budgets, hiring plans, tools, and expectations from employees."
        )
    if any(term in text for term in ("regulation", "law", "policy", "rbi", "sebi", "sec", "federal reserve", "central bank")):
        return (
            "This matters because new rules or central-bank decisions can change how companies borrow money, manage risk, "
            "follow rules, and plan future growth."
        )
    if any(term in text for term in ("jobs", "layoffs", "hiring", "workforce", "employees", "wage", "skills")):
        return (
            "This matters because job news shows which skills companies want, which roles may shrink, and where workers may "
            "need to learn new tools to stay useful."
        )
    if any(term in text for term in ("investment", "funding", "billion", "capex", "data center", "chip", "semiconductor")):
        return (
            "This matters because big investments show where companies believe future demand will grow and where new business "
            "opportunities may appear."
        )
    return (
        "This matters because important business stories usually point to a bigger shift in money, rules, technology, jobs, "
        "or company strategy."
    )


BOILERPLATE_RE = re.compile(
    r"(?i)\b("
    r"catch all|latest news updates|technology news news|click here|read more|subscribe|"
    r"how practical is|can a rajan-style|save the rupee|follow us|download app|"
    r"share this article|advertisement|recommended stories"
    r")\b"
)


def plain_terms(text: str) -> str:
    replacements = {
        "capital allocation": "where money gets invested",
        "capital expenditure": "large spending plan",
        "capex": "large spending plan",
        "macro": "big-picture",
        "macroeconomic": "big-picture economic",
        "compliance": "rules companies must follow",
        "infrastructure": "core systems",
        "productivity": "getting more useful work done",
        "enterprise": "large-company",
        "geopolitical": "country-level",
        "regulatory": "rule-related",
        "liquidity": "available money",
        "monetary policy": "central-bank decisions",
        "stakeholders": "people affected",
        "paradigm": "major shift",
        "headwinds": "problems",
        "tailwinds": "support",
        "compute": "computing power",
        "synergies": "combined benefits",
    }
    clean = text
    for old, new in replacements.items():
        clean = re.sub(rf"\b{re.escape(old)}\b", new, clean, flags=re.IGNORECASE)
    return clean


def first_sentences(text: str, limit: int = 620) -> str:
    cleaned = clean_summary_text(text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    total = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 30:
            continue
        if total + len(sentence) > limit and selected:
            break
        selected.append(sentence)
        total += len(sentence)
        if total >= limit:
            break
    return clip_words(" ".join(selected), limit)


def clean_summary_text(text: str | None) -> str:
    cleaned = plain_terms(clean_text(text))
    if not cleaned:
        return ""
    pieces = re.split(r"(?<=[.!?])\s+", cleaned)
    useful = []
    for piece in pieces:
        piece = piece.strip(" -|")
        if len(piece) < 18:
            continue
        if BOILERPLATE_RE.search(piece):
            continue
        useful.append(piece)
        if len(" ".join(useful)) >= 260:
            break
    return " ".join(useful) if useful else cleaned[:260].rsplit(" ", 1)[0]


def clip_words(text: str, limit: int) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    clipped = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return f"{clipped}."


def story_area(article: Article) -> str:
    text = f"{article.title} {article.summary} {' '.join(article.tags)}".lower()
    if re.search(r"\b(jobs?|hiring|layoffs?|workforce|employees?|skills?|workplace|freshers)\b", text):
        return "jobs"
    if re.search(r"\b(chips?|semiconductor|data\s*cent(?:er|re)s?|datacent(?:er|re)s?|cloud|computing power|nvidia)\b", text):
        return "compute"
    if re.search(r"\b(sebi|sec|rbi|federal reserve|central bank|bond|debt fundraising|aif|alternative investment fund|fdi|fpi|inflation|interest rate|repo rate|monetary policy|gdp)\b", text):
        return "finance"
    if re.search(r"\b(ai|artificial intelligence)\b", text) and re.search(r"\b(bubble|washing|hype|overvalued)\b", text):
        return "ai"
    if re.search(r"\b(raises?|raised|secures?|funding|financing|valuation|acquisition|merger|ipo|series\s+[a-z]|fundraise|fundraising|investment in|invests?|invested|tech fund|venture fund|launch(?:es|ed)? .{0,40}\bfund)\b", text):
        return "investment"
    if re.search(r"\b(ai|artificial intelligence|automation|agents?|models?|openai|anthropic|gemini)\b", text):
        return "ai"
    if re.search(r"\b(regulation|rules?|policy|law|tariff|sec|sebi|probe|investigation)\b", text):
        return "rules"
    return "business"


def headline_subject(title: str) -> str:
    cleaned = re.sub(r"['\"\u2018\u2019\u201c\u201d]", "", clean_text(title))
    startup_match = re.search(
        r"\b(?:logistics|ai|hardware|tech|fintech|cloud|software|security|battery|quick commerce|b2b)\s+startup\s+([A-Z][A-Za-z0-9&.\-]+)",
        cleaned,
        re.IGNORECASE,
    )
    if startup_match:
        return startup_match.group(1)
    subject = re.split(
        r"\b(raises?|raised|secures?|gets?|bags?|plans?|launches?|announces?|reports?|backs?|bets?|sees?|faces?)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" :-,")
    if not subject or len(subject) < 4:
        subject = cleaned
    return clip_words(subject, 70)


def meaning_line(area: str, title: str, summary: str) -> str:
    subject = headline_subject(title)
    combined = f"{title} {summary}".lower()
    if area == "investment":
        if "tech fund" in combined or "venture fund" in combined:
            return "Investors are setting aside money specifically for technology startups. This matters because startup funding decides which new tools, jobs, and companies get built next in India. For readers, it is a signal of where future work and business opportunity may grow."
        if "logistics" in combined:
            return f"{subject} works in logistics technology, which means software and systems used to move goods faster and cheaper. New funding here shows investors believe supply chains can become more digital. This can affect delivery costs, business efficiency, and startup jobs."
        if "data center" in combined or "cloud" in combined:
            return "This is about money moving into digital infrastructure. Think of data centres and cloud systems as the power stations for AI and online services. When investment rises here, companies may spend more on AI tools and digital work."
        if "startup" in combined:
            return f"{subject} is a startup trying to grow in a competitive market. Fresh funding means investors believe its product can become larger. For students and workers, this can show which sectors may create future jobs."
        return "This story shows fresh money moving into a company or sector. That usually means investors expect demand to grow. It can point to future hiring, competition, or market opportunity."
    if area == "ai":
        if "bubble" in combined:
            return "AI is attracting huge money, but not every AI company will become successful. This story warns that hype can push valuations too high, like people paying too much for a house in a hot market. Readers should care because jobs, investments, and startup plans can suffer if the bubble cools."
        if re.search(r"\b(model|tool|agent|launch|release|software)\b", combined):
            return "This is about a new AI tool or product entering real business use. Such tools can act like a digital assistant for writing, coding, support, or analysis. Readers should watch this because it can change what skills employers expect."
        return "AI is moving from experiments into normal business work. Companies are using it to save time, reduce costs, and change teams. This matters because the same trend can affect jobs, salaries, and daily office work."
    if area == "compute":
        if "chip" in combined or "semiconductor" in combined:
            return "AI needs powerful computer chips, just like a factory needs machines to produce goods. When chip supply or chip design changes, it affects the cost and speed of AI growth. This matters to workers and investors because many technology plans depend on these chips."
        return "AI and digital services need large data centres and cloud capacity to run. Think of them as the engine rooms behind apps, payments, and AI tools. More demand here can change energy use, company costs, and technology jobs."
    if area == "jobs":
        if re.search(r"\b(jobs apocalypse|job losses|job loss)\b", combined):
            return "People are worried that AI may remove office jobs, but leaders are also saying many roles will change instead of disappearing overnight. The deeper issue is that routine work is becoming easier to automate. Readers should care because the safest workers will be those who learn to use AI well."
        if re.search(r"\b(freshers?|entry-level|campus)\b", combined):
            return "Entry-level hiring is changing because companies now expect beginners to be useful with AI, cloud, security, and automation tools. This is different from the old model where freshers learned everything after joining. Students should care because college learning alone may not be enough."
        if re.search(r"\b(skills?|ai-ready|training|reskill|upskill)\b", combined):
            return "Employers are raising the bar for digital and AI skills. A normal resume may not stand out if the person cannot use modern tools. This matters because students and workers need to update skills before the job market forces them to."
        if re.search(r"\b(layoffs?|job cuts?|cuts)\b", combined):
            return "Job cuts often show that a company is under pressure or changing how it works. Sometimes this happens because demand is weak, and sometimes because technology can do more tasks. Readers should care because it signals which roles may become risky."
        return "This story is about how hiring and workplace expectations are changing. The bigger trend is that companies want people who can work with new tools and adapt quickly. This matters for anyone planning a career."
    if area == "finance":
        if re.search(r"\b(aif|alternative investment fund|credit fund|bond etf|debt fundraising|tokenisation)\b", combined):
            return "This is about new ways for money to flow into companies and debt markets. Such funds can help businesses borrow and grow outside normal bank loans. Readers should care because stronger capital markets can affect jobs, startup funding, and investment choices."
        if re.search(r"\b(sebi|sec|rbi|federal reserve|central bank|regulator)\b", combined):
            return "A regulator or central bank is shaping how money moves in the economy. These decisions can affect loans, investment, stock markets, and business confidence. Readers should care because such changes can touch savings, jobs, and company plans."
        return "This story shows a change in markets or money conditions. When markets move, companies can become more careful with spending and hiring. It matters because personal money and business decisions often follow the same signals."
    if area == "rules":
        return "A rule or policy change can quietly change how companies behave. It can raise costs, reduce risk, or open a new opportunity. Readers should care because policy often decides where businesses invest and hire."
    return "This story points to a business change, not just a headline. It may affect how companies spend money, hire people, or use technology. Readers should watch it because these signals often become visible in jobs and markets later."


def short_why_it_matters(area: str) -> str:
    if area == "ai":
        return "Companies may change tools, budgets, and job expectations. Students and employees should watch which AI skills become useful."
    if area == "jobs":
        return "It shows what employers may want next. Workers can understand which roles or skills may grow."
    if area == "finance":
        return "Market and money conditions affect hiring, spending, and investment decisions."
    if area == "investment":
        return "Funding shows where investors expect growth. It can signal demand in a company, sector, or technology."
    if area == "compute":
        return "AI progress depends on chips, cloud, and data centres. These areas can shape costs and competition."
    if area == "rules":
        return "Rules can quickly change business plans, risk, and opportunity."
    return "It may affect company plans, jobs, markets, or technology spending."


def hinglish_line(area: str, summary: str) -> str:
    if area == "investment":
        return "Saar: is khabar mein naya nivesh dikh raha hai. Niveshak is kshetra mein aage badhne ka mauka dekh rahe hain."
    if area == "ai":
        return "Saar: is khabar se pata chalta hai ki AI ka asar kaam aur faislon par badh raha hai. Naye hunar seekhna zaroori ho sakta hai."
    if area == "jobs":
        return "Saar: naukri aur hunar ki maang badal rahi hai. Vidyarthi aur karmchari naye hunar par dhyan dein."
    if area == "finance":
        return "Saar: paisa aur bazaar ki disha badal rahi hai. Iska asar nivesh, kharch aur naukri par pad sakta hai."
    if area == "compute":
        return "Saar: AI chalane ke liye chip, cloud aur data centre zaroori hain. Is kshetra mein nivesh aur pratiyogita badh sakti hai."
    if area == "rules":
        return "Saar: naye niyam company ke faisle aur jokhim badal sakte hain. Iska asar paisa aur yojana par pad sakta hai."
    return "Saar: yeh khabar vyapar ya bazaar mein badlav dikhati hai. Iska asar paisa, naukri ya takneek par pad sakta hai."


def strip_source_mentions(text: str, source: str) -> str:
    cleaned = clean_text(text)
    names = {
        source,
        normalize_source(source),
        "Reuters",
        "Economic Times",
        "The Economic Times",
        "The Hindu Business",
        "The Guardian",
        "Livemint",
        "BBC",
        "AP Business",
        "Google News",
    }
    for name in sorted((n for n in names if n), key=len, reverse=True):
        cleaned = re.sub(rf"\b{re.escape(name)}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    return cleaned.strip(" -|,;")


def generate_template_sections(article: Article, article_text: str = "") -> None:
    area = story_area(article)
    title_plain = plain_terms(article.title)
    summary_clean = clean_summary_text(article.summary) or title_plain
    event = first_sentences(summary_clean, 190) or clip_words(summary_clean, 190)

    article.context = strip_source_mentions(meaning_line(area, title_plain, summary_clean), article.source)
    article.background = article.context
    article.what_happened = strip_source_mentions(plain_terms(clip_words(event, 210)), article.source)
    article.short_explanation = article.what_happened
    article.why_it_matters = strip_source_mentions(plain_terms(short_why_it_matters(area)), article.source)
    article.hinglish_summary = strip_source_mentions(plain_terms(hinglish_line(area, summary_clean)), article.source)


def generate_full_context(article: Article) -> str:
    article_text = extract_article_text(article.url)
    generate_template_sections(article, article_text)
    return " ".join(
        [
            article.background,
            article.what_happened,
            article.why_it_matters,
            article.hinglish_summary,
        ]
    )


def collect_articles(now: datetime, refresh_seed: str = "", seen_ids: set[str] | None = None) -> tuple[list[Article], datetime, datetime, dict]:
    window_end = now.astimezone(timezone.utc)
    window_start = window_end - timedelta(hours=NEWS_WINDOW_HOURS)
    collected: list[Article] = []
    source_counts: dict[str, int] = {}
    seen_ids = seen_ids or set()
    feeds = all_discovery_feeds()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_feed, source, feed_url): source for source, feed_url in feeds}
        for future in as_completed(futures):
            source = futures[future]
            feed_articles = future.result()
            source_counts[normalize_source(source)] = source_counts.get(normalize_source(source), 0) + len(feed_articles)
            collected.extend(feed_articles)

    unique = deduplicate(collected)
    candidates = [
        a
        for a in unique
        if in_window(a, window_start, window_end)
        and not is_junk(a)
        and TRUSTED_SOURCE_RE.search(a.source)
    ]
    candidates = [a for a in candidates if is_relevant(a)]

    for article in candidates:
        article.impact_score, article.tags = score_article(article)
        article.curation_score = curation_score_5(article, now)
        article.priority_group = priority_group(article)

    candidates = [a for a in candidates if a.curation_score >= 3]
    strict = [a for a in candidates if a.impact_score >= STRICT_RELEVANCE_FLOOR]
    relaxed = [a for a in candidates if a.impact_score >= RELAXED_RELEVANCE_FLOOR]
    ranked = strict if len(strict) >= RETURN_ARTICLE_COUNT else relaxed
    ranked.sort(key=lambda item: (item.impact_score, item.published_at), reverse=True)

    shown_counts = history_counts()
    top_articles, candidate_pool_count, unseen_candidate_count = select_fresh_top_mix(
        ranked,
        refresh_seed or str(time.time()),
        seen_ids,
        shown_counts,
    )

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(generate_full_context, article): article for article in top_articles}
        for future in as_completed(futures):
            article = futures[future]
            article.full_context = future.result()

    diagnostics = {
        "sources": len(feeds),
        "direct_sources": len(MACRO_FEEDS),
        "google_news_queries": len(GOOGLE_NEWS_QUERIES),
        "collected": len(collected),
        "deduplicated": len(unique),
        "qualified": len(candidates),
        "strict_qualified": len(strict),
        "minimum_curation_score": 3,
        "candidate_pool_count": candidate_pool_count,
        "unseen_candidate_count": unseen_candidate_count,
        "history_items": len(shown_counts),
        "seen_penalized": len(seen_ids),
        "source_mix": dict(sorted({article.source: sum(1 for item in top_articles if item.source == article.source) for article in top_articles}.items())),
        "source_counts": source_counts,
    }
    save_shown_articles(top_articles)
    return top_articles, window_start, window_end, diagnostics


def build_podcast_script(language: str, articles: list[dict]) -> str:
    cleaned = [a for a in articles if isinstance(a, dict)][:RETURN_ARTICLE_COUNT]
    lines = [
        "Welcome to the AHEAD A.I. quick briefing.",
        "Here are the ten most useful business, finance, automation, and AI stories.",
    ]
    for index, article in enumerate(cleaned, start=1):
        title = clip_words(article.get("title", "Important story"), 78)
        signal = clip_words(article.get("why_it_matters") or article.get("context") or article.get("summary", ""), 62)
        lines.append(
            f"{index}. {title} {signal}"
        )
    lines.append(
        "That is the briefing. Watch where money moves, where work changes, and where AI becomes normal business."
    )
    return " ".join(lines)


def pcm_to_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm)
    return b"".join(
        [
            b"RIFF",
            (36 + data_size).to_bytes(4, "little"),
            b"WAVEfmt ",
            (16).to_bytes(4, "little"),
            (1).to_bytes(2, "little"),
            channels.to_bytes(2, "little"),
            sample_rate.to_bytes(4, "little"),
            byte_rate.to_bytes(4, "little"),
            block_align.to_bytes(2, "little"),
            (sample_width * 8).to_bytes(2, "little"),
            b"data",
            data_size.to_bytes(4, "little"),
            pcm,
        ]
    )


def generate_gemini_tts(script: str) -> bytes | None:
    api_key = RUNTIME_KEYS.get("gemini") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    prompt = (
        "Read this as a premium human business podcast host. Voice style: warm, confident, calm, "
        "natural Indian-global English, clear pauses, not robotic, not too fast. Transcript:\n\n"
        f"{script}"
    )
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": GEMINI_TTS_VOICE,
                        }
                    }
                },
            },
        }
    ).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=75) as response:
        payload = json.loads(response.read().decode("utf-8"))

    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        encoded = inline.get("data")
        if encoded:
            pcm = base64.b64decode(encoded)
            return pcm_to_wav(pcm)
    raise RuntimeError("Gemini TTS returned no audio data")


def generate_openai_tts(script: str) -> bytes | None:
    api_key = RUNTIME_KEYS.get("openai") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    instructions = (
        "Speak as a calm, premium business podcast host. Keep the delivery clear, warm, and confident. "
        "Use natural pronunciation for Indian business terms and AI terms."
    )
    body = json.dumps(
        {
            "model": "gpt-4o-mini-tts",
            "voice": "marin",
            "input": script,
            "instructions": instructions,
            "response_format": "mp3",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def download_file_if_missing(url: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 1024:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "AHEAD-AI-Kokoro-Setup/1.0"})
    with urllib.request.urlopen(req, timeout=240) as response:
        path.write_bytes(response.read())


def samples_to_wav(samples, sample_rate: int) -> bytes:
    if hasattr(samples, "tolist"):
        samples = samples.tolist()
    if samples and isinstance(samples[0], list):
        samples = [item for row in samples for item in row]
    pcm = bytearray()
    for value in samples:
        clipped = max(-1.0, min(1.0, float(value)))
        pcm.extend(int(clipped * 32767).to_bytes(2, "little", signed=True))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(pcm))
    return output.getvalue()


def kokoro_status_payload() -> dict:
    try:
        import kokoro_onnx  # noqa: F401

        package_available = True
        package_error = ""
    except Exception as exc:
        package_available = False
        package_error = str(exc)
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_compatible = (3, 9) <= sys.version_info[:2] < (3, 14)
    return {
        "available": python_compatible and package_available and KOKORO_MODEL_PATH.exists() and KOKORO_VOICES_PATH.exists(),
        "package_available": package_available,
        "package_error": package_error,
        "python_version": py_version,
        "python_compatible": python_compatible,
        "model_exists": KOKORO_MODEL_PATH.exists(),
        "voices_exists": KOKORO_VOICES_PATH.exists(),
        "model_path": str(KOKORO_MODEL_PATH),
        "voices_path": str(KOKORO_VOICES_PATH),
        "setup": "Install Python 3.12 or 3.13, then run: py -3.12 -m pip install kokoro-onnx soundfile",
        "note": "kokoro-onnx requires Python <3.14. This local server is running Python " + py_version + ".",
    }


def generate_kokoro_tts(text: str) -> bytes:
    try:
        from kokoro_onnx import Kokoro
    except Exception as exc:
        raise RuntimeError(f"Kokoro package is not available: {exc}") from exc

    download_file_if_missing(KOKORO_MODEL_URL, KOKORO_MODEL_PATH)
    download_file_if_missing(KOKORO_VOICES_URL, KOKORO_VOICES_PATH)
    kokoro = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
    create_kwargs = {"voice": "af_heart", "speed": 0.92, "lang": "en-us"}
    try:
        samples, sample_rate = kokoro.create(text, **create_kwargs)
    except TypeError:
        samples, sample_rate = kokoro.create(text, voice="af_heart", speed=0.92)
    return samples_to_wav(samples, int(sample_rate))


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "AHEAD A.I. macro news engine"})


@app.get("/api/voice-status")
def voice_status():
    kokoro = kokoro_status_payload()
    return jsonify(
        {
            "kokoro_configured": kokoro["available"],
            "kokoro": kokoro,
            "gemini_configured": bool(RUNTIME_KEYS.get("gemini") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
            "openai_configured": bool(RUNTIME_KEYS.get("openai") or os.getenv("OPENAI_API_KEY")),
            "preferred": "kokoro",
        }
    )


@app.post("/api/voice-key")
def voice_key():
    payload = request.get_json(silent=True) or {}
    provider = payload.get("provider", "gemini")
    api_key = clean_text(payload.get("api_key", ""))
    if provider not in {"gemini", "openai"}:
        return jsonify({"error": "provider must be gemini or openai"}), 400
    if len(api_key) < 12:
        return jsonify({"error": "api_key looks too short"}), 400
    RUNTIME_KEYS[provider] = api_key
    return jsonify({"status": "saved", "provider": provider})


@app.get("/api/crucial-news")
def crucial_news():
    now = datetime.now(timezone.utc)
    started = time.perf_counter()
    refresh_seed = clean_text(request.args.get("refresh", "")) or str(time.time_ns())
    seen_ids = {
        item.strip()
        for item in clean_text(request.args.get("seen", "")).split(",")
        if re.fullmatch(r"[a-f0-9]{8,64}", item.strip())
    }
    articles, window_start, window_end, diagnostics = collect_articles(now, refresh_seed=refresh_seed, seen_ids=seen_ids)
    generated_at = datetime.now(timezone.utc).isoformat()
    response = jsonify(
        {
            "generated_at": generated_at,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "hours": NEWS_WINDOW_HOURS,
            },
            "count": len(articles),
            "candidate_pool_count": diagnostics.get("candidate_pool_count", 0),
            "processing_seconds": round(time.perf_counter() - started, 2),
            "diagnostics": diagnostics,
            "articles": [article.as_dict() for article in articles],
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.post("/api/podcast-script")
def podcast_script():
    payload = request.get_json(silent=True) or {}
    language = payload.get("language", "en")
    if language != "en":
        return jsonify({"error": "Only English podcast playback is enabled."}), 400
    articles = payload.get("articles", [])
    if not isinstance(articles, list) or not articles:
        return jsonify({"error": "articles must be a non-empty list"}), 400
    return jsonify({"language": language, "script": build_podcast_script(language, articles)})


@app.post("/api/mark-read")
def mark_read():
    payload = request.get_json(silent=True) or {}
    article_ids = payload.get("article_ids", [])
    if not isinstance(article_ids, list):
        return jsonify({"error": "article_ids must be a list"}), 400
    updated = mark_articles_read([clean_text(str(item)) for item in article_ids])
    return jsonify({"status": "ok", "updated": updated})


@app.post("/api/local-tts")
def local_tts():
    payload = request.get_json(silent=True) or {}
    text = clean_text(payload.get("text", ""))[:12000]
    if not text:
        return jsonify({"error": "text is required"}), 400
    try:
        audio = generate_kokoro_tts(text)
        return Response(audio, mimetype="audio/wav", headers={"X-AHEAD-Voice-Provider": "Kokoro Local Neural TTS"})
    except Exception as exc:
        return jsonify({"error": "Kokoro local TTS unavailable", "detail": str(exc), "kokoro": kokoro_status_payload()}), 503


@app.post("/api/podcast-audio")
def podcast_audio():
    payload = request.get_json(silent=True) or {}
    script = clean_text(payload.get("script", ""))[:6000]
    language = payload.get("language", "en")
    if language != "en":
        return jsonify({"error": "Only English podcast playback is enabled."}), 400
    if not script:
        return jsonify({"error": "script is required"}), 400

    try:
        gemini_audio = generate_gemini_tts(script)
        if gemini_audio:
            return Response(gemini_audio, mimetype="audio/wav", headers={"X-AHEAD-Voice-Provider": "Gemini Native TTS"})

        openai_audio = generate_openai_tts(script)
        if openai_audio:
            return Response(openai_audio, mimetype="audio/mpeg", headers={"X-AHEAD-Voice-Provider": "OpenAI TTS"})

        return (
            jsonify(
                {
                    "error": "No premium voice API key configured. Add GEMINI_API_KEY for free-tier native TTS, or OPENAI_API_KEY for OpenAI TTS.",
                    "setup": "Set GEMINI_API_KEY in the terminal before running python server.py.",
                }
            ),
            503,
        )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        return jsonify({"error": "Premium audio generation failed", "detail": error_body}), exc.code
    except Exception as exc:
        return jsonify({"error": "Premium audio generation failed", "detail": str(exc)}), 502


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/app.js")
def app_js():
    return send_from_directory(APP_DIR, "app.js")


@app.get("/styles.css")
def styles_css():
    return send_from_directory(APP_DIR, "styles.css")


@app.get("/data.js")
def data_js():
    return send_from_directory(APP_DIR, "data.js")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
