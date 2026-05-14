"""HMAC + endpoint coverage for the gh-proxy FastAPI app."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from robomp.config import Settings
from robomp.github_client import GitHubClient
from robomp.proxy.server import create_proxy_app
from robomp.proxy_hmac import HEADER_SIGNATURE, HEADER_TIMESTAMP, sign
from robomp.sandbox import workspace_key

_HMAC = "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TOKEN = "ghp_test_token_value"


# ---------- shared fixtures ----------


def _build_settings(tmp_path: Path) -> Settings:
    """Construct a Settings object for the proxy side without going through
    the orchestrator-mode mutual-exclusion validator (the proxy reads token +
    hmac key directly; the validator is geared at orchestrator deployments)."""
    cfg = Settings.model_construct(
        github_token=SecretStr(_TOKEN),
        github_webhook_secret=SecretStr("webhook-secret"),
        bot_login="robomp-bot",
        git_author_email="robomp-bot@example.invalid",
        repo_allowlist_raw="octo/widget",
        gh_proxy_url=None,
        gh_proxy_hmac_key=SecretStr(_HMAC),
        gh_proxy_bind_host="0.0.0.0",
        gh_proxy_bind_port=8081,
        workspace_root=tmp_path / "workspaces",
        sqlite_path=tmp_path / "robomp.sqlite",
        log_dir=tmp_path / "logs",
    )
    cfg.ensure_paths()
    return cfg


@pytest.fixture
def proxy_settings(tmp_path: Path) -> Settings:
    return _build_settings(tmp_path)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """Bare local repo with one commit on `main`."""
    repo = tmp_path / "upstream.git"
    repo.mkdir()
    _git(["init", "--initial-branch=main", "--bare", str(repo)], tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "--initial-branch=main", str(seed)], tmp_path)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "."], tmp_path)
    _git(["-C", str(seed), "commit", "-m", "init"], tmp_path)
    _git(["-C", str(seed), "remote", "add", "origin", str(repo)], tmp_path)
    _git(["-C", str(seed), "push", "origin", "main"], tmp_path)
    return repo


def _stage_workspace(cfg: Settings, upstream: Path, repo: str, number: int, branch: str) -> tuple[Path, str]:
    """Pre-stage a workspace clone with one new commit on `branch`."""
    ws_dir = Path(cfg.workspace_root) / workspace_key(repo, number)
    ws_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = ws_dir / "repo"
    _git(["clone", str(upstream), str(repo_dir)], ws_dir)
    _git(["-C", str(repo_dir), "config", "user.email", "t@t"], ws_dir)
    _git(["-C", str(repo_dir), "config", "user.name", "t"], ws_dir)
    _git(["-C", str(repo_dir), "checkout", "-b", branch], ws_dir)
    (repo_dir / "x.txt").write_text("x", encoding="utf-8")
    _git(["-C", str(repo_dir), "add", "."], ws_dir)
    _git(["-C", str(repo_dir), "commit", "-m", "x"], ws_dir)
    proc = _git(["-C", str(repo_dir), "rev-parse", "HEAD"], ws_dir)
    return repo_dir, proc.stdout.strip()


def _bare_has_branch(bare: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


# ---------- HMAC + signed request helpers ----------


def _signed(
    method: str, path: str, body: bytes = b"", *, ts: str | None = None, key: bytes | None = None
) -> dict[str, str]:
    timestamp, sig = sign(method=method, path=path, body=body, key=key or _HMAC.encode(), timestamp=ts)
    return {HEADER_TIMESTAMP: timestamp, HEADER_SIGNATURE: sig}


def _build_app(cfg: Settings, gh_handler: Callable[[httpx.Request], httpx.Response] | None = None):
    app = create_proxy_app(cfg)
    transport = httpx.MockTransport(gh_handler) if gh_handler is not None else None
    app.state.github = GitHubClient(_TOKEN, transport=transport)
    app.state.settings = cfg
    return app


async def _async_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )


# ============================================================================
# HMAC behavior
# ============================================================================


async def test_hmac_accept_post_comment_round_trip(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={
                "id": 42,
                "user": {"login": "robomp-bot"},
                "body": "hello",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hello"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"id": 42, "author": "robomp-bot", "body": "hello", "created_at": "2026-01-01T00:00:00Z"}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/comments"


async def test_hmac_reject_missing_headers(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    async with await _async_client(app) as client:
        resp = await client.get("/gh/v1/repo", params={"repo": "octo/widget"})
    assert resp.status_code == 401


async def test_hmac_reject_bad_signature(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/repo",
            params={"repo": "octo/widget"},
            headers={HEADER_TIMESTAMP: str(int(time.time())), HEADER_SIGNATURE: "0" * 64},
        )
    assert resp.status_code == 401


async def test_hmac_reject_stale_timestamp(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json={}))
    stale = str(int(time.time()) - 120)
    headers = _signed("GET", "/gh/v1/repo", ts=stale)
    async with await _async_client(app) as client:
        resp = await client.get("/gh/v1/repo", params={"repo": "octo/widget"}, headers=headers)
    assert resp.status_code == 401


# ============================================================================
# GET endpoints
# ============================================================================


async def test_get_repo(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget"
        return httpx.Response(
            200,
            json={
                "full_name": "octo/widget",
                "default_branch": "main",
                "clone_url": "https://github.com/octo/widget.git",
                "private": False,
            },
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/repo",
            params={"repo": "octo/widget"},
            headers=_signed("GET", "/gh/v1/repo"),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "full_name": "octo/widget",
        "default_branch": "main",
        "clone_url": "https://github.com/octo/widget.git",
        "private": False,
    }


async def test_get_issue(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues/1"
        return httpx.Response(
            200,
            json={
                "number": 1,
                "title": "T",
                "body": "B",
                "state": "open",
                "user": {"login": "alice"},
                "labels": [{"name": "bug"}],
            },
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/issue",
            params={"repo": "octo/widget", "number": 1},
            headers=_signed("GET", "/gh/v1/issue"),
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["repo"] == "octo/widget"
    assert payload["number"] == 1
    assert payload["labels"] == ["bug"]
    assert payload["is_pull_request"] is False


async def test_list_issues(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "first",
                    "state": "open",
                    "user": {"login": "alice"},
                    "labels": [{"name": "bug"}],
                    "comments": 0,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "created_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://example/1",
                },
                # A PR — must be filtered out.
                {
                    "number": 2,
                    "title": "pr",
                    "pull_request": {"url": "x"},
                    "user": {"login": "alice"},
                },
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/issues",
            params={"repo": "octo/widget"},
            headers=_signed("GET", "/gh/v1/issues"),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["number"] == 1


async def test_list_comments(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/issues/1/comments"
        return httpx.Response(
            200,
            json=[
                {"id": 1, "user": {"login": "u"}, "body": "hi", "created_at": "2026-01-01T00:00:00Z"},
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/comments",
            params={"repo": "octo/widget", "number": 1},
            headers=_signed("GET", "/gh/v1/comments"),
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [{"id": 1, "author": "u", "body": "hi", "created_at": "2026-01-01T00:00:00Z"}],
    }


async def test_list_review_comments(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/comments"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9,
                    "user": {"login": "rev"},
                    "body": "nit",
                    "path": "a.py",
                    "line": 5,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/review_comments",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/review_comments"),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["path"] == "a.py"
    assert items[0]["line"] == 5


async def test_list_pr_reviews(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/octo/widget/pulls/1/reviews"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 11,
                    "user": {"login": "rev"},
                    "body": "looks good",
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T00:00:00Z",
                },
                # Empty body — must be filtered out by GitHubClient.
                {"id": 12, "user": {"login": "rev"}, "body": "  ", "state": "COMMENTED"},
            ],
        )

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/pr_reviews",
            params={"repo": "octo/widget", "pr_number": 1},
            headers=_signed("GET", "/gh/v1/pr_reviews"),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "APPROVED"


async def test_authenticated_login(proxy_settings: Settings) -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/user"
        return httpx.Response(200, json={"login": "robomp-bot"})

    app = _build_app(proxy_settings, gh)
    async with await _async_client(app) as client:
        resp = await client.get(
            "/gh/v1/authenticated_login",
            headers=_signed("GET", "/gh/v1/authenticated_login"),
        )
    assert resp.status_code == 200
    assert resp.json() == {"login": "robomp-bot"}


# ============================================================================
# POST endpoints
# ============================================================================


async def test_post_comment_forwards_body(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={"id": 7, "user": {"login": "b"}, "body": "hi", "created_at": "2026-01-01T00:00:00Z"},
        )

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hi"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    req = captured["req"]
    assert req.method == "POST"
    assert req.url.path == "/repos/octo/widget/issues/1/comments"
    import json

    assert json.loads(req.content) == {"body": "hi"}


async def test_add_issue_labels(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(200, json=[{"name": "triage"}, {"name": "bug"}])

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"labels":["triage","bug"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/add_issue_labels",
            content=body,
            headers={**_signed("POST", "/gh/v1/add_issue_labels", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"labels": ["triage", "bug"]}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/labels"
    import json

    assert json.loads(captured["req"].content) == {"labels": ["triage", "bug"]}


async def test_add_assignees(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(201, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"assignees":["alice"]}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/add_assignees",
            content=body,
            headers={**_signed("POST", "/gh/v1/add_assignees", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured["req"].url.path == "/repos/octo/widget/issues/1/assignees"
    import json

    assert json.loads(captured["req"].content) == {"assignees": ["alice"]}


async def test_open_pull_request(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(
            201,
            json={
                "number": 4,
                "html_url": "https://example/4",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "state": "open",
            },
        )

    app = _build_app(proxy_settings, gh)
    body = (
        b'{"repo":"octo/widget","head":"feature","base":"main",'
        b'"title":"t","body":"b","draft":false,"maintainer_can_modify":true}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/open_pull_request",
            content=body,
            headers={**_signed("POST", "/gh/v1/open_pull_request", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json()["number"] == 4
    assert captured["req"].url.path == "/repos/octo/widget/pulls"
    import json

    sent = json.loads(captured["req"].content)
    assert sent["head"] == "feature"
    assert sent["base"] == "main"
    assert sent["title"] == "t"


async def test_request_reviewers(proxy_settings: Settings) -> None:
    captured: dict[str, httpx.Request] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return httpx.Response(201, json={})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","pr_number":4,"reviewers":["alice"],"team_reviewers":null}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/request_reviewers",
            content=body,
            headers={**_signed("POST", "/gh/v1/request_reviewers", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured["req"].url.path == "/repos/octo/widget/pulls/4/requested_reviewers"
    import json

    assert json.loads(captured["req"].content) == {"reviewers": ["alice"]}


# ============================================================================
# GitHub error passthrough
# ============================================================================


async def test_github_error_passthrough_422(proxy_settings: Settings) -> None:
    def gh(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "validation failed"})

    app = _build_app(proxy_settings, gh)
    body = b'{"repo":"octo/widget","number":1,"body":"hi"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/post_comment",
            content=body,
            headers={**_signed("POST", "/gh/v1/post_comment", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["kind"] == "github"
    assert err["status"] == 422
    assert err["message"] == "validation failed"


# ============================================================================
# git transport endpoints
# ============================================================================


async def test_git_clone_creates_pool_dir(proxy_settings: Settings, upstream_repo: Path) -> None:
    app = _build_app(proxy_settings)
    body = b'{"repo":"octo/widget","clone_url":"' + str(upstream_repo).encode() + b'","default_branch":"main"}'
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/clone",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/clone", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    pool_dir = Path(resp.json()["pool_dir"])
    assert pool_dir.is_dir()
    assert pool_dir == Path(proxy_settings.workspace_root) / "_pool" / "octo__widget"
    assert (pool_dir / "HEAD").exists() or (pool_dir / ".git" / "HEAD").exists()


async def test_git_push_happy_path(proxy_settings: Settings, upstream_repo: Path) -> None:
    branch = "farm/abc/feature"
    _, head = _stage_workspace(proxy_settings, upstream_repo, "octo/widget", 1, branch)
    # Rewire origin to the bare upstream so the proxy's push lands there.
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"octo__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"head": head, "branch": branch}
    assert _bare_has_branch(upstream_repo, branch)


async def test_git_push_head_drift(proxy_settings: Settings, upstream_repo: Path) -> None:
    branch = "farm/abc/drift"
    _, _ = _stage_workspace(proxy_settings, upstream_repo, "octo/widget", 1, branch)
    app = _build_app(proxy_settings)
    fake_head = "0" * 40
    body = (
        b'{"repo":"octo/widget","workspace_key":"octo__widget__1","branch":"'
        + branch.encode()
        + b'","expected_head":"'
        + fake_head.encode()
        + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["kind"] == "head_drift"
    assert not _bare_has_branch(upstream_repo, branch)


async def test_git_push_workspace_key_mismatch(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings)
    body = (
        b'{"repo":"octo/widget","workspace_key":"other__repo__1","branch":"x","expected_head":"' + (b"0" * 40) + b'"}'
    )
    async with await _async_client(app) as client:
        resp = await client.post(
            "/gh/v1/git/push",
            content=body,
            headers={**_signed("POST", "/gh/v1/git/push", body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "workspace_key" in resp.text
