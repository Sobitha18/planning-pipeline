"""Task 09: minimal test for the static webapp route. Everything else about
this task (the actual UI behavior) is a manual click-through — see the file
header of webapp/index.html.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src import api


def test_root_serves_html_with_app_root(db_url):
    with TestClient(api.app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'id="app"' in resp.text


def test_root_serves_options_and_spec_chat_markup(db_url):
    """No JS test runner here by design (see the module docstring) — this
    stays a substring check on the served source, same as the app-root
    assertion above, just confirming the option-question and gate-1-chat
    functions actually shipped in the page."""
    with TestClient(api.app) as client:
        resp = client.get("/")
    assert "function renderQuestion(" in resp.text
    assert "function collectQuestionAnswers(" in resp.text
    assert "function renderSpecChat(" in resp.text
    assert "function sendSpecChat(" in resp.text
    assert "/spec/chat" in resp.text
