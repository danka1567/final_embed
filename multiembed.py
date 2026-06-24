#!/usr/bin/env python3
"""
Stream URL extractor with proxy rotation + cookie session handling.
Output: stream_urls and iframe_urls only.

Requires:
    pip install curl_cffi

Run:
    python multiembed.py [url] [--debug] [--single]
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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ── constants ─────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT   = 30
DEFAULT_INPUT_URL = "https://multiembed.mov/?video_id=931285&tmdb=1"
DEFAULT_UA        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_IFRAME_DEPTH = 3
DEBUG = False

# ── proxy list ────────────────────────────────────────────────────────────────
PROXIES = [
    ("31.59.20.176",    6754,  "glsbcfvl", "336gxb0or4n9"),
    ("31.56.127.193",   7684,  "glsbcfvl", "336gxb0or4n9"),
    ("45.38.107.97",    6014,  "glsbcfvl", "336gxb0or4n9"),
    ("38.154.203.95",   5863,  "glsbcfvl", "336gxb0or4n9"),
    ("198.105.121.200", 6462,  "glsbcfvl", "336gxb0or4n9"),
    ("64.137.96.74",    6641,  "glsbcfvl", "336gxb0or4n9"),
    ("198.23.243.226",  6361,  "glsbcfvl", "336gxb0or4n9"),
    ("38.154.185.97",   6370,  "glsbcfvl", "336gxb0or4n9"),
    ("142.111.67.146",  5611,  "glsbcfvl", "336gxb0or4n9"),
    ("191.96.254.138",  6185,  "glsbcfvl", "336gxb0or4n9"),
]

_proxy_idx    = 0
_failed_set: set = set()


def _next_proxy(skip_failed: bool = True) -> Tuple[str, str]:
    """Return (proxy_url, label). Rotates through proxies, skipping failed ones."""
    global _proxy_idx
    pool = [(i, p) for i, p in enumerate(PROXIES) if i not in _failed_set] if skip_failed else list(enumerate(PROXIES))
    if not pool:
        _failed_set.clear()
        pool = list(enumerate(PROXIES))
    idx, (host, port, user, pwd) = pool[_proxy_idx % len(pool)]
    _proxy_idx += 1
    url   = f"http://{user}:{pwd}@{host}:{port}"
    label = f"{host}:{port}"
    return url, label


def _fail_proxy(label: str):
    for i, (host, port, *_) in enumerate(PROXIES):
        if f"{host}:{port}" == label:
            _failed_set.add(i)
            if DEBUG:
                print(f"[proxy] marked failed: {label}", file=sys.stderr)


# ── regex ─────────────────────────────────────────────────────────────────────
STREAM_URL_RE   = re.compile(r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:m3u8|mpd|mp4|m3u)(?:\?[^\s"\'<>\\]*)?', re.I)
PLAYER_FILE_RE  = re.compile(r"""(?:file|src|url)\s*[:=]\s*(['"])(?P<url>https?://.*?\.(?:m3u8|mpd|mp4)(?:\?.*?)?)\1""", re.I | re.S)
PLAYERJS_RE     = re.compile(r"""Playerjs\s*\(\s*\{[^}]*file\s*:\s*['"]([^'"]+)['"]""", re.I | re.S)
BASE64_RE       = re.compile(r'(?:atob|btoa)\s*\(\s*["\']([^"\']+)["\']\s*\)', re.I)
VIPSTREAM_RE    = re.compile(r'(?:window\.location\.href|src)\s*=\s*["\']([^"\']*vipstream[^"\']*)["\']', re.I)
PLAY_TOKEN_RE   = re.compile(r"""[?&]play=([^&"'<>]+)""", re.I)
LOAD_SRC_RE     = re.compile(r"""load_sources\((['"])(?P<t>[^'"]+)\1\)""")
IFRAME_RE       = re.compile(r"""<iframe\b[^>]*\bsrc=(['"])(?P<src>.*?)\1""", re.I | re.S)
SOURCE_LI_RE    = re.compile(r"""<li\b(?P<attrs>[^>]*\bdata-id=[^>]*)>""",    re.I | re.S)
ATTR_RE         = re.compile(r"""([:\w-]+)\s*=\s*(['"])(.*?)\2""", re.S)
JS_STREAM_RE    = re.compile(r'(?:source|src|file|url)\s*[:=]\s*["\']([^"\']*\.(?:m3u8|mpd|mp4)[^"\']*)["\']', re.I)
VOE_RE          = re.compile(r"'hls':\s*'([^']+)'", re.I)
STAPE_RE        = re.compile(r"get_video\?id=[^'\"]+", re.I)
DOOD_RE         = re.compile(r"'(/pass_md5/[^']+)'", re.I)
JSON_SRC_RE     = re.compile(r'"(?:file|src|url)"\s*:\s*"([^"]+\.(?:m3u8|mpd|mp4)[^"]*)"', re.I)
EVAL_RE         = re.compile(r'eval\s*\(\s*function\s*\(p,a,c,k,e', re.I)
JS_WIN_LOC_RE   = re.compile(r"""window\.location\s*=\s*['"]([^'"]+)['"]""", re.I)
JS_SRC_SET_RE   = re.compile(r"""\.src\s*=\s*['"]([^'"]+)['"]""", re.I)
JS_URL_PATTERNS = [
    re.compile(r"""['"](https?://[^'"]*(?:dood|dsvplay|vipstream|streamwish|mixdrop|playmogo|streamtape|voe\.sx|filemoon|upstream)[^'"]*)['"]""", re.I),
]
ANY_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)


# ── dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class SourceChoice:
    video_id:  str
    server_id: str


@dataclass
class ResolveResult:
    input_url:   str
    ok:          bool
    status:      str
    stream_urls: List[str] = field(default_factory=list)
    iframe_urls: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_url":   self.input_url,
            "ok":          self.ok,
            "status":      self.status,
            "stream_urls": self.stream_urls,
            "iframe_urls": self.iframe_urls,
        }


