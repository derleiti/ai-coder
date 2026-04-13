"""web_search.py — Local web search + fetch for ai-coder when MCP is unavailable."""
from __future__ import annotations
import json
from . import __version__
import re
from typing import Tuple
from urllib.parse import quote_plus

# Use stdlib only — no extra dependencies
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


_HEADERS = {
    "User-Agent": f"ai-coder/{__version__} (AILinux; +https://ailinux.me)",
    "Accept": "text/html,application/json,text/plain;q=0.9",
}
_TIMEOUT = 15


def web_fetch(url: str, max_chars: int = 8000) -> Tuple[str, bool]:
    """Fetch a URL and return plain text content. Returns (text, is_error)."""
    try:
        req = Request(url, headers=_HEADERS)
        with urlopen(req, timeout=_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_chars * 2)  # read extra, we'll trim

            if "json" in content_type:
                try:
                    data = json.loads(raw)
                    return json.dumps(data, indent=2, ensure_ascii=False)[:max_chars], False
                except Exception:
                    pass

            # Decode HTML/text
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            text = raw.decode(charset, errors="replace")

            # Strip HTML tags for readability
            if "html" in content_type:
                # Remove script/style blocks
                text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.I)
                # Remove tags
                text = re.sub(r"<[^>]+>", " ", text)
                # Collapse whitespace
                text = re.sub(r"\s+", " ", text).strip()

            return text[:max_chars], False
    except HTTPError as e:
        return f"HTTP {e.code}: {e.reason} — {url}", True
    except URLError as e:
        return f"URL Error: {e.reason} — {url}", True
    except Exception as e:
        return f"web_fetch error: {e}", True


def web_search_duckduckgo(query: str, max_results: int = 5) -> Tuple[str, bool]:
    """Search via DuckDuckGo HTML (no API key needed). Returns (results_text, is_error)."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        req = Request(url, headers={
            **_HEADERS,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        })
        with urlopen(req, timeout=_TIMEOUT) as resp:
            html = resp.read(50000).decode("utf-8", errors="replace")

        # Parse results from DDG HTML
        results = []
        # DDG wraps results in <a class="result__a" href="...">title</a>
        # and <a class="result__snippet">snippet</a>
        links = re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        )
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        )

        for i, (href, title) in enumerate(links[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            results.append(f"{i+1}. {title_clean}\n   {href}\n   {snippet}")

        if not results:
            return f"No results found for: {query}", False

        return "\n\n".join(results), False
    except Exception as e:
        return f"web_search error: {e}", True
