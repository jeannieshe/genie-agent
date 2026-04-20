import os
import json
import subprocess
from datetime import datetime
from pathlib import Path

# Where raw sources live — relative to this file, one level up
RAW_DIR = Path(__file__).parent.parent / "raw" # since path == genie/agent/tools.py, this resolves to genie/raw/


def fetch_content(url: str) -> str:
    """
    Uses agent-browser to visit a URL and extract readable text.
    Returns the page text, or an empty string if it fails.

    subprocess.run() launches agent-browser as a CLI command — same as
    typing it in your terminal, but called from Python. We capture stdout
    so we can read the result back into our script.
    """
    try:
        ab = "/opt/homebrew/bin/agent-browser"

        r1 = subprocess.run([ab, "open", url], capture_output=True, text=True, timeout=60)
        print(f"[browser] open: {r1.returncode} | {r1.stderr.strip()}", flush=True)

        r2 = subprocess.run([ab, "snapshot"], capture_output=True, text=True, timeout=30)
        print(f"[browser] snapshot: {r2.returncode} | {r2.stderr.strip()} | content_len={len(r2.stdout)}", flush=True)

        subprocess.run([ab, "close"], capture_output=True, timeout=10)

        return r2.stdout.strip()
    except Exception as e:
        print(f"[browser] exception: {e}", flush=True)
        return ""


def save_article(url: str, topic: str, type: str, estimated_time: str, notes: str = "") -> str:
    """
    Saves a new source to raw/. Called by Claude when it decides to save something.
    Returns a confirmation string that gets sent back to you on Telegram.
    """

    # Build a filename from the topic + timestamp so nothing ever collides
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{topic.replace(' ', '-')}_{timestamp}.md"
    filepath = RAW_DIR / filename

    # Fetch actual page content via browser
    page_content = fetch_content(url) # uses the agent-browser tool
    content_section = f"\n## Content\n\n{page_content}\n" if page_content else ""

    # The actual file content — clean markdown that your wiki agent can process later
    content = f"""# {topic}

- **URL**: {url}
- **Type**: {type}
- **Estimated time**: {estimated_time}
- **Added**: {datetime.now().strftime("%Y-%m-%d")}
- **Notes**: {notes}
{content_section}"""

    filepath.write_text(content)
    return f"Saved to raw/{filename}" + (" (with content)" if page_content else " (URL only — browser fetch failed)")


# This is the tool *schema* — the JSON description Claude reads to know how to call save_article.
# Think of it as the function signature, but in a format Claude understands.
TOOLS = [
    {
        "name": "save_article",
        "description": (
            "Save a new article, paper, tweet, video, or tutorial to Jeannie's knowledge base. "
            "Use this whenever the user shares a link they want to remember."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url":            {"type": "string", "description": "The link being saved"},
                "topic":          {"type": "string", "description": "Short topic slug, e.g. 'diffusion-models'"},
                "type":           {"type": "string", "enum": ["tweet", "paper", "article", "video", "tutorial"]},
                "estimated_time": {"type": "string", "description": "How long it takes to read/watch, e.g. '15 min'"},
                "notes":          {"type": "string", "description": "Jeannie's comment or reason for saving"},
            },
            "required": ["url", "topic", "type", "estimated_time"],
        },
    }
]


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Routes Claude's tool call to the right Python function."""
    if tool_name == "save_article":
        return save_article(**tool_input)
    return f"Unknown tool: {tool_name}"
