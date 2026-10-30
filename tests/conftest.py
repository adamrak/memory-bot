import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

# Tests assume the default layout (notes under memory/), regardless of the developer's environment.
os.environ.pop("MEMORY_DIR", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorybot.store import MemoryStore  # noqa: E402
from tests.fake_github import FakeGitHub  # noqa: E402

PDF = b"%PDF-1.4 fake manual"
AKASO_NOTE = (
    "### 2026-10-30 – Akaso Brave 7\n"
    "Kamera je v spálni.\n\n"
    "📎 [2026-10-30-navod.pdf](/memory/elektronika/akaso-brave/2026-10-30-navod.pdf)\n"
)


@pytest.fixture
def initial_files() -> dict[str, bytes | str]:
    return {
        "README.md": "repo readme, outside memory/",
        "memory/auto/stk.md": "### STK\nKončí 15. 3. 2027.\n",
        "memory/elektronika/akaso-brave/index.md": AKASO_NOTE,
        "memory/elektronika/akaso-brave/2026-10-30-navod.pdf": PDF,
    }


@pytest.fixture
async def github(initial_files):
    fake = FakeGitHub(initial_files)
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


@pytest.fixture
async def store(github):
    s = MemoryStore("owner/repo", "token", api_url=github.url)
    await s.start()
    await s.sync()
    yield s
    await s.close()
