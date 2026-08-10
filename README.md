# LaValienteService

Backend de La Valiente, construido con FastAPI, PostgreSQL, SQLAlchemy async y Alembic.

Documentación: [API v1](docs/API.md) · [Arquitectura](docs/ARCHITECTURE.md) ·
[Identidad y acceso](docs/IDENTITY_AND_ACCESS.md) · [Despliegue](docs/DEPLOYMENT.md) ·
[Planes](docs/plans/README.md).
Con el servidor corriendo, el detalle por ruta está en `/docs`.

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

Después, siembra los datos de arranque. Todos los seeders son idempotentes, así que
volver a correrlos no duplica nada:

```bash
poetry run python -m scripts.seed_permissions
poetry run python -m scripts.seed_catalog
poetry run python -m scripts.seed_promotions
poetry run python -m scripts.seed_staff
poetry run python -m scripts.seed_expense_categories
poetry run python -m scripts.seed_products
```

Los tres últimos son los del registro diario: turnos y tarifa de hora extra,
categorías de gasto y los insumos que se venden en el mostrador. Sin ellos la
pantalla de asistencia no tiene turnos que ofrecer y un gasto no tiene dónde
clasificarse.

`seed_permissions` crea los roles `admin`, `collaborator` y `system_admin`, y otorga a los
dos administrativos el **permiso comodín `*.*`**: pasan cualquier chequeo, de modo que un
módulo nuevo no los deja fuera hasta que alguien recuerde otorgarles su permiso. Un `deny`
asignado a una persona en concreto le gana al comodín. Ese es el mecanismo con el que la
app decide qué secciones dibuja: pregunta por permisos, nunca por el nombre del rol.

Después de aplicar las migraciones, crea el primer administrador. Ninguna variable tiene valor
por omisión —un usuario y contraseña por defecto son una credencial publicada el día que la API
tiene URL pública, y esta cuenta es la que puede otorgar cualquier permiso—, así que el script
se niega a correr sin ellas:

```bash
BOOTSTRAP_ADMIN_USERNAME=... BOOTSTRAP_ADMIN_PASSWORD=... \
poetry run python -m scripts.create_superuser
```

`BOOTSTRAP_ADMIN_EMAIL` y `BOOTSTRAP_ADMIN_PHONE` son opcionales. La contraseña necesita 8
caracteres o más.

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

### Promociones: el pedido manda el código, no el monto

Una promoción viaja en el pedido como `{"promotion_code": "domicilio_50"}` y **sin monto**:
cuánto rebaja lo resuelve el servidor contra los cargos de esa boleta, igual que hace con los
precios. Hay tres formas de rebajar (`percentage`, `fixed_amount`, `special_price`) y cada una
puede apuntar a ciertos servicios (`applies_to_service_codes`) o al pedido entero.

Elegir una promoción vigente **no exige permiso de descuento**: la decisión se tomó al crearla.
Lo que exige `orders.manual_discount` es teclear un monto a mano. Una promoción que no está
vigente el día del pedido, o que no rebaja nada en esa boleta, se **rechaza con un mensaje** en
vez de aplicarse en silencio: dejarla caer callada le haría creer al mostrador que el cliente
ya tiene su descuento. La descripción que queda en la línea es una copia congelada del nombre,
así que renombrar la promoción mañana no reescribe los pedidos de ayer.

### Corregir un pedido: se reemplaza, no se parcha

`PUT /orders/{id}` recibe la boleta como debería leerse y la vuelve a calcular entera, contra
el catálogo de **la fecha del pedido** y no la de hoy. No se editan tres cosas: la fecha y el
correlativo, que son el nombre por el que todos llaman al pedido; el estado, que tiene sus
propias puertas; y los pagos, porque el dinero recibido es un hecho que ocurrió.

Quién puede corregir depende del estado (plan 0001 §7.3): `received` e `in_progress` los edita
cualquiera que tome pedidos, uno `ready` exige `orders.update_ready` —ya está contado, lavado y
doblado— y uno entregado o anulado no lo edita nadie: eso se corrige anulando y volviendo a
capturar. Una edición que dejara el total por debajo de lo ya pagado se rechaza, porque eso es
una devolución y las devoluciones son un movimiento de caja que este módulo no sabe registrar.

`GET /orders/daily-summary?date=` cuenta y suma los pedidos de la fecha: cuántos hay por estado,
piezas, subtotal, descuento, total, cobrado y saldo. Los anulados cuentan como pedido pero no
como dinero — salvo lo que ya se les había cobrado, que sigue en el cajón.

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

## Escaneo con IA (módulo temporal)

