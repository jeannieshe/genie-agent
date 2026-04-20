import os
import re
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from triage import process_url
from library import TOOLS, handle_tool_call

load_dotenv()
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

URL_RE = re.compile(r"https?://\S+")
RELEVANCE_EMOJI = {"high": "🔥", "medium": "📘", "low": "📎"}

SYSTEM_PROMPT = """You are Genie, Jeannie's reading-list assistant on Telegram.

Her reading list is a JSONL file of unread/read items, each with title, category,
relevance (high/medium/low), estimated_read_time, and summary.

When she asks something, route to the right tool:
- "find the Karpathy autograd video" / "search for X" → web_search, then save_link_from_url
- "note on ESM: <thoughts>" / "just read X, here's what I got: ..." → note_on (pass her thoughts verbatim as `thoughts`; do NOT paraphrase)
- Any general/substantive question ("what do I have on RoPE?", "explain diffusion using my saved stuff", "thoughts on ESM vs AlphaFold?") → call search_library FIRST with key terms from her question, then weave relevant saved items into your answer with titles + URLs. If nothing relevant, answer from general knowledge and say so.
- "what should I read next?" / "I have 20 min" / "suggest something on diffusion" → suggest_next
- "mark X as read" / "finished the ESM paper" → mark_read
- "what's unread?" / "show me biology stuff" → list_unread

When using web_search: prefer official channels and original authors (e.g. Karpathy's
own YouTube channel over re-uploads, arXiv over blog summaries). Show the URL you
picked in your reply. If multiple good matches exist or you're unsure, ask her to
confirm before calling save_link_from_url.

Duplicates are rejected automatically — if she sees "⚠️ Already in list", that's expected.

Always call a tool when the intent matches. Reply in short Telegram-friendly text.
If intent is unclear, ask one brief clarifying question.
"""


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    await update.message.chat.send_action("typing")

    # URL path: always triage and save
    match = URL_RE.search(text)
    if match:
        url = match.group(0).rstrip(").,;")
        note = text.replace(url, "").strip()
        try:
            entry = process_url(url, user_note=note)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {e}")
            return
        if entry.get("_duplicate"):
            await update.message.reply_text(
                f"⚠️ Already in list: {entry['title']}\nstatus: {entry.get('status','unread')}\n{entry['url']}"
            )
            return
        emoji = RELEVANCE_EMOJI.get(entry["relevance"], "📘")
        cats = " · ".join(entry["category"]) if entry["category"] else "uncategorized"
        reply = (
            f"✅ Saved: {entry['title']}\n"
            f"→ {cats} {emoji} relevance: {entry['relevance']} · ~{entry['estimated_read_time']}\n\n"
            f"{entry['summary']}"
        )
        await update.message.reply_text(reply[:4000])
        return

    # Chat path: tool-calling loop for queries
    messages = [{"role": "user", "content": text}]
    for _ in range(4):  # bounded loop
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "tool_use":
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            results = [
                {"type": "tool_result", "tool_use_id": tu.id, "content": handle_tool_call(tu.name, tu.input)}
                for tu in tool_uses
            ]
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})
            continue
        reply = next((b.text for b in resp.content if hasattr(b, "text")), "(no reply)")
        await update.message.reply_text(reply[:4000])
        return


def main():
    print("Genie triage bot is running...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
