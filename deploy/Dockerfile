# osu!MapMatcher API.
#
# Two targets from one file:
#
#   runtime  (default)  serves the API. numpy, usearch, rosu-pp, fastapi.
#   builder             turns a corpus into an index. Adds pandas, pyarrow and
#                       scikit-learn — needed to *fit* a scaler, never to read
#                       one back, so the serving image stays free of them.
#
# The index is deliberately NOT baked in: ~290 MB per mod variant, on a
# different release schedule from the code. Mount /data.
#
#   docker build -f deploy/Dockerfile -t osu-mapmatcher-api .
#   docker run -p 8080:8080 -v osu-mapmatcher-index:/data osu-mapmatcher-api

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OSUMAPMATCHER_DATA=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

RUN useradd --system --create-home --uid 10001 mapmatcher \
    && mkdir -p /data \
    && chown -R mapmatcher:mapmatcher /data


# --- builder : fabrique un index, ne sert rien --------------------------------

FROM base AS builder
RUN pip install --no-cache-dir ".[api,corpus,index]"
USER mapmatcher
# a lancer explicitement, par variante :
#   docker compose --profile setup run --rm builder \
#     python scripts/build_index.py --corpus /data/corpus/DT --out /data/index/DT
CMD ["python", "scripts/build_all.py", "--help"]


# --- runtime : sert l'API, et rien d'autre ------------------------------------

FROM base AS runtime
RUN pip install --no-cache-dir ".[api]"
USER mapmatcher
VOLUME ["/data"]
EXPOSE 8080

# /health repond meme sans index (ready=false) : c'est la liveness, pas la
# readiness -- un conteneur sans index doit etre visible, pas redemarre en
# boucle
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=4)"

# aucun acces reseau sortant au demarrage : recuperer un index est une action
# explicite (`--profile setup run --rm fetch`), pas un effet de bord du boot
CMD ["python", "-m", "osumapmatcher.api", "--host", "0.0.0.0", "--port", "8080"]
