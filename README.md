# Genie — a Telegram reading-list triage agent

A personal Telegram bot that turns a messy stream of links into a triaged,
searchable reading list. Built to solve one problem: I kept saving articles
to "read later" and never reading them.

## What it does

Send the bot a URL (arXiv, YouTube, X/Twitter, or any article):

1. **Extracts** the content — arXiv API for papers, YouTube oEmbed + transcript
   for videos, `fxtwitter` for tweets, `trafilatura` for general web.
2. **Triages** it with Claude: 2–3 sentence summary, 1–2 category tags,
   relevance score (high/medium/low) against your focus areas in `context.md`,
   and an estimated read time.
3. **Dedups** against what's already saved (ignores tracking params, preserves
   real article IDs in query strings).
4. **Appends** a structured entry to `reading_list.jsonl`.

Then chat with it naturally:

- *"what should I read next? I have 20 min on diffusion"* → `suggest_next`
- *"find the Karpathy micrograd video and save it"* → `web_search` → `save_link_from_url`
- *"note on ESM: <thoughts>"* → writes `notes/<date>_<slug>.md`, marks read
- *"what do I have on RoPE?"* → keyword search + cites your saved items
- *"mark X as read"* · *"list unread biology stuff"*

## Architecture

```
bot.py         Telegram handler + Claude tool-calling loop
triage.py      URL → extract → Claude summary+tags → jsonl append
library.py    Query/mutation ops (search, suggest, mark-read, note_on) + tool schemas
context.md    Your focus areas — drives relevance scoring
```

Claude does the heavy lifting (summarization, tag inference, query intent).
Everything else is a few hundred lines of Python.

## Setup

```bash
uv sync              # installs anthropic, python-telegram-bot, trafilatura, youtube-transcript-api, httpx
cp .env.example .env # fill in TELEGRAM_TOKEN and ANTHROPIC_API_KEY
uv run python bot.py
```

To run as a background service on macOS, drop a LaunchAgent plist in
`~/Library/LaunchAgents/` pointing at `uv run python bot.py`.

## Why

I wanted a reading-list tool that (a) worked from my phone, (b) understood
what I actually care about instead of dumping links into a void, and
(c) let me talk to my library instead of scrolling through it. This is it.

## Stack

- Anthropic Claude (sonnet-4-6) for triage and chat
- `python-telegram-bot` for the Telegram interface
- `trafilatura` + arXiv API + YouTube oEmbed for extraction
- Plain JSONL as the datastore — portable, greppable, diffable
