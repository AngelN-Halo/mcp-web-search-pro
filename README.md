# mcp-web-search-pro

The "pro" build of the MCP web-search server — adds JS-rendered fetch, PDF
extraction, smarter article extraction, Wayback Machine fallback, and YouTube
transcripts. Same stateless Streamable HTTP transport, no API keys required.

Runs as a **separate container on port 8082** alongside the lightweight
`mcp-web-search` (port 8081). Point clients at whichever fits the task — if pro
ever feels sluggish, the lightweight one is still right there untouched.

## Tools

| Tool | Description |
|------|-------------|
| `web_search`         | Search the web via DuckDuckGo (ddgs). Args: `query` (required), `max_results` (1–20), `region`. |
| `web_fetch`          | Fetch a URL → Markdown. Auto-detects PDFs and extracts text. Uses trafilatura for clean article extraction (strips nav/ads/boilerplate). Args: `url`, `timeout`. |
| `web_fetch_js`       | Render a JS-heavy / SPA page with headless Chromium, then extract text. Use when `web_fetch` returns an empty/stub page. Slower (~5–15s). Args: `url`, `wait_ms` (0–15000, default 2500), `timeout`. |
| `web_fetch_archive`  | Fetch the latest (or timestamped) archived snapshot of a URL from the Wayback Machine. Retries on transient 503s. Args: `url`, `timestamp` (optional `YYYYMMDDhhmm`). |
| `youtube_transcript` | Fetch a YouTube video's transcript. Accepts a full YouTube URL or bare 11-char video id. Args: `url`, `include_timestamps` (bool, default false). |

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
http://<server-host>:8082/sse
```

No headers or API keys required. The server runs in stateless mode, so clients
that skip the MCP `initialize` handshake (e.g. llama-ui) work fine.

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

## Before committing or pushing

This repository is intended to be pushed as source code and Docker build
configuration. Before the first commit:

1. Review the files that will be committed:

   ```bash
   git status --short
   git diff -- . ':!README.md'
   ```

2. Confirm that no secrets or local state are present. Do not commit `.env`
   files, virtual environments, caches, logs, editor settings, or local agent
   state. The repository `.gitignore` excludes these by default.

3. Validate the resolved Compose configuration:

   ```bash
   docker compose config
   ```

4. Build and smoke-test the service locally:

   ```bash
   docker compose up -d --build
   curl http://localhost:8082/health
   docker compose logs --tail=100
   docker compose down
   ```

5. Review the final staged file list before committing:

   ```bash
   git diff --cached --name-status
   git diff --cached --check
   ```

There is currently no automated test, lint, typecheck, or CI suite in this
project. The Compose validation and health check above are the available
project-level checks.

## First Git push

Run these commands from the project directory after reviewing the staged
content. Replace the remote URL and branch name with the intended destination:

```bash
git init
git add .
git diff --cached --check
git diff --cached --name-status
git commit -m "Initial commit: MCP web search pro"
git branch -M main
git remote add origin <repository-url>
git push -u origin main
```

Do not publish this service directly to the public internet without adding
authentication or placing it behind a trusted reverse proxy. The HTTP mode
binds to `0.0.0.0`, permits requests without API keys, and allows all origins;
that is convenient for local MCP clients but is not an access-control layer.

## Operational security notes

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
