"""Content ingestion — acquisition, normalization, type detection, provenance.

Pipeline stage 1-3 of the verification flow (spec #3):

    USER INPUT -> CONTENT INGESTION -> CONTENT NORMALIZATION -> CONTENT TYPE DETECTION

Capabilities (spec #5, #13, #19, #21):
  * URL fetching with SSRF protection (private networks blocked), redirect
    chain recording, size/time limits. Public content only — authentication,
    paywalls, robots-blocked resources and CAPTCHAs are respected and
    reported as such, never bypassed.
  * Rich article extraction: title, author, publisher, dates, main text,
    OpenGraph metadata, canonical URL.
  * File parsing: PDF (pypdf + PyMuPDF), DOCX (python-docx), TXT, CSV, HTML,
    JSON, and images (delegated to the media forensics engine for OCR).
  * Every result carries a source classification (spec #29).
"""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.verify.ingestion")

# --------------------------------------------------------------------------- data types
SUPPORTED_INPUT_KINDS = {"url", "text", "image", "pdf", "docx", "screenshot", "csv", "html", "json"}


@dataclass
class IngestedContent:
    """Normalized representation of anything the user submitted."""
    input_kind: str                       # url|text|image|pdf|docx|screenshot|csv|html|json
    content_type: str                     # article|social_post|image|pdf|docx|text|csv|html|json|screenshot
    title: Optional[str] = None
    author: Optional[str] = None
    publisher: Optional[str] = None
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    original_url: Optional[str] = None
    canonical_url: Optional[str] = None
    redirect_chain: list[str] = field(default_factory=list)
    og_metadata: dict[str, str] = field(default_factory=dict)
    fetch_status: str = "ok"              # ok|robots_blocked|auth_required|paywall|error|skipped
    http_status: Optional[int] = None
    raw_text: str = ""
    og_image: Optional[str] = None
    text_stats: dict[str, Any] = field(default_factory=dict)
    file_meta: dict[str, Any] = field(default_factory=dict)
    source_classification: str = "LIVE"
    links: list[str] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "input_kind": self.input_kind,
            "content_type": self.content_type,
            "title": self.title,
            "author": self.author,
            "publisher": self.publisher,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "original_url": self.original_url,
            "canonical_url": self.canonical_url,
            "redirect_chain": self.redirect_chain,
            "fetch_status": self.fetch_status,
            "http_status": self.http_status,
            "text_stats": self.text_stats,
            "file_meta": self.file_meta,
            "source_classification": self.source_classification,
            "chars": len(self.raw_text or ""),
        }


class IngestionError(Exception):
    """Raised when content cannot be acquired at all."""


