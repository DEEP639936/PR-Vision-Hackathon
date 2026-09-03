"""Auth + cases + alerts API tests (spec #3, #14, #13, #17)."""
from __future__ import annotations

import uuid

import pytest


def _register_or_login(app, email, password="Sup3rSecret!"):
    """Idempotent helper: the test DB persists across runs."""
    r = app.post("/api/auth/register", json={
        "email": email, "display_name": "Test Analyst", "password": password})
    if r.status_code == 409:
        r = app.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def user_headers(app):
    email = f"analyst-{uuid.uuid4().hex[:8]}@prvision.io"
    return _register_or_login(app, email)


def test_register_login_me_logout_flow(app):
    email = f"flow-{uuid.uuid4().hex[:8]}@prvision.io"
    r = app.post("/api/auth/register", json={
        "email": email, "display_name": "Flow", "password": "Sup3rSecret!"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == email
    assert body["user"]["role"] in ("admin", "analyst")

    r = app.post("/api/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = app.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["user"]["display_name"] == "Flow"

    r = app.post("/api/auth/logout", headers=headers)
    assert r.status_code == 200 and r.json()["logged_out"] is True
    # token must be dead after logout
    r = app.get("/api/auth/me", headers=headers)
    assert r.status_code == 401


def test_register_rejects_duplicate_and_weak_password(app):
    email = f"dup-{uuid.uuid4().hex[:8]}@prvision.io"
    payload = {"email": email, "display_name": "D", "password": "Sup3rSecret!"}
    assert app.post("/api/auth/register", json=payload).status_code == 200
    r = app.post("/api/auth/register", json=payload)
    assert r.status_code == 409
    r = app.post("/api/auth/register", json={
        "email": f"weak-{uuid.uuid4().hex[:8]}@prvision.io", "display_name": "W",
        "password": "short"})
    assert r.status_code == 422


def test_login_invalid_credentials_is_401(app):
    r = app.post("/api/auth/login", json={"email": "ghost@prvision.io", "password": "Whatever1"})
    assert r.status_code == 401


def test_seeded_demo_user_can_login(app):
    r = app.post("/api/auth/login", json={
        "email": "demo@prvision.ai", "password": "DemoVision!2026"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"


@pytest.fixture()
def completed_job(db_session):
    """A synthetic completed verification job (pipeline not run)."""
    from app.db.models import VerificationJob

    job = VerificationJob(
        status="completed", input_kind="text", input_label="synthetic job",
        progress=100, stage="done",
        result_summary='{"overall": {"verdict": "UNVERIFIED", "detail": "d", "caveats": []},'
                       '"priority": {"intervention_priority": 55.5, "label": "HIGH", "factors": []},'
                       '"risk": {"misinformation_risk": 0.3}, "providers": {}}')
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job.id


def test_cases_require_auth(app, completed_job):
    assert app.get("/api/cases").status_code == 401
    assert app.post("/api/cases", json={
        "verification_job_id": completed_job, "title": "No auth case"}).status_code == 401
    assert app.get(f"/api/verify/{completed_job}/export.pdf").status_code in (200, 409)
    # export endpoints are readable; case mutations are not anonymous


def test_case_lifecycle_with_notes(app, user_headers, completed_job):
    r = app.post("/api/cases", headers=user_headers, json={
        "verification_job_id": completed_job,
        "title": "Synthetic investigation", "summary": "created by tests"})
    assert r.status_code == 200, r.text
    case = r.json()
    assert case["status"] == "OPEN"
    # snapshot came from the job's result_summary
    assert case["priority_snapshot"] == 55.5
    assert case["severity_label"] == "HIGH"
    assert case["verdict_snapshot"] == "UNVERIFIED"
    case_id = case["case_id"]

    r = app.post(f"/api/cases/{case_id}/notes", headers=user_headers,
                 json={"body": "First investigator note."})
    assert r.status_code == 200

    r = app.patch(f"/api/cases/{case_id}", headers=user_headers, json={"status": "MONITORING"})
    assert r.status_code == 200 and r.json()["status"] == "MONITORING"

    r = app.get(f"/api/cases/{case_id}", headers=user_headers)
    assert r.status_code == 200
    detail = r.json()
    assert len(detail["notes"]) == 1
    assert detail["notes"][0]["body"] == "First investigator note."

    r = app.get("/api/cases", headers=user_headers)
    assert r.status_code == 200 and r.json()["total"] >= 1

    r = app.delete(f"/api/cases/{case_id}", headers=user_headers)
    assert r.status_code == 200
    assert app.get(f"/api/cases/{case_id}", headers=user_headers).status_code == 404


def test_case_rejects_incomplete_job(app, user_headers, db_session):
    from app.db.models import VerificationJob

    job = VerificationJob(status="running", input_kind="text", input_label="running job",
                          progress=10, stage="working")
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    r = app.post("/api/cases", headers=user_headers,
                 json={"verification_job_id": job.id, "title": "Too early case"})
    assert r.status_code == 409


def test_alerts_endpoints(app, user_headers, db_session):
    from app.db.models import Alert

    db_session.add(Alert(severity="HIGH", kind="acceleration_spike",
                         title="t", message="m", metrics={}))
    db_session.add(Alert(severity="CRITICAL", kind="misinfo_risk",
                         title="t2", message="m2", metrics={}))
    db_session.commit()

    r = app.get("/api/alerts?severity=CRITICAL")
    assert r.status_code == 200
    rows = r.json()["alerts"]
    assert rows and all(a["severity"] == "CRITICAL" for a in rows)

    r = app.get("/api/alerts/summary")
    assert r.status_code == 200 and r.json()["total_unacknowledged"] >= 2

    alert_id = rows[0]["alert_id"]
    # ack requires auth
    assert app.post(f"/api/alerts/{alert_id}/ack", json={}).status_code == 401
    r = app.post(f"/api/alerts/{alert_id}/ack", headers=user_headers, json={"note": "seen"})
    assert r.status_code == 200 and r.json()["acknowledged"] is True
