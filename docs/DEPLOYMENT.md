# Despliegue en Railway

Cómo se pone en línea el backend, y por qué está armado así. Dos entornos —
`production` y `staging`— dentro de **un solo proyecto** de Railway, cada uno con
su base de datos, su volumen y sus variables.

Archivos que participan:

| Archivo | Qué es |
| --- | --- |
| [`docker/Dockerfile`](../docker/Dockerfile) | La imagen. La misma en los dos entornos. |
| [`docker/entrypoint.sh`](../docker/entrypoint.sh) | Traduce `api`, `migrate` y `seed` al comando real, y suelta privilegios. |
| [`railway.json`](../railway.json) | Cómo construye y despliega Railway, con lo específico de staging aparte. |
| [`docker-compose.prod.yml`](../docker-compose.prod.yml) | La imagen de producción corriendo en tu máquina, para probarla antes. |

## Una sola imagen para los dos entornos

No hay `Dockerfile.staging`. Staging existe para responder una pregunta —*¿esto
va a funcionar en producción?*— y una imagen distinta la deja sin responder: lo
que se probó no sería lo que se despliega. Lo que cambia entre entornos son las
variables y un par de ajustes en `railway.json`, no el binario.

De ahí también que la imagen no lleve Poetry, ni pytest, ni el código montado
desde el disco. Son 246 MB, el proceso corre como el usuario `app` (uid 1001) y
lo único que puede escribir es el volumen: la aplicación no puede reescribirse a
sí misma.

## Antes de tocar Railway: probar la imagen en local

`docker-compose.dev.yml` no sirve para esto — usa otro Dockerfile, monta el
código y corre como root. La imagen real se prueba así:

```bash
export POSTGRES_PASSWORD=... JWT_SECRET_KEY=...

docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm api migrate
docker compose -f docker-compose.prod.yml run --rm api seed
docker compose -f docker-compose.prod.yml up
```

Luego `curl http://localhost:8000/health` debe responder `{"status":"ok"}`.
Postgres queda en `localhost:5434`, así que puede convivir con el entorno de
desarrollo (5433) y con una instalación local (5432).

## Montar el entorno en Railway

### 1. Proyecto y servicio de la API

1. Crea el proyecto y conéctalo al repositorio. Railway detecta `railway.json` y
   construye con `docker/Dockerfile`; no hace falta elegir builder.
2. En el servicio, **Settings → Source**: rama `main` para `production`.
3. Si el repositorio tiene varias carpetas (`BACKEND`, `APP`), define el
   **Root Directory** del servicio como `BACKEND`.

### 2. Base de datos

**+ New → Database → PostgreSQL** dentro del mismo entorno. Railway la
administra: respaldos, versiones y parches no son tuyos. No la levantes desde un
Dockerfile — un Postgres propio en un contenedor sin respaldos es la peor parte
de este despliegue esperando a fallar.

### 3. Volumen (antes del primer despliegue)

En el servicio de la API: **+ Volume**, punto de montaje `/data`.

`railway.json` declara `requiredMountPath: "/data"` y eso hace que un despliegue
**sin volumen falle a propósito**. Es intencional: las fotos de producto y las
de boletas viven en disco (solo la ruta llega a la base de datos), y sin volumen
el sistema de archivos del contenedor se borra en cada despliegue. Preferimos un
despliegue que no arranca a uno que arranca y pierde fotos en silencio.

Un volumen solo puede pertenecer a un servicio. Eso importa más adelante, cuando
haya que borrar escaneos vencidos.

### 4. Variables

En **Variables** del servicio de la API:

| Variable | Valor | Nota |
| --- | --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Referencia, no la cadena copiada: apunta a la red privada y sigue viva si Railway rota la contraseña. |
| `JWT_SECRET_KEY` | secreto largo y aleatorio | **Distinto en cada entorno.** Uno compartido significa que un token de staging abre producción. |
| `ENVIRONMENT` | `production` / `staging` | |
| `WEB_CONCURRENCY` | `1` | Ver «Cuántos workers». |
| `MEDIA_DIR` | `/data/media` | Ya es el valor de la imagen; defínelo solo si mueves el montaje. |
| `SCAN_STORAGE_PATH` | `/data/media/scans` | Igual que el anterior. |
| `SCAN_ENABLED` | `false` | Enciéndelo cuando la key esté puesta. |
| `GEMINI_API_KEY` | la key | Solo backend; la app nunca la ve (Plan 0003 D1). |
| `DOCS_ENABLED` | no la definas | Con `ENVIRONMENT=production`, `/docs` y `/openapi.json` no se publican. Ponla en `true` para abrirlos un rato sin desplegar código. |
| `CORS_ORIGINS` | no la definas | Solo hace falta si algún día hay pantalla web. Vacía, la API no manda encabezados CORS, que es lo correcto para un cliente que no es un navegador. |

`PORT` lo inyecta Railway y el entrypoint lo respeta. No lo definas a mano.

Generar una llave:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

### 5. Primer despliegue

Railway construye, corre el pre-deploy (`entrypoint.sh migrate`) y solo entonces
manda tráfico a la versión nueva. Si las migraciones fallan, el despliegue se
aborta y la versión anterior sigue atendiendo.

