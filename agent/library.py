"""
Read/query operations on reading_list.jsonl.
Kept separate from triage.py so ingestion and retrieval stay decoupled.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
READING_LIST = ROOT / "raw" / "reading_list.jsonl"
NOTES_DIR = ROOT / "notes"
CONTEXT_FILE = ROOT / "context.md"


def _load_all() -> list[dict]:
    if not READING_LIST.exists():
        return []
    return [json.loads(l) for l in READING_LIST.read_text().splitlines() if l.strip()]


def _write_all(entries: list[dict]) -> None:
    READING_LIST.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _minutes(s: str) -> int:
    import re
    m = re.search(r"(\d+)", s or "")
    return int(m.group(1)) if m else 10


RELEVANCE_RANK = {"high": 0, "medium": 1, "low": 2}


def suggest_next(time_minutes: int = 30, focus: str = "") -> str:
    """Return a short ranked suggestion list that fits the time budget."""
    entries = [e for e in _load_all() if e.get("status") == "unread"]
    if not entries:
        return "Nothing unread in your reading list."

    focus_lc = focus.lower()

    def score(e):
        rel = RELEVANCE_RANK.get(e.get("relevance", "medium"), 1)
        cats = " ".join(e.get("category", [])).lower()
        match = 0 if (focus_lc and focus_lc in cats) or (focus_lc and focus_lc in (e.get("title","").lower())) else 1
        return (match, rel, _minutes(e.get("estimated_read_time", "10 min")))

    fits = [e for e in entries if _minutes(e.get("estimated_read_time", "10 min")) <= time_minutes + 5]
    pool = fits or entries
    ranked = sorted(pool, key=score)[:3]

    lines = [f"Top picks for ~{time_minutes} min" + (f" on '{focus}'" if focus else "") + ":\n"]
    for i, e in enumerate(ranked, 1):
        cats = ", ".join(e.get("category", [])) or "uncategorized"
        lines.append(
            f"{i}. {e['title']}\n"
            f"   {cats} · {e['relevance']} · ~{e['estimated_read_time']}\n"
            f"   {e['summary']}\n"
            f"   {e['url']}"
        )
    return "\n".join(lines)


def mark_read(query: str) -> str:
    """Find an unread entry whose title/url contains `query` (case-insensitive) and mark it read."""
    entries = _load_all()
    q = query.lower().strip()
    hits = [
        i for i, e in enumerate(entries)
        if e.get("status") == "unread" and (q in e.get("title","").lower() or q in e.get("url","").lower())
    ]
    if not hits:
        return f"No unread item matching '{query}'."
    if len(hits) > 1:
        titles = "\n".join(f"- {entries[i]['title']}" for i in hits[:5])
        return f"Multiple matches — be more specific:\n{titles}"
    entries[hits[0]]["status"] = "read"
    _write_all(entries)
    return f"✅ Marked read: {entries[hits[0]]['title']}"


def list_unread(category: str = "", limit: int = 10) -> str:
    entries = [e for e in _load_all() if e.get("status") == "unread"]
    if category:
        cat = category.lower()
        entries = [e for e in entries if any(cat in c.lower() for c in e.get("category", []))]
    if not entries:
        return "Nothing unread" + (f" in '{category}'" if category else "") + "."
    entries = sorted(entries, key=lambda e: RELEVANCE_RANK.get(e.get("relevance","medium"), 1))[:limit]
    return "\n".join(
        f"• [{e['relevance']}] {e['title']} — {', '.join(e.get('category',[]))} (~{e['estimated_read_time']})"
        for e in entries
    )


def search_library(query: str, limit: int = 8) -> str:
    """Keyword search across title, summary, category, notes. Returns compact hits for Claude to cite."""
    entries = _load_all()
    if not entries:
        return "Reading list is empty."
    tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    if not tokens:
        return "Query too short."

    def score(e):
        blob = " ".join([
            e.get("title", ""),
            e.get("summary", ""),
            " ".join(e.get("category", [])),
            e.get("notes", ""),
        ]).lower()
        return sum(blob.count(t) for t in tokens)

    ranked = sorted(((score(e), e) for e in entries), key=lambda x: -x[0])
    hits = [e for s, e in ranked if s > 0][:limit]
    if not hits:
        return f"No matches for '{query}' in reading list."
    lines = [f"{len(hits)} matches:"]
    for e in hits:
        cats = ", ".join(e.get("category", [])) or "-"
        lines.append(
            f"- [{e.get('status','unread')}] {e['title']} ({cats}, {e.get('relevance','?')}, ~{e.get('estimated_read_time','?')})\n"
            f"  {e['url']}\n"
            f"  {e.get('summary','')}"
        )
    return "\n".join(lines)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "note"


NOTE_PROMPT = """You help Jeannie synthesize notes after she reads something.