# ----------------------------------------------------------------------- SSRF guard
def _validate_resolved_ip(host: str) -> bool:
    """Resolve `host` and require EVERY address to be globally routable.

    Closes the classic SSRF allowlist gaps (0.0.0.0, 100.64/10 CGNAT, IPv6
    ULA/link-local/mapped, multicast, reserved) by delegating to the
    `ipaddress` module instead of string prefixes.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        return False
    # Literal IPs must also pass; getaddrinfo handles both names and literals.
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        # ip.v4 maps IPv6-mapped addresses back to v4 so ::ffff:10.0.0.1 is caught.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if not addr.is_global:
            return False
        if addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local:
            return False
        if addr.is_private or addr.is_unspecified:
            return False
    return True


def _is_public_host(host: str) -> bool:
    """Backwards-compatible wrapper around the ipaddress-based validator."""
    return _validate_resolved_ip(host)


def validate_url(raw: str) -> str:
    """Validate and normalise a user-supplied URL. Raises IngestionError."""
    url = (raw or "").strip()
    if not url:
        raise IngestionError("Empty URL")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IngestionError(f"Invalid URL: {raw!r}")
    if parsed.username or parsed.password:
        raise IngestionError("Refused: URLs with embedded credentials are not fetchable")
    if not _is_public_host(parsed.hostname):
        raise IngestionError("Refused: private / local network addresses are not fetchable")
    return url


# ------------------------------------------------------------------ fetch machinery
_FETCH_HEADERS = {
    "User-Agent": "PRVisionEarlyWarning/1.0 (+verification research; respectful fetching)",
    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SOCIAL_HOSTS = (
    "twitter.com", "x.com", "facebook.com", "fb.com", "instagram.com",
    "reddit.com", "linkedin.com", "youtube.com", "youtu.be", "tiktok.com",
)


def detect_input_kind(raw: str, filename: Optional[str] = None) -> str:
    """Detect whether raw user input is a URL, text, or a file kind."""
    s = (raw or "").strip()
    if s.startswith(("http://", "https://", "www.")) and " " not in s.split("\n")[0]:
        first = s.split("\n")[0].strip()
        host = urlparse(first if first.startswith("http") else "https://" + first).hostname or ""
        for social in _SOCIAL_HOSTS:
            if social in host.lower():
                return "url"  # social URL — still a URL; content_type refined later
        return "url"
    if filename:
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "heic"}:
            return "screenshot" if "screen" in (filename or "").lower() else "image"
        if ext == "pdf":
            return "pdf"
        if ext in {"docx", "doc"}:
            return "docx"
        if ext == "csv" or ext == "tsv":
            return "csv"
        if ext in {"html", "htm"}:
            return "html"
        if ext == "json":
            return "json"
        if ext in {"txt", "md", "text"}:
            return "text"
    # long multi-line content or short pasted text
    return "text"


# ------------------------------------------------------------------ robots.txt (honored)
_UA_TOKEN = "PRVisionEarlyWarning"
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}
_ROBOTS_TTL_SECONDS = 600.0


def _robots_allows(client: httpx.Client, url: str) -> tuple[bool, str | None]:
    """True unless robots.txt disallows this UA for the URL.

    Robots errors (unreachable robots.txt) default to allow — the target page
    itself is still subject to auth/paywall/451 handling below.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    now = datetime.now(timezone.utc).timestamp()
    cached = _robots_cache.get(parsed.netloc)
    rp = None
    if cached and now - cached[0] < _ROBOTS_TTL_SECONDS:
        rp = cached[1]
    else:
        try:
            resp = client.get(robots_url)
            if resp.status_code >= 400:
                rp = None  # no robots.txt -> allow
            else:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
        except httpx.HTTPError:
            rp = None
        _robots_cache[parsed.netloc] = (now, rp)
    if rp is None:
        return True, None
    try:
        allowed = rp.can_fetch(_UA_TOKEN, url)
    except Exception:
        allowed = True
    return allowed, None if allowed else "robots.txt disallows this path for our user agent"


def fetch_url(url: str) -> IngestedContent:
    """Fetch a public URL, record the redirect chain, extract article data.

    Security posture (spec #17):
      * every hop (initial URL + each redirect) is re-validated against the
        SSRF guard BEFORE the request is made (no blind auto-redirects);
      * robots.txt is honored — disallowed paths return fetch_status
        "robots_blocked" and are never fetched;
      * authentication walls, paywalls and 451 withdrawals are respected.
    """
    target = validate_url(url)
    content = IngestedContent(input_kind="url", content_type="article", original_url=target)

    try:
        transport = httpx.HTTPTransport(retries=1)
        with httpx.Client(
            transport=transport,
            headers=_FETCH_HEADERS,
            timeout=settings.VERIFY_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,  # redirects are followed manually, re-validated
            max_redirects=6,
        ) as client:
            allowed, reason = _robots_allows(client, target)
            if not allowed:
                content.fetch_status = "robots_blocked"
                content.raw_text = ""
                content.og_metadata["robots"] = reason or "robots.txt disallow"
                logger.info("Fetch skipped (robots): %s", target)
                return content

            current = target
            chain: list[str] = []
            response = None
            for _hop in range(6):
                # Re-validate EVERY hop (SSRF: redirects must not cross into private nets)
                hop_url = validate_url(current)
                response = client.get(hop_url)
                chain.append(str(response.url))
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        break
                    current = urljoin(str(response.url), location)
                    continue
                break
            if response is None:
                content.fetch_status = "error"
                content.og_metadata["error"] = "no_response"
                return content

            content.redirect_chain = chain
            content.http_status = response.status_code
            content.canonical_url = str(response.url)

            ctype = response.headers.get("content-type", "")
            if response.status_code in (401, 403):
                content.fetch_status = "auth_required"
                return content
            if response.status_code == 451:
                content.fetch_status = "error"
                content.raw_text = "Resource withheld (HTTP 451)."
                return content
            if response.status_code >= 400:
                content.fetch_status = "error"
                content.raw_text = ""
                content.og_metadata["error"] = f"http_{response.status_code}"
                return content

            body = response.content[: settings.VERIFY_FETCH_MAX_BYTES]
            if "pdf" in ctype or target.lower().endswith(".pdf"):
                content.input_kind = "pdf"
                content.content_type = "pdf"
                content.fetch_status = "ok"
                content.file_meta = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "mime": "application/pdf"}
                content.raw_text = ""  # parsed by media engine
                content.file_meta["_bytes_b64_hint"] = False
                # keep bytes available for downstream parser via closure attr
                content.og_metadata["_fetched_pdf_bytes"] = ""
                _stash_bytes(content, body)
                return content
            if "image" in ctype:
                content.input_kind = "image"
                content.content_type = "image"
                content.file_meta = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "mime": ctype}
                _stash_bytes(content, body)
                return content

            html = body.decode(response.encoding or "utf-8", errors="replace")
            _extract_article(content, html, target)
            return content
    except IngestionError as exc:
        # A redirect hop pointed at a private/internal address.
        content.fetch_status = "error"
        content.og_metadata["error"] = f"redirect_blocked: {exc}"
        logger.warning("Redirect chain blocked for %s: %s", target, exc)
        return content
    except httpx.TimeoutException:
        content.fetch_status = "error"
        content.raw_text = ""
        content.og_metadata["error"] = "fetch_timeout"
        return content
    except httpx.HTTPError as exc:
        content.fetch_status = "error"
        content.og_metadata["error"] = f"fetch_failed: {exc.__class__.__name__}"
        logger.warning("URL fetch failed for %s: %s", target, exc)
        return content