# ── utilities ─────────────────────────────────────────────────────────────────
def uniq(items) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


def hdrs(referer: str = None, origin: str = None, ajax: bool = False) -> Dict[str, str]:
    if referer and not origin:
        p = urllib.parse.urlparse(referer)
        origin = f"{p.scheme}://{p.netloc}"
    h = {
        "User-Agent":      DEFAULT_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control":   "no-cache",
        "Pragma":          "no-cache",
    }
    if ajax:
        h.update({"Accept": "application/json, text/javascript, */*; q=0.01",
                   "X-Requested-With": "XMLHttpRequest",
                   "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                   "Sec-Fetch-Site": "same-origin"})
    else:
        h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                   "Sec-Fetch-Site": "cross-site" if origin else "none",
                   "Upgrade-Insecure-Requests": "1"})
    if referer: h["Referer"] = referer
    if origin:  h["Origin"]  = origin
    return h


# ── Session: one persistent curl_cffi session per resolve() call ──────────────
class ProxySession:
    """
    Wraps a curl_cffi Session that keeps cookies across requests,
    all routed through the SAME proxy for the lifetime of one resolve() call.
    Falls back to next proxy on connection error.
    """
    def __init__(self):
        self._session   = None
        self._proxy_url = None
        self._label     = None
        self._imp       = "chrome131"
        self._pick_proxy()

    def _pick_proxy(self):
        self._proxy_url, self._label = _next_proxy()
        if HAS_CURL_CFFI:
            self._session = curl_requests.Session()
        if DEBUG:
            print(f"[proxy] using {self._label}", file=sys.stderr)

    def get(self, url: str, *, referer=None, origin=None, allow_redirects=True,
            ajax=False, timeout=DEFAULT_TIMEOUT, retries=3) -> Tuple[int, str, Dict, str]:
        if not url or not url.startswith("http"):
            return 0, url, {}, ""
        if DEBUG:
            print(f"[GET] {url[:100]}", file=sys.stderr)

        h = hdrs(referer, origin, ajax=ajax)
        for attempt in range(retries):
            try:
                if HAS_CURL_CFFI and self._session:
                    resp = self._session.get(
                        url, headers=h, impersonate=self._imp,
                        proxies={"http": self._proxy_url, "https": self._proxy_url},
                        timeout=timeout, allow_redirects=allow_redirects,
                    )
                    print(f"[proxy {self._label}] GET {resp.status_code} {url[:80]}", file=sys.stderr)
                    return resp.status_code, resp.url, dict(resp.headers), resp.text
                else:
                    raise RuntimeError("curl_cffi not available")
            except Exception as e:
                if DEBUG:
                    print(f"[proxy fail] {self._label} – {e}", file=sys.stderr)
                _fail_proxy(self._label)
                self._pick_proxy()
        return 0, url, {}, ""

    def post(self, url: str, data: Dict, *, referer=None, origin=None,
             timeout=DEFAULT_TIMEOUT, retries=3) -> Tuple[int, str, Dict, str]:
        if DEBUG:
            print(f"[POST] {url[:100]}", file=sys.stderr)
        h = hdrs(referer, origin, ajax=True)
        for attempt in range(retries):
            try:
                if HAS_CURL_CFFI and self._session:
                    resp = self._session.post(
                        url, data=data, headers=h, impersonate=self._imp,
                        proxies={"http": self._proxy_url, "https": self._proxy_url},
                        timeout=timeout,
                    )
                    print(f"[proxy {self._label}] POST {resp.status_code} {url[:80]}", file=sys.stderr)
                    return resp.status_code, resp.url, dict(resp.headers), resp.text
                else:
                    raise RuntimeError("curl_cffi not available")
            except Exception as e:
                if DEBUG:
                    print(f"[proxy fail] {self._label} – {e}", file=sys.stderr)
                _fail_proxy(self._label)
                self._pick_proxy()
        return 0, url, {}, ""


