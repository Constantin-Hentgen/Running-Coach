"""Messages vocaux Telegram → texte, transcrits LOCALEMENT avec faster-whisper : l'audio ne quitte pas le serveur.

Claude ne prend pas d'audio en entrée : le vocal est transcrit ici, puis le texte est traité comme un message écrit.
Le modèle (WHISPER_MODEL, « small » par défaut, bon compromis en français sur CPU) est téléchargé au
premier vocal dans le volume /data (il survit donc aux redéploiements), puis gardé en mémoire.
"""
import io
import logging
import threading
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

MAX_SECONDS = 300
# Amorce de vocabulaire : oriente Whisper vers le jargon running (sinon « sortie longue » peut devenir n'importe quoi).
VOCABULARY = {
    "fr": ("Message vocal à mon coach de course à pied : séance, sortie longue, fractionné, footing, seuil, "
           "allure, VMA, récupération, décale, déplace, agenda, courbatures, semi-marathon, trail."),
    "en": ("Voice message to my running coach: session, long run, intervals, easy run, tempo, threshold, pace, "
           "recovery, reschedule, move, calendar, sore legs, half marathon, trail."),
}[settings.language]
_lock = threading.Lock()   # un seul modèle chargé, une transcription à la fois
_model = None


def enabled() -> bool:
    return bool(settings.whisper_model)


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel   # import paresseux : lourd, et inutile si les vocaux sont désactivés

        root = Path(settings.data_dir) / "whisper"
        logger.info(f"[VOIX] Chargement du modèle Whisper « {settings.whisper_model} » "
                    f"(premier usage : téléchargement dans {root})…")
        _model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8", download_root=str(root))
        logger.info("[VOIX] Modèle prêt.")
    return _model


def transcribe(audio: bytes) -> str:
    """Audio Telegram (ogg/opus, mp3, m4a…) → texte, dans la langue du bot. Bloquant : à appeler dans un thread."""
    with _lock:
        segments, __ = _load().transcribe(io.BytesIO(audio), language=settings.language, vad_filter=True, initial_prompt=VOCABULARY)
        return " ".join(seg.text.strip() for seg in segments).strip()
