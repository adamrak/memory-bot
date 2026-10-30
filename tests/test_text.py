from memorybot.text import extract_attach_lines, split_message


def test_split_message_prefers_line_breaks():
    text = "\n".join(f"line {i:03}" for i in range(400))  # ~3600 chars
    chunks = split_message(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n".join(chunks) == text
    assert all(c.startswith("line") for c in chunks)


def test_split_message_hard_cuts_long_lines():
    chunks = split_message("x" * 4500, limit=2000)
    assert [len(c) for c in chunks] == [2000, 2000, 500]


def test_split_message_short_and_empty():
    assert split_message("hi") == ["hi"]
    assert split_message("") == []


def test_extract_attach_lines():
    answer = (
        "Gombík je vpravo dole (📁 domacnost/kurenie.md).\n"
        "ATTACH: /memory/domacnost/2026-10-30-gombik.jpg\n"
        "ATTACH: domacnost/neexistuje.jpg\n"
        "ATTACH: domacnost/2026-10-30-gombik.jpg\n"
    )
    text, paths = extract_attach_lines(answer, {"domacnost/2026-10-30-gombik.jpg": ("sha", 1)})
    assert text == "Gombík je vpravo dole (📁 domacnost/kurenie.md)."
    assert paths == ["domacnost/2026-10-30-gombik.jpg"]


def test_extract_attach_lines_caps_files():
    files = {f"f{i}.jpg": ("s", 1) for i in range(5)}
    _, paths = extract_attach_lines("\n".join(f"ATTACH: f{i}.jpg" for i in range(5)), files)
    assert paths == ["f0.jpg", "f1.jpg", "f2.jpg"]
