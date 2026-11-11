"""Agent tools for the web: ``web_search`` (DuckDuckGo, no key) and ``web_fetch``.

Search parses DuckDuckGo's plain HTML page (``html.duckduckgo.com/html/``), so no key
or account is needed, at the price of breaking if they change that page. Fetch turns
HTML into plain text with the stdlib parser. Both send something out of this machine
(the query, the URL), so both ask first, as one "web" permission group.
"""

from __future__ import annotations

# Boundary module: raw HTTP responses and HTML in, text for the model out.
# aihealth: disable=primitive_obsession

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx

from tai.agent.tools import Preview, Tool, ToolContext, ToolError, truncate

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "tai-agent (+https://github.com/epatel/tiiny-cli)"
MAX_RESULTS = 10
MAX_PAGE = 20_000  # characters of page text returned per fetch (offset reads further)
MAX_DOWNLOAD = 5 * 1024**2
TIMEOUT = 20.0
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""


# ------------------------------------------------------------------ search
class _ResultParser(HTMLParser):
    """Collects ``a.result__a`` (title + link) and ``.result__snippet`` from the results page."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[Result] = []
        self._field: str | None = None  # "title" | "snippet" while inside one

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.results.append(Result("", _real_url(a.get("href") or "")))
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data):
        if self._field:
            r = self.results[-1]
            setattr(r, self._field, getattr(r, self._field) + data)


def _real_url(href: str) -> str:
    """DuckDuckGo sometimes wraps links as //duckduckgo.com/l/?uddg=<target>."""
    if href.startswith("//"):
        href = "https:" + href
    u = urlparse(href)
    if u.netloc.endswith("duckduckgo.com") and u.path.startswith("/l/"):
        return parse_qs(u.query).get("uddg", [href])[0]
    return href


def parse_results(page: str) -> list[Result]:
    parser = _ResultParser()
    parser.feed(page)
    ads = ("duckduckgo.com/y.js", "/y.js?")
    return [Result(_clean(r.title), r.url, _clean(r.snippet)) for r in parser.results
            if r.url.startswith("http") and not any(a in r.url for a in ads)]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def search(query: str, http: httpx.Client | None = None) -> list[Result]:
    http = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    try:
        r = http.post(SEARCH_URL, data={"q": query})
    except httpx.HTTPError as e:
        raise ToolError(f"Search failed: {e}") from e
    if r.status_code != 200:
        raise ToolError(f"Search failed: HTTP {r.status_code} from DuckDuckGo (rate limited? try again later)")
    return parse_results(r.text)


def format_results(results: list[Result], limit: int) -> str:
    if not results:
        return "No results"
    return "\n\n".join(f"{i}. {r.title}\n   {r.url}" + (f"\n   {r.snippet}" if r.snippet else "")
                       for i, r in enumerate(results[:limit], 1))


# ------------------------------------------------------------------ fetch
class _TextParser(HTMLParser):
    """Visible text of an HTML page: drops scripts/styles, breaks lines at block elements."""
    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "aside"}  # incl. site menus
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section",
             "article", "header", "footer", "blockquote", "table", "ul", "ol", "dt", "dd"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._pre = 0  # inside <pre>: keep the source's line breaks
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        self._skip += tag in self.SKIP
        self._pre += tag == "pre"
        self._in_title = self._in_title or tag == "title"
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        self._skip -= tag in self.SKIP and self._skip > 0
        self._pre -= tag == "pre" and self._pre > 0
        self._in_title = self._in_title and tag != "title"
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._pre:
            self.parts.append(data)
        elif not self._skip:
            text = re.sub(r"\s+", " ", data)  # like a browser: whitespace runs are one space
            after_space = not self.parts or self.parts[-1].endswith((" ", "\n"))
            self.parts.append(text.lstrip() if after_space else text)


def html_text(page: str) -> tuple[str, str]:
    """(title, text) of an HTML page."""
    parser = _TextParser()
    parser.feed(page)
    lines = (_tidy(line) for line in "".join(parser.parts).split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line != "-")).strip()
    return _clean(parser.title), text


def _tidy(line: str) -> str:
    """Trailing space off; one leading space (left by a block break) off, while the deeper
    indentation of <pre> code stays. A bullet with nothing after it (icon links) becomes "-"."""
    line = line.rstrip()
    return line[1:] if line.startswith(" ") and not line.startswith("  ") else line


def _check_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ToolError(f"Only http(s) URLs can be fetched, not {url!r}")
    return url


@dataclass(frozen=True)
class Page:
    url: str
    kind: str  # content type without parameters, e.g. text/html ("" if not sent)
    body: str

    @property
    def is_html(self) -> bool:
        return "html" in self.kind or (not self.kind and "<html" in self.body[:500].lower())


def fetch(url: str, http: httpx.Client | None = None) -> Page:
    """The page; at most MAX_DOWNLOAD bytes are read."""
    http = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)
    try:
        with http.stream("GET", _check_url(url)) as r:
            if r.status_code >= 400:
                raise ToolError(f"HTTP {r.status_code} for {url}")
            kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and not kind.startswith(TEXT_TYPES):
                raise ToolError(f"{url} is {kind}, not a text page")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= MAX_DOWNLOAD:
                    break
            return Page(url, kind, body.decode(r.encoding or "utf-8", errors="replace"))
    except httpx.HTTPError as e:
        raise ToolError(f"Fetch failed: {e}") from e


def page_text(page: Page, offset: int = 0) -> str:
    url = page.url
    title, text = html_text(page.body) if page.is_html else ("", page.body)
    part = text[offset:offset + MAX_PAGE]
    rest = len(text) - offset - len(part)
    head = f"{title}\n{url}\n\n" if title else f"{url}\n\n"
    more = f"\n\n… {rest} more characters; fetch again with offset={offset + len(part)}" if rest > 0 else ""
    return truncate(head + part + more)


# ------------------------------------------------------------------ tools
def run_search(args: dict, ctx: ToolContext) -> str:
    limit = max(1, min(int(args.get("max_results") or MAX_RESULTS), 25))
    return format_results(search(args["query"]), limit)


def run_fetch(args: dict, ctx: ToolContext) -> str:
    return page_text(fetch(args["url"]), max(0, int(args.get("offset") or 0)))


def web_tools() -> dict[str, Tool]:
    tools = [
        Tool("web_search", "Search the web (DuckDuckGo). Returns titles, URLs and snippets. "
                           "Use web_fetch to read a result.",
             {"query": {"type": "string"},
              "max_results": {"type": "integer", "description": f"Default {MAX_RESULTS}, at most 25"}},
             run_search, lambda a, c: Preview("Search the web", a["query"], "command"), grant="web",
             required=["query"]),
        Tool("web_fetch", "Fetch a web page (http/https) and return its text, without scripts and styles. "
                          f"Long pages come in parts of {MAX_PAGE} characters; pass offset for the next part.",
             {"url": {"type": "string"},
              "offset": {"type": "integer", "description": "Character offset to continue from"}},
             run_fetch, lambda a, c: Preview("Fetch a web page", a["url"], "command"), grant="web",
             required=["url"]),
    ]
    return {t.name: t for t in tools}