# ── extraction helpers ────────────────────────────────────────────────────────
def _b64_streams(text: str) -> List[str]:
    out = []
    for m in BASE64_RE.findall(text):
        try:
            out += _stream_urls(base64.b64decode(m + "==").decode("utf-8", errors="replace"))
        except Exception:
            pass
    return out


def _unpack(js: str) -> str:
    if not EVAL_RE.search(js):
        return js
    try:
        m = re.search(
            r"eval\(function\(p,a,c,k,e,(?:d|r)\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
            js, re.S)
        if not m:
            return js
        payload, radix_s, _, wl = m.groups()
        words = wl.split("|"); radix = int(radix_s)
        def dw(w):
            try:
                n = int(w, radix); return words[n] if 0 <= n < len(words) and words[n] else w
            except Exception:
                return w
        return re.sub(r'\b(\w+)\b', lambda x: dw(x.group(1)), payload)
    except Exception:
        return js


def _stream_urls(text: str) -> List[str]:
    text2 = _unpack(text)
    urls = []
    for t in (text, text2):
        urls += [html.unescape(m.group("url")) for m in PLAYER_FILE_RE.finditer(t)]
        urls += [html.unescape(m.group(0))     for m in STREAM_URL_RE.finditer(t)]
        urls += [m.group(1) for m in PLAYERJS_RE.finditer(t)]
        urls += [m.group(1) for m in JS_STREAM_RE.finditer(t)]
        urls += [m.group(1) for m in VOE_RE.finditer(t)]
        urls += [m.group(0) for m in STAPE_RE.finditer(t)]
        urls += [m.group(1) for m in DOOD_RE.finditer(t)]
        urls += [m.group(1) for m in JSON_SRC_RE.finditer(t)]
        urls += _b64_streams(t)
        urls += [m.group(1) for m in VIPSTREAM_RE.finditer(t)]
    clean = []
    for u in urls:
        u = html.unescape(u).strip()
        if u.startswith("//"): u = "https:" + u
        if u.startswith("http"): clean.append(u)
    return uniq(clean)


def _js_urls(text: str) -> List[str]:
    urls = []
    for m in JS_WIN_LOC_RE.finditer(text):
        u = m.group(1)
        if u.startswith("http"): urls.append(u)
    for m in JS_SRC_SET_RE.finditer(text):
        u = m.group(1)
        if u.startswith("//"): u = "https:" + u
        if u.startswith("http"): urls.append(u)
    for pat in JS_URL_PATTERNS:
        for m in pat.finditer(text):
            urls.append(m.group(1))
    return uniq(u for u in urls if u and u != "https://")


def _iframe_urls(text: str, base: str) -> List[str]:
    urls = []
    for m in IFRAME_RE.finditer(text):
        src = html.unescape(m.group("src")).strip()
        if src: urls.append(urllib.parse.urljoin(base, src))
    for u in _js_urls(text):
        urls.append(u if u.startswith("http") else urllib.parse.urljoin(base, u))
    return uniq(u for u in urls if u and u != "https://")


