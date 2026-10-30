"""Minimal in-memory imitation of the GitHub REST endpoints MemoryStore uses."""
import base64
import hashlib
import json
from collections.abc import Callable

from aiohttp import web

PREFIX = "/repos/owner/repo"


class FakeGitHub:
    def __init__(self, files: dict[str, bytes | str], branch: str = "main"):
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}  # tree sha -> {path: blob sha}
        self.commits: dict[str, dict] = {}  # commit sha -> {tree, parent, message}
        self.branch = branch
        self.blob_reads = 0
        self.before_ref_update: Callable[[], None] | None = None  # e.g. simulate a concurrent push
        self.url = ""
        root = self._commit({p: self._blob(d) for p, d in files.items()}, None, "Initial commit")
        self.refs = {branch: root}

    # --- model ---
    def _blob(self, data: bytes | str) -> str:
        data = data.encode() if isinstance(data, str) else data
        sha = hashlib.sha1(b"blob" + data).hexdigest()
        self.blobs[sha] = data
        return sha

    def _tree(self, paths: dict[str, str]) -> str:
        sha = "t" + hashlib.sha1(json.dumps(sorted(paths.items())).encode()).hexdigest()
        self.trees[sha] = dict(paths)
        return sha

    def _commit(self, paths: dict[str, str], parent: str | None, message: str) -> str:
        tree = self._tree(paths)
        sha = hashlib.sha1(f"{tree}{parent}{message}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parent": parent, "message": message}
        return sha

    @property
    def head(self) -> str:
        return self.refs[self.branch]

    def tree_of(self, ref: str) -> dict[str, str]:
        sha = self.refs.get(ref, ref)
        return self.trees[self.commits[sha]["tree"]] if sha in self.commits else self.trees[sha]

    def files(self) -> dict[str, bytes]:
        return {p: self.blobs[s] for p, s in self.tree_of(self.head).items()}

    def push(self, changes: dict[str, bytes | str | None], message: str = "manual edit"):
        """Commit directly to the branch, as if edited on github.com."""
        paths = dict(self.tree_of(self.head))
        for path, data in changes.items():
            if data is None:
                paths.pop(path)
            else:
                paths[path] = self._blob(data)
        self.refs[self.branch] = self._commit(paths, self.head, message)

    # --- HTTP ---
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(PREFIX, self.get_repo)
        app.router.add_get(PREFIX + "/commits/{ref:.+}", self.get_commit)
        app.router.add_get(PREFIX + "/git/trees/{sha}", self.get_tree)
        app.router.add_get(PREFIX + "/git/blobs/{sha}", self.get_blob)
        app.router.add_post(PREFIX + "/git/blobs", self.post_blob)
        app.router.add_post(PREFIX + "/git/trees", self.post_tree)
        app.router.add_post(PREFIX + "/git/commits", self.post_commit)
        app.router.add_patch(PREFIX + "/git/refs/heads/{branch:.+}", self.patch_ref)
        return app

    async def get_repo(self, _request):
        return web.json_response({"default_branch": self.branch})

    async def get_commit(self, request):
        ref = request.match_info["ref"]
        sha = self.refs.get(ref, ref)
        if "vnd.github.sha" in request.headers.get("Accept", ""):
            return web.Response(text=sha)
        commit = self.commits[sha]
        new = self.trees[commit["tree"]]
        old = self.tree_of(commit["parent"]) if commit["parent"] else {}
        files = [{"filename": p, "status": "added"} for p in new if p not in old]
        files += [{"filename": p, "status": "removed"} for p in old if p not in new]
        files += [{"filename": p, "status": "modified"} for p in new if p in old and new[p] != old[p]]
        return web.json_response({
            "sha": sha,
            "parents": [{"sha": commit["parent"]}] if commit["parent"] else [],
            "commit": {"message": commit["message"], "author": {"name": "bot", "date": "2026-10-30T21:00:00Z"}},
            "files": files,
        })

    async def get_tree(self, request):
        sha = request.match_info["sha"]
        tree_sha = self.commits[sha]["tree"] if sha in self.commits else sha
        entries = [
            {"path": p, "type": "blob", "sha": s, "size": len(self.blobs[s])}
            for p, s in sorted(self.trees[tree_sha].items())
        ]
        return web.json_response({"sha": tree_sha, "tree": entries, "truncated": False})

    async def get_blob(self, request):
        self.blob_reads += 1
        return web.Response(body=self.blobs[request.match_info["sha"]])

    async def post_blob(self, request):
        body = await request.json()
        return web.json_response({"sha": self._blob(base64.b64decode(body["content"]))}, status=201)

    async def post_tree(self, request):
        body = await request.json()
        paths = dict(self.trees[body["base_tree"]])
        for e in body["tree"]:
            if "content" in e:
                paths[e["path"]] = self._blob(e["content"])
            elif e["sha"] is None:
                if e["path"] not in paths:
                    return web.json_response({"message": "GitTreeError: path not found"}, status=422)
                del paths[e["path"]]
            else:
                if e["sha"] not in self.blobs:
                    return web.json_response({"message": "sha not found"}, status=422)
                paths[e["path"]] = e["sha"]
        return web.json_response({"sha": self._tree(paths)}, status=201)

    async def post_commit(self, request):
        body = await request.json()
        sha = hashlib.sha1(f"{body['tree']}{body['parents']}{body['message']}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"tree": body["tree"], "parent": body["parents"][0], "message": body["message"]}
        return web.json_response({"sha": sha}, status=201)

    async def patch_ref(self, request):
        if self.before_ref_update:
            hook, self.before_ref_update = self.before_ref_update, None
            hook()
        sha = (await request.json())["sha"]
        if self.commits[sha]["parent"] != self.head:
            return web.json_response({"message": "Update is not a fast forward"}, status=422)
        self.refs[request.match_info["branch"]] = sha
        return web.json_response({"object": {"sha": sha}})
