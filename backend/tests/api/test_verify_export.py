"""Verify-input hardening + export endpoint tests (spec #8, #17, #19)."""
from __future__ import annotations


def test_upload_rejects_content_type_mismatch(app):
    # claims to be a PNG but is plain text — must be refused (magic-byte check)
    r = app.post("/api/verify",
                 files={"file": ("screenshot.png", b"definitely not a png", "image/png")})
    assert r.status_code == 415
    assert "magic-byte" in r.json()["detail"]


def test_upload_rejects_disallowed_extension(app):
    r = app.post("/api/verify",
                 files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")})
    assert r.status_code == 415


def test_upload_accepts_genuine_png_and_persists(app):
    from app.core.config import settings
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 96)  # valid signature, 1x1-ish stub
    r = app.post("/api/verify",
                 files={"file": ("tiny.png", png, "image/png")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    directory = settings.upload_dir
    assert (directory / f"job{job_id}_tiny.png").exists()


def test_private_url_rejected(app):
    r = app.post("/api/verify", data={"url": "http://127.0.0.1:8000/admin"})
    assert r.status_code == 400
    assert "private" in r.json()["detail"].lower()


def test_exports_of_completed_job(app, db_session):
    """Minimal completed job exports cleanly in all three formats."""
    from app.db.models import VerificationJob

    job = VerificationJob(
        status="completed", input_kind="text", input_label="export smoke",
        progress=100, stage="done",
        result_summary='{"overall": {"verdict": "UNVERIFIED", "detail": "", "caveats": []},'
                       '"priority": {"intervention_priority": 12.0, "label": "LOW", "factors": ["calm"]},'
                       '"risk": {"misinformation_risk": 0.1}, "providers": {}}')
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # report itself renders with empty artifacts
    r = app.get(f"/api/verify/{job.id}/report")
    assert r.status_code == 200 and r.json()["report_ready"] is True

    r = app.get(f"/api/verify/{job.id}/export.json")
    assert r.status_code == 200
    payload = r.json()
    assert "limitations" in payload["export"] and len(payload["export"]["limitations"]) >= 5

    r = app.get(f"/api/verify/{job.id}/export.csv")
    assert r.status_code == 200
    assert "claim_ordinal" in r.text.splitlines()[0]

    r = app.get(f"/api/verify/{job.id}/export.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"
    assert "attachment" in r.headers.get("content-disposition", "")


def test_export_unavailable_for_running_job(app, db_session):
    from app.db.models import VerificationJob

    job = VerificationJob(status="running", input_kind="text", input_label="busy",
                          progress=40, stage="working")
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    r = app.get(f"/api/verify/{job.id}/export.json")
    assert r.status_code == 409


def test_pages_served(app):
    for path, marker in (
        ("/", "PR"), ("/dashboard", "PR"), ("/verify", "PR"),
        ("/login", "PR"), ("/register", "PR"), ("/cases", "PR"),
    ):
        r = app.get(path)
        assert r.status_code == 200, path
        assert "PR" in r.text


def test_security_headers_present(app):
    r = app.get("/api/health")
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
