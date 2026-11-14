"""Nutrition : table Ciqual, repas pesés, profil, bilan énergétique, /nutrition."""
import datetime

from running_coach import commands, nutrition, tools
from running_coach.config import TZ
from running_coach.memory import Store


def test_ciqual_search_prefers_the_right_generic_food():
    assert nutrition.search("pâtes cuites", 1)[0]["name"] == "Pâtes sèches standard, cuites, non salées"
    assert nutrition.search("riz blanc cuit", 1)[0]["name"] == "Riz blanc, cuit, non salé"
    assert nutrition.search("lentilles cuites", 1)[0]["name"].startswith("Lentille")
    assert nutrition.search("thon au naturel", 1)[0]["name"].startswith("Thon, au naturel")
    assert len(nutrition.foods()) > 2000


def test_compute_meal_from_ciqual():
    out = nutrition.compute([{"food": "pâtes cuites", "grams": 250}, {"food": "thon au naturel", "grams": 100},
                             {"food": "xyzzy", "grams": 20}])
    pasta, tuna = out["items"]
    assert pasta["ciqual"].startswith("Pâtes sèches") and pasta["kcal"] == 315.0      # 126 kcal / 100 g
    assert tuna["protein_g"] == 26.8 and out["unmatched"] == ["xyzzy"]
    assert out["totals"]["kcal"] == 426
    by_code = nutrition.compute([{"food": "n'importe", "grams": 100, "code": "9104"}])   # le code choisi prime
    assert by_code["items"][0]["ciqual"] == "Riz blanc, cuit, non salé"


def test_log_meal_tool_stores_items_and_nutrients(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(tools, "store", store)
    out = tools.tool_log_meal({"description": "Pâtes au thon", "time": "2026-11-13T12:30:00",
                               "items": [{"food": "pâtes cuites", "grams": 250}, {"food": "thon au naturel", "grams": 100}]},
                              tools.Ledger())
    assert out["ok"] and out["items"] == ["250 g Pâtes sèches standard, cuites, non salées",
                                          "100 g Thon, au naturel, appertisé, égoutté"]
    meal = store.meals_since(datetime.date(2026, 11, 13))[-1]
    assert meal["slot"] == "déjeuner" and meal["kcal"] == 426 and meal["fat_g"] is not None
    assert [i["grams"] for i in meal["items"]] == [250, 100]
    found = tools.tool_find_food({"queries": ["fromage râpé"]}, tools.Ledger())["per_100g"]["fromage râpé"]
    assert found and {"code", "name", "kcal"} <= set(found[0])


def test_profile_and_energy_balance(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(tools, "store", store)
    today = datetime.date(2026, 11, 14)
    assert "profil incomplet" in nutrition.energy_context([], [], {}, today)
    tools.tool_set_profile({"weight_kg": 70, "height_cm": 178, "birth_year": 1996, "sex": "homme"}, tools.Ledger())
    profile = store.profile
    assert nutrition.bmr(profile, today) == 1667.5                                      # Mifflin-St Jeor
    run = {"startDateLocal": "2026-11-14T08:00:00", "distanceInMeter": 10000, "calories": 650}
    for t, items in (("08:00", [{"food": "flocons d'avoine", "grams": 60}]),
                     ("12:30", [{"food": "pâtes cuites", "grams": 250}])):
        out = nutrition.compute(items)
        store.log_meal("repas", datetime.datetime.fromisoformat(f"2026-11-14T{t}:00").replace(tzinfo=TZ),
                       **out["totals"], items=out["items"])
    ctx = nutrition.energy_context(store.meals_since(today), [run], profile, today)
    assert "aujourd'hui (jusqu'ici)" in ctx and "2 repas (déjeuner, petit-déjeuner)" in ctx
    assert f"besoins ~{1667.5 * 1.4 + 650:.0f} kcal (dont course 650)" in ctx and "il reste" in ctx
    assert "Repères : protéines 98-126 g/j" in ctx
    text = commands.format_nutrition(store.meals_since(today), [run], profile, today)
    assert text.startswith("🍽️ *Aujourd'hui*") and "encore ~" in text and "Ciqual" in text


def test_chosen_dish_carries_computed_nutrients(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(tools, "store", store)
    out = tools.tool_propose_meal({"title": "🍝 Pâtes au thon", "recipe": "1. Cuire", "uses": [],
                                   "items": [{"food": "pâtes cuites", "grams": 250}]}, tools.Ledger())
    macros = store.proposal(out["meal_id"])["macros"]
    assert macros["kcal"] == 315 and macros["items"][0]["grams"] == 250 and out["portion"]["kcal"] == 315
    meal = store.log_meal("🍝 Pâtes au thon", datetime.datetime(2026, 11, 14, 20, 0, tzinfo=TZ), **macros)
    assert meal["kcal"] == 315 and meal["slot"] == "dîner"
