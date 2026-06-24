#!/usr/bin/env python3
"""
Stream URL extractor with proxy rotation.
Output: stream_urls and iframe_urls only.

Requires:
    pip install curl_cffi

Run:
    python multiembed.py [url]
    python multiembed.py --serve --port 8787
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
import random

# ── optional CF-bypass libs ──────────────────────────────────────────────────
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

# ── constants ────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT    = 30
DEFAULT_INPUT_URL  = "https://multiembed.mov/?video_id=931285&tmdb=1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_IFRAME_DEPTH = 3
DEBUG = False

# ── proxy list ───────────────────────────────────────────────────────────────
PROXIES = [
    ("31.59.20.176",    6754, "glsbcfvl", "336gxb0or4n9"),
    ("31.56.127.193",   7684, "glsbcfvl", "336gxb0or4n9"),
    ("45.38.107.97",    6014, "glsbcfvl", "336gxb0or4n9"),
    ("38.154.203.95",   5863, "glsbcfvl", "336gxb0or4n9"),
    ("198.105.121.200", 6462, "glsbcfvl", "336gxb0or4n9"),
    ("64.137.96.74",    6641, "glsbcfvl", "336gxb0or4n9"),
    ("198.23.243.226",  6361, "glsbcfvl", "336gxb0or4n9"),
    ("38.154.185.97",   6370, "glsbcfvl", "336gxb0or4n9"),
    ("142.111.67.146",  5611, "glsbcfvl", "336gxb0or4n9"),
    ("191.96.254.138",  6185, "glsbcfvl", "336gxb0or4n9"),
]

_proxy_index = 0
_failed_proxies: set = set()


def get_proxy_url() -> Optional[str]:
    global _proxy_index
    available = [p for i, p in enumerate(PROXIES) if i not in _failed_proxies]
    if not available:
        _failed_proxies.clear()
        available = PROXIES
    host, port, user, pwd = available[_proxy_index % len(available)]
    _proxy_index += 1
    return f"http://{user}:{pwd}@{host}:{port}"


def mark_proxy_failed(proxy_url: str):
    for i, (host, port, user, pwd) in enumerate(PROXIES):
        if f"{host}:{port}" in proxy_url:
            _failed_proxies.add(i)
            if DEBUG:
                print(f"[proxy] Marked failed: {host}:{port}", file=sys.stderr)
            break


# ── regex patterns ────────────────────────────────────────────────────────────
STREAM_URL_RE = re.compile(
    r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:m3u8|mpd|mp4|m3u)(?:\?[^\s"\'<>\\]*)?',
    re.IGNORECASE,
)
PLAYER_FILE_RE = re.compile(
    r"""(?:file|src|url)\s*[:=]\s*(['"])(?P<url>https?://.*?\.(?:m3u8|mpd|mp4)(?:\?.*?)?)\1""",
    re.IGNORECASE | re.DOTALL,
)
PLAYERJS_RE = re.compile(
    r"""Playerjs\s*\(\s*\{[^}]*file\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
BASE64_STREAM_RE = re.compile(r'(?:atob|btoa)\s*\(\s*["\']([^"\']+)["\']\s*\)', re.IGNORECASE)
VIPSTREAM_RE = re.compile(
    r'(?:window\.location\.href|src)\s*=\s*["\']([^"\']*vipstream[^"\']*)["\']', re.IGNORECASE
)
PLAY_TOKEN_RE   = re.compile(r"""[?&]play=([^&"'<>]+)""", re.IGNORECASE)
LOAD_SOURCES_RE = re.compile(r"""load_sources\((['"])(?P<token>[^'"]+)\1\)""")
IFRAME_SRC_RE   = re.compile(r"""<iframe\b[^>]*\bsrc=(['"])(?P<src>.*?)\1""", re.IGNORECASE | re.DOTALL)
SOURCE_LI_RE    = re.compile(r"""<li\b(?P<attrs>[^>]*\bdata-id=[^>]*)>""", re.IGNORECASE | re.DOTALL)
ATTR_RE         = re.compile(r"""([:\w-]+)\s*=\s*(['"])(.*?)\2""", re.DOTALL)
JS_STREAM_RE    = re.compile(
    r'(?:source|src|file|url)\s*[:=]\s*["\']([^"\']*\.(?:m3u8|mpd|mp4)[^"\']*)["\']',
    re.IGNORECASE,
)
VOE_PATTERN        = re.compile(r"'hls':\s*'([^']+)'", re.IGNORECASE)
STREAMTAPE_PATTERN = re.compile(r"get_video\?id=[^'\"]+", re.IGNORECASE)
DOODSTREAM_RAND    = re.compile(r"'(/pass_md5/[^']+)'", re.IGNORECASE)
JSON_SOURCE_RE     = re.compile(
    r'"(?:file|src|url)"\s*:\s*"([^"]+\.(?:m3u8|mpd|mp4)[^"]*)"', re.IGNORECASE,
)
EVAL_PACKED_RE = re.compile(r'eval\s*\(\s*function\s*\(p,a,c,k,e', re.IGNORECASE)

JS_IFRAME_SRC_RE      = re.compile(r"""(?:iframe|frame)['"]?\s*,\s*['"]src['"]\s*:\s*['"]([^'"]+)['"]""", re.IGNORECASE)
JS_WINDOW_LOCATION_RE = re.compile(r"""window\.location\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
JS_SRC_ASSIGN_RE      = re.compile(r"""\.src\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
JS_URL_PATTERNS = [
    re.compile(r"""['"](https?://[^'"]*dood[^'"]*)['"]""",       re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*dsvplay[^'"]*)['"]""",    re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*vipstream[^'"]*)['"]""",  re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*streamwish[^'"]*)['"]""", re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*mixdrop[^'"]*)['"]""",    re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*playmogo[^'"]*)['"]""",   re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*streamtape[^'"]*)['"]""", re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*voe\.sx[^'"]*)['"]""",    re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*filemoon[^'"]*)['"]""",   re.IGNORECASE),
    re.compile(r"""['"](https?://[^'"]*upstream[^'"]*)['"]""",   re.IGNORECASE),
]


# ── dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class SourceChoice:
    video_id: str
    server_id: str
    label: str = ""
    quality: str = ""


@dataclass
class ResolveResult:
    input_url: str
    ok: bool
    status: str
    stream_urls: List[str] = field(default_factory=list)
    iframe_urls: List[str] = field(default_factory=list)

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "input_url": self.input_url,
            "ok": self.ok,
            "status": self.status,
            "stream_urls": self.stream_urls,
            "iframe_urls": self.iframe_urls,
        }


# ── helpers ───────────────────────────────────────────────────────────────────
def unique_keep_order(items) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def request_headers(
    referer: Optional[str] = None,
    origin: Optional[str] = None,
    ajax: bool = False,
    extra: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    if referer and not origin:
        parsed = urllib.parse.urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    headers: Dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if ajax:
        headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin" if (
                origin and referer and
                urllib.parse.urlparse(origin).netloc == urllib.parse.urlparse(referer).netloc
            ) else "cross-site",
        })
    else:
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not?A_Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site" if origin else "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    if extra:
        headers.update(extra)
    return headers


# ── HTTP with proxy rotation ──────────────────────────────────────────────────
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
    origin: Optional[str] = None,
    allow_redirects: bool = True,
    ajax: bool = False,
    max_proxy_tries: int = 3,
) -> Tuple[int, str, Dict[str, str], str]:
    if not url or not url.startswith("http"):
        return 0, url, {}, ""
    if DEBUG:
        print(f"[DEBUG GET] {url}", file=sys.stderr)

    hdrs = request_headers(referer, origin, ajax=ajax)
    last_error = None

    for attempt in range(max_proxy_tries):
        proxy_url = get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url}

        # Try curl_cffi with proxy
        if HAS_CURL_CFFI:
            for imp in ["chrome131", "chrome124", "chrome120"]:
                try:
                    session = curl_requests.Session()
                    resp = session.get(
                        url, headers=hdrs, impersonate=imp,
                        timeout=timeout, allow_redirects=allow_redirects,
                        proxies=proxies,
                    )
                    if DEBUG:
                        print(f"[DEBUG curl_cffi/{imp}] {resp.status_code} via {proxy_url.split('@')[1]}", file=sys.stderr)
                    return resp.status_code, resp.url, dict(resp.headers), resp.text
                except Exception as e:
                    last_error = e
                    if DEBUG:
                        print(f"[DEBUG curl_cffi fail] {e}", file=sys.stderr)
            mark_proxy_failed(proxy_url)
            continue

        # Try cloudscraper with proxy
        if HAS_CLOUDSCRAPER:
            try:
                scraper = cloudscraper.create_scraper()
                resp = scraper.get(url, headers=hdrs, timeout=timeout,
                                   allow_redirects=allow_redirects, proxies=proxies)
                return resp.status_code, resp.url, dict(resp.headers), resp.text
            except Exception as e:
                last_error = e
                mark_proxy_failed(proxy_url)
                continue

        # urllib fallback with proxy
        proxy_handler = urllib.request.ProxyHandler(proxies)
        handlers = [proxy_handler]
        if not allow_redirects:
            handlers.append(NoRedirect())
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, resp.geturl(), dict(resp.headers.items()), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, url, dict(exc.headers.items()), body
        except Exception as e:
            last_error = e
            mark_proxy_failed(proxy_url)

    if DEBUG:
        print(f"[DEBUG all proxies failed] last: {last_error}", file=sys.stderr)
    return 0, url, {}, ""


def http_post_form(
    url: str,
    form: Dict[str, str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
    origin: Optional[str] = None,
    max_proxy_tries: int = 3,
) -> Tuple[int, str, Dict[str, str], str]:
    if DEBUG:
        print(f"[DEBUG POST] {url}", file=sys.stderr)
    hdrs = request_headers(referer, origin, ajax=True)
    last_error = None

    for attempt in range(max_proxy_tries):
        proxy_url = get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url}

        if HAS_CURL_CFFI:
            for imp in ["chrome131", "chrome124", "chrome120"]:
                try:
                    session = curl_requests.Session()
                    resp = session.post(url, data=form, headers=hdrs,
                                        impersonate=imp, timeout=timeout, proxies=proxies)
                    return resp.status_code, resp.url, dict(resp.headers), resp.text
                except Exception as e:
                    last_error = e
            mark_proxy_failed(proxy_url)
            continue

        body_bytes = urllib.parse.urlencode(form).encode("utf-8")
        proxy_handler = urllib.request.ProxyHandler(proxies)
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method="POST")
        try:
            with opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return resp.status, resp.geturl(), dict(resp.headers.items()), text
        except Exception as e:
            last_error = e
            mark_proxy_failed(proxy_url)

    return 0, url, {}, ""


# ── stream/iframe extraction ──────────────────────────────────────────────────
def decode_base64_streams(text: str) -> List[str]:
    streams = []
    for match in BASE64_STREAM_RE.findall(text):
        try:
            decoded = base64.b64decode(match + "==").decode("utf-8", errors="replace")
            streams.extend(extract_stream_urls(decoded))
        except Exception:
            pass
    return streams


def try_unpack_eval(js: str) -> str:
    if not EVAL_PACKED_RE.search(js):
        return js
    try:
        m = re.search(
            r"eval\(function\(p,a,c,k,e,(?:d|r)\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
            js, re.DOTALL
        )
        if not m:
            return js
        payload, radix_s, _, wordlist_s = m.groups()
        radix = int(radix_s)
        words = wordlist_s.split("|")
        def decode_word(w: str) -> str:
            if not w:
                return w
            try:
                n = int(w, radix)
                return words[n] if 0 <= n < len(words) and words[n] else w
            except Exception:
                return w
        return re.sub(r'\b(\w+)\b', lambda x: decode_word(x.group(1)), payload)
    except Exception:
        return js


def extract_stream_urls(text: str) -> List[str]:
    text2 = try_unpack_eval(text)
    urls = []
    for t in (text, text2):
        urls += [html.unescape(m.group("url")) for m in PLAYER_FILE_RE.finditer(t)]
        urls += [html.unescape(m.group(0))     for m in STREAM_URL_RE.finditer(t)]
        urls += [m.group(1) for m in PLAYERJS_RE.finditer(t)]
        urls += [m.group(1) for m in JS_STREAM_RE.finditer(t)]
        urls += [m.group(1) for m in VOE_PATTERN.finditer(t)]
        urls += [m.group(0) for m in STREAMTAPE_PATTERN.finditer(t)]
        urls += [m.group(1) for m in DOODSTREAM_RAND.finditer(t)]
        urls += [m.group(1) for m in JSON_SOURCE_RE.finditer(t)]
        urls += decode_base64_streams(t)
        urls += [m.group(1) for m in VIPSTREAM_RE.finditer(t)]
    clean = []
    for u in urls:
        u = html.unescape(u).strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            continue
        clean.append(u)
    return unique_keep_order(clean)


def extract_js_constructed_urls(html_content: str) -> List[str]:
    urls = []
    for m in JS_IFRAME_SRC_RE.finditer(html_content):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            urls.append(u)
    for m in JS_WINDOW_LOCATION_RE.finditer(html_content):
        u = m.group(1)
        if u.startswith("http"):
            urls.append(u)
    for m in JS_SRC_ASSIGN_RE.finditer(html_content):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            urls.append(u)
    for pattern in JS_URL_PATTERNS:
        for m in pattern.finditer(html_content):
            urls.append(m.group(1))
    return unique_keep_order([u for u in urls if u and u != "https://"])


def extract_iframe_urls(text: str, base_url: str) -> List[str]:
    urls = []
    for m in IFRAME_SRC_RE.finditer(text):
        src = html.unescape(m.group("src")).strip()
        if src:
            urls.append(urllib.parse.urljoin(base_url, src))
    for u in extract_js_constructed_urls(text):
        if u.startswith("http"):
            urls.append(u)
        else:
            urls.append(urllib.parse.urljoin(base_url, u))
    return unique_keep_order([u for u in urls if u and u != "https://"])


def extract_source_choices(response_html: str) -> List[SourceChoice]:
    sources, matches = [], list(SOURCE_LI_RE.finditer(response_html))
    for idx, m in enumerate(matches):
        attrs_raw = m.group("attrs")
        attrs = {n.lower(): html.unescape(v) for n, _, v in ATTR_RE.findall(attrs_raw)}
        vid, srv = attrs.get("data-id"), attrs.get("data-server")
        if vid and srv:
            sources.append(SourceChoice(video_id=vid, server_id=srv))
    return sources


def extract_play_token(text: str) -> Optional[str]:
    m = PLAY_TOKEN_RE.search(text)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = LOAD_SOURCES_RE.search(text)
    if m:
        return m.group("token")
    return None


def extract_doodstream_url(html_content: str, base_url: str = "") -> Optional[str]:
    domain = urllib.parse.urlparse(base_url).netloc if base_url else "dsvplay.com"
    for pat in [
        r'https?://[^"\']+/pass_md5/[^"\']+',
        r'https?://[^"\']+\.m3u8[^"\']*',
        r'https?://[^"\']+\.mp4[^"\']*',
        r"/pass_md5/[^'\"]+",
    ]:
        for match in re.findall(pat, html_content, re.IGNORECASE):
            if match.startswith("http"):
                return match
            if match.startswith("/"):
                return f"https://{domain}{match}"
    return None


# ── deep embed processor ──────────────────────────────────────────────────────
def process_embed_url(
    embed_url: str,
    server_id: str,
    referer: str,
    result: ResolveResult,
    depth: int = 0,
) -> None:
    if depth > MAX_IFRAME_DEPTH:
        return
    if not embed_url or embed_url == "https://" or not embed_url.startswith("http"):
        return

    if any(ext in embed_url for ext in [".m3u8", ".mp4", "/pass_md5/"]):
        if embed_url not in result.stream_urls:
            result.stream_urls.append(embed_url)
        return

    parsed_embed = urllib.parse.urlparse(embed_url)
    embed_origin = f"{parsed_embed.scheme}://{parsed_embed.netloc}"

    try:
        emb_status, emb_final_url, _, embed_html = http_get(
            embed_url, referer=referer,
            origin=embed_origin if embed_url != referer else None,
        )
        if DEBUG:
            print(f"[DEBUG embed depth={depth}] {emb_status} {embed_url[:80]}", file=sys.stderr)

        if any(x in embed_url.lower() for x in ["dood", "dsvplay"]):
            dood_url = extract_doodstream_url(embed_html, emb_final_url)
            if dood_url and dood_url not in result.stream_urls:
                result.stream_urls.append(dood_url)

        for s in extract_stream_urls(embed_html):
            if s not in result.stream_urls:
                result.stream_urls.append(s)

        for s in JSON_SOURCE_RE.findall(embed_html):
            if s not in result.stream_urls:
                result.stream_urls.append(s)

        nested_iframes = extract_iframe_urls(embed_html, emb_final_url)
        nested_js_urls = extract_js_constructed_urls(embed_html)

        for nested in unique_keep_order(nested_iframes + nested_js_urls):
            if nested and nested != "https://":
                if nested not in result.iframe_urls:
                    result.iframe_urls.append(nested)
                process_embed_url(nested, server_id, embed_url, result, depth + 1)

    except Exception as e:
        if DEBUG:
            print(f"[DEBUG embed error depth={depth}] {e}", file=sys.stderr)


# ── main resolver ─────────────────────────────────────────────────────────────
def resolve(input_url: str, all_servers: bool = True) -> ResolveResult:
    result = ResolveResult(input_url=input_url, ok=False, status="resolving")

    try:
        # Step 1: initial GET
        status, final_url, headers, body = http_get(input_url, allow_redirects=False)
        if DEBUG:
            print(f"[DEBUG step1] HTTP {status}", file=sys.stderr)

        location = headers.get("Location") or headers.get("location")
        play_url = urllib.parse.urljoin(input_url, location) if location else (
            final_url if final_url != input_url else input_url
        )
        play_token = extract_play_token(play_url) or extract_play_token(body)

        if not play_url:
            result.status = "no_play_url"
            return result

        # Step 2: play page
        parsed_play  = urllib.parse.urlparse(play_url)
        play_origin  = f"{parsed_play.scheme}://{parsed_play.netloc}"
        parsed_input = urllib.parse.urlparse(input_url)
        input_origin = f"{parsed_input.scheme}://{parsed_input.netloc}"

        status, final_url, headers, page = http_get(
            play_url, referer=input_url, origin=input_origin,
        )
        if DEBUG:
            print(f"[DEBUG step2] HTTP {status}", file=sys.stderr)

        play_token = play_token or extract_play_token(page)
        result.stream_urls.extend(extract_stream_urls(page))
        result.iframe_urls.extend(extract_iframe_urls(page, final_url))

        # Step 3: response.php
        if play_token:
            response_url = urllib.parse.urljoin(play_url, "/response.php")
            r_status, _, _, response_html = http_post_form(
                response_url, {"token": play_token},
                referer=play_url, origin=play_origin,
            )
            if DEBUG:
                print(f"[DEBUG step3 response.php] HTTP {r_status}", file=sys.stderr)

            sources = extract_source_choices(response_html)
            if DEBUG:
                print(f"[DEBUG] found {len(sources)} sources", file=sys.stderr)

            sources_to_try = sources if all_servers else (sources[:1] if sources else [])
            if not sources_to_try:
                sources_to_try = sources

            # Step 4: playvideo.php per source
            for source in sources_to_try:
                playvideo_url = urllib.parse.urljoin(
                    play_url,
                    f"/playvideo.php?video_id={urllib.parse.quote(source.video_id)}"
                    f"&server_id={urllib.parse.quote(source.server_id)}"
                    f"&token={urllib.parse.quote(play_token)}&init=1",
                )
                for attempt in range(2):
                    try:
                        pv_status, _, _, pv_html = http_get(
                            playvideo_url, referer=play_url, origin=play_origin,
                        )
                        if pv_status == 403 and attempt == 0:
                            time.sleep(0.5)
                            continue
                        if pv_status not in (200, 206):
                            break

                        for s in extract_stream_urls(pv_html):
                            if s not in result.stream_urls:
                                result.stream_urls.append(s)

                        iframes   = extract_iframe_urls(pv_html, playvideo_url)
                        js_urls   = extract_js_constructed_urls(pv_html)
                        all_embeds = unique_keep_order(iframes + js_urls)

                        for embed_url in all_embeds:
                            if embed_url and embed_url != "https://":
                                if embed_url not in result.iframe_urls:
                                    result.iframe_urls.append(embed_url)
                                process_embed_url(embed_url, source.server_id, playvideo_url, result, depth=0)
                        break
                    except Exception as e:
                        if attempt == 1 and DEBUG:
                            print(f"[DEBUG playvideo error] {e}", file=sys.stderr)

        result.stream_urls = unique_keep_order([u for u in result.stream_urls if u and u != "https://"])
        result.iframe_urls = unique_keep_order([u for u in result.iframe_urls if u and u != "https://"])
        result.ok     = bool(result.stream_urls)
        result.status = "ok" if result.ok else "no_streams"
        return result

    except Exception as exc:
        result.status = "error"
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        return result


# ── HTTP server ───────────────────────────────────────────────────────────────
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "StreamResolver/5.0"

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self.write_json({
                    "ok": True,
                    "cf_bypass": {"curl_cffi": HAS_CURL_CFFI, "cloudscraper": HAS_CLOUDSCRAPER},
                    "proxies": len(PROXIES),
                })
                return

            if parsed.path == "/resolve":
                url = (params.get("url") or [""])[0]
                if not url:
                    self.write_json({"ok": False, "error": "Missing url parameter."}, 400)
                    return
                all_servers = (params.get("all") or ["1"])[0] not in ("0", "false")
                r = resolve(url, all_servers=all_servers)
                self.write_json(r.to_jsonable())
                return

            self.write_json({
                "ok": False, "error": "Not found.",
                "endpoints": ["/health", "/resolve?url=<url>&all=1"],
            }, 404)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def write_json(self, payload: Dict[str, Any], status: int = 200):
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def serve(host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Server on http://{host}:{port}")
    print(f"CF bypass: curl_cffi={HAS_CURL_CFFI}  cloudscraper={HAS_CLOUDSCRAPER}")
    print(f"Proxies: {len(PROXIES)} configured")
    print(f"Endpoint: http://{host}:{port}/resolve?url=<embed-url>&all=1")
    httpd.serve_forever()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stream URL extractor with proxy rotation.")
    parser.add_argument("url", nargs="?", default=DEFAULT_INPUT_URL)
    parser.add_argument("--serve",  action="store_true", help="Start JSON API server")
    parser.add_argument("--host",   default="127.0.0.1")
    parser.add_argument("--port",   type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--single", action="store_true", help="Only first server, not all")
    parser.add_argument("--debug",  action="store_true", help="Enable debug output")
    args = parser.parse_args(argv)

    global DEBUG
    DEBUG = args.debug

    if args.serve:
        serve(args.host, args.port)
        return 0

    all_servers = not args.single
    result = resolve(args.url, all_servers=all_servers)
    print(json.dumps(result.to_jsonable(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
