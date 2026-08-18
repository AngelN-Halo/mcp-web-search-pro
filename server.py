"""
MCP server providing web search, fetch, and content-extraction capabilities.
The "pro" build adds JS-rendered fetch, PDF extraction, smarter article
extraction (trafilatura), Wayback Machine fallback, and YouTube transcripts.

Transports (auto-detected):
  - stdio (default): pipe-based, for local MCP clients like opencode, Claude Desktop
  - SSE/HTTP: use --sse [--port PORT] for web-based MCP clients (Open WebUI, llama-ui, etc.)

Tools:
  - web_search:         Search the web via DuckDuckGo (no API key needed)
  - web_fetch:          Fetch a URL; auto-extracts Markdown via trafilatura, or text from PDFs
  - web_fetch_js:       Render a JS-heavy / SPA page with headless Chromium, then extract text
  - web_fetch_archive:  Fetch the latest archived snapshot of a URL from the Wayback Machine
  - youtube_transcript: Fetch the transcript of a YouTube video

Examples:
  python server.py                             # stdio (default)
  python server.py --sse                       # SSE on 0.0.0.0:8082
  python server.py --sse --port 9090 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import urllib.parse
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

SERVER_NAME = "web-search-pro"

server = Server(SERVER_NAME)

# ── helpers ──────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    return url


def _looks_like_pdf(url: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "application/pdf" in ct or url.lower().split("?")[0].endswith(".pdf")


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())
    if not pages:
        return "PDF contained no extractable text (may be image-based/scanned)."
    return "\n\n".join(pages)


def _extract_html(html: str, url: str) -> str:
    import trafilatura

    extracted = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=False,
        include_tables=True,
        include_images=False,
    )
    if extracted and extracted.strip():
        # Pull the title via trafilatura's metadata for a clean header
        metadata = trafilatura.extract_metadata(html) or {}
        title = ""
        if metadata:
            # trafilatura >=2 returns a Document object; older versions return a dict
            title = getattr(metadata, "title", None) or (
                metadata.get("title") if isinstance(metadata, dict) else None
            ) or ""
            title = title.strip()
        return f"# {title}\n\nSource: {url}\n\n{extracted.strip()}" if title else f"Source: {url}\n\n{extracted.strip()}"

    # Fallback to a minimal BeautifulSoup-based extraction
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.find("body") or soup
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    text = re.sub(r"\n{3,}", "\n\n", md(str(body), heading_style="ATX", strip=["img", "a"])).strip()
    return f"# {title}\n\nSource: {url}\n\n{text}" if title else f"Source: {url}\n\n{text}"


def _extract_youtube_id(url: str) -> str | None:
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?.*?[&?]v=([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # Allow a bare 11-char video id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


# ── tool definitions ─────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description="Search the web using DuckDuckGo. Returns a list of result titles, URLs, and snippets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (1-20)",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "region": {
                        "type": "string",
                        "description": "Region code like 'wt-wt' (worldwide), 'us-en', etc.",
                        "default": "wt-wt",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="web_fetch",
            description="Fetch a URL and return its content as Markdown. Auto-extracts text from PDFs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds",
                        "default": 30,
                        "minimum": 5,
                        "maximum": 120,
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="web_fetch_js",
            description="Render a JavaScript-heavy web page with a real browser and return its text as Markdown.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to render and fetch"},
                    "wait_ms": {
                        "type": "integer",
                        "description": "Extra time to wait for JS rendering after load (ms)",
                        "default": 2500,
                        "minimum": 0,
                        "maximum": 15000,
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Total browser timeout in seconds",
                        "default": 30,
                        "minimum": 10,
                        "maximum": 90,
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="web_fetch_archive",
            description="Fetch an archived snapshot of a URL from the Wayback Machine. Useful for 404 or paywalled pages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch from the archive"},
                    "timestamp": {
                        "type": "string",
                        "description": "Optional YYYYMMDDhhmm timestamp; defaults to latest available snapshot",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="youtube_transcript",
            description="Fetch the transcript (captions) of a YouTube video. Accepts a YouTube URL or video id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "YouTube video URL or 11-character video id",
                    },
                    "include_timestamps": {
                        "type": "boolean",
                        "description": "If true, prefix each snippet with its start time",
                        "default": False,
                    },
                },
                "required": ["url"],
            },
        ),
    ]


# ── tool handlers ────────────────────────────────────────────────────────


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "web_search":
            return await _handle_search(arguments)
        if name == "web_fetch":
            return await _handle_fetch(arguments)
        if name == "web_fetch_js":
            return await _handle_fetch_js(arguments)
        if name == "web_fetch_archive":
            return await _handle_fetch_archive(arguments)
        if name == "youtube_transcript":
            return await _handle_youtube(arguments)
        raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:
        return [TextContent(type="text", text=f"Error executing {name}: {exc}")]


async def _handle_search(args: dict) -> list[TextContent]:
    from ddgs import DDGS

    query = args["query"]
    max_results = min(args.get("max_results", 5), 20)

    # ddgs is synchronous — offload to a thread so searches don't block the loop.
    def _do_search() -> list:
        ddgs = DDGS()
        return list(ddgs.text(query, max_results=max_results))

    try:
        raw = await asyncio.to_thread(_do_search)
    except Exception as exc:
        return [TextContent(type="text", text=f"Search failed: {exc}")]

    if not raw:
        return [TextContent(type="text", text=f"No results found for: {query}")]

    lines: list[str] = []
    for i, r in enumerate(raw, 1):
        title = r.get("title", "")
        url = r.get("href") or r.get("url") or ""
        snippet = r.get("body") or r.get("snippet") or ""
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet[:500]}")

    return [TextContent(type="text", text=f"Search results for: {query}\n\n" + "\n\n".join(lines))]


async def _handle_fetch(args: dict) -> list[TextContent]:
    url = _clean_url(args["url"])
    timeout = min(args.get("timeout", 30), 120)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")

            if _looks_like_pdf(str(resp.url), content_type):
                text = await asyncio.to_thread(_extract_pdf, resp.content)
                return [TextContent(type="text", text=f"PDF content from {url}\n\n{text[:20000]}")]

            if "text/html" in content_type or "application/xhtml" in content_type or "text/plain" in content_type:
                readable = await asyncio.to_thread(_extract_html, resp.text, str(resp.url))
                return [TextContent(type="text", text=readable[:20000])]

            # Unknown content type — return a small preview
            preview = resp.text[:2000] if resp.text else "(binary content)"
            return [TextContent(type="text", text=f"Non-HTML response ({content_type}) from {url}:\n\n{preview}")]
    except httpx.HTTPStatusError as exc:
        return [TextContent(type="text", text=f"HTTP {exc.response.status_code} fetching {url}")]
    except httpx.RequestError as exc:
        return [TextContent(type="text", text=f"Request failed for {url}: {exc}")]
    except ValueError as exc:
        return [TextContent(type="text", text=str(exc))]


async def _handle_fetch_js(args: dict) -> list[TextContent]:
    url = _clean_url(args["url"])
    wait_ms = min(args.get("wait_ms", 2500), 15000)
    timeout_s = min(args.get("timeout", 30), 90)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [TextContent(type="text", text="Playwright is not installed in this server build.")]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                if wait_ms:
                    await page.wait_for_timeout(wait_ms)
                # Let any late network settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                html = await page.content()
                title = await page.title()
            finally:
                await browser.close()

        readable = await asyncio.to_thread(_extract_html, html, url)
        # Prefer the browser-reported title if trafilatura didn't supply one
        if title and not readable.lstrip().startswith("# "):
            readable = f"# {title}\n\n{readable}"
        return [TextContent(type="text", text=readable[:20000])]
    except Exception as exc:
        return [TextContent(type="text", text=f"JS render failed for {url}: {exc}")]


async def _handle_fetch_archive(args: dict) -> list[TextContent]:
    url = _clean_url(args["url"])
    timestamp = args.get("timestamp")

    # Wayback Machine: fetching https://web.archive.org/web/{url} (or /web/{ts}/{url})
    # 302-redirects to the closest archived snapshot. More reliable than the
    # availability API, which returns empty for some URLs it actually has.
    if timestamp:
        archive_url = f"https://web.archive.org/web/{timestamp}/{url}"
    else:
        archive_url = f"https://web.archive.org/web/{url}"

    import asyncio as _asyncio

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            # Wayback intermittently returns 503 (overloaded); retry a few times.
            resp = None
            for attempt in range(3):
                try:
                    resp = await client.get(archive_url)
                    if resp.status_code == 200:
                        break
                    if resp.status_code == 404:
                        return [TextContent(type="text", text=f"No archived snapshot found for {url}.")]
                except httpx.RequestError:
                    if attempt == 2:
                        raise
                await _asyncio.sleep(1.5 * (attempt + 1))

            if resp is None or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                return [TextContent(type="text", text=f"Wayback returned HTTP {status} for {url} after retries.")]

            snap_url = str(resp.url)
            content_type = resp.headers.get("content-type", "")

            if _looks_like_pdf(snap_url, content_type):
                text = await asyncio.to_thread(_extract_pdf, resp.content)
                return [TextContent(type="text", text=f"Archived PDF from {snap_url}\n\n{text[:20000]}")]
            readable = await asyncio.to_thread(_extract_html, resp.text, snap_url)
            return [TextContent(type="text", text=f"Archived snapshot: {snap_url}\n\n{readable[:20000]}")]
    except httpx.RequestError as exc:
        return [TextContent(type="text", text=f"Wayback request failed for {url}: {type(exc).__name__}: {exc}")]
    except ValueError as exc:
        return [TextContent(type="text", text=str(exc))]


async def _handle_youtube(args: dict) -> list[TextContent]:
    raw = args["url"]
    include_ts = args.get("include_timestamps", False)

    video_id = _extract_youtube_id(raw)
    if not video_id:
        return [TextContent(type="text", text=f"Could not extract a YouTube video id from: {raw}")]

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        # youtube_transcript_api is synchronous — offload to a thread.
        def _fetch():
            api = YouTubeTranscriptApi()
            return api.fetch(video_id)

        transcript = await asyncio.to_thread(_fetch)
        if not transcript:
            return [TextContent(type="text", text=f"No transcript available for video {video_id}.")]

        if include_ts:
            lines = [f"[{_fmt_ts(s.start)}] {s.text}" for s in transcript]
        else:
            lines = [s.text for s in transcript]
        full = " ".join(lines).strip()
        return [TextContent(type="text", text=f"YouTube transcript for {video_id}:\n\n{full[:20000]}")]
    except Exception as exc:
        msg = str(exc)
        if "disabled" in msg.lower() or "no transcript" in msg.lower():
            return [TextContent(type="text", text=f"No transcript available for video {video_id} (captions may be disabled).")]
        return [TextContent(type="text", text=f"YouTube transcript failed for {video_id}: {exc}")]


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ── transports ───────────────────────────────────────────────────────────


async def run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ASGI app that creates its own MCP transport inside the lifespan handler, so
# each uvicorn worker process gets an independent transport+session. This is
# what makes `--workers N` safe (no shared state across processes).
class McpApp:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.transport = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        method = scope["method"]
        path = scope.get("path", "")

        if method == "OPTIONS":
            headers = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"*"),
                (b"access-control-max-age", b"86400"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body"})
            return

        if method == "GET" and path.rstrip("/") == "/health":
            body = b'{"status":"ok"}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"access-control-allow-origin", b"*"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        original_send = send
        verbose = self.verbose

        if verbose and method == "POST":
            async def logged_receive():
                chunks = []
                while True:
                    event = await receive()
                    if event.get("type") == "http.request":
                        chunk = event.get("body", b"") or b""
                        if chunk:
                            chunks.append(chunk)
                        if not event.get("more_body", False):
                            break
                    else:
                        return event
                full = b"".join(chunks)
                try:
                    import json as _json
                    print(f">>> INCOMING POST {path}: {_json.dumps(_json.loads(full))}", flush=True)
                except Exception:
                    print(f">>> INCOMING POST {path} (raw): {full[:1000]!r}", flush=True)
                return {"type": "http.request", "body": full, "more_body": False}

            used_receive = logged_receive
        else:
            used_receive = receive

        async def cors_send(event):
            if event["type"] == "http.response.start":
                existing = dict(event.get("headers") or [])
                existing.setdefault(b"access-control-allow-origin", b"*")
                existing.setdefault(b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS")
                existing.setdefault(b"access-control-allow-headers", b"*")
                event["headers"] = list(existing.items())
                if verbose:
                    print(f"<<< OUTGOING status={event.get('status')}", flush=True)
            elif event["type"] == "http.response.body" and verbose:
                body = event.get("body", b"") or b""
                if body:
                    try:
                        import json as _json
                        print(f"<<< OUTGOING body: {_json.dumps(_json.loads(body))}", flush=True)
                    except Exception:
                        print(f"<<< OUTGOING body (raw): {body[:500]!r}", flush=True)
            await original_send(event)

        await self.transport.handle_request(scope, used_receive, cors_send)

    async def _lifespan(self, receive, send):
        import anyio
        from mcp.server.streamable_http import StreamableHTTPServerTransport

        self.transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
        )
        async with self.transport.connect() as streams:
            async with anyio.create_task_group() as tg:
                async def run_stateless():
                    await server.run(
                        streams[0],
                        streams[1],
                        server.create_initialization_options(),
                        stateless=True,
                    )
                tg.start_soon(run_stateless)
                await send({"type": "lifespan.startup.complete"})
                await receive()
                await send({"type": "lifespan.shutdown.complete"})


def create_app() -> McpApp:
    """ASGI app factory used by uvicorn workers. Reads config from env so
    forked workers all build the same app."""
    return McpApp(verbose=os.environ.get("MCP_VERBOSE", "") == "1")


# ── entry point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP web-search-pro server")
    parser.add_argument("--sse", action="store_true", help="Run with SSE/HTTP transport instead of stdio")
    parser.add_argument("--host", default="0.0.0.0", help="SSE listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8082, help="SSE listen port (default: 8082)")
    parser.add_argument("--workers", type=int, default=1, help="Number of uvicorn worker processes (default: 1)")
    parser.add_argument("--verbose", action="store_true", help="Log raw request/response bodies (for debugging)")
    args = parser.parse_args()

    if args.sse:
        os.environ["MCP_VERBOSE"] = "1" if args.verbose else ""
        import uvicorn

        uvicorn.run(
            "server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level="info",
        )
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
