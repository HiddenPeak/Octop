"""Unit tests for OpenAPI schema customization."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.support.app import ensure_control_plane_bound, write_inhere_box_config

from inhere_box.api.app import build_app
from inhere_box.infra.server import InhereBoxServer


async def test_openapi_has_bearer_security_and_descriptions(tmp_inhere_box_home: Path) -> None:
    write_inhere_box_config(tmp_inhere_box_home, enable_api_docs=True)
    srv = InhereBoxServer(home=tmp_inhere_box_home)
    await srv.start()
    await ensure_control_plane_bound(srv)
    try:
        app = build_app(srv)
        with TestClient(app) as c:
            spec = c.get("/api/openapi.json").json()
            docs = c.get("/api/docs")
            assert docs.status_code == 200
            assert "/api/openapi.json" in docs.text
        assert spec["info"]["description"]
        assert "BearerAuth" in spec["components"]["securitySchemes"]
        login = spec["paths"]["/api/auth/login"]["post"]
        assert login["summary"] == "Sign in"
        me = spec["paths"]["/api/auth/me"]["get"]
        assert "BearerAuth" in me["security"][0]
        tags = {tag["name"]: tag["description"] for tag in spec["tags"]}
        assert "agentcomputer" not in tags
        assert "projects" in tags
        assert "security" not in spec["paths"]["/api/setup/status"]["get"]
    finally:
        await srv.stop()