Her current focus context:
---
{context}
---

She just read: {title}
URL: {url}
Her raw thoughts:
---
{thoughts}
---

Source summary (for reference): {summary}

Write a concise synthesis in markdown with these sections (skip any that don't apply):
## Key claims
- (2-4 bullets of the core argument/findings)
## Open questions
- (things worth following up)
## Connections to her work
- (how this ties to her pillars: protein FMs, PyTorch replication, diffusion, RL, AIxBio cluster)

Be concrete and specific. No filler. Total under 250 words."""


def note_on(query: str, thoughts: str) -> str:
    """Write a synthesized note for an item matching `query`, then mark it read."""
    import anthropic
    entries = _load_all()
    q = query.lower().strip()
    hits = [
        (i, e) for i, e in enumerate(entries)
        if q in e.get("title", "").lower() or q in e.get("url", "").lower()
    ]
    if not hits:
        return f"No reading-list item matching '{query}'."
    if len(hits) > 1:
        titles = "\n".join(f"- {e['title']}" for _, e in hits[:5])
        return f"Multiple matches — be more specific:\n{titles}"
    idx, entry = hits[0]

    context = CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else ""
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = NOTE_PROMPT.format(
        context=context,
        title=entry.get("title", ""),
        url=entry.get("url", ""),
        thoughts=thoughts,
        summary=entry.get("summary", ""),
    )
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    synthesis = next(b.text for b in resp.content if hasattr(b, "text")).strip()

    date = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(entry.get("title", "note"))
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / f"{date}_{slug}.md"
    path.write_text(
        f"# {entry.get('title','')}\n\n"
        f"- **URL**: {entry.get('url','')}\n"
        f"- **Date**: {date}\n"
        f"- **Category**: {', '.join(entry.get('category', []))}\n"
        f"- **Source summary**: {entry.get('summary','')}\n\n"
        f"## My thoughts\n\n{thoughts}\n\n"
        f"## Synthesis\n\n{synthesis}\n"
    )

    entries[idx]["status"] = "read"
    entries[idx]["note_file"] = str(path.relative_to(ROOT))
    _write_all(entries)
    return f"📝 Note saved: notes/{path.name}\n✅ Marked read: {entry['title']}"


def save_link_from_url(url: str, note: str = "") -> str:
    from triage import process_url
    entry = process_url(url, user_note=note)
    if entry.get("_duplicate"):
        return f"⚠️ Already in list: {entry['title']} (status: {entry.get('status','unread')}) — {entry['url']}"
    cats = ", ".join(entry.get("category", [])) or "uncategorized"
    return (
        f"✅ Saved: {entry['title']}\n"
        f"{cats} · {entry['relevance']} · ~{entry['estimated_read_time']}\n"
        f"{entry['url']}"
    )


TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    {
        "name": "save_link_from_url",
        "description": "Save a URL to the reading list. Use after web_search once you have the target URL. Triage and dedup happen automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "note": {"type": "string", "description": "Optional user context about why"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_library",
        "description": "Search Jeannie's reading list by keyword across titles, summaries, and categories. Use this when she asks a general question — cite relevant saved items in your answer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "note_on",
        "description": "Write a synthesized note for a reading-list item and mark it read. Use when she says 'note on X: <thoughts>' or 'I just read X, thinking <thoughts>'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring of the item's title or URL"},
                "thoughts": {"type": "string", "description": "Her raw thoughts/takeaways — used as-is, not rewritten"},
            },
            "required": ["query", "thoughts"],
        },
    },
    {
        "name": "suggest_next",
        "description": "Suggest 1-3 unread items from the reading list that fit the user's time budget and optional focus area.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_minutes": {"type": "integer", "description": "Minutes available"},
                "focus": {"type": "string", "description": "Optional topic filter, e.g. 'protein', 'diffusion'"},
            },
            "required": ["time_minutes"],
        },
    },
    {
        "name": "mark_read",
        "description": "Mark an unread item as read by a substring of its title or URL.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_unread",
        "description": "List unread items, optionally filtered by category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
]


def handle_tool_call(name: str, args: dict) -> str:
    if name == "save_link_from_url":
        return save_link_from_url(**args)
    if name == "note_on":
        return note_on(**args)
    if name == "search_library":
        return search_library(**args)
    if name == "suggest_next":
        return suggest_next(**args)
    if name == "mark_read":
        return mark_read(**args)
    if name == "list_unread":
        return list_unread(**args)
    return f"Unknown tool: {name}"