Con el servicio arriba, siembra los datos de arranque desde la terminal del
servicio (**Deployments → ⋮ → Terminal**, o `railway ssh`):

```bash
entrypoint.sh seed
```

Son los seeders del README, todos idempotentes. Después, el primer
administrador, **una sola vez y con las variables puestas en el momento**:

```bash
BOOTSTRAP_ADMIN_USERNAME=... BOOTSTRAP_ADMIN_PASSWORD=... \
BOOTSTRAP_ADMIN_EMAIL=... BOOTSTRAP_ADMIN_PHONE=... \
python -m scripts.create_superuser
```

`USERNAME` y `PASSWORD` (8 caracteres o más) son obligatorias y no tienen valor
por omisión: el script se niega a correr sin ellas, porque esta es la cuenta que
puede otorgar cualquier permiso. `EMAIL` y `PHONE` son opcionales.

### 6. Dominio

**Settings → Networking → Generate Domain** para una URL de Railway, o
**Custom Domain** para la propia. La app apunta ahí con
`--dart-define-from-file`, igual que hoy apunta a `10.0.2.2:8000`.

## Staging

Duplica el entorno: **Environments → New Environment**, nombrado `staging`
(el nombre tiene que coincidir con la clave en `railway.json`). Dentro, su
propio Postgres, su propio volumen y sus propias variables — sobre todo su
propio `JWT_SECRET_KEY`. Apunta el servicio a la rama de trabajo (`dev/sebas`).

`railway.json` le cambia dos cosas:

- `sleepApplication: true` — se duerme cuando nadie lo usa y solo cobra por el
  tiempo despierto. A cambio, la primera petición después de un rato tarda unos
  segundos. Para un entorno de pruebas ese trato conviene.
- `overlapSeconds: 0` — no solapa la versión vieja con la nueva; en producción sí
  hay solape para que un despliegue no corte peticiones en curso.

**Para apagarlo cuando ya no lo necesites:** borra el entorno completo
(Environments → staging → Delete). Eso se lleva el servicio, la base y el
volumen; un servicio pausado con su base de datos viva sigue costando. El bloque
`environments.staging` de `railway.json` puede quedarse: sin un entorno con ese
nombre, no hace nada.

## Operación

### Migraciones

Van en el pre-deploy, no en el arranque. Con una réplica la diferencia parece
académica; con dos, cada una intentaría migrar a la vez. Y en el arranque, una
migración rota se lleva el servicio, mientras que en el pre-deploy solo cancela
el despliegue.

Alembic corre con `psycopg` y la API con `asyncpg`, desde la misma
`DATABASE_URL`: la conversión la hace `src/core/config.py`.

### Cuántos workers

`WEB_CONCURRENCY=1` por omisión. La forma de escalar en un contenedor son
réplicas, no procesos adentro: cada worker abre su propio pool de SQLAlchemy
(5 conexiones + 10 de desborde), así que subir workers multiplica las conexiones
contra Postgres mucho más rápido de lo que uno espera. Para el volumen de una
lavandería, un worker sobra; si alguna vez hace falta más, sube `numReplicas`
antes que `WEB_CONCURRENCY` y vigila el límite de conexiones de la base.

### Borrar escaneos vencidos

Las fotos de boleta caducan a los 90 días (Plan 0003 D9) y hoy nadie las borra
solo. **No puede ser un servicio cron de Railway**: el volumen pertenece al
servicio de la API y un segundo servicio no lo puede montar, así que borraría
filas sin borrar archivos. Hasta que la retención viva dentro del proceso, se
corre a mano desde la terminal del servicio:

```bash
python -m scripts.prune_scans --dry-run   # lista lo que se iría
python -m scripts.prune_scans             # lo borra
```

### Volver atrás

**Deployments → un despliegue anterior → Redeploy.** Ojo: eso devuelve el código,
no la base de datos. Una migración que borra una columna no se deshace sola —
esas hay que pensarlas en dos pasos (agregar, migrar datos, quitar después).

### Registros

`docker/entrypoint.sh` pasa `--proxy-headers --forwarded-allow-ips='*'`, así que
en los registros aparece la IP del cliente y no la del proxy de Railway. El
comodín es seguro porque al contenedor solo llega tráfico del borde de Railway.

## Lo que cambia al poner `ENVIRONMENT=production`

Una sola variable, tres efectos, y conviene saberlos antes de que sorprendan:

1. **`/docs`, `/redoc` y `/openapi.json` dejan de publicarse.** Las tres a la
   vez: esconder `/docs` y dejar el `openapi.json` arriba publica el mismo mapa,
   nada más que incómodo. `DOCS_ENABLED=true` los devuelve sin desplegar código,
   y `DOCS_ENABLED=false` los apaga también en staging.
2. **CORS sigue apagado mientras `CORS_ORIGINS` esté vacía**, en cualquier
   entorno. Cuando se defina, la sesión viaja en el encabezado `Authorization` y
   no en una cookie, así que el middleware no habilita credenciales.
3. **`create_superuser` exige sus variables** — eso vale en todos lados, no solo
   en producción.

Lo que queda pendiente de verdad es la retención de escaneos: hoy se corre a
mano (ver arriba) porque el volumen impide que un servicio cron la haga.
