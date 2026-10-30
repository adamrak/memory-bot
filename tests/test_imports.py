"""Smoke test: every module imports without a Discord token or API keys (catches wiring mistakes before deploy)."""
import importlib

import pytest


@pytest.mark.parametrize("module", [
    "memorybot.config", "memorybot.paths", "memorybot.triggers", "memorybot.text", "memorybot.models",
    "memorybot.changes", "memorybot.files", "memorybot.store", "memorybot.gemini", "memorybot.history",
    "memorybot.ui", "memorybot.bot",
])
def test_module_imports(module):
    importlib.import_module(module)


def test_bot_commands_registered():
    from memorybot.bot import bot
    assert {c.name for c in bot.commands} >= {"sync", "undo", "ping"}