def _sources(html_body: str) -> List[SourceChoice]:
    out = []
    for m in SOURCE_LI_RE.finditer(html_body):
        a = {n.lower(): html.unescape(v) for n, _, v in ATTR_RE.findall(m.group("attrs"))}
        vid, srv = a.get("data-id"), a.get("data-server")
        if vid and srv:
            out.append(SourceChoice(video_id=vid, server_id=srv))
    return out


def _play_token(text: str) -> Optional[str]:
    m = PLAY_TOKEN_RE.search(text)
    if m: return urllib.parse.unquote(m.group(1))
    m = LOAD_SRC_RE.search(text)
    if m: return m.group("t")
    return None


def _dood_url(body: str, base: str = "") -> Optional[str]:
    dom = urllib.parse.urlparse(base).netloc or "dsvplay.com"
    for pat in [r'https?://[^"\']+/pass_md5/[^"\']+',
                r'https?://[^"\']+\.m3u8[^"\']*',
                r"/pass_md5/[^'\"]+"]:
        for hit in re.findall(pat, body, re.I):
            if hit.startswith("http"): return hit
            if hit.startswith("/"): return f"https://{dom}{hit}"
    return None


# ── recursive embed processor ─────────────────────────────────────────────────
def _process_embed(url: str, server_id: str, referer: str,
                   result: ResolveResult, sess: ProxySession, depth: int = 0):
    if depth > MAX_IFRAME_DEPTH or not url or not url.startswith("http"):
        return
    if any(x in url for x in [".m3u8", ".mp4", "/pass_md5/"]):
        if url not in result.stream_urls:
            result.stream_urls.append(url)
        return

    p = urllib.parse.urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    status, final, _, body = sess.get(url, referer=referer, origin=origin)
    if not body:
        return

    if any(x in url.lower() for x in ["dood", "dsvplay"]):
        du = _dood_url(body, final)
        if du and du not in result.stream_urls:
            result.stream_urls.append(du)

    for s in _stream_urls(body):
        if s not in result.stream_urls:
            result.stream_urls.append(s)

    nested = _iframe_urls(body, final) + _js_urls(body)
    for nurl in uniq(nested):
        if nurl and nurl != "https://":
            if nurl not in result.iframe_urls:
                result.iframe_urls.append(nurl)
            _process_embed(nurl, server_id, url, result, sess, depth + 1)


