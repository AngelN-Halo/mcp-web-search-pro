"""Offline regression tests for all exposed MCP tools."""
import io
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from pypdf import PdfWriter

import server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_client_can_initialize_and_discover_annotations(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(server.__file__).resolve())],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.list_tools()
                self.assertEqual(len(result.tools), 5)
                for tool in result.tools:
                    self.assertIsNotNone(tool.annotations)
                    for hint in ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint'):
                        self.assertIs(type(tool.model_dump()['annotations'][hint]), bool)
                response = await session.call_tool('youtube_transcript', {'url': 'invalid'})
                self.assertIn('Could not extract', response.content[0].text)

    async def test_discovery_declares_all_four_boolean_hints(self):
        tools = await server.list_tools()
        self.assertEqual({t.name for t in tools}, {
            'web_search', 'web_fetch', 'web_fetch_js',
            'web_fetch_archive', 'youtube_transcript',
        })
        for tool in tools:
            with self.subTest(tool=tool.name):
                hints = tool.model_dump()['annotations']
                for hint in ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint'):
                    self.assertIs(type(hints[hint]), bool)
                self.assertFalse(hints['destructiveHint'])
                self.assertTrue(hints['openWorldHint'])
                self.assertEqual(hints['readOnlyHint'], tool.name != 'web_fetch_js')
                self.assertEqual(hints['idempotentHint'], tool.name != 'web_fetch_js')

    async def test_dispatches_every_declared_tool(self):
        handlers = {
            'web_search': '_handle_search', 'web_fetch': '_handle_fetch',
            'web_fetch_js': '_handle_fetch_js', 'web_fetch_archive': '_handle_fetch_archive',
            'youtube_transcript': '_handle_youtube',
        }
        for name, handler in handlers.items():
            with self.subTest(tool=name), patch.object(server, handler, new_callable=AsyncMock) as mock:
                mock.return_value = [server.TextContent(type='text', text='ok')]
                result = await server.call_tool(name, {'url': 'https://example.org'})
                self.assertEqual(result[0].text, 'ok')
                mock.assert_awaited_once_with({'url': 'https://example.org'})

    async def test_unknown_tool_returns_error(self):
        result = await server.call_tool('unknown', {})
        self.assertIn('Unknown tool', result[0].text)

    async def test_missing_argument_returns_error(self):
        result = await server.call_tool('web_fetch', {})
        self.assertIn('Error executing web_fetch', result[0].text)

    async def test_search_formats_results_and_caps_limit(self):
        with patch('ddgs.DDGS') as ddgs:
            ddgs.return_value.text.return_value = [{'title': 'Example', 'href': 'https://example.org', 'body': 'x' * 600}]
            result = await server.call_tool('web_search', {'query': 'example', 'max_results': 99})
            ddgs.return_value.text.assert_called_once_with('example', max_results=20)
            self.assertIn('**Example**', result[0].text)
            self.assertIn('https://example.org', result[0].text)
            self.assertNotIn('x' * 501, result[0].text)

    async def test_search_no_results(self):
        with patch('ddgs.DDGS') as ddgs:
            ddgs.return_value.text.return_value = []
            result = await server.call_tool('web_search', {'query': 'missing'})
            self.assertIn('No results found', result[0].text)

    async def test_search_provider_failure(self):
        with patch('ddgs.DDGS') as ddgs:
            ddgs.return_value.text.side_effect = RuntimeError('provider offline')
            result = await server.call_tool('web_search', {'query': 'example'})
            self.assertIn('Search failed: provider offline', result[0].text)

    def http_client(self, responses=None, error=None):
        client = AsyncMock()
        if error:
            client.get.side_effect = error
        else:
            client.get.side_effect = responses
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        return patch.object(server.httpx, 'AsyncClient', return_value=context), client

    def response(self, status=200, body=b'', content_type='text/html', url='https://example.org'):
        return httpx.Response(status, content=body, headers={'content-type': content_type}, request=httpx.Request('GET', url))

    async def test_fetch_html_extracts_content(self):
        mock, client = self.http_client([self.response(body=b'<html><title>Example</title><main><p>Useful content.</p></main></html>')])
        with mock:
            result = await server.call_tool('web_fetch', {'url': 'https://example.org'})
        self.assertIn('Useful content.', result[0].text)
        self.assertIn('Source: https://example.org', result[0].text)
        client.get.assert_awaited_once_with('https://example.org')

    async def test_fetch_pdf_selects_pdf_extraction_and_truncates(self):
        mock, _ = self.http_client([self.response(body=b'pdf bytes', content_type='application/pdf')])
        with mock, patch.object(server, '_extract_pdf', return_value='x' * 21000) as extract:
            result = await server.call_tool('web_fetch', {'url': 'https://example.org'})
        extract.assert_called_once_with(b'pdf bytes')
        self.assertIn('PDF content from', result[0].text)
        self.assertNotIn('x' * 20001, result[0].text)

    async def test_fetch_unknown_content_type(self):
        mock, _ = self.http_client([self.response(body=b'{}', content_type='application/json')])
        with mock:
            result = await server.call_tool('web_fetch', {'url': 'https://example.org'})
        self.assertIn('Non-HTML response (application/json)', result[0].text)

    async def test_fetch_http_error(self):
        mock, _ = self.http_client([self.response(status=404)])
        with mock:
            result = await server.call_tool('web_fetch', {'url': 'https://example.org'})
        self.assertIn('HTTP 404', result[0].text)

    async def test_fetch_timeout(self):
        mock, _ = self.http_client(error=httpx.ReadTimeout('timed out'))
        with mock:
            result = await server.call_tool('web_fetch', {'url': 'https://example.org'})
        self.assertIn('Request failed', result[0].text)

    async def test_fetch_tools_reject_non_http_urls(self):
        for name in ('web_fetch', 'web_fetch_js', 'web_fetch_archive'):
            with self.subTest(tool=name), patch.object(server.httpx, 'AsyncClient') as client:
                result = await server.call_tool(name, {'url': 'file:///etc/passwd'})
                self.assertIn('Unsupported URL scheme', result[0].text)
                client.assert_not_called()

    def browser_context(self):
        page = AsyncMock()
        page.content.return_value = '<main><p>Rendered content.</p></main>'
        page.title.return_value = 'Rendered title'
        browser = AsyncMock()
        browser.new_page.return_value = page
        runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=runtime)
        context.__aexit__ = AsyncMock(return_value=False)
        return patch('playwright.async_api.async_playwright', return_value=context), browser, page

    async def test_js_fetch_renders_and_closes_browser(self):
        mock, browser, page = self.browser_context()
        page.wait_for_load_state.side_effect = RuntimeError('network stays busy')
        with mock:
            result = await server.call_tool('web_fetch_js', {'url': 'https://example.org', 'wait_ms': 20000, 'timeout': 200})
        self.assertIn('Rendered content.', result[0].text)
        self.assertIn('# Rendered title', result[0].text)
        page.goto.assert_awaited_once_with('https://example.org', wait_until='domcontentloaded', timeout=90000)
        page.wait_for_timeout.assert_awaited_once_with(15000)
        browser.close.assert_awaited_once()

    async def test_js_fetch_closes_browser_on_navigation_failure(self):
        mock, browser, page = self.browser_context()
        page.goto.side_effect = RuntimeError('navigation failed')
        with mock:
            result = await server.call_tool('web_fetch_js', {'url': 'https://example.org'})
        self.assertIn('JS render failed', result[0].text)
        browser.close.assert_awaited_once()

    async def test_archive_fetch_uses_timestamp_and_snapshot_url(self):
        snapshot = 'https://web.archive.org/web/202601010000/https://example.org'
        mock, client = self.http_client([self.response(body=b'<main>Archived content.</main>', url=snapshot)])
        with mock:
            result = await server.call_tool('web_fetch_archive', {'url': 'https://example.org', 'timestamp': '202601010000'})
        client.get.assert_awaited_once_with(snapshot)
        self.assertIn('Archived content.', result[0].text)
        self.assertIn('Archived snapshot: ' + snapshot, result[0].text)

    async def test_archive_missing_snapshot(self):
        mock, _ = self.http_client([self.response(status=404)])
        with mock:
            result = await server.call_tool('web_fetch_archive', {'url': 'https://example.org'})
        self.assertIn('No archived snapshot found', result[0].text)

    async def test_archive_retries_transient_failure(self):
        mock, client = self.http_client([self.response(status=503), self.response(body=b'<main>Recovered snapshot.</main>')])
        with mock, patch.object(server.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            result = await server.call_tool('web_fetch_archive', {'url': 'https://example.org'})
        self.assertEqual(client.get.await_count, 2)
        sleep.assert_awaited_once_with(1.5)
        self.assertIn('Recovered snapshot.', result[0].text)

    async def test_archive_exhausted_retries(self):
        mock, client = self.http_client([self.response(status=503)] * 3)
        with mock, patch.object(server.asyncio, 'sleep', new_callable=AsyncMock):
            result = await server.call_tool('web_fetch_archive', {'url': 'https://example.org'})
        self.assertEqual(client.get.await_count, 3)
        self.assertIn('after retries', result[0].text)

    async def test_archive_pdf_extraction(self):
        mock, _ = self.http_client([self.response(body=b'pdf bytes', content_type='application/pdf')])
        with mock, patch.object(server, '_extract_pdf', return_value='Archived PDF text'):
            result = await server.call_tool('web_fetch_archive', {'url': 'https://example.org/report.pdf'})
        self.assertIn('Archived PDF text', result[0].text)

    async def test_youtube_transcript_with_and_without_timestamps(self):
        for include_ts in (False, True):
            with self.subTest(timestamps=include_ts), patch('youtube_transcript_api.YouTubeTranscriptApi') as api:
                api.return_value.fetch.return_value = [SimpleNamespace(start=65.9, text='Hello there')]
                result = await server.call_tool('youtube_transcript', {'url': 'https://youtu.be/abcdefghijk', 'include_timestamps': include_ts})
                api.return_value.fetch.assert_called_once_with('abcdefghijk')
                self.assertIn('Hello there', result[0].text)
                self.assertEqual('[01:05]' in result[0].text, include_ts)

    async def test_youtube_empty_transcript(self):
        with patch('youtube_transcript_api.YouTubeTranscriptApi') as api:
            api.return_value.fetch.return_value = []
            result = await server.call_tool('youtube_transcript', {'url': 'abcdefghijk'})
        self.assertIn('No transcript available', result[0].text)

    async def test_youtube_disabled_captions(self):
        with patch('youtube_transcript_api.YouTubeTranscriptApi') as api:
            api.return_value.fetch.side_effect = RuntimeError('captions disabled')
            result = await server.call_tool('youtube_transcript', {'url': 'abcdefghijk'})
        self.assertIn('captions may be disabled', result[0].text)

    async def test_youtube_provider_failure(self):
        with patch('youtube_transcript_api.YouTubeTranscriptApi') as api:
            api.return_value.fetch.side_effect = RuntimeError('provider offline')
            result = await server.call_tool('youtube_transcript', {'url': 'abcdefghijk'})
        self.assertIn('YouTube transcript failed', result[0].text)

    async def test_youtube_invalid_id(self):
        with patch('youtube_transcript_api.YouTubeTranscriptApi') as api:
            result = await server.call_tool('youtube_transcript', {'url': 'invalid'})
        api.assert_not_called()
        self.assertIn('Could not extract', result[0].text)


class ExtractionTests(unittest.TestCase):
    def test_youtube_supported_url_formats(self):
        for url in ('abcdefghijk', 'https://youtu.be/abcdefghijk',
                    'https://www.youtube.com/watch?v=abcdefghijk',
                    'https://www.youtube.com/watch?feature=share&v=abcdefghijk',
                    'https://www.youtube.com/embed/abcdefghijk',
                    'https://www.youtube.com/shorts/abcdefghijk'):
            with self.subTest(url=url):
                self.assertEqual(server._extract_youtube_id(url), 'abcdefghijk')

    def test_pdf_detection(self):
        self.assertTrue(server._looks_like_pdf('https://example.org/a.PDF?download=1', ''))
        self.assertTrue(server._looks_like_pdf('https://example.org/download', 'application/pdf'))
        self.assertFalse(server._looks_like_pdf('https://example.org', 'text/html'))

    def test_pdf_without_text(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        content = io.BytesIO()
        writer.write(content)
        self.assertIn('no extractable text', server._extract_pdf(content.getvalue()))

    def test_pdf_text_is_extracted_in_page_order(self):
        writer = PdfWriter()
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        for text in ('First page', 'Second page'):
            page = writer.add_blank_page(width=100, height=100)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 10 50 Td ({text}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
        content = io.BytesIO()
        writer.write(content)
        self.assertEqual(server._extract_pdf(content.getvalue()), 'First page\n\nSecond page')

    def test_html_fallback_removes_scripts_and_navigation(self):
        html = '<html><title>Example</title><body><nav>Menu</nav><script>secret()</script><main><h1>Article</h1><p>Useful text.</p></main></body></html>'
        with patch('trafilatura.extract', return_value=None):
            text = server._extract_html(html, 'https://example.org')
        self.assertIn('Useful text.', text)
        self.assertNotIn('secret()', text)
        self.assertNotIn('Menu', text)

    def test_html_article_metadata_title(self):
        with patch('trafilatura.extract', return_value='Article text'), patch('trafilatura.extract_metadata', return_value=SimpleNamespace(title='Title')):
            text = server._extract_html('<html></html>', 'https://example.org')
        self.assertEqual(text, '# Title\n\nSource: https://example.org\n\nArticle text')


if __name__ == '__main__':
    unittest.main()
