# Imagen de producción, y la misma que corre en staging a propósito: si lo que
# se prueba no es el binario que se despliega, staging deja de responder la única
# pregunta para la que existe. Entre entornos cambian las variables, nunca este
# archivo (ver docs/DEPLOYMENT.md).

# ---------------------------------------------------------------------------
# Etapa 1 — dependencias
#
# Poetry, el compilador y la caché de ruedas se quedan aquí. Lo único que cruza
# a la imagen final es el virtualenv ya resuelto, que es lo que hace la
# diferencia entre ~1 GB y ~250 MB en el registro y en cada arranque en frío.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

# La versión de Poetry es la que generó `poetry.lock`; una distinta puede
# resolver distinto y entonces la imagen deja de ser reproducible.
ENV POETRY_VERSION=2.4.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# Sólo los archivos de dependencias antes del código: mientras el lock no
# cambie, un `git push` que toca `src/` reusa esta capa y el build dura
# segundos en vez de minutos.
COPY pyproject.toml poetry.lock README.md ./

# `--only main` deja fuera pytest, mypy y testcontainers: en producción no se
# corren y cada uno es superficie de ataque que no hace falta cargar.
# `--no-root` porque el proyecto no se instala como paquete, se copia y se
# ejecuta desde `/app`.
RUN poetry install --only main --no-root --no-ansi

# ---------------------------------------------------------------------------
# Etapa 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONPATH=/app \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    APP_USER=app \
    HOST=0.0.0.0 \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    MEDIA_DIR=/data/media \
    SCAN_STORAGE_PATH=/data/media/scans

# `gosu` es para soltar privilegios en el entrypoint. Hace falta porque el
# volumen persistente se monta como root y el proceso corre sin privilegios:
# alguien tiene que ajustar el dueño antes de que la API escriba la primera
# foto (ver docker/entrypoint.sh).
RUN apt-get update \
    && apt-get install --no-install-recommends -y gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv

# Rutas explícitas y no `COPY . .`: así un `.env`, la carpeta `media/` con
# fotos de clientes o cualquier archivo suelto del entorno de trabajo no
# terminan dentro de la imagen aunque `.dockerignore` se quede corto.
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root src ./src
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# El `sed` quita los CR de un checkout hecho en Windows: con finales CRLF el
# kernel busca el intérprete "/bin/sh\r" y el contenedor muere con un
# "no such file or directory" que no dice nada. `.gitattributes` ya fuerza LF;
# esto es el cinturón sobre los tirantes.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
    && chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p "${MEDIA_DIR}" "${SCAN_STORAGE_PATH}" \
    && chown -R app:app /data

# El código es de root y el proceso corre como `app`: la aplicación no puede
# reescribirse a sí misma. Lo único que le pertenece es `/data`.

EXPOSE 8000

# Para Docker/Compose. Railway usa su propio healthcheck HTTP (`healthcheckPath`
# en railway.json) y este lo ignora. Sin `curl` en la imagen: una línea de
# Python evita otro paquete que parchear.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
