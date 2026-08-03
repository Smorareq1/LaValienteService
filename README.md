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

```bash
poetry run alembic upgrade head
```

Después, siembra permisos y catálogo (ambos son idempotentes):

```bash
poetry run python -m scripts.seed_permissions
poetry run python -m scripts.seed_catalog
```

`seed_permissions` crea los roles `admin`, `collaborator` y `system_admin`, y otorga a los
dos administrativos el **permiso comodín `*.*`**: pasan cualquier chequeo, de modo que un
módulo nuevo no los deja fuera hasta que alguien recuerde otorgarles su permiso. Un `deny`
asignado a una persona en concreto le gana al comodín. Ese es el mecanismo con el que la
app decide qué secciones dibuja: pregunta por permisos, nunca por el nombre del rol.

Después de aplicar las migraciones, crea el primer administrador con variables de entorno
`BOOTSTRAP_ADMIN_EMAIL` y `BOOTSTRAP_ADMIN_PASSWORD`:

```bash
poetry run python -m scripts.create_superuser
```

El script crea (si no existen) el rol de sistema `system_admin` y los permisos administrativos
necesarios. Los permisos y roles posteriores se administran por API.

## Pedidos: el total lo calcula el servidor

`POST /orders` recibe cantidades y selecciones, **nunca precios** (plan 0001 D5). Cada línea
se resuelve contra el catálogo vigente en la fecha del pedido y se guarda con su precio
congelado, así que subir mañana la tina grande no reescribe lo que un pedido de hoy dice que
costó. La única excepción son los servicios `variable` (recepción y entrega), donde el monto
es lo que cobró el motorista y va en el cuerpo.

Dos números se calculan y no se aceptan del cliente: `total_pieces` es la suma del detalle de
prendas, y el correlativo del día (`daily_number`) lo asigna el servidor con reintento ante
colisión. El motor está en [`src/modules/orders/pricing.py`](src/modules/orders/pricing.py) y
es lógica pura: sus pruebas no abren una conexión.

Un descuento sin promoción detrás exige `orders.manual_discount`, y el chequeo vive en el
service porque la condición es el cuerpo del pedido, no la ruta.

### Ciclo de vida: cada cosa por su puerta

`POST /orders/{id}/status` mueve el pedido por la cadena `received → in_progress → ready`, y
deja retroceder un paso. Lo que **no** hace es entregar ni anular: cada uno tiene su endpoint
(`/deliver`, `/cancel`) porque necesita datos que ese cuerpo no tiene —el conteo de prendas
que sí volvieron, el motivo— y porque `/status` solo pide `orders.update`, que todo
colaborador tiene.

El saldo (`total − Σ pagos`) se calcula, no se guarda: una copia del saldo es lo primero que
se queda vieja el día que se anule un pago. Un pago no puede exceder el saldo, porque el
vuelto que se da en el mostrador no es dinero que se quedó en la caja. Entregar con saldo
pendiente exige `orders.deliver_unpaid`: fiar es una decisión, no un descuido, y el pedido
sigue aceptando pagos después de entregado, que es como se salda.

### Usuario para las pruebas end-to-end

Las pruebas de integración de la app necesitan una cuenta propia, para no usar la de una persona
real. Requiere `scripts.seed_permissions` corrido antes, porque toma el rol `admin`:

```bash
E2E_PASSWORD=... poetry run python -m scripts.create_e2e_user
```

Es idempotente: si el usuario ya existe, solo le restablece la contraseña.

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
