"""Project + select-repos HTTP endpoints. The router itself is monkeypatched
(its logic is covered in test_repo_router.py); this checks routing, readiness
gating and problem+json shapes. Kept separate from test_api.py."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src import api, repo_router
from src.db import get_session
from src.models import RepoSelection, Repo


@pytest.fixture
def client(db_url):
    with TestClient(api.app) as c:
        yield c


def make_repo(status="ready") -> int:
    session = get_session()
    try:
        repo = Repo(root_path=f"/tmp/proj-api-{uuid.uuid4()}", status=status)
        session.add(repo)
        session.commit()
        return repo.id
    finally:
        session.close()


def name() -> str:
    return f"proj-{uuid.uuid4().hex[:8]}"


def test_create_get_and_add_repo(client):
    r1, r2 = make_repo(), make_repo()
    created = client.post("/v1/projects", json={"name": name(), "repo_ids": [r1]})
    assert created.status_code == 201
    pid = created.json()["project_id"]
    assert [r["repo_id"] for r in created.json()["repos"]] == [r1]

    added = client.post(f"/v1/projects/{pid}/repos", json={"repo_id": r2})
    assert [r["repo_id"] for r in added.json()["repos"]] == [r1, r2]
    # adding twice is a no-op
    again = client.post(f"/v1/projects/{pid}/repos", json={"repo_id": r2})
    assert len(again.json()["repos"]) == 2
    assert len(client.get(f"/v1/projects/{pid}").json()["repos"]) == 2


def test_create_rejects_unknown_repo_and_duplicate_name(client):
    resp = client.post("/v1/projects", json={"name": name(), "repo_ids": [999999]})
    assert resp.status_code == 404 and resp.json()["title"] == "Repo Not Found"
    n = name()
    assert client.post("/v1/projects", json={"name": n}).status_code == 201
    dup = client.post("/v1/projects", json={"name": n})
    assert dup.status_code == 409 and dup.json()["title"] == "Project Name Taken"


def test_unknown_project_is_404(client):
    assert client.get("/v1/projects/999999").json()["title"] == "Project Not Found"
    resp = client.post("/v1/projects/999999/select-repos", json={"text": "x"})
    assert resp.status_code == 404


def test_select_repos_409_while_a_repo_is_still_indexing(client):
    pid = client.post("/v1/projects", json={"name": name(), "repo_ids": [make_repo(), make_repo("building")]}).json()["project_id"]
    resp = client.post(f"/v1/projects/{pid}/select-repos", json={"text": "x"})
    assert resp.status_code == 409 and resp.json()["title"] == "Project Not Ready"
    assert "building" in resp.json()["detail"]


def test_select_repos_returns_the_routers_selection(client, monkeypatch):
    pid = client.post("/v1/projects", json={"name": name(), "repo_ids": [make_repo()]}).json()["project_id"]
    seen = {}

    def fake_select(project_id, text, attachments=None, **kw):
        seen.update(project_id=project_id, text=text, attachments=attachments)
        return RepoSelection(project_id=project_id, repos=[], stats={"turns": 1})

    monkeypatch.setattr(repo_router, "select_repos", fake_select)
    resp = client.post(f"/v1/projects/{pid}/select-repos", json={"text": "add coupons", "attachments": ["trace"]})
    assert resp.status_code == 200
    assert resp.json() == {"project_id": pid, "repos": [], "stats": {"turns": 1}}
    assert seen == {"project_id": pid, "text": "add coupons", "attachments": ["trace"]}


def test_select_repos_failure_maps_to_502(client, monkeypatch):
    pid = client.post("/v1/projects", json={"name": name(), "repo_ids": [make_repo()]}).json()["project_id"]

    def boom(*a, **kw):
        raise repo_router.RepoSelectionError("invalid after repair")

    monkeypatch.setattr(repo_router, "select_repos", boom)
    resp = client.post(f"/v1/projects/{pid}/select-repos", json={"text": "x"})
    assert resp.status_code == 502 and resp.json()["title"] == "Repo Selection Failed"


# ---------------------------------------------- repos given as URL or path

import subprocess
from pathlib import Path

MULTI = Path(__file__).parent / "fixtures" / "multi_repo"


@pytest.fixture
def inline_indexing(monkeypatch):
    """Index synchronously so a repo is `ready` by the time the response is built."""
    monkeypatch.setattr(api, "_start_indexing", lambda repo_id: api._index_repo_job(repo_id))


def test_create_project_from_absolute_paths_indexes_them(client, inline_indexing):
    paths = [str(MULTI / "orders-api"), str(MULTI / "storefront-web")]
    resp = client.post("/v1/projects", json={"name": name(), "repos": paths})
    assert resp.status_code == 201
    body = resp.json()
    assert [a["action"] for a in body["added"]] == ["local", "local"]
    assert [a["source"] for a in body["added"]] == paths
    # inline indexing finished: the project reads back as fully ready
    again = client.get(f"/v1/projects/{body['project_id']}").json()
    assert [r["status"] for r in again["repos"]] == ["ready", "ready"]


def test_create_project_from_a_git_url_clones_then_indexes(client, inline_indexing, tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "clones"))
    work = tmp_path / "work"
    work.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path)}
    run = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)  # noqa: E731
    run("init", "-q", "-b", "main")
    (work / "svc.py").write_text("def handler():\n    return 1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    bare = tmp_path / "git" / "acme" / "svc.git"
    bare.parent.mkdir(parents=True)
    run("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)

    resp = client.post("/v1/projects", json={
        "name": name(), "repos": [f"file://{bare}", str(MULTI / "inventory-service")]})
    assert resp.status_code == 201, resp.text
    cloned, local = resp.json()["added"]
    assert cloned["action"] == "cloned"
    assert cloned["root_path"] == str((tmp_path / "clones" / "local" / "acme" / "svc").resolve())
    assert local["action"] == "local"

    # the cloned repo really got indexed (its symbol is searchable)
    from src import index_query as iq
    assert any(h.name == "handler" for h in iq.find_symbols(cloned["repo_id"], "handler"))


def test_bad_reference_is_422_and_creates_nothing(client):
    n = name()
    resp = client.post("/v1/projects", json={"name": n, "repos": ["/definitely/not/a/dir"]})
    assert resp.status_code == 422 and resp.json()["title"] == "Invalid Repo Source"
    # the name is still free: nothing was created
    assert client.post("/v1/projects", json={"name": n}).status_code == 201


def test_one_bad_reference_aborts_before_any_repo_is_registered(client, inline_indexing):
    good = str(MULTI / "orders-api")
    resp = client.post("/v1/projects", json={"name": name(), "repos": [good, "not-a-repo"]})
    assert resp.status_code == 422
    # validation happens up front, so the good path was never (re)registered by this call
    assert "not-a-repo" in resp.json()["detail"]


def test_duplicate_name_is_rejected_before_any_cloning(client, monkeypatch):
    n = name()
    client.post("/v1/projects", json={"name": n})
    called = []
    monkeypatch.setattr(api.repo_sources, "resolve", lambda s: called.append(s))
    resp = client.post("/v1/projects", json={"name": n, "repos": ["https://github.com/a/b"]})
    assert resp.status_code == 409 and called == []


def test_unknown_repo_id_is_rejected_before_any_cloning(client, monkeypatch):
    called = []
    monkeypatch.setattr(api.repo_sources, "resolve", lambda s: called.append(s))
    resp = client.post("/v1/projects", json={
        "name": name(), "repos": ["https://github.com/a/b"], "repo_ids": [999999]})
    assert resp.status_code == 404 and called == []


def test_add_repo_to_existing_project_by_path_and_validation(client, inline_indexing):
    pid = client.post("/v1/projects", json={"name": name()}).json()["project_id"]
    resp = client.post(f"/v1/projects/{pid}/repos", json={"repo": str(MULTI / "analytics-jobs")})
    assert resp.status_code == 200
    assert resp.json()["added"][0]["action"] == "local"
    assert [r["status"] for r in resp.json()["repos"]] == ["ready"]
    # exactly one of repo / repo_id
    assert client.post(f"/v1/projects/{pid}/repos", json={}).status_code == 422
    assert client.post(f"/v1/projects/{pid}/repos", json={"repo": "/x", "repo_id": 1}).status_code == 422
