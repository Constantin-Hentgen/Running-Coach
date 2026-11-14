"""Langue du bot (BOT_LANGUAGE) pour les messages écrits par le code.

Le texte français est la clé : `_("Bon app' 😋")` renvoie la traduction de la langue choisie, ou le français s'il n'y
en a pas. Les textes avec des valeurs utilisent des champs nommés : `_("Il t'en reste {n}.").format(n=...)`.
Les messages écrits par le modèle ne passent pas par ici : le prompt système lui donne la langue (LANGUAGE_RULE).
Le test test_i18n vérifie que chaque `_("…")` du code a sa traduction.
"""
from .config import LANGUAGES, settings
from .locales import en

CATALOGS = {"en": en.MESSAGES}


def _(text: str) -> str:
    return CATALOGS.get(settings.language, {}).get(text, text)


def language_name() -> str:
    """Nom de la langue du bot, pour le modèle (« français », « English »)."""
    return LANGUAGES[settings.language]


def decimal(text: str) -> str:
    """Séparateur décimal de la langue : « 6,2 » en français, « 6.2 » en anglais."""
    return text.replace(".", ",") if settings.language == "fr" else text


def plural(n: int) -> bool:
    """Pluriel ? En français 0 et 1 sont au singulier, en anglais seul 1 l'est."""
    return n > 1 if settings.language == "fr" else n != 1


def colon(label: str, value: str = "") -> str:
    """« Maison : pluie » en français, « Home: rain » en anglais (pas d'espace avant les deux-points)."""
    sep = " :" if settings.language == "fr" else ":"
    return f"{label}{sep} {value}" if value else f"{label}{sep}"
