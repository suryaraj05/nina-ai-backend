"""Firestore project detection must survive large minified JS bundles."""

from __future__ import annotations

from unittest.mock import MagicMock

from nina.catalog_probe import (
    _detect_firestore_project,
    _detect_firestore_project_from_scripts,
    _firestore_project_in_text,
    pull_product_catalog,
)


def test_firestore_project_after_bundle_truncation_cutoff():
    """projectId often sits past the old 500k scan window in Vite bundles."""
    padding = "x" * 600_000
    tail = 'authDomain:"agrobot-82a6b.firebaseapp.com",projectId:"agrobot-82a6b"'
    text = padding + tail
    assert _firestore_project_in_text(text) == "agrobot-82a6b"
    assert _detect_firestore_project("", text[:500_000]) is None
    assert _detect_firestore_project("", text) == "agrobot-82a6b"


def test_detect_firestore_project_from_scripts_scans_full_js():
    html = '<script src="/assets/index-deadbeef.js"></script>'
    js_body = "z" * 700_000 + 'projectId:"shop-firebase-prod"'

    client = MagicMock()
    js_resp = MagicMock(status_code=200, text=js_body)
    client.get.return_value = js_resp

    assert _detect_firestore_project_from_scripts(client, "https://shop.test", html) == "shop-firebase-prod"


def test_pull_product_catalog_uses_stored_firestore_hint(monkeypatch):
    captured: dict[str, str | None] = {}

    def fake_try(client, project):
        captured["project"] = project
        return []

    monkeypatch.setattr("nina.catalog_probe._try_firestore_rest", fake_try)
    monkeypatch.setattr(
        "nina.catalog_probe._detect_firestore_project_from_scripts",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("nina.catalog_probe._collect_bundle_text", lambda *a, **k: "")
    monkeypatch.setattr("nina.catalog_probe._jsonld_from_paths", lambda *a, **k: [])
    monkeypatch.setattr("nina.catalog_probe.discover_paths", lambda *a, **k: [])

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return MagicMock(status_code=200, text="<html></html>")

    monkeypatch.setattr("nina.catalog_probe.httpx.Client", lambda **k: FakeClient())

    _, meta = pull_product_catalog("https://shop.test", firestore_project="agrobot-82a6b")
    assert captured["project"] == "agrobot-82a6b"
    assert meta["firestoreProject"] == "agrobot-82a6b"
