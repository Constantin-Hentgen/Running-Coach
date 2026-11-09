FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app

# Dépendances d'abord (couche mise en cache tant que pyproject/uv.lock ne changent pas)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY running_coach ./running_coach

# /data : mémoire du coach + historique (volume nommé, hérite de ces droits à sa création)
RUN useradd --system --no-create-home coach && mkdir -p /data && chown coach /data
ENV COACH_DATA_DIR=/data
# Modèle Whisper (vocaux) et cache Hugging Face dans le volume : téléchargés une fois, gardés entre redéploiements
ENV HF_HOME=/data/huggingface
VOLUME /data
USER coach

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "running_coach.app:app", "--host", "0.0.0.0", "--port", "8000"]