# ── main resolver ─────────────────────────────────────────────────────────────
def resolve(input_url: str, all_servers: bool = True) -> ResolveResult:
    result = ResolveResult(input_url=input_url, ok=False, status="resolving")
    sess   = ProxySession()   # one session = one proxy = shared cookies

    try:
        # ── 1. Initial GET (no redirect) ──────────────────────────────────
        status, final_url, resp_hdrs, body = sess.get(input_url, allow_redirects=False)
        loc = resp_hdrs.get("Location") or resp_hdrs.get("location") or ""
        play_url   = urllib.parse.urljoin(input_url, loc) if loc else (final_url or input_url)
        play_token = _play_token(play_url) or _play_token(body)
        print(f"[step1] HTTP {status}  play_url={play_url[:80]}", file=sys.stderr)

        # ── 2. Play page ──────────────────────────────────────────────────
        p_play  = urllib.parse.urlparse(play_url)
        p_input = urllib.parse.urlparse(input_url)
        play_origin  = f"{p_play.scheme}://{p_play.netloc}"
        input_origin = f"{p_input.scheme}://{p_input.netloc}"

        status, final_url, _, page = sess.get(
            play_url, referer=input_url, origin=input_origin)
        print(f"[step2] HTTP {status}  {len(page)} bytes", file=sys.stderr)

        play_token = play_token or _play_token(page)
        result.stream_urls += _stream_urls(page)
        result.iframe_urls += _iframe_urls(page, final_url)

        if not play_token:
            print("[warn] no play token found – cannot call response.php", file=sys.stderr)
            result.status = "no_token"
            result.stream_urls = uniq(result.stream_urls)
            result.iframe_urls = uniq(result.iframe_urls)
            result.ok = bool(result.stream_urls)
            return result

        # ── 3. response.php (POST with same session = cookies carried) ────
        response_url = urllib.parse.urljoin(play_url, "/response.php")
        status, _, _, resp_html = sess.post(
            response_url, {"token": play_token},
            referer=play_url, origin=play_origin)
        print(f"[step3] response.php HTTP {status}  {len(resp_html)} bytes", file=sys.stderr)

        if status == 403:
            # Site checks that the POST comes from a browser that loaded the page.
            # Retry once with an explicit Cookie echo if the session captured any.
            print("[warn] 403 on response.php – retrying with explicit headers", file=sys.stderr)
            status, _, _, resp_html = sess.post(
                response_url, {"token": play_token},
                referer=final_url, origin=play_origin)
            print(f"[step3-retry] response.php HTTP {status}", file=sys.stderr)

        sources = _sources(resp_html)
        print(f"[step3] found {len(sources)} sources", file=sys.stderr)

        sources_to_try = sources if all_servers else sources[:1]
        if not sources_to_try:
            sources_to_try = sources

        # ── 4. playvideo.php per source ───────────────────────────────────
        for src in sources_to_try:
            pv_url = urllib.parse.urljoin(
                play_url,
                f"/playvideo.php?video_id={urllib.parse.quote(src.video_id)}"
                f"&server_id={urllib.parse.quote(src.server_id)}"
                f"&token={urllib.parse.quote(play_token)}&init=1",
            )
            for attempt in range(2):
                pv_status, _, _, pv_html = sess.get(
                    pv_url, referer=play_url, origin=play_origin)
                if pv_status == 403 and attempt == 0:
                    time.sleep(1); continue
                if pv_status not in (200, 206):
                    print(f"[step4] server {src.server_id} HTTP {pv_status} – skip", file=sys.stderr)
                    break
                print(f"[step4] server {src.server_id} HTTP {pv_status}  {len(pv_html)} bytes", file=sys.stderr)

                for s in _stream_urls(pv_html):
                    if s not in result.stream_urls:
                        result.stream_urls.append(s)

                embeds = uniq(_iframe_urls(pv_html, pv_url) + _js_urls(pv_html))
                for eu in embeds:
                    if eu and eu != "https://":
                        if eu not in result.iframe_urls:
                            result.iframe_urls.append(eu)
                        _process_embed(eu, src.server_id, pv_url, result, sess, depth=0)
                break

        result.stream_urls = uniq(u for u in result.stream_urls if u and u != "https://")
        result.iframe_urls = uniq(u for u in result.iframe_urls if u and u != "https://")
        result.ok     = bool(result.stream_urls)
        result.status = "ok" if result.ok else "no_streams"
        return result

    except Exception as exc:
        result.status = "error"
        print(f"[error] {exc}", file=sys.stderr)
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        return result


# ── HTTP API server ───────────────────────────────────────────────────────────
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "StreamResolver/6.0"

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._json({"ok": True, "curl_cffi": HAS_CURL_CFFI, "proxies": len(PROXIES)})
                return
            if parsed.path == "/resolve":
                url = (params.get("url") or [""])[0]
                if not url:
                    self._json({"ok": False, "error": "Missing url"}, 400); return
                all_s = (params.get("all") or ["1"])[0] not in ("0", "false")
                self._json(resolve(url, all_servers=all_s).to_dict())
                return
            self._json({"ok": False, "error": "Not found",
                        "endpoints": ["/health", "/resolve?url=<url>"]}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, payload, status=200):
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def serve(host: str, port: int):
    srv = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Listening on http://{host}:{port}")
    print(f"curl_cffi={HAS_CURL_CFFI}  proxies={len(PROXIES)}")
    srv.serve_forever()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url",     nargs="?", default=DEFAULT_INPUT_URL)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host",  default="127.0.0.1")
    ap.add_argument("--port",  type=int, default=int(os.environ.get("PORT", "8787")))
    ap.add_argument("--single",action="store_true", help="Only first server")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    global DEBUG
    DEBUG = args.debug

    if args.serve:
        serve(args.host, args.port)
        return 0

    r = resolve(args.url, all_servers=not args.single)
    print(json.dumps(r.to_dict(), indent=2, ensure_ascii=False))
    return 0 if r.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
