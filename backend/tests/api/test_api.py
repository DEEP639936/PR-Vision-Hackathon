"""API tests — every major endpoint against a real TestClient + SQLite DB."""
from __future__ import annotations


def test_health_reflects_real_state(app):
    response = app.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"healthy", "degraded", "unhealthy"}
    assert data["database"] == "connected"      # test DB is reachable
    assert "version" in data


def test_platforms_list_includes_all_supported(app):
    response = app.get("/api/platforms")
    assert response.status_code == 200
    platforms = {p["platform"] for p in response.json()["platforms"]}
    assert {"demo", "x", "reddit", "instagram", "facebook", "linkedin"} <= platforms
    # real connectors without credentials must honestly report not_configured
    by_name = {p["platform"]: p for p in response.json()["platforms"]}
    assert by_name["x"]["configured"] is False
    assert by_name["x"]["status"] == "not_configured"


def test_demo_generation_then_posts_list(app):
    response = app.post("/api/demo/generate", json={"num_posts": 2, "score": True})
    assert response.status_code == 200
    created = response.json()
    assert created["created"] == 2

    posts = app.get("/api/posts?platform=demo").json()
    assert posts["total"] >= 2
    for post in posts["posts"]:
        assert post["is_demo"] is True
        assert post["latest_metrics"] is not None


def test_demo_generation_validates_archetypes(app):
    response = app.post("/api/demo/generate", json={"num_posts": 1, "archetypes": ["bogus"]})
    assert response.status_code == 422


def test_post_metrics_time_series(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    data = app.get(f"/api/posts/{post_id}/metrics").json()
    assert data["total"] >= 5
    stamps = [s["timestamp"] for s in data["snapshots"]]
    assert stamps == sorted(stamps), "snapshots must be time-ordered"
    shares = [s["shares"] for s in data["snapshots"]]
    assert all(b >= a for a, b in zip(shares, shares[1:]))


def test_post_metrics_window_filter(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    data = app.get(f"/api/posts/{post_id}/metrics?window_minutes=30").json()
    assert data["total"] >= 1


def test_post_features_endpoint(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    data = app.get(f"/api/posts/{post_id}/features").json()
    assert len(data) >= 1
    first = data[0]
    # engineered fields must be present (values may be None early on)
    assert "share_velocity" in first
    assert "engagement_velocity" in first


def test_post_propagation_edges(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    data = app.get(f"/api/posts/{post_id}/propagation").json()
    assert data["total"] >= 0
    assert "events" in data


def test_prediction_payload_contract(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    data = app.get(f"/api/posts/{post_id}/prediction?refresh=true").json()
    assert 0 <= data["intervention_priority"] <= 100
    assert data["priority_label"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert 0 <= data["misinformation_risk"] <= 1
    assert 0 <= data["spread_risk"] <= 1
    assert {"30", "60", "120"} <= set(data["horizons"].keys())
    for horizon in data["horizons"].values():
        assert horizon["prediction_type"] in {"model", "baseline"}
        if horizon["prediction_type"] == "baseline":
            assert "reason" in horizon
    assert data["explanation"], "explanation must never be empty"
    assert data["top_factors"]


def test_intervention_score_persisted(app, demo_posts):
    post_id = demo_posts[1]["post_id"]
    app.get(f"/api/posts/{post_id}/prediction?refresh=true")
    data = app.get(f"/api/posts/{post_id}/intervention-score").json()
    assert data["post_id"] == post_id
    assert data["priority_label"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_dashboard_summary_counts(app, demo_posts):
    data = app.get("/api/dashboard/summary").json()
    assert data["posts_monitored"] >= 2
    assert isinstance(data["platform_counts"], dict) and data["platform_counts"].get("demo", 0) >= 2
    assert data["models"]["forecast"]["60"]["available"] is True


def test_dashboard_high_priority_sorted(app, demo_posts):
    data = app.get("/api/dashboard/high-priority?limit=10").json()
    priorities = [p["intervention_priority"] for p in data["posts"]]
    assert priorities == sorted(priorities, reverse=True)


def test_dashboard_trending(app, demo_posts):
    data = app.get("/api/dashboard/trending?limit=5").json()
    assert data["total"] >= 1


def test_dashboard_label_filter(app, demo_posts):
    data = app.get("/api/dashboard/high-priority?label=CRITICAL").json()
    for post in data["posts"]:
        assert post["priority_label"] == "CRITICAL"


def test_404_for_unknown_post(app):
    assert app.get("/api/posts/999999").status_code == 404
    assert app.get("/api/posts/999999/prediction").status_code == 404


def test_ml_predict_endpoint(app, demo_posts):
    post_id = demo_posts[0]["post_id"]
    response = app.post("/api/ml/predict", json={"post_id": post_id, "persist": True})
    assert response.status_code == 200
    assert response.json()["scored"] == 1


def test_ingestion_start_stop_cycle(app):
    start = app.post("/api/ingestion/start", json={"platforms": ["demo"], "interval_seconds": 15})
    assert start.status_code == 200
    status = app.get("/api/ingestion/status").json()
    assert status["running"] is True
    assert "demo" in status["platforms"]

    stop = app.post("/api/ingestion/stop")
    assert stop.status_code == 200
    assert stop.json()["running"] is False


def test_ingestion_poll_once(app, demo_posts):
    response = app.post("/api/ingestion/poll-once?platform=demo")
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "demo"
    assert "snapshots_added" in body


def test_openapi_schema_available(app):
    schema = app.get("/openapi.json").json()
    paths = set(schema["paths"].keys())
    expected = {
        "/api/health", "/api/platforms", "/api/posts", "/api/posts/{post_id}",
        "/api/posts/{post_id}/metrics", "/api/posts/{post_id}/features",
        "/api/posts/{post_id}/prediction", "/api/posts/{post_id}/intervention-score",
        "/api/dashboard/summary", "/api/dashboard/trending", "/api/dashboard/high-priority",
        "/api/demo/generate", "/api/ingestion/start", "/api/ingestion/stop",
        "/api/ml/train", "/api/ml/predict", "/api/ml/status",
    }
    assert expected <= paths, f"missing endpoints: {expected - paths}"


def test_frontend_served(app):
    response = app.get("/")
    assert response.status_code == 200
    assert "PR" in response.text and "VISION" in response.text
    assert app.get("/js/api.js").status_code == 200
    assert app.get("/css/styles.css").status_code == 200
