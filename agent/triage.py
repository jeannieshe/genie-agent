"""
Reading-list triage pipeline.

URL in → lightweight extraction (arXiv API / YouTube oEmbed+transcript / trafilatura)
       → Claude summary + category + relevance
       → structured dict ready to append to reading_list.jsonl
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET

import anthropic
import httpx
import trafilatura
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

load_dotenv(Path(__file__).parent / ".env")

ROOT = Path(__file__).parent.parent
CONTEXT_FILE = ROOT / "context.md"
READING_LIST = ROOT / "raw" / "reading_list.jsonl"

_claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ---------- link type detection ----------

def detect_type(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if host == "x.com" or host.endswith(".x.com") or "twitter.com" in host:
        return "twitter"
    return "web"


def _extract_twitter(url: str) -> dict:
    if "/i/article/" in url or "/i/spaces/" in url:
        return {"title": url, "text": "(X article/space — no scrape-free extraction; relying on user note)"}
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return {"title": url, "text": ""}
    tid = m.group(1)
    try:
        r = httpx.get(f"https://api.fxtwitter.com/status/{tid}", timeout=15, follow_redirects=True)
        r.raise_for_status()
        tweet = r.json().get("tweet", {})
        author = tweet.get("author", {}).get("name", "unknown")
        text = tweet.get("text", "")
        title = f"Tweet by {author}: {text[:80]}"
        return {"title": title, "text": f"Author: {author}\n\n{text}"}
    except Exception as e:
        return {"title": url, "text": f"(fxtwitter failed: {e})"}


# ---------- extractors ----------

def _extract_arxiv(url: str) -> dict:
    m = re.search(r"(\d{4}\.\d{4,5})", url)
    if not m:
        return {"title": url, "text": ""}
    arxiv_id = m.group(1)
    r = httpx.get(
        f"https://export.arxiv.org/api/query?id_list={arxiv_id}",
        timeout=15,
        follow_redirects=True,
    )
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None:
        return {"title": url, "text": ""}
    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
    summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
    authors = [
        (a.findtext("a:name", default="", namespaces=ns) or "").strip()
        for a in entry.findall("a:author", ns)
    ]
    text = f"Authors: {', '.join(authors)}\n\nAbstract: {summary}"
    return {"title": title, "text": text}


def _youtube_id(url: str) -> str | None:
    p = urlparse(url)
    if "youtu.be" in p.netloc:
        return p.path.lstrip("/") or None
    if p.path == "/watch":
        return parse_qs(p.query).get("v", [None])[0]
    if p.path.startswith("/shorts/"):
        return p.path.split("/")[2]
    return None


def _extract_youtube(url: str) -> dict:
    title = url
    try:
        r = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=15,
            follow_redirects=True,
        )
        if r.status_code == 200:
            data = r.json()
            title = data.get("title") or title
    except Exception:
        pass

    transcript_text = ""
    vid = _youtube_id(url)
    if vid:
        try:
            tr = YouTubeTranscriptApi.get_transcript(vid)
            transcript_text = " ".join(seg["text"] for seg in tr)[:6000]
        except Exception:
            transcript_text = ""
    return {"title": title, "text": transcript_text}


def _extract_web(url: str) -> dict:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return {"title": url, "text": ""}
    text = trafilatura.extract(downloaded, include_comments=False) or ""
    meta = trafilatura.extract_metadata(downloaded)
    title = (meta.title if meta and meta.title else url)
    return {"title": title, "text": text[:6000]}


def extract(url: str) -> dict:
    kind = detect_type(url)
    if kind == "arxiv":
        data = _extract_arxiv(url)
    elif kind == "youtube":
        data = _extract_youtube(url)
    elif kind == "twitter":
        data = _extract_twitter(url)
    else:
        data = _extract_web(url)
    data["type"] = kind
    return data


# ---------- Claude triage ----------

TRIAGE_PROMPT = """You triage reading-list items for Jeannie's knowledge base.

Her current focus context:
---
{context}
---

Given the extracted content below, return STRICT JSON with these fields:
- "summary": 2-3 sentence plain-English summary
- "category": array of 1-2 short tags (e.g. ["ML", "biology"], ["systems"], ["personal"])
- "relevance": one of "high", "medium", "low" — based on alignment with her focus context
- "estimated_read_time": string like "5 min", "12 min", "30 min" (estimate from length/type)

Return ONLY the JSON object. No prose, no code fences.

Title: {title}
Type: {type}
Content:
{text}
"""


def _load_context() -> str:
    if CONTEXT_FILE.exists():
        return CONTEXT_FILE.read_text()
    return "(no context.md — score everything medium)"


def triage_with_claude(extracted: dict) -> dict:
    prompt = TRIAGE_PROMPT.format(
        context=_load_context(),
        title=extracted.get("title", ""),
        type=extracted.get("type", "web"),
        text=(extracted.get("text") or "(no content extracted — use title + URL only)")[:6000],
    )
    resp = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = next(b.text for b in resp.content if hasattr(b, "text")).strip()
    # strip accidental code fences
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ---------- orchestration ----------

def _normalize_url(u: str) -> str:
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    # Preserve query string — some sites (PLOS, etc.) put article IDs there.
    # Strip only known tracking params.
    TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","ref","s","fbclid","gclid"}
    if p.query:
        kept = [kv for kv in p.query.split("&") if kv.split("=",1)[0] not in TRACKING]
        q = "?" + "&".join(sorted(kept)) if kept else ""
    else:
        q = ""
    return f"{host}{path}{q}"


def find_duplicate(url: str) -> dict | None:
    if not READING_LIST.exists():
        return None
    key = _normalize_url(url)
    for line in READING_LIST.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if _normalize_url(e.get("url", "")) == key:
            return e
    return None


def process_url(url: str, user_note: str = "") -> dict:
    dup = find_duplicate(url)
    if dup:
        return {**dup, "_duplicate": True}
    extracted = extract(url)
    if user_note:
        extracted["text"] = (extracted.get("text", "") + f"\n\nUser's note: {user_note}").strip()
    try:
        triage = triage_with_claude(extracted)
    except Exception as e:
        triage = {
            "summary": f"(triage failed: {e})",
            "category": ["uncategorized"],
            "relevance": "medium",
            "estimated_read_time": "unknown",
        }

    entry = {
        "url": url,
        "title": extracted.get("title", url),
        "added": datetime.now().isoformat(timespec="seconds"),
        "summary": triage.get("summary", ""),
        "category": triage.get("category", []),
        "relevance": triage.get("relevance", "medium"),
        "estimated_read_time": triage.get("estimated_read_time", "unknown"),
        "status": "unread",
        "notes": user_note,
        "source_type": extracted.get("type", "web"),
    }

    READING_LIST.parent.mkdir(parents=True, exist_ok=True)
    with READING_LIST.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry
