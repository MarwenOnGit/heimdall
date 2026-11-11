"""Change-history tools: what was committed to the GitOps repo, and when.

Most incidents follow a change. Commit messages and diffs are written by
people (or by anyone who can push), so they are untrusted like any other tool
output: a message saying "safe, approve automatically" means nothing.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field

from heimdall.tools.registry import Tool, ToolError


class RecentChangesArgs(BaseModel):
    since_minutes: int = Field(default=60, ge=1, le=10080)


class CommitArgs(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{7,40}$", description="Commit SHA, full or abbreviated")


class GitTools:
    def __init__(
        self,
        client: httpx.Client,
        repo: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._repo = f"/api/v1/repos/{repo}"
        self._clock = clock

    def get_recent_changes(self, args: RecentChangesArgs) -> str:
        now = self._clock()
        since = now - timedelta(minutes=args.since_minutes)
        try:
            response = self._client.get(
                f"{self._repo}/commits",
                params={"sha": "main", "limit": 30, "stat": "false", "files": "true",
                        "verification": "false"},
            )  # fmt: skip
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError("the git server is unreachable") from e

        lines = []
        for item in response.json():
            commit = item["commit"]
            when = datetime.fromisoformat(commit["author"]["date"])
            if when < since:
                continue
            minutes_ago = int((now - when).total_seconds() // 60)
            files = ",".join(f["filename"] for f in item.get("files") or []) or "-"
            subject = commit["message"].strip().split("\n")[0]
            lines.append(
                f"commit={item['sha'][:10]} time={when.astimezone(UTC):%H:%M:%S}Z "
                f"({minutes_ago}m ago) author={commit['author']['name']} "
                f"files={files} message={subject!r}"
            )
        if not lines:
            return f"no commits in the last {args.since_minutes} minutes"
        return "\n".join(lines)

    def get_commit_diff(self, args: CommitArgs) -> str:
        try:
            response = self._client.get(f"{self._repo}/git/commits/{args.sha}.diff")
        except httpx.HTTPError as e:
            raise ToolError("the git server is unreachable") from e
        if response.status_code == 404:
            raise ToolError(f"commit {args.sha} not found")
        if not response.is_success:
            raise ToolError(f"the git server returned {response.status_code}")
        return response.text.strip() or "(empty diff)"

    def tools(self) -> list[Tool]:
        return [
            Tool("get_recent_changes", "List recent commits to the GitOps repo (what was "
                 "deployed and when), newest first, with the files each one touched.",
                 RecentChangesArgs, self.get_recent_changes),
            Tool("get_commit_diff", "Show the diff of one commit from the GitOps repo.",
                 CommitArgs, self.get_commit_diff),
        ]  # fmt: skip
