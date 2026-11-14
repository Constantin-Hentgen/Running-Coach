"""Langue du bot : chaque texte `_("…")` du code a sa traduction anglaise, avec les mêmes champs {…}."""
import ast
import pathlib
import string

import pytest

from running_coach import i18n
from running_coach.locales import en

ROOT = pathlib.Path(__file__).resolve().parent.parent / "running_coach"


def _wrapped() -> list[tuple[str, int, str]]:
    out = []
    for path in sorted(ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_":
                arg = node.args[0] if node.args else None
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                    f"{path.name}:{node.lineno} : _() prend un texte littéral (pas de f-string), puis .format()"
                out.append((path.name, node.lineno, arg.value))
    return out


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_every_wrapped_text_has_an_english_translation():
    missing = [f"{name}:{line} {text!r}" for name, line, text in _wrapped() if text not in en.MESSAGES]
    assert not missing, "Traductions manquantes dans locales/en.py :\n" + "\n".join(missing)


def test_translations_keep_the_same_placeholders_and_none_is_unused():
    used = {text for _, _, text in _wrapped()}
    for fr, eng in en.MESSAGES.items():
        assert _fields(fr) == _fields(eng), f"champs différents : {fr!r} → {eng!r}"
    unused = set(en.MESSAGES) - used
    assert not unused, f"traductions inutilisées : {sorted(unused)[:10]}"


@pytest.mark.parametrize("lang, expected", [("fr", "Ok 👌"), ("en", "OK 👌")])
def test_translate_follows_bot_language(monkeypatch, lang, expected):
    monkeypatch.setitem(en.MESSAGES, "Ok 👌", "OK 👌")
    object.__setattr__(i18n.settings, "language", lang)
    try:
        assert i18n._("Ok 👌") == expected
        assert i18n._("texte sans traduction") == "texte sans traduction"
    finally:
        object.__setattr__(i18n.settings, "language", "fr")


def test_no_throwaway_underscore_shadows_the_translation_function():
    """`_` est la fonction de traduction : jamais une variable (`for __, x in …` pour ignorer une valeur)."""
    stores = [f"{path.name}:{n.lineno}" for path in sorted(ROOT.rglob("*.py"))
              for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
              if isinstance(n, ast.Name) and n.id == "_" and isinstance(n.ctx, ast.Store)]
    assert not stores, f"`_` utilisé comme variable : {stores}"


ENGLISH_RUN = r'''
import datetime
from running_coach import agent, commands, recipes, routes, voice, weather, weather_alerts as wa
from running_coach.config import TZ, settings
from running_coach.handlers import ui
day = datetime.date(2026, 11, 14)
meals = [{"id": 1, "date": "2026-11-14", "time": "08:00", "text": "Porridge", "kcal": 450, "protein_g": 15, "carbs_g": 70, "fat_g": 10}]
profile = {"weight_kg": 70, "height_cm": 178, "birth_year": 1995, "sex": "m"}
lp = routes.Loop(points=[], km=6.24, quiet=.5, calm=.4, busy=0, paved=1, crossings=12, marked=.3)
n = wa.Notice("alert", key="k", uid="u", summary="Easy run", start=datetime.datetime(2026, 11, 15, 18, tzinfo=TZ),
              end=datetime.datetime(2026, 11, 15, 19, tzinfo=TZ), place="Home", hazards={"pluie": "80 %, 2.0 mm"}, others={"Work": {}})
daily = {"time": ["2026-11-15"], "weathercode": [61], "temperature_2m_min": [4.0], "temperature_2m_max": [11.0],
         "precipitation_sum": [2.4], "precipitation_probability_max": [80], "windspeed_10m_max": [20.0]}
out = [commands.HELP_TEXT, *(c.description for c in commands.BOT_COMMANDS),
       commands.format_day_recap(meals, [], profile, day), commands.format_nutrition(meals, [], profile, day),
       commands.format_agenda([], 7), commands.format_stock_reply([], day), commands.format_goals_reply([], day),
       commands.format_form({"tsb": {}, "acRatio": {}}, [], day), routes.describe(lp, "route"), routes.describe(lp, "nature"),
       wa.format_notice(n, datetime.datetime(2026, 11, 14, 20, tzinfo=TZ)), weather.format_forecast([daily], ["Home"], {"Home": []}),
       ui.PROPOSAL_OUTCOME["a"], ui.MEAL_HINT, *ui.LEG_LABELS.values()]
print("\n@@\n".join(out))
print("@@LANG@@", agent.SYSTEM_PROMPT.startswith("LANGUE (IMPÉRATIF)"), recipes.API_URL, settings.language, voice.VOCABULARY[:5])
'''


def test_english_bot_speaks_english_end_to_end(tmp_path):
    import os
    import re
    import subprocess
    import sys
    env = {**os.environ, "BOT_LANGUAGE": "en", "COACH_DATA_DIR": str(tmp_path)}
    res = subprocess.run([sys.executable, "-c", ENGLISH_RUN], capture_output=True, text=True, env=env,
                         cwd=ROOT.parent, timeout=120)
    assert res.returncode == 0, res.stderr
    text, lang = res.stdout.split("@@LANG@@")
    assert lang.split() == ["True", "https://en.wikibooks.org/w/api.php", "en", "Voice"]
    assert "Your day on the plate" in text and "12 crossings" in text and "Home: ☔ rain" in text and "Sun 15/11" in text
    french = re.findall(r"\b(?:tu|ton|ta|tes|je|séances?|repas|boîtes?|aujourd'hui|jours?|pluie|besoins|sortie|"
                        r"lundi|dimanche|dans|avec|pour|sur)\b", text, re.I)
    assert not french, f"mots français dans la sortie anglaise : {sorted(set(french))}"
