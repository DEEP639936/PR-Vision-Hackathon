"""Rate-limit bucket routing + upload magic-byte validation tests."""
from __future__ import annotations

from app.core.config import settings
from app.core.ratelimit import _bucket_for
from app.api.routes.verify import _content_matches_extension


def _limits_enabled(monkeypatch, enabled: bool = True):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", enabled)


def test_verify_status_polls_use_api_bucket():
    """GET /api/verify/{id} and /report must NOT burn the submission budget."""
    name, _ = _bucket_for("/api/verify/12", "GET")
    assert name == "api"
    name, _ = _bucket_for("/api/verify/12/report", "GET")
    assert name == "api"
    name, _ = _bucket_for("/api/verify/jobs", "GET")
    assert name == "api"


def test_verify_submission_uses_verify_bucket():
    name, limit = _bucket_for("/api/verify", "POST")
    assert name == "verify"
    assert limit == settings.RATE_LIMIT_VERIFY_PER_MINUTE


def test_auth_and_export_buckets_unchanged():
    assert _bucket_for("/api/auth/login", "POST")[0] == "auth"
    assert _bucket_for("/api/export/report/1", "GET")[0] == "export"
    assert _bucket_for("/api/posts", "GET")[0] == "api"


# --------------------------------------------------------------- magic bytes
def test_magic_doc_accepts_ole2():
    assert _content_matches_extension("doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)


def test_magic_doc_accepts_zip_container():
    assert _content_matches_extension("doc", b"PK\x03\x04" + b"\x00" * 64)


def test_magic_doc_rejects_rtf_named_doc():
    assert not _content_matches_extension("doc", b"{\\rtf1ansi")


def test_magic_gif_accepts_both_variants():
    assert _content_matches_extension("gif", b"GIF89a" + b"\x00" * 32)
    assert _content_matches_extension("gif", b"GIF87a" + b"\x00" * 32)


def test_magic_webp_compound_signature():
    riff = b"RIFF" + b"\x00" * 4 + b"WEBP"
    assert _content_matches_extension("webp", riff)
    assert not _content_matches_extension("webp", b"RIFF" + b"\x00" * 4 + b"JUNK")


def test_magic_png_rejects_text_file():
    assert not _content_matches_extension("png", b"just some plain text bytes" * 4)


def test_text_extension_rejects_binary():
    assert not _content_matches_extension("csv", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + b"\x00" * 32)
    assert _content_matches_extension("csv", b"a,b,c\n1,2,3\n4,5,6\n")