_BYTES_SLOT: dict[int, bytes] = {}


def _stash_bytes(content: IngestedContent, data: bytes) -> None:
    """Attach fetched bytes for the media parsers (kept out of the dataclass for clarity)."""
    _BYTES_SLOT[id(content)] = data


def pop_stashed_bytes(content: IngestedContent) -> Optional[bytes]:
    return _BYTES_SLOT.pop(id(content), None)


def parse_date_guess(value: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-ish date parsing from article metadata."""
    if not value:
        return None
    v = (value or "").strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%Y/%m/%d", "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _extract_article(content: IngestedContent, html: str, url: str) -> None:
    """Extract title/author/dates/text/OG/canonical/links from article HTML."""
    soup = BeautifulSoup(html, "lxml")

    # OpenGraph / meta
    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name")
        val = meta.get("content")
        if prop and val:
            og[prop] = val.strip()
    content.og_metadata = {k: v for k, v in og.items() if k.startswith(("og:", "article:", "twitter:"))}
    content.og_image = og.get("og:image")

    content.title = (
        og.get("og:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else None)
        or (soup.find("h1").get_text(strip=True) if soup.find("h1") else None)
    )
    content.author = (
        og.get("article:author")
        or (soup.find("meta", attrs={"name": "author"}).get("content") if soup.find("meta", attrs={"name": "author"}) else None)
        or _guess_author(soup)
    )
    content.publisher = og.get("og:site_name") or urlparse(url).hostname
    content.published_at = parse_date_guess(
        og.get("article:published_time") or og.get("article:published") or og.get("date") or og.get("pubdate")
    )
    content.updated_at = parse_date_guess(og.get("article:modified_time"))

    # canonical
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        content.canonical_url = urljoin(url, canon["href"])

    # main text extraction: prefer <article>, fall back to biggest text container
    container = soup.find("article") or soup.find("main") or soup.body or soup
    for tag in container(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "iframe"]):
        tag.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all(["p", "h2", "h3", "li", "blockquote"])]
    text = "\n\n".join(unescape(p) for p in paragraphs if p and len(p) > 25)
    if len(text) < 200:  # very sparse page — keep visible text instead
        text = unescape(container.get_text(" ", strip=True))
    content.raw_text = text[:200_000]

    content.links = [a["href"] for a in soup.find_all("a", href=True)][:100]
    content.text_stats = _text_stats(content.raw_text)

    host = (urlparse(url).hostname or "").lower()
    if any(s in host for s in _SOCIAL_HOSTS):
        content.content_type = "social_post"
    if og.get("og:type") in ("video", "video.other"):
        content.content_type = content.content_type or "article"


def _guess_author(soup: BeautifulSoup) -> Optional[str]:
    el = soup.find(attrs={"class": lambda c: c and any(s in str(c).lower() for s in ("author", "byline"))})
    if el:
        t = el.get_text(" ", strip=True)
        return t[:200] if t else None
    rel = soup.find(attrs={"rel": "author"})
    if rel:
        t = rel.get_text(" ", strip=True)
        return t[:200] if t else None
    return None


def text_from_plain(raw: str) -> IngestedContent:
    """Normalize pasted text input."""
    text = (raw or "").strip()
    content = IngestedContent(
        input_kind="text", content_type="text", raw_text=text[:200_000],
        fetch_status="skipped", title=text[:120] or "Pasted text",
    )
    content.text_stats = _text_stats(text)
    return content


def parse_pdf_bytes(data: bytes, filename: str = "document.pdf") -> tuple[IngestedContent, dict[str, Any]]:
    """Parse PDF: text + metadata. Forensics run by the media engine."""
    from app.media.pdf_analysis import extract_pdf

    parsed = extract_pdf(data)
    content = IngestedContent(
        input_kind="pdf", content_type="pdf",
        title=parsed.get("title") or filename,
        author=parsed.get("author"),
        published_at=parsed.get("creation_date"),
        updated_at=parsed.get("mod_date"),
        raw_text=parsed.get("text", "")[:200_000],
        fetch_status="skipped",
        file_meta={"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "pages": parsed.get("pages"), "mime": "application/pdf"},
    )
    content.text_stats = _text_stats(content.raw_text)
    return content, parsed


def parse_docx_bytes(data: bytes, filename: str = "document.docx") -> tuple[IngestedContent, dict[str, Any]]:
    from app.media.docx_analysis import extract_docx

    parsed = extract_docx(data)
    content = IngestedContent(
        input_kind="docx", content_type="docx",
        title=parsed.get("title") or filename,
        author=parsed.get("author"),
        raw_text=parsed.get("text", "")[:200_000],
        fetch_status="skipped",
        file_meta={"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": parsed.get("mime", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    content.text_stats = _text_stats(content.raw_text)
    return content, parsed


# ---------------------------------------------------------------- legacy .doc
def _strip_rtf(data: bytes) -> str:
    """Deterministic RTF control-word strip (best-effort plain text)."""
    raw = data.decode("latin-1", errors="replace")
    out: list[str] = []
    i, n = 0, len(raw)
    depth_skip = 0
    while i < n:
        ch = raw[i]
        if ch == "{":
            # \* destinations (unsupported) are skipped entirely
            if raw[i:i+2] == "{\\*":
                depth_skip += 1
                i += 1
                continue
            i += 1
            continue
        if ch == "}":
            if depth_skip:
                depth_skip -= 1
            i += 1
            continue
        if depth_skip:
            i += 1
            continue
        if ch == "\\":
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", raw[i:])
            if m:
                word, param = m.group(1), m.group(2)
                if word == "par" or word == "line":
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word in ("u",) and param:
                    try:
                        out.append(chr(int(param) % 65536))
                    except ValueError:
                        pass
                # skip \'hh hex escapes
                if raw[i + len(m.group(0)):i + len(m.group(0)) + 1] == "'" and word not in ("'",):
                    pass
                i += len(m.group(0))
                continue
            m = re.match(r"\\'([0-9a-fA-F]{2})", raw[i:])
            if m:
                out.append(chr(int(m.group(1), 16)))
                i += 3
                continue
            if i + 1 < n and raw[i + 1] == "\\":
                out.append("\\")
                i += 2
                continue
            i += 2  # escaped symbol (\'{ \'}) — drop the backslash, keep char
            continue
        if ch in "\r\n":
            out.append("\n")
            i += 1
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _scrape_ole2_text(data: bytes) -> str:
    """Best-effort text scan of a legacy Word OLE2 (.doc) container.

    Legacy .doc stores body text as cp1252 or UTF-16LE runs inside binary
    streams. Without a full piece-table parser the extraction is APPROXIMATE —
    callers must label it as such (this is why it is never presented as the
    authoritative document text).
    """
    chunks: list[str] = []
    # UTF-16LE runs: printable char followed by \x00, sustained
    i, n = 0, len(data) - 1
    run: list[str] = []
    while i < n:
        lo, hi = data[i], data[i + 1]
        if hi == 0 and (32 <= lo < 127 or lo in (9, 10, 13) or 0xA0 <= lo <= 0xFF):
            run.append(chr(lo))
            i += 2
            continue
        if len(run) >= 8:
            chunks.append("".join(run))
        run = []
        i += 1
    if len(run) >= 8:
        chunks.append("".join(run))
    # cp1252 runs (legacy single-byte text)
    run2: list[str] = []
    for b in data:
        if 32 <= b < 127 or b in (9, 10, 13) or 0xA0 <= b <= 0xFF:
            run2.append(chr(b))
        else:
            if len(run2) >= 24:
                chunks.append("".join(run2))
            run2 = []
    if len(run2) >= 24:
        chunks.append("".join(run2))
    text = "\n".join(chunks)
    # collapse whitespace noise from binary padding
    text = re.sub(r"[ \t]{3,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:200_000]


def parse_legacy_doc_bytes(data: bytes, filename: str = "document.doc") -> tuple[IngestedContent, dict[str, Any]]:
    """Honest legacy .doc handling: route by ACTUAL bytes, never pretend.

    * OLE2 compound file -> approximate binary scan, clearly labelled
    * RTF in disguise    -> deterministic control-word strip
    * HTML / plain text  -> decoded directly
    """
    text = ""
    method = "unknown"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        method = "ole2_best_effort_scan"
        text = _scrape_ole2_text(data)
    elif data[:5] == b"{\\rtf":
        method = "rtf_strip"
        text = _strip_rtf(data)
    elif b"<html" in data[:2048].lower() or b"<p>" in data[:2048].lower():
        method = "html_mislabeled_as_doc"
        from bs4 import BeautifulSoup as _BS
        html = data.decode("utf-8-sig", errors="replace")
        soup = _BS(html, "lxml")
        for t in soup(["script", "style"]):
            t.decompose()
        text = soup.get_text(" ", strip=True)
    else:
        method = "plain_text_decode"
        text = data.decode("utf-8-sig", errors="replace")

    content = IngestedContent(
        input_kind="docx", content_type="docx", title=filename,
        raw_text=text[:200_000], fetch_status="skipped",
        file_meta={"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "mime": "application/msword",
                   "extraction_method": method,
                   "extraction_note": (
                       "Legacy .doc extracted with a best-effort binary scan — "
                       "layout/formatting is not reconstructed; save as .docx or "
                       "PDF for faithful extraction." if method == "ole2_best_effort_scan"
                       else f"Extracted via {method}.")},
    )
    content.text_stats = _text_stats(content.raw_text)
    parsed = {"text": content.raw_text, "extraction_method": method, "title": filename}
    return content, parsed


def parse_csv_bytes(data: bytes, filename: str = "data.csv") -> tuple[IngestedContent, dict[str, Any]]:
    """Parse CSV and capture structure for table verification (spec #22)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    header = rows[0] if rows else []
    stats = {
        "rows": max(0, len(rows) - 1),
        "columns": len(header),
        "header": header[:64],
        "sample_rows": rows[1:6],
        "numeric_columns": [],
    }
    # detect numeric columns (deterministic)
    for idx, col in enumerate(header):
        vals = [r[idx] for r in rows[1:11] if idx < len(r) and r[idx].strip()]
        ok = 0
        for v in vals:
            try:
                float(v.replace(",", "").replace("%", "").replace(" ", ""))
                ok += 1
            except ValueError:
                pass
        if vals and ok / len(vals) > 0.7:
            stats["numeric_columns"].append(col)
    content = IngestedContent(
        input_kind="csv", content_type="csv", title=filename,
        raw_text=text[:200_000], fetch_status="skipped",
        file_meta={"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": "text/csv"},
    )
    content.text_stats = stats
    return content, stats


def parse_json_bytes(data: bytes, filename: str = "data.json") -> IngestedContent:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        obj = json.loads(text)
        pretty = json.dumps(obj, ensure_ascii=False, indent=2)[:100_000]
        flat = json.dumps(obj, ensure_ascii=False)[:200_000] if not isinstance(obj, str) else obj[:200_000]
    except json.JSONDecodeError:
        pretty, flat = "", text
    content = IngestedContent(
        input_kind="json", content_type="json", title=filename, raw_text=flat or pretty,
        fetch_status="skipped",
        file_meta={"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": "application/json"},
    )
    content.text_stats = _text_stats(content.raw_text)
    return content


def parse_html_bytes(data: bytes, filename: str = "page.html") -> IngestedContent:
    html = data.decode("utf-8-sig", errors="replace")
    content = IngestedContent(input_kind="html", content_type="html", title=filename, fetch_status="skipped")
    _extract_article(content, html, "file://" + filename)
    content.original_url = None
    return content


def _text_stats(text: str) -> dict[str, Any]:
    words = len(text.split())
    sentences = max(1, text.count(".") + text.count("!") + text.count("?"))
    return {
        "words": words,
        "sentences": sentences,
        "chars": len(text),
        "reading_time_min": round(max(1, words / 220), 1),
        "avg_sentence_len": round(words / max(1, sentences), 1),
    }
