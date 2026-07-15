# LaValienteService

Backend de La Valiente, construido con FastAPI, PostgreSQL, SQLAlchemy async y Alembic.

## Desarrollo

La configuración se recibe exclusivamente mediante variables de entorno; no se versionan archivos
`.env`. Como mínimo, define `DATABASE_URL` y `JWT_SECRET_KEY`. `DATABASE_URL` puede usar
`postgresql://`, `postgres://` o `postgresql+asyncpg://`; el servicio selecciona el driver async
y Alembic usa automáticamente `psycopg`.

Instala las dependencias y levanta el servicio:

```bash
poetry install
poetry run fastapi dev src/main.py
```

El comando de desarrollo usa el CLI de FastAPI, no Uvicorn directamente.

## Base de datos

Las migraciones están preparadas pero **no se han ejecutado**. Cuando PostgreSQL esté disponible:

```bash
poetry run alembic upgrade head
```

Después de aplicar las migraciones, crea el primer administrador con variables de entorno
`BOOTSTRAP_ADMIN_EMAIL` y `BOOTSTRAP_ADMIN_PASSWORD`:

```bash
poetry run python -m scripts.create_superuser
```

El script crea (si no existen) el rol de sistema `system_admin` y los permisos administrativos
necesarios. Los permisos y roles posteriores se administran por API.

## Entorno Docker de desarrollo

`docker-compose.dev.yml` levanta la API y PostgreSQL `18.4-alpine` en contenedores separados.
La imagen está fijada a la versión menor actual para evitar cambios inesperados, y el volumen se
monta en la ruta de datos específica de PostgreSQL 18.
PostgreSQL se expone en `localhost:5433`, para no competir con una instalación local que use
el puerto predeterminado `5432`; desde la API Docker se mantiene en `postgres:5432`.

Antes de iniciarlo define, en tu entorno, `POSTGRES_PASSWORD` y `JWT_SECRET_KEY`; de forma
opcional puedes definir `POSTGRES_USER` y `POSTGRES_DB`. Después usa:

```bash
docker compose -f docker-compose.dev.yml up --build
```

Esto no ejecuta Alembic. Cuando decidas aplicar las migraciones, hazlo explícitamente desde el
contenedor `api` con `poetry run alembic upgrade head`.
