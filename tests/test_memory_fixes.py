import os

import pytest

from running_coach.memory import MAX_ITEMS, Store


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state.json"


def texts(s):
    return [m["text"] for m in s.memories]


def test_failed_replace_keeps_replaced_memory(path):
    s = Store(path)
    a = s.remember("autre", "Footings de 4,5 km minimum")
    s.remember("autre", "Pas deux sorties consécutives")
    with pytest.raises(ValueError, match="déjà mémorisé"):
        s.remember("autre", "Pas deux sorties consécutives", replaces=a["id"])
    assert "Footings de 4,5 km minimum" in texts(s)
    assert "Footings de 4,5 km minimum" in texts(Store(path))


def test_replace_with_unique_text(path):
    s = Store(path)
    a = s.remember("autre", "ancien")
    b = s.remember("autre", "nouveau", replaces=a["id"])
    assert texts(s) == ["nouveau"] and b["id"] != a["id"]
    assert texts(Store(path)) == ["nouveau"]


def test_replace_with_same_text_as_replaced_is_allowed(path):
    s = Store(path)
    a = s.remember("autre", "identique")
    s.remember("autre", "Identique", replaces=a["id"])
    assert texts(s) == ["Identique"]


def test_unknown_replaces_raises_without_change(path):
    s = Store(path)
    s.remember("autre", "un")
    before = list(s.memories)
    next_id = s.state["next_id"]
    with pytest.raises(LookupError):
        s.remember("autre", "deux", replaces=999)
    assert s.memories == before and s.state["next_id"] == next_id


def test_full_memory_replace_frees_a_slot(path):
    s = Store(path)
    ids = [s.remember("autre", f"souvenir {i}")["id"] for i in range(MAX_ITEMS)]
    with pytest.raises(ValueError, match="pleine"):
        s.remember("autre", "en trop")
    assert len(s.memories) == MAX_ITEMS
    s.remember("autre", "remplaçant", replaces=ids[0])
    assert len(s.memories) == MAX_ITEMS and "remplaçant" in texts(s) and "souvenir 0" not in texts(s)


def test_full_memory_failed_replace_changes_nothing(path):
    s = Store(path)
    ids = [s.remember("autre", f"souvenir {i}")["id"] for i in range(MAX_ITEMS)]
    with pytest.raises(ValueError, match="déjà mémorisé"):
        s.remember("autre", "souvenir 1", replaces=ids[0])
    assert len(s.memories) == MAX_ITEMS and "souvenir 0" in texts(s)


def test_corrupt_file_gets_timestamped_backup(path):
    path.write_text("{pas du json", encoding="utf-8")
    s = Store(path)
    assert s.memories == []
    backups = list(path.parent.glob("state.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{pas du json"
    assert not path.exists()


def test_two_corruptions_keep_two_backups(path):
    path.write_text("corrompu 1", encoding="utf-8")
    Store(path)
    path.write_text("corrompu 2", encoding="utf-8")
    Store(path)
    backups = list(path.parent.glob("state.json.corrupt-*"))
    assert sorted(b.read_text(encoding="utf-8") for b in backups) == ["corrompu 1", "corrompu 2"]


def test_failed_save_sets_error_and_does_not_raise(path, monkeypatch):
    s = Store(path)
    assert s.last_save_error is None

    def boom(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(os, "replace", boom)
    s.remember("autre", "survit en mémoire vive")   # ne lève pas
    assert "disque plein" in s.last_save_error
    monkeypatch.undo()
    s.remember("autre", "autre souvenir")
    assert s.last_save_error is None
    assert texts(Store(path)) == ["survit en mémoire vive", "autre souvenir"]


def test_save_in_unwritable_location_does_not_raise(tmp_path):
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    s = Store(tmp_path / "state.json")
    s.path = blocker / "sous" / "state.json"   # le parent est un fichier : mkdir échoue
    s.remember("autre", "test")
    assert s.last_save_error
