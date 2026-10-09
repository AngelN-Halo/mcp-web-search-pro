# MCP Web Search Pro

[![M8ven Score](https://m8ven.ai/badge/mcp/angeln-halo-mcp-web-search-pro-1el0du?v=f453bfeab1a5f0809748a2bfba8f24a2)](https://m8ven.ai/mcp/angeln-halo-mcp-web-search-pro-1el0du?s=readme)

An extended, self-hosted web research server for MCP-compatible clients. It
gives an AI assistant tools for discovering web pages, extracting readable
content, rendering JavaScript applications, using the Internet Archive when a
page is unavailable, and reading YouTube captions.

The service is designed for local or trusted-network use. It is stateless,
requires no API keys, and exposes the MCP Streamable HTTP transport on port
`8082`. It can run beside a lighter web-search MCP server on another port.

Runs as a **separate container on port 8082** alongside the lightweight
`mcp-web-search` (port 8081). Point clients at whichever fits the task — if pro
ever feels sluggish, the lightweight one is still right there untouched.

## Features

- Web search through DuckDuckGo, with result limits and region selection.
- Readable HTML extraction to Markdown with navigation and boilerplate removed.
- Automatic PDF detection and text extraction.
- Headless Chromium rendering for React, Vue, SPA, and other JS-heavy pages.
- Wayback Machine snapshots for unavailable or changed pages.
- YouTube transcripts from URLs or video IDs, with optional timestamps.
- `/health` endpoint for container and process checks.
- Stateless Streamable HTTP and stdio MCP transports.

## MCP tools

| Tool | Description |
|------|-------------|
| `web_search`         | Search the web via DuckDuckGo (ddgs). Args: `query` (required), `max_results` (1–20), `region`. |
| `web_fetch`          | Fetch a URL → Markdown. Auto-detects PDFs and extracts text. Uses trafilatura for clean article extraction (strips nav/ads/boilerplate). Args: `url`, `timeout`. |
| `web_fetch_js`       | Render a JS-heavy / SPA page with headless Chromium, then extract text. Use when `web_fetch` returns an empty/stub page. Slower (~5–15s). Args: `url`, `wait_ms` (0–15000, default 2500), `timeout`. |
| `web_fetch_archive`  | Fetch the latest (or timestamped) archived snapshot of a URL from the Wayback Machine. Retries on transient 503s. Args: `url`, `timestamp` (optional `YYYYMMDDhhmm`). |
| `youtube_transcript` | Fetch a YouTube video's transcript. Accepts a full YouTube URL or bare 11-char video id. Args: `url`, `include_timestamps` (bool, default false). |

Use `web_fetch` for ordinary pages because it is faster. Use `web_fetch_js`
when the first tool returns an empty shell or incomplete content because the
site depends on client-side JavaScript.

### Tool annotations

All five tools explicitly declare `readOnlyHint`, `destructiveHint`,
`idempotentHint`, and `openWorldHint`. Search, ordinary fetch, archive fetch,
and transcripts declare read-only, non-destructive, idempotent behavior with
external access. Results can change between calls; idempotency describes side
effects, not identical results.

`web_fetch_js` declares `readOnlyHint: false` and `idempotentHint: false`
conservatively because it executes third-party JavaScript, which can send
requests or cause side effects when a page loads. It declares
`destructiveHint: false` because the handler does not intentionally delete or
modify resources, and `openWorldHint: true` because it accesses external sites.
Annotations describe behavior to clients; they do not enforce access controls.

## Run with Docker

```bash
docker compose up -d --build
```

Verify:
```bash
curl http://localhost:8082/health      # → {"status":"ok"}
```

Logs / restart / stop:
```bash
docker compose logs -f
docker compose restart
docker compose down
```

> The first build is large (~400MB) because it downloads Chromium for
> Playwright. Subsequent builds reuse the cached layer.

## Connecting clients

Point any MCP-compatible client at:

```
http://<server-host>:8082/mcp
```

No headers or API keys are required. The server runs in stateless mode.

## Run locally without Docker

Python 3.12 is recommended:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
python server.py
```

Run the HTTP transport directly with:

```bash
python server.py --sse --host 127.0.0.1 --port 8082
```

Despite the historical `--sse` option name, the HTTP implementation uses the
stateless MCP Streamable HTTP transport.

## Performance notes

- Only `web_fetch_js` is heavy at runtime — it launches Chromium per call
  (~5–15s). The other four tools are lightweight pure-Python and stay snappy.
- `shm_size: 1gb` in `docker-compose.yml` prevents Chromium sandbox crashes
  inside the container.
- For routine non-JS pages, use `web_fetch` (fast); reserve `web_fetch_js` for
  React/Vue/SPA sites that render content with JavaScript.

## Configuration

To change the port, edit both the `ports:` mapping and the `command:` in
`docker-compose.yml`. To enable raw request/response logging for debugging,
switch to the commented `--verbose` `command:` line.

## Validation

This repository is intended to be pushed as source code and Docker build
configuration. The automated test suite exercises all five tools, content
extraction, error
handling, browser cleanup, and the annotations returned by MCP tool discovery.
External providers and browser operations are mocked, so tests do not require
internet access or a Chromium installation. Run it after installing the Python
dependencies:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs these tests on Python 3.12 for pushes and pull requests.
For a local container smoke check, also run:

```bash
git diff --check
docker compose config
docker compose up -d --build
curl http://localhost:8082/health
docker compose logs --tail=100
docker compose down
```

Do not publish this service directly to the public internet without adding
authentication or placing it behind a trusted reverse proxy. The HTTP mode
binds to `0.0.0.0`, permits requests without API keys, and allows all origins;
that is convenient for local MCP clients but is not an access-control layer.

## Security and operating notes

- `web_fetch` and `web_fetch_js` can request arbitrary HTTP(S) URLs. Restrict
  network access or add URL/DNS safeguards if untrusted users can call the
  service.
- Keep `--verbose` disabled outside short local debugging sessions because it
  logs request and response bodies.
- Dependencies use minimum-version constraints. Pin or lock versions before
  relying on reproducible production builds.

## Files

- `server.py` — the MCP server (5 tools)
- `requirements.txt` — `mcp`, `httpx`, `beautifulsoup4`, `markdownify`, `ddgs`,
  `trafilatura`, `pypdf`, `youtube-transcript-api`, `playwright`
- `Dockerfile` — Python 3.12-slim + Playwright Chromium
- `docker-compose.yml` — service on port 8082 with `shm_size: 1gb`

## License

MIT — see [LICENSE](LICENSE).
