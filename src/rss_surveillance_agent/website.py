from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx


class WebsiteFetchError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WebsiteContent:
    final_url: str
    title: str
    text: str


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "noscript", "svg", "template"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if not self.hidden:
            cleaned = " ".join(data.split())
            if cleaned:
                self.parts.append(cleaned)


class WebsiteFetcher:
    """Bounded HTTP fetcher with SSRF checks; deploy behind egress controls too."""

    def __init__(self, *, timeout: float = 12, max_bytes: int = 1_500_000, client: httpx.Client | None = None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    @staticmethod
    def _validate_url(url: str) -> tuple[str, int]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise WebsiteFetchError("invalid_website_url")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise WebsiteFetchError("invalid_website_url") from exc
        if port not in {80, 443}:
            raise WebsiteFetchError("website_port_blocked")
        try:
            answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise WebsiteFetchError("website_dns_failed") from exc
        if not answers:
            raise WebsiteFetchError("website_dns_failed")
        for answer in answers:
            ip = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
            if not ip.is_global:
                raise WebsiteFetchError("website_address_blocked")
        return parsed.hostname.lower(), port

    def fetch(self, url: str) -> WebsiteContent:
        current = url
        initial_scheme = urlsplit(url).scheme
        original_host: str | None = None
        for hop in range(4):
            host, _ = self._validate_url(current)
            if original_host is None:
                original_host = host
            elif host != original_host:
                raise WebsiteFetchError("cross_host_redirect_blocked")
            try:
                with self.client.stream("GET", current, headers={"Accept": "text/html"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop == 3:
                            raise WebsiteFetchError("website_redirect_limit")
                        current = urljoin(current, location)
                        if initial_scheme == "https" and urlsplit(current).scheme != "https":
                            raise WebsiteFetchError("https_downgrade_blocked")
                        continue
                    if response.status_code >= 400:
                        raise WebsiteFetchError("website_http_error")
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                        raise WebsiteFetchError("website_not_html")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise WebsiteFetchError("website_too_large")
                        chunks.append(chunk)
                    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            except httpx.TimeoutException as exc:
                raise WebsiteFetchError("website_timeout") from exc
            except httpx.HTTPError as exc:
                raise WebsiteFetchError("website_unavailable") from exc
            parser = _TextParser()
            parser.feed(html)
            text = " ".join(" ".join(parser.parts).split())[:30_000]
            if len(text) < 80:
                raise WebsiteFetchError("website_content_insufficient")
            return WebsiteContent(current, " ".join(" ".join(parser.title_parts).split())[:300], text)
        raise WebsiteFetchError("website_redirect_limit")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

