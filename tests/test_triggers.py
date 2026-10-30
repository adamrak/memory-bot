import pytest

from memorybot.triggers import is_edit_command, is_revert_command, is_save_command, wants_pdf_read


@pytest.mark.parametrize("text, kind", [
    ("zapíš si, že STK končí v marci", "save"),
    ("Zapis si to do auto/stk", "save"),
    ("ulož túto fotku", "save"),
    ("doplň k pračke model", "save"),
    ("save: note", "save"),
    ("oprav, STK končí až v apríli", "edit"),
    ("premenuj priecinok akaso-brave na akaso-brave-go-pro", "edit"),
    ("presuň akaso do vlastného priečinka", "edit"),
    ("zmaž poznámku o žiarovkách", "edit"),
    ("zmeň dátum STK", "edit"),
    ("rename folder", "edit"),
    ("vráť poslednú zmenu", "revert"),
    ("Undo", "revert"),
    ("revert", "revert"),
    ("kedy mi končí STK?", "answer"),
    ("zmena oleja kedy?", "answer"),
    ("vrátim sa k tomu neskôr", "answer"),
    ("savings account?", "answer"),
    ("čo je v podkroví? zapíš", "answer"),  # command words count only at the start
])
def test_command_routing(text, kind):
    detected = [k for k, f in (("save", is_save_command), ("edit", is_edit_command), ("revert", is_revert_command)) if f(text)]
    assert detected == ([] if kind == "answer" else [kind])


@pytest.mark.parametrize("text, expected", [
    ("prečítaj ten PDF a zapíš hlavné údaje", True),
    ("zapíš si to, analyzuj návod", True),
    ("please read the manual", True),
    ("zapíš si tento záručák", False),
    ("ready?", False),
])
def test_wants_pdf_read(text, expected):
    assert wants_pdf_read(text) is expected
