"""Repo references: classify + resolve (clone/update). No network: clones come
from a local bare repo via file://, which exercises the real git code path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src import repo_sources as rs


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                        "HOME": str(cwd or "/tmp")})


@pytest.fixture
def remote(tmp_path):
    """A bare 'remote' with one commit, plus the work tree used to add more."""
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "a.py").write_text("x = 1\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "one", cwd=work)
    bare = tmp_path / "remotes" / "acme" / "widgets.git"
    bare.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=work)
    git("fetch", "-q", cwd=work)
    git("branch", "-q", "--set-upstream-to=origin/main", "main", cwd=work)
    return {"url": f"file://{bare}", "work": work}


@pytest.fixture(autouse=True)
def clone_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "clones"))


# ---------------------------------------------------------------- classify


def test_absolute_path_must_exist(tmp_path):
    src = rs.classify(str(tmp_path))
    assert (src.kind, src.path) == ("path", tmp_path.resolve())
    with pytest.raises(rs.RepoSourceError, match="does not exist"):
        rs.classify(str(tmp_path / "nope"))


@pytest.mark.parametrize("raw,rel", [
    ("https://github.com/acme/widgets", "github.com/acme/widgets"),
    ("https://github.com/acme/widgets.git", "github.com/acme/widgets"),
    ("https://github.com/acme/widgets/", "github.com/acme/widgets"),
    ("git@github.com:acme/widgets.git", "github.com/acme/widgets"),
    ("ssh://git@gitlab.com/group/sub/widgets.git", "gitlab.com/sub/widgets"),
    ("file:///srv/git/acme/widgets.git", "local/acme/widgets"),
])
def test_url_maps_to_host_owner_repo_dir(raw, rel):
    src = rs.classify(raw)
    assert (src.kind, src.rel_dir) == ("url", Path(rel))


@pytest.mark.parametrize("raw", [
    "", "   ", "widgets", "relative/path", "http://github.com/a/b", "ftp://x/y/z",
    "--upload-pack=evil", "-c core.sshCommand=evil", "https://github.com/",
])
def test_rejects_unusable_references(raw):
    with pytest.raises(rs.RepoSourceError):
        rs.classify(raw)


def test_hostile_segments_cannot_escape_the_clone_dir():
    src = rs.classify("https://github.com/../..%2f/etc")
    assert ".." not in src.rel_dir.parts and not src.rel_dir.is_absolute()


# ----------------------------------------------------------------- resolve


def test_path_is_used_in_place(tmp_path):
    out = rs.resolve(rs.classify(str(tmp_path)))
    assert (out.root, out.action) == (tmp_path.resolve(), "local")


def test_url_is_cloned_then_fast_forwarded(remote, tmp_path):
    src = rs.classify(remote["url"])
    first = rs.resolve(src)
    assert first.action == "cloned"
    assert first.root == (tmp_path / "clones" / src.rel_dir).resolve()
    assert (first.root / "a.py").read_text() == "x = 1\n"

    (remote["work"] / "b.py").write_text("y = 2\n")
    git("add", "-A", cwd=remote["work"])
    git("commit", "-q", "-m", "two", cwd=remote["work"])
    git("push", "-q", "origin", "main", cwd=remote["work"])

    second = rs.resolve(src)
    assert second.action == "updated" and second.root == first.root
    assert (second.root / "b.py").exists()


def test_existing_clone_of_a_different_remote_is_refused(remote, tmp_path):
    src = rs.classify(remote["url"])
    dest = tmp_path / "clones" / src.rel_dir
    dest.parent.mkdir(parents=True)
    git("clone", "-q", str(tmp_path / "work"), str(dest), cwd=tmp_path)   # origin = the work tree
    with pytest.raises(rs.RepoSourceError, match="already holds a clone of"):
        rs.resolve(src)


def test_non_git_directory_in_the_way_is_not_overwritten(remote, tmp_path):
    src = rs.classify(remote["url"])
    dest = tmp_path / "clones" / src.rel_dir
    dest.mkdir(parents=True)
    (dest / "precious.txt").write_text("keep")
    with pytest.raises(rs.RepoSourceError, match="not a git clone"):
        rs.resolve(src)
    assert (dest / "precious.txt").read_text() == "keep"


def test_clone_failure_is_a_readable_error(tmp_path):
    src = rs.classify(f"file://{tmp_path}/missing/owner/repo.git")
    with pytest.raises(rs.RepoSourceError, match="git clone failed"):
        rs.resolve(src)
