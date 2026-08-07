from fastapi.testclient import TestClient

from crate_digger.web.app import create_app


def test_dashboard_has_no_track_profiles_page_or_endpoints(tmp_path):
    client = TestClient(create_app(db_path=tmp_path / "collection.sqlite3"))

    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/profiles"' not in home.text

    discovery = client.get("/discover")
    assert discovery.status_code == 200
    assert 'href="/profiles"' not in discovery.text

    assert client.get("/profiles").status_code == 404
    assert client.post("/profiles/update").status_code == 404
    assert (
        client.get("/profiles/audio", params={"path": "/missing.mp3"}).status_code
        == 404
    )
    assert client.get("/api/profiles").status_code == 404
    assert (
        client.get("/api/profiles/detail", params={"path": "/missing.mp3"}).status_code
        == 404
    )
    assert client.post("/api/profiles/update", json={}).status_code == 404


def test_dashboard_serves_template_assets(tmp_path):
    client = TestClient(create_app(db_path=tmp_path / "collection.sqlite3"))

    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/static/app.css"' in home.text
    assert 'href="/static/dashboard.css"' in home.text
    assert 'src="/static/dashboard.js"' in home.text
    assert "<style>" not in home.text

    for path, content_type in (
        ("/static/app.css", "text/css"),
        ("/static/dashboard.css", "text/css"),
        ("/static/dashboard.js", "javascript"),
        ("/static/discover.css", "text/css"),
        ("/static/discover.js", "javascript"),
        ("/static/genres.css", "text/css"),
        ("/static/link.css", "text/css"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert response.content
