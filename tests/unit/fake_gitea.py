"""A small in-memory stand-in for the Gitea API, with real history: every
commit is a snapshot, so content can be read at any ref."""

import base64
import json
from typing import Any

import httpx

REPO = "/api/v1/repos/heimdall/gitops"


class FakeGitea:
    def __init__(self, files: dict[str, str]) -> None:
        self.snapshots: dict[str, dict[str, str]] = {"c0": dict(files)}
        self.commits: list[dict[str, Any]] = [
            {"sha": "c0", "parent": None, "message": "Initial", "paths": set(files), "n": 0}
        ]
        self.branches: dict[str, str] = {"main": "c0"}
        self.pulls: list[dict[str, Any]] = []

    # --------------------------------------------------------- helpers

    def commit(self, message: str, changes: dict[str, str | None], branch: str = "main") -> str:
        parent = self.branches[branch]
        files = dict(self.snapshots[parent])
        for path, content in changes.items():
            if content is None:
                files.pop(path, None)
            else:
                files[path] = content
        sha = f"c{len(self.commits)}"
        self.snapshots[sha] = files
        self.commits.append({"sha": sha, "parent": parent, "message": message,
                             "paths": set(changes), "n": len(self.commits)})  # fmt: skip
        self.branches[branch] = sha
        return sha

    def files(self, ref: str = "main") -> dict[str, str]:
        return self.snapshots[self.branches.get(ref, ref)]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), base_url="http://gitea")

    def _blob(self, path: str, content: str) -> dict[str, Any]:
        return {"path": path, "name": path.split("/")[-1], "type": "file",
                "content": base64.b64encode(content.encode()).decode(),
                "sha": f"blob-{hash(content) & 0xFFFFFF:x}"}  # fmt: skip

    # --------------------------------------------------------- routing

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(REPO)
        params = request.url.params
        body = json.loads(request.content) if request.content else {}

        if request.method == "GET" and path.startswith("/contents/"):
            files = self.files(params.get("ref", "main"))
            target = path.removeprefix("/contents/")
            if target in files:
                return httpx.Response(200, json=self._blob(target, files[target]))
            listing = [self._blob(p, c) for p, c in files.items() if p.startswith(target + "/")]
            return httpx.Response(200, json=listing) if listing else httpx.Response(404)

        if request.method == "GET" and path == "/commits":
            wanted = params.get("path")
            history = self._history(self.branches[params.get("sha", "main")])
            touching = [c for c in history if wanted in c["paths"]]
            return httpx.Response(200, json=[self._commit_json(c) for c in touching[:1]])

        if request.method == "POST" and path == "/branches":
            if body["new_branch_name"] in self.branches:
                return httpx.Response(409)
            self.branches[body["new_branch_name"]] = self.branches[body["old_branch_name"]]
            return httpx.Response(201, json={})

        if request.method == "POST" and path == "/contents":
            changes: dict[str, str | None] = {}
            for op in body["files"]:
                changes[op["path"]] = (
                    None
                    if op["operation"] == "delete"
                    else base64.b64decode(op["content"]).decode()
                )
            sha = self.commit(body["message"], changes, body["branch"])
            return httpx.Response(201, json={"commit": {"sha": sha}})

        if request.method == "POST" and path == "/pulls":
            if any(p["head"]["ref"] == body["head"] and p["state"] == "open" for p in self.pulls):
                return httpx.Response(409)
            number = len(self.pulls) + 1
            pull = {"number": number, "state": "open", "merged": False, "title": body["title"],
                    "body": body["body"], "head": {"ref": body["head"]},
                    "html_url": f"http://gitea/heimdall/gitops/pulls/{number}"}  # fmt: skip
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)

        if request.method == "GET" and path == "/pulls":
            return httpx.Response(200, json=[p for p in self.pulls if p["state"] == "open"])

        if request.method == "GET" and path.startswith("/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            found = [p for p in self.pulls if p["number"] == number]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404)

        return httpx.Response(404)

    def _history(self, sha: str | None) -> list[dict[str, Any]]:
        by_sha = {c["sha"]: c for c in self.commits}
        out = []
        while sha is not None:
            out.append(by_sha[sha])
            sha = by_sha[sha]["parent"]
        return out

    def _commit_json(self, commit: dict[str, Any]) -> dict[str, Any]:
        return {
            "sha": commit["sha"],
            "parents": [{"sha": commit["parent"]}] if commit["parent"] else [],
            "commit": {
                "message": commit["message"],
                "author": {"name": "dev-alice", "date": f"2026-11-10T13:{commit['n']:02d}:00Z"},
            },
        }

    def merge(self, number: int) -> None:
        pull = self.pulls[number - 1]
        self.snapshots["merge"] = dict(self.files(pull["head"]["ref"]))
        self.branches["main"] = "merge"
        pull.update(state="closed", merged=True)
