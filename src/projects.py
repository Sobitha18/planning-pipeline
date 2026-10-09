"""Projects: named sets of already-registered repos. Persistence + the
readiness gate the repo router needs; no LLM here (see src/repo_router.py)."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src import index_query as iq
from src import repo_sources
from src.models import Project, ProjectRepo, Repo
from src.runs import DomainError, RepoNotFound


class ProjectNotFound(DomainError):
    pass


class ProjectNameTaken(DomainError):
    pass


class ProjectNotReady(DomainError):
    """A project's repos must all be indexed (status=ready) before routing."""


def register_sources(session, sources: list[str], index: Callable[[int], None]) -> list[dict]:
    """Make every reference (git URL or absolute path) a registered, indexing
    repo and return one entry per input, in order.

    All references are validated before anything is cloned, so a typo in the
    last one doesn't leave earlier repos half-processed. Then each is cloned /
    updated / located, its Repo row is (re)set to status=building, and
    `index(repo_id)` is called to start indexing in the background.
    """
    parsed = [repo_sources.classify(raw) for raw in sources]   # RepoSourceError -> 422

    out: list[dict] = []
    for source in parsed:
        resolved = repo_sources.resolve(source)
        root = str(resolved.root)
        repo = session.execute(select(Repo).where(Repo.root_path == root)).scalar_one_or_none()
        if repo is None:
            repo = Repo(root_path=root, status="building")
            session.add(repo)
        else:
            repo.status = "building"
        session.commit()
        index(repo.id)
        entry = {"source": source.raw, "repo_id": repo.id, "root_path": root,
                 "action": resolved.action, "status": repo.status}
        if not (resolved.root / ".git").exists():
            entry["warning"] = "not a git repo; indexing without SHA pinning"
        out.append(entry)
    return out


def ensure_name_free(session, name: str) -> None:
    if session.execute(select(Project.id).where(Project.name == name)).first() is not None:
        raise ProjectNameTaken(f"a project named {name!r} already exists")


def require_repos_exist(session, repo_ids: list[int]) -> None:
    ids = list(dict.fromkeys(repo_ids))
    found = {r for (r,) in session.execute(select(Repo.id).where(Repo.id.in_(ids))).all()} if ids else set()
    missing = [i for i in ids if i not in found]
    if missing:
        raise RepoNotFound(f"no repos with ids {missing}")


def create_project(session, name: str, repo_ids: list[int]) -> Project:
    ids = list(dict.fromkeys(repo_ids))  # de-dupe, keep order
    require_repos_exist(session, ids)
    project = Project(name=name)
    project.repos = [ProjectRepo(repo_id=i) for i in ids]
    session.add(project)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ProjectNameTaken(f"a project named {name!r} already exists") from None
    return project


def get_project_by_name(session, name: str) -> Project | None:
    return session.execute(select(Project).where(Project.name == name)).scalar_one_or_none()


def get_project(session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(f"no project with id {project_id}")
    return project


def add_repo(session, project_id: int, repo_id: int) -> Project:
    project = get_project(session, project_id)
    if session.get(Repo, repo_id) is None:
        raise RepoNotFound(f"no repo with id {repo_id}")
    if all(pr.repo_id != repo_id for pr in project.repos):
        project.repos.append(ProjectRepo(repo_id=repo_id))
        session.commit()
    return project


def describe(session, project: Project) -> dict:
    return {
        "project_id": project.id,
        "name": project.name,
        "repos": [
            {"repo_id": r["id"], "root_path": r["root_path"], "status": r["status"]}
            for r in iq.get_project_repos(project.id)
        ],
    }


def require_ready(project_id: int) -> None:
    not_ready = [r for r in iq.get_project_repos(project_id) if r["status"] != "ready"]
    if not_ready:
        raise ProjectNotReady(
            "repos not indexed yet: " + ", ".join(f"{r['root_path']} ({r['status']})" for r in not_ready)
        )
