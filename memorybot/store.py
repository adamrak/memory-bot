"""GitHub-backed note store: in-memory cache of notes + atomic multi-file commits via the Git Data API."""
import asyncio
import base64
import logging
from collections.abc import Callable
from urllib.parse import quote

import aiohttp

from .changes import BlobRef, Changes, ConflictError
from .config import GITHUB_API, MEMORY_DIR

log = logging.getLogger("memorybot")


class GitHubError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"GitHub {status}: {text}")
        self.status = status


class MemoryStore:
    """In-memory copy of all notes under memory_dir (+ list of attachments). GitHub is the source of truth."""

    def __init__(self, repo: str, token: str, branch: str = "", memory_dir: str = MEMORY_DIR, api_url: str = GITHUB_API):
        self.repo = repo
        self.branch = branch
        self.memory_dir = memory_dir
        self.files: dict[str, str] = {}  # note path -> content
        self.attachments: dict[str, tuple[str, int]] = {}  # attachment path -> (blob sha, size)
        self.sha: str | None = None  # commit the cache reflects
        self.tree_sha: str | None = None
        self._token = token
        self._api_url = api_url
        self._prefix = f"{memory_dir}/" if memory_dir else ""
        self._md_sha: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=aiohttp.ClientTimeout(total=120),
        )

    async def close(self):
        if self._session:
            await self._session.close()

    def repo_path(self, path: str) -> str:
        return f"{self._prefix}{path}"

    def blob_url(self, path: str) -> str:
        return f"https://github.com/{self.repo}/blob/{quote(self.branch)}/{quote(self.repo_path(path))}"

    async def _request(self, method: str, url: str, *, accept: str | None = None, raw: bool = False, **kwargs):
        headers = {"Accept": accept} if accept else None
        async with self._session.request(
            method, f"{self._api_url}/repos/{self.repo}{url}", headers=headers, **kwargs
        ) as r:
            if r.status >= 400:
                raise GitHubError(r.status, (await r.text())[:300])
            return await r.read() if raw else await r.json()

    async def read_blob(self, sha: str) -> bytes:
        return await self._request("GET", f"/git/blobs/{sha}", accept="application/vnd.github.raw+json", raw=True)

    async def sync(self) -> bool:
        async with self._lock:
            return await self._sync_locked()

    async def _sync_locked(self) -> bool:
        """Reload only notes whose blob changed since the last sync. Returns True if the head moved."""
        if not self.branch:
            self.branch = (await self._request("GET", ""))["default_branch"]
        head = (
            await self._request("GET", f"/commits/{quote(self.branch)}", accept="application/vnd.github.sha", raw=True)
        ).decode().strip()
        if head == self.sha:
            return False

        tree = await self._request("GET", f"/git/trees/{head}", params={"recursive": "1"})
        md_sha: dict[str, str] = {}
        attachments: dict[str, tuple[str, int]] = {}
        for e in tree["tree"]:
            if e["type"] != "blob" or not e["path"].startswith(self._prefix):
                continue
            path = e["path"][len(self._prefix):]
            if path.lower().endswith(".md"):
                md_sha[path] = e["sha"]
            else:
                attachments[path] = (e["sha"], e.get("size", 0))

        changed = [p for p, s in md_sha.items() if self._md_sha.get(p) != s]
        semaphore = asyncio.Semaphore(8)

        async def fetch(path: str) -> tuple[str, str]:
            async with semaphore:
                return path, (await self.read_blob(md_sha[path])).decode("utf-8", errors="replace")

        fetched = dict(await asyncio.gather(*(fetch(p) for p in changed)))
        self.files = {p: fetched[p] if p in fetched else self.files[p] for p in md_sha}
        self._md_sha, self.attachments = md_sha, attachments
        self.sha, self.tree_sha = head, tree["sha"]
        log.info("Synced %d notes, %d attachments @ %s (%d notes fetched)",
                 len(self.files), len(self.attachments), head[:7], len(changed))
        return True

    async def head_commit(self) -> dict:
        """Latest commit on the branch, including its changed files."""
        async with self._lock:
            await self._sync_locked()
            return await self._request("GET", f"/commits/{self.sha}")

    async def tree_blobs(self, commit_sha: str) -> dict[str, str]:
        """Repo path -> blob sha for every file in the given commit."""
        tree = await self._request("GET", f"/git/trees/{commit_sha}", params={"recursive": "1"})
        return {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}

    async def apply(self, build: Callable[[], Changes], message: str) -> str:
        """Run build() against fresh state and commit its result atomically. Returns the commit URL.
        If the branch moves while committing, the change set is rebuilt on top of it (build may raise ConflictError)."""
        async with self._lock:
            for attempt in range(3):
                await self._sync_locked()
                changes = build()
                if not changes:
                    raise ValueError("Nothing to change.")
                entries = []
                for path, value in changes.items():
                    entry: dict = {"path": self.repo_path(path), "mode": "100644", "type": "blob"}
                    if value is None:
                        entry["sha"] = None
                    elif isinstance(value, BlobRef):
                        entry["sha"] = value.sha
                    elif isinstance(value, bytes):
                        blob = await self._request(
                            "POST", "/git/blobs",
                            json={"content": base64.b64encode(value).decode("ascii"), "encoding": "base64"},
                        )
                        entry["sha"] = blob["sha"]
                    else:
                        entry["content"] = value
                    entries.append(entry)
                tree = await self._request("POST", "/git/trees", json={"base_tree": self.tree_sha, "tree": entries})
                commit = await self._request(
                    "POST", "/git/commits", json={"message": message, "tree": tree["sha"], "parents": [self.sha]}
                )
                try:
                    await self._request("PATCH", f"/git/refs/heads/{self.branch}", json={"sha": commit["sha"]})
                except GitHubError as e:
                    if e.status == 422 and attempt < 2:  # branch moved meanwhile -> rebuild on top of it
                        continue
                    raise
                await self._sync_locked()
                return f"https://github.com/{self.repo}/commit/{commit['sha']}"
        raise ConflictError("Repository keeps changing, try again.")
