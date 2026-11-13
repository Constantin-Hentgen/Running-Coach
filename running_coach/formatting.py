"""Mise en forme Telegram (Markdown « legacy »)."""
import re

TELEGRAM_LIMIT = 3900   # marge sous la limite de 4096 caractères

_MD_SPECIAL = re.compile(r"([_*`\[])")
_MD_ESCAPED = re.compile(r"\\([_*`\[])")


def md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_unescape(text: str) -> str:
    return _MD_ESCAPED.sub(r"\1", text)


def bubbles(text: str) -> list[str]:
    """Un message par paragraphe (séparés par une ligne vide), comme dans une conversation ; un paragraphe trop long
    pour Telegram est lui-même découpé."""
    parts = [p.strip("\n") for p in re.split(r"\n[ \t]*\n", text or "") if p.strip()]
    return [chunk for p in parts for chunk in split_message(p)]


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Découpe par lignes ; une ligne plus longue que la limite est elle-même coupée."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks or [text]
