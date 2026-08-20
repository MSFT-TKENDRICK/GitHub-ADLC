"""Shared fixtures for the L5 GitHub task store tests.

Everything here runs with **no credentials and no network**. ``FakeGitHub`` is an
in-memory simulation of the slice of the GitHub REST API this adapter uses. It
deliberately gives issues an ``id`` that is *not* derivable from the ``number``
by accident, so any place the adapter confuses the two fails loudly.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from adlc.config import Config

SLUG = "acme/widgets"


def issue_id_for(number: int) -> int:
    """Ids and numbers must never be interchangeable in tests."""
    return 900_000 + number * 7


class FakeGitHub:
    """In-memory GitHub REST/GraphQL double implementing ``GitHubTransport``."""

    def __init__(self, slug: str = SLUG) -> None:
        self.slug = slug
        self.issues: dict[int, dict[str, Any]] = {}
        self.sub_issues: dict[int, list[int]] = {}
        self.parent_of: dict[int, int] = {}
        self.blocked_by: dict[int, list[int]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.graphql_calls: list[tuple[str, dict[str, Any]]] = []
        self.dependency_status: int = 201
        self._next_number = 1

    # -- helpers ----------------------------------------------------------
    @property
    def mutations(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] in {"POST", "PATCH", "PUT", "DELETE"}]

    def by_id(self, issue_id: int) -> dict[str, Any] | None:
        return next((i for i in self.issues.values() if i["id"] == issue_id), None)

    def _summary(self, number: int) -> dict[str, int]:
        children = self.sub_issues.get(number, [])
        total = len(children)
        completed = 0
        for cid in children:
            child = self.by_id(cid)
            if child is not None and child["state"] == "closed":
                completed += 1
        percent = round(100 * completed / total) if total else 0
        return {"total": total, "completed": completed, "percent_completed": percent}

    def _view(self, issue: dict[str, Any]) -> dict[str, Any]:
        number = issue["number"]
        blocking = sum(1 for ids in self.blocked_by.values() if issue["id"] in ids)
        return {
            **issue,
            "labels": [{"name": name} for name in issue["labels"]],
            "sub_issues_summary": self._summary(number),
            "issue_dependencies_summary": {
                "blocked_by": len(self.blocked_by.get(number, [])),
                "blocking": blocking,
                "total_blocked_by": len(self.blocked_by.get(number, [])),
                "total_blocking": blocking,
            },
        }

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        number = self._next_number
        self._next_number += 1
        issue = {
            "id": issue_id_for(number),
            "number": number,
            "node_id": f"I_kw{number}",
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "state": "open",
            "state_reason": None,
            "html_url": f"https://github.com/{self.slug}/issues/{number}",
            "labels": list(body.get("labels") or []),
        }
        self.issues[number] = issue
        return self._view(issue)

    def add_pull_request(self, labels: list[str]) -> dict[str, Any]:
        """PRs come back from the issues list endpoint too; the adapter must skip them."""
        created = self._create({"title": "a pull request", "body": "", "labels": labels})
        self.issues[created["number"]]["pull_request"] = {
            "url": f"https://api.github.com/repos/{self.slug}/pulls/{created['number']}"
        }
        return created

    # -- GitHubTransport --------------------------------------------------
    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.calls.append((method.upper(), path, body))
        method = method.upper()
        parts = urllib.parse.urlsplit(path).path.strip("/").split("/")
        assert parts[:4] == ["repos", *self.slug.split("/"), "issues"], path
        tail = parts[4:]

        if method == "POST" and not tail:
            return 201, self._create(body or {})

        number = int(tail[0]) if tail and tail[0].isdigit() else None
        if number is None or number not in self.issues:
            return 404, {"message": "Not Found"}
        issue = self.issues[number]
        rest = tail[1:]
        payload = body or {}

        if method == "GET" and not rest:
            return 200, self._view(issue)

        if method == "PATCH" and not rest:
            for key in ("title", "body", "state", "state_reason"):
                if key in payload:
                    issue[key] = payload[key]
            if "labels" in payload:
                issue["labels"] = list(payload["labels"])
            return 200, self._view(issue)

        if method == "POST" and rest == ["comments"]:
            comment = {"id": len(self.comments.get(number, [])) + 1, "body": payload["body"]}
            self.comments.setdefault(number, []).append(comment)
            return 201, comment

        if method == "POST" and rest == ["sub_issues"]:
            child_id = payload["sub_issue_id"]
            if self.by_id(child_id) is None:
                return 422, {"message": f"no issue with id {child_id}"}
            # Real GitHub allows exactly one parent per sub-issue.
            current_parent = self.parent_of.get(child_id)
            if current_parent is not None and current_parent != number:
                if not payload.get("replace_parent"):
                    return 422, {"message": "sub-issue already has a parent"}
                self.sub_issues[current_parent].remove(child_id)
            children = self.sub_issues.setdefault(number, [])
            if child_id not in children:
                children.append(child_id)
            self.parent_of[child_id] = number
            return 201, self._view(issue)

        if method == "GET" and rest == ["parent"]:
            parent_number = self.parent_of.get(issue["id"])
            if parent_number is None:
                return 404, {"message": "Not Found"}
            return 200, self._view(self.issues[parent_number])

        if method == "DELETE" and rest == ["sub_issue"]:
            child_id = payload["sub_issue_id"]
            if self.parent_of.get(child_id) != number:
                return 404, {"message": "sub-issue not found under this parent"}
            self.sub_issues[number].remove(child_id)
            del self.parent_of[child_id]
            return 200, self._view(self.issues[self.by_id(child_id)["number"]])

        if method == "POST" and rest == ["dependencies", "blocked_by"]:
            if self.dependency_status >= 400:
                return self.dependency_status, {"message": "issue dependencies disabled"}
            blocker_id = payload["issue_id"]
            if self.by_id(blocker_id) is None:
                return 422, {"message": f"no issue with id {blocker_id}"}
            blockers = self.blocked_by.setdefault(number, [])
            if blocker_id not in blockers:
                blockers.append(blocker_id)
            return 201, self._view(issue)

        return 404, {"message": f"unhandled {method} {path}"}

    def paginate(self, path: str) -> list[Any]:
        self.calls.append(("GET", path, None))
        split = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(split.query)
        tail = split.path.strip("/").split("/")[4:]

        if not tail:
            wanted = set(query.get("labels", [""])[0].split(","))
            return [
                self._view(issue)
                for issue in self.issues.values()
                if wanted & set(issue["labels"])
            ]

        number = int(tail[0])
        rest = tail[1:]
        if rest == ["sub_issues"]:
            return [self._view(self.by_id(cid)) for cid in self.sub_issues.get(number, [])]
        if rest == ["comments"]:
            return list(self.comments.get(number, []))
        if rest == ["dependencies", "blocked_by"]:
            return [self._view(self.by_id(bid)) for bid in self.blocked_by.get(number, [])]
        return []

    def graphql(self, query: str, variables: Any) -> Any:
        self.graphql_calls.append((query, dict(variables)))
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "PVTI_fake"}}}
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_fake"}}}


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(root=tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must behave identically on a developer laptop and in CI."""
    for var in (
        "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL", "GITHUB_API_URL", "GITHUB_GRAPHQL_URL",
        "ADLC_GITHUB_PROJECT", "ADLC_RUN_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def issue_number(external_id: str) -> int:
    """Parse an ``owner/repo#N`` external id back to its issue number."""
    return int(external_id.rsplit("#", 1)[1])


def make_graph(node_count: int = 3, run_id: str = "2026-08-19-a1b2") -> dict[str, Any]:
    """A small, schema-shaped task graph: a T001 -> T002 -> ... dependency chain."""
    nodes: list[dict[str, Any]] = []
    for i in range(1, node_count + 1):
        nodes.append(
            {
                "id": f"T{i:03d}",
                "title": f"Task number {i}",
                "kind": "implement" if i % 2 else "test",
                "dependsOn": [f"T{i - 1:03d}"] if i > 1 else [],
                "level": i - 1,
                "writeSet": [f"src/mod{i}.py"],
                "acceptance": [f"US1-AC{i}"],
            }
        )
    return {
        "runId": run_id,
        "baseSha": "0" * 40,
        "specDigest": "sha256:deadbeef",
        "nodes": nodes,
    }