`intake_scan` lee una fotografía de papel con Gemini y devuelve algo que una persona confirma.
Es temporal por diseño (plan 0003 D2): el día que la operación abandone el papel, retirarlo es
apagar el flag, borrar `src/modules/intake_scan/`, sus endpoints y sus ajustes, y una migración
de limpieza.

Lee **dos documentos distintos**, con su propio prompt y su propio esquema cada uno:

| Documento | Endpoint | Qué devuelve |
| --- | --- | --- |
| Boleta del talonario | `POST /scans` | Un **borrador** para prellenar la toma de pedido. |
| Boleta ya existente | `POST /scans/lookup` | **Cuál** es, para entregarla sin teclear. |
| Hoja «Registro Diario» | `POST /scans/cash-close` | Las filas del día, **emparejadas** contra la base. |

Está **apagado por omisión**. Para encenderlo hacen falta dos variables:

```bash
SCAN_ENABLED=true
GEMINI_API_KEY=...   # solo en el backend; la app nunca la ve (D1)
```

Los demás ajustes tienen valores por omisión razonables: `SCAN_MODEL` (`gemini-2.5-flash`),
`SCAN_PROMPT_VERSION` (`v1`), `SCAN_CASH_PROMPT_VERSION` (`cash_v1`), `SCAN_TIMEOUT_S` (30),
`SCAN_MAX_IMAGE_MB` (8), `SCAN_DAILY_LIMIT` (200, el control de costos), `SCAN_STORAGE_PATH`
(`./media/scans`) y `SCAN_IMAGE_RETENTION_DAYS` (90).

La regla que no se rompe: **la IA nunca guarda un pedido.** Produce un borrador que una persona
revisa y confirma, y el motor de precios del plan 0001 §6 recalcula todos los montos — los
totales leídos de la foto solo sirven para avisar de una discrepancia. Y si Gemini falla, tarda
o devuelve basura, la captura a mano funciona entera sin este módulo (D8).

### La hoja del día es la excepción que escribe

`POST /scans/cash-close/{id}/apply` es el único sitio de este módulo que **escribe**, y por eso
tiene su propio permiso (`scans.import_close`, que el colaborador no tiene por omisión). Lo que
lo mantiene dentro de las reglas:

- lo que se aplica **no** es lo que se leyó: es lo que una persona confirmó fila por fila en la
  pantalla, sin una sola `confidence` en el cuerpo de la petición;
- cada escritura pasa por el service del módulo dueño, así que el cobro se sigue recortando al
  saldo, entregar debiendo sigue exigiendo `orders.deliver_unpaid` y un día cerrado sigue
  rechazando;
- **fila por fila y nunca todo o nada**: una línea mala de quince no deshace las catorce buenas,
  y la respuesta dice qué pasó con cada una;
- los ids de los pagos y gastos se derivan del escaneo (uuid5), así que reintentar una petición
  cuya respuesta se perdió aterriza sobre las filas que ya existen en vez de duplicarlas. Un
  segundo intento **deliberado** de importar la misma hoja se rechaza con 409.

Las fotos contienen datos personales, así que se sirven solo con `scans.read` y se borran
pasado su plazo:

```bash
poetry run python -m scripts.prune_scans --dry-run   # lista lo que se iría
poetry run python -m scripts.prune_scans             # lo borra
```

Al cambiar el prompt o el modelo hay que correr la evaluación contra el set dorado y comparar
con la corrida anterior; la regla del plan es que no se sube una versión que empeore la
exactitud global. **El set dorado todavía no existe** — son fotos reales de boletas anotadas a
mano, mínimo veinte y variadas, en `tests/fixtures/scan_golden/`:

```bash
poetry run python -m scripts.eval_scan
```

No corre en CI: necesita la key y cada corrida cuesta dinero.

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

## Despliegue

`docker/Dockerfile` es la imagen de producción, y es la misma que corre en staging: si lo que se
prueba no es el binario que se despliega, staging deja de responder la única pregunta para la que
existe. Entre entornos cambian las variables, no el Dockerfile.

Antes de subir nada, esa imagen se prueba en tu máquina con `docker-compose.prod.yml`, que no es
"el compose de producción" —en Railway la base la administra Railway— sino la forma de verificar
que la imagen arranca sin Poetry, sin dependencias de desarrollo, sin el código montado y sin
privilegios:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm api migrate
docker compose -f docker-compose.prod.yml up
```

El resto —crear el proyecto, el volumen, las variables, los seeders y el primer administrador—
está en [Despliegue](docs/DEPLOYMENT.md).
