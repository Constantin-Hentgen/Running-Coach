"""Garde-fous de configuration : toute variable lue par le code doit être déclarée partout."""
import os
import re
from pathlib import Path

import yaml

from running_coach import config

ROOT = Path(__file__).resolve().parent.parent
CODE_VARS = {
    m for f in (ROOT / "running_coach").glob("*.py")
    for m in re.findall(r'(?:_str|_int|_float|getenv)\("([A-Z_]+)"', f.read_text())
}
INTERNAL = {"TZ", "COACH_DATA_DIR"}   # fixés par docker-compose.yml lui-même


def test_every_code_variable_is_passed_by_docker_compose():
    env = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["running-coach"]["environment"]
    missing = CODE_VARS - set(env)
    assert not missing, f"à ajouter dans docker-compose.yml : {sorted(missing)}"


def test_every_code_variable_is_documented_in_env_example():
    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    legacy = {"DREEVE_API_URL", "WEATHER_LAT", "WEATHER_LON", "TZ"}   # anciens noms, encore acceptés
    missing = CODE_VARS - documented - legacy
    assert not missing, f"à documenter dans .env.example : {sorted(missing)}"


def test_str_strips_quotes_and_treats_empty_as_unset(monkeypatch):
    monkeypatch.setenv("X_TEST", '"contrainte: a | objectif: b"')
    assert config._str("X_TEST") == "contrainte: a | objectif: b"
    monkeypatch.setenv("X_TEST", "")
    assert config._str("X_TEST", "def") == "def"


def test_briefing_off():
    assert config._briefing_time("off") is None and config._briefing_time("07:30") == (7, 30)
