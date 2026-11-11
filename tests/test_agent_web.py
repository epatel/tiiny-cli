"""web_search / web_fetch: DuckDuckGo's HTML page and plain-text pages, over mocked HTTP."""

import httpx
import pytest
import respx

from tai.agent import web
from tai.agent.command import load_extensions
from tai.agent.tools import ToolContext, ToolError

RESULTS = """<html><body>
<div class="result results_links"><h2 class="result__title">
  <a rel="nofollow" class="result__a" href="https://textual.textualize.io/guide/screens/">Textual - <b>Screens</b></a></h2>
  <a class="result__snippet" href="https://textual.textualize.io/guide/screens/"><b>Textual</b> is a TUI framework.</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x">Wrapped</a></div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">An ad</a></div>
</body></html>"""

PAGE = """<html><head><title>Docs  page</title><style>p{color:red}</style><script>var x=1;</script></head>
<body><h1>Install</h1><p>Run   <code>pip install x</code>.</p><ul><li>one</li><li>two</li></ul></body></html>"""


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(tmp_path)


def test_results_are_parsed_unwrapped_and_ads_dropped():
    results = web.parse_results(RESULTS)
    assert [(r.title, r.url) for r in results] == [
        ("Textual - Screens", "https://textual.textualize.io/guide/screens/"),
        ("Wrapped", "https://example.com/a?b=1")]
    assert results[0].snippet == "Textual is a TUI framework."


@respx.mock
def test_search_tool(ctx):
    route = respx.post(web.SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTS))
    out = web.web_tools()["web_search"].run({"query": "textual screens", "max_results": 1}, ctx)
    assert out == ("1. Textual - Screens\n   https://textual.textualize.io/guide/screens/\n"
                   "   Textual is a TUI framework.")
    assert route.calls[0].request.content == b"q=textual+screens"


@respx.mock
def test_search_rate_limit_is_a_tool_error(ctx):
    respx.post(web.SEARCH_URL).mock(return_value=httpx.Response(202, text="anomaly"))
    with pytest.raises(ToolError, match="HTTP 202"):
        web.web_tools()["web_search"].run({"query": "x"}, ctx)


@respx.mock
def test_fetch_html_as_text(ctx):
    respx.get("https://docs.example/x").mock(
        return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
    out = web.web_tools()["web_fetch"].run({"url": "https://docs.example/x"}, ctx)
    assert out == "Docs page\nhttps://docs.example/x\n\nInstall\n\nRun pip install x.\n\n- one\n\n- two"


@respx.mock
def test_fetch_long_page_in_parts_and_rejects(ctx, monkeypatch):
    monkeypatch.setattr(web, "MAX_PAGE", 10)
    respx.get("https://a.example/t").mock(
        return_value=httpx.Response(200, text="0123456789abcdefghij!", headers={"content-type": "text/plain"}))
    first = web.web_tools()["web_fetch"].run({"url": "https://a.example/t"}, ctx)
    assert "0123456789" in first and "11 more characters; fetch again with offset=10" in first
    assert web.web_tools()["web_fetch"].run({"url": "https://a.example/t", "offset": 20}, ctx).endswith("!")
    respx.get("https://a.example/img").mock(
        return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    with pytest.raises(ToolError, match="image/png"):
        web.fetch("https://a.example/img")
    with pytest.raises(ToolError, match="Only http"):
        web.fetch("file:///etc/passwd")


def test_both_ask_as_one_web_group_and_are_offered(tmp_path):
    tools = web.web_tools()
    assert {t.grant for t in tools.values()} == {"web"}
    assert tools["web_fetch"].preview({"url": "https://x"}, None).body == "https://x"
    assert {"web_search", "web_fetch"} <= set(load_extensions(tmp_path, use_mcp=False).tools)
