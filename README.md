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
