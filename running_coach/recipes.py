"""Recettes de base libres : le Livre de cuisine de Wikibooks (CC BY-SA), via l'API MediaWiki (sans clé), dans la langue
du bot (BOT_LANGUAGE) : « Livre de cuisine » de fr.wikibooks, ou « Cookbook » de en.wikibooks.

Sert à ancrer les recettes de /eat dans une source ouverte : le coach s'en inspire, la simplifie et l'adapte au stock,
puis cite le lien. Seul le nom du plat part dans la recherche.
"""
import logging
import re
import time
from urllib.parse import quote

import requests

from .config import settings

logger = logging.getLogger(__name__)

# Par langue : (site, préfixe des pages de recettes, espace de noms de recherche, pages de sommaire à écarter)
SOURCES = {
    "fr": ("https://fr.wikibooks.org", "Livre de cuisine/", 0,
           re.compile(r"/(?:Liste|Recettes?\b|Catégorie|Sommaire|Index|Intersection)", re.I)),
    "en": ("https://en.wikibooks.org", "Cookbook:", 102,
           re.compile(r":(?:Table of Contents|Recipes\b|Cuisine\b|Ingredients\b|Equipment\b|Techniques\b)", re.I)),
}
SITE, PREFIX, NAMESPACE, NOT_A_RECIPE = SOURCES[settings.language]
API_URL = f"{SITE}/w/api.php"
USER_AGENT = "running-coach-bot/1.0 (self-hosted personal assistant)"
MAX_RESULTS = 2
MAX_CHARS = 1500
CACHE_S = 24 * 3600          # une recherche par plat et par jour suffit
BACKOFF_S = 600              # après un refus (429), on laisse Wikibooks tranquille 10 min
_cache: dict[str, tuple[float, list[dict]]] = {}
_blocked_until = 0.0
STOPWORDS = {"au", "aux", "à", "la", "le", "les", "de", "des", "du", "et", "en", "avec", "sans", "d", "l", "un", "une",
             "and", "with", "the", "of", "in", "a", "an", "without"}


def _get(**params) -> dict:
    res = requests.get(API_URL, params={**params, "format": "json"}, headers={"User-Agent": USER_AGENT},
                       timeout=settings.http_timeout)
    res.raise_for_status()
    return res.json()


def page_url(title: str) -> str:
    return f"{SITE}/wiki/" + quote(title.replace(" ", "_"), safe=":/")


def search(query: str) -> list[dict]:
    """Les recettes les plus proches : [{title, url, text}] (ingrédients et préparation, texte brut tronqué).
    Liste vide si rien ne colle ou si Wikibooks ne répond pas : le coach écrit alors la recette lui-même."""
    global _blocked_until
    query = " ".join((query or "").split())[:80]
    key = query.casefold()
    if not query or time.monotonic() < _blocked_until:
        return []
    if key in _cache and time.monotonic() - _cache[key][0] < CACHE_S:
        return _cache[key][1]
    try:
        out = _search(query)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _blocked_until = time.monotonic() + BACKOFF_S
        logger.warning(f"Wikibooks indisponible ({query}) : {e}")
        return []
    except Exception as e:
        logger.warning(f"Wikibooks indisponible ({query}) : {e}")
        return []
    _cache[key] = (time.monotonic(), out)
    return out


def _search(query: str) -> list[dict]:
    # le nom du plat dans le titre d'abord (tous les mots, puis le mot principal), le texte intégral en dernier recours
    words = [w for w in re.findall(r"[\wÀ-ÿ'-]+", query.lower()) if w not in STOPWORDS and len(w) > 2]
    attempts = ([" ".join(f"intitle:{w}" for w in words)] if len(words) > 1 else []) + \
               ([f"intitle:{words[0]}"] if words else []) + [query]
    titles = []
    for attempt in attempts:
        found = _get(action="query", list="search", srsearch=attempt, srnamespace=NAMESPACE, srlimit=10)
        titles += [r["title"] for r in found.get("query", {}).get("search", [])
                   if r["title"].startswith(PREFIX) and not NOT_A_RECIPE.search(r["title"]) and r["title"] not in titles]
        if len(titles) >= MAX_RESULTS:
            break
    titles = titles[:MAX_RESULTS]
    if not titles:
        return []
    pages = _get(action="query", prop="extracts", explaintext=1, titles="|".join(titles))
    extracts = {p.get("title"): (p.get("extract") or "").strip() for p in pages.get("query", {}).get("pages", {}).values()}
    out = []
    for title in titles:
        text = re.sub(r"\n{3,}", "\n\n", extracts.get(title) or "")
        if text:
            out.append({"title": title.removeprefix(PREFIX), "url": page_url(title),
                        "text": text[:MAX_CHARS] + ("…" if len(text) > MAX_CHARS else "")})
    return out
