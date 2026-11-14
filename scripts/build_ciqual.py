"""Construit running_coach/data/ciqual_2020.csv depuis la table Ciqual 2020 de l'ANSES (XML officiel).

Source : « Table de composition nutritionnelle des aliments Ciqual 2020 », ANSES, Licence Ouverte (Etalab),
https://www.data.gouv.fr/fr/datasets/table-de-composition-nutritionnelle-des-aliments-ciqual/
(archive XML_2020_07_07.zip, à décompresser dans un dossier).

Usage : python scripts/build_ciqual.py <dossier contenant alim_2020_07_07.xml et compo_2020_07_07.xml>
"""
import csv
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "running_coach" / "data" / "ciqual_2020.csv"
# code constituant Ciqual → colonne (par 100 g)
NUTRIENTS = {"328": "kcal", "25000": "protein_g", "31000": "carbs_g", "40000": "fat_g",
             "32000": "sugars_g", "34100": "fiber_g", "10004": "salt_g"}
KCAL_FALLBACK = "333"   # énergie N x facteur Jones, quand la valeur réglementaire manque


def _read(path: Path) -> str:
    raw = path.read_bytes()
    enc = re.search(rb'encoding="([^"]+)"', raw[:200]).group(1).decode()
    return raw.decode(enc)


def _value(text: str) -> float | None:
    """« 12,3 » → 12.3 ; « < 0,5 » → 0.25 (la moitié du seuil) ; « traces » → 0 ; « - » → inconnu."""
    text = text.strip().replace(",", ".")
    if not text or text == "-":
        return None
    if text.lower() == "traces":
        return 0.0
    if text.startswith("<"):
        try:
            return float(text[1:].strip()) / 2
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def main(folder: str) -> None:
    folder = Path(folder)
    alim = _read(next(folder.glob("alim_2*.xml")))
    names = dict(re.findall(r"<alim_code>\s*(\d+)\s*</alim_code>\s*<alim_nom_fr>\s*(.*?)\s*</alim_nom_fr>", alim, re.S))
    values: dict[str, dict[str, float | None]] = {code: {} for code in names}
    compo = _read(next(folder.glob("compo_2*.xml")))
    for code, const, teneur in re.findall(
            r"<alim_code>\s*(\d+)\s*</alim_code>\s*<const_code>\s*(\d+)\s*</const_code>\s*<teneur>(.*?)</teneur>", compo, re.S):
        if code in values and (const in NUTRIENTS or const == KCAL_FALLBACK):
            values[code][const] = _value(teneur)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", *NUTRIENTS.values()])
        for code, name in sorted(names.items(), key=lambda kv: int(kv[0])):
            v = values[code]
            if v.get("328") is None:
                v["328"] = v.get(KCAL_FALLBACK)
            if v.get("328") is None:
                continue   # sans énergie, inutilisable pour le suivi
            w.writerow([code, " ".join(name.split()),
                        *("" if v.get(c) is None else f"{v[c]:g}" for c in NUTRIENTS)])
            rows += 1
    print(f"{rows} aliments → {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
