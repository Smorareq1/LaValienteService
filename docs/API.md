# API v1

Referencia de la API de La Valiente al cierre de la fase 1 (planes 0001, 0004 y
0005). Describe lo que un cliente necesita saber **antes** de leer una ruta
concreta: cómo se autentica, qué formas tienen los datos, qué significa cada
código de error y qué reglas del negocio pueden rechazar una escritura que en
apariencia es válida.

El detalle de cada ruta —parámetros, esquemas, ejemplos— lo genera FastAPI y
está en `/docs` (Swagger) y `/openapi.json` con el servidor corriendo. Esta
página no lo repite: cubre lo transversal, que es justamente lo que no cabe en
la descripción de un endpoint.

- **Prefijo:** `/api/v1` (configurable en `API_V1_PREFIX`).
- **Salud:** `GET /health`, sin prefijo y sin autenticación.
- **Formato:** JSON en todo, salvo la subida y descarga de imágenes de producto.

## 1. Autenticación

`POST /auth/login` recibe `{"identifier", "password"}` — `identifier` acepta
indistintamente el usuario o el correo — y responde:

```json
{ "access_token": "…", "refresh_token": "…", "token_type": "bearer", "expires_in": 900 }
```

El resto de la API se llama con `Authorization: Bearer <access_token>`.
`POST /auth/refresh` cambia un refresh token por un par nuevo; `POST /auth/logout`
cierra la sesión actual y `POST /auth/logout-all` todas las del usuario.
`GET /auth/sessions` lista las sesiones abiertas (una por dispositivo o login) y
`DELETE /auth/sessions/{id}` cierra una concreta.

`POST /auth/register` **no es público**: crear cuentas es administrar usuarios y
pide `authorization.users.manage`. La primera cuenta del sistema se crea con
`python -m scripts.create_superuser`.

## 2. Autorización

Un permiso es un par `recurso.acción` (`orders.create`, `daily_close.reopen`). El
catálogo completo vive en `src/modules/identity/permissions.py`, junto al código
que lo comprueba, y `scripts/seed_permissions.py` lo escribe en la base. Nada
puede exigirse sin estar ahí: `require_permission` rechaza un código desconocido
al importar el módulo, para que un error de tipeo se vea al arrancar y no como un
403 que solo sufre quien no tiene el comodín.

- Los permisos se acumulan por rol y por concesión directa.
- Una **denegación** directa sobre una persona gana sobre cualquier rol.
- El rol `admin` tiene el comodín `*.*`, pero además tiene concedido cada permiso
  concreto, para que quitarle el comodín algún día deje un administrador que
  funciona en vez de uno bloqueado.
- `GET /auth/me` devuelve `permissions` y `denied_permissions`: la app decide qué
  **mostrar** con las mismas dos listas con las que el servidor decide qué
  **permitir**, así que ninguna sección puede aparecer para quien la API
  rechazaría.

Los dos roles que siembra el sistema son `admin` y `collaborator`. Qué recibe cada
uno está en la tabla de la §7 del plan 0005 y comprobado en
`tests/unit/test_permission_catalog.py`.

## 3. Formatos

| Cosa | Forma | Nota |
|---|---|---|
| Dinero | Cadena decimal: `"20.00"` | Nunca un número en coma flotante. Dos decimales siempre. |
| Fecha de negocio | `"2026-07-18"` | Zona `America/Guatemala`: a las 19:00 en Cobán todavía es hoy, aunque en UTC ya sea mañana. |
| Marca de tiempo | `"2026-08-04T04:45:40.093552Z"` | UTC, ISO-8601. Son sellos de auditoría, no fechas de negocio. |
| Identificadores | UUID v4 | El cliente puede proponer el suyo al crear, que es lo que hace idempotente un reintento (§7). |
| Enumerados | Cadenas en minúscula: `"received"`, `"cash"` | |

**Paginación.** Las listas largas responden con la misma envoltura:

```json
{ "items": [ … ], "total": 43, "page": 1, "page_size": 2 }
```

`page` empieza en 1. Las listas cortas y acotadas por naturaleza —los turnos, las
categorías de gasto, los productos— devuelven un arreglo pelado.

## 4. Errores

El cuerpo de error es siempre `{"detail": "…"}`, salvo el 422 de validación, que
usa la forma de FastAPI (una lista con `loc`, `msg` y `type` por campo).

| Código | Cuándo |
|---|---|
| `401` | Falta el token, está vencido, o la sesión fue revocada. |
| `403` | La cuenta no tiene el permiso que la ruta exige. |
| `404` | No existe, o está archivado/anulado y por tanto ya no se lee. |
| `409` | Una regla del dominio dice que no: el día está cerrado, no hay existencias, la boleta ya está entregada, la versión que se editó ya no es la vigente. |
| `422` | El cuerpo no valida contra el esquema. |

El `409` es el que más conviene leer con cuidado: para la API es un solo código,
pero para la cola de revisión del teléfono son dos cosas distintas (§7).

## 5. Concurrencia: `base_version`

Toda fila sincronizable lleva un `version` que sube en cada escritura. Las
ediciones aceptan el parámetro de consulta `base_version` con la versión sobre la
que se construyó el cambio:

```
PATCH /api/v1/expenses/{id}?base_version=3
```

Si la fila ya va por otra versión, la respuesta es `409` y **no** se escribe nada.
Omitirlo fuerza la escritura, y esa es la respuesta en todas las pantallas por
igual: un dispositivo siempre lo manda, las pantallas de administración pueden no
hacerlo.

## 6. Reglas que rechazan escrituras válidas

### 6.1 El candado de fecha

Cerrar un día congela lo que ese día registró. A partir de ahí, una operación se
rechaza con `409` **según la fecha del hecho que escribe, no según la edad del
papel donde escribe**:

- Corregir o anular una boleta consulta **la fecha de la boleta**: eso sí cambia
  lo que aquel día valió.
- Entregar ropa y cobrar consultan **hoy**: la entrega del miércoles es del
  miércoles aunque la ropa entrara el lunes y el lunes esté cerrado. El dinero de
  un pago cae siempre en la hoja de hoy.
- Un gasto, una venta de mostrador o una jornada consultan su propia fecha.

Reabrir un día (`POST /daily-close/{id}/reopen`, solo administrador) levanta el
candado y deja rastro; el acta reabierta se conserva y obliga a volver a cerrar.

`GET /daily-close/preview` acompaña las cifras con `warnings`: lo que vale la pena
leer antes de firmar, y que **nunca** impide cerrar. Viajan como `código` o
`código:valor` —`open_tickets:3`, `uncollected:120.00`, `pending_expenses:80.00`,
`reopened`— igual que los avisos del motor de sincronización, y no como frases: el
servidor es el único que puede contarlas (ve las boletas de todos los
dispositivos), pero la frase la escribe el cliente, que es el que habla español.

### 6.2 Existencias

Una venta de mostrador reparte FIFO sobre los lotes vendibles. Si no alcanza, la
venta entera se rechaza —no se sirve a medias— y la respuesta dice qué producto,
cuánto se pidió y cuánto había.

## 7. Sincronización

Dos rutas y ninguna magia. Ambas piden solo sesión: los permisos se evalúan por
operación, en el momento de **aplicarla**, así que un permiso retirado mientras el
teléfono estaba sin señal surte efecto igual.

**`POST /sync/push`** manda un lote de operaciones. Cada una lleva su `op_id`
(clave de idempotencia), un `seq` local que fija el orden, la entidad, el tipo de
operación, el `entity_id` que acuñó el dispositivo y opcionalmente `base_version`.
Cada resultado vuelve con uno de cuatro estados:

| Estado | Qué pasó | Qué hace la app |
|---|---|---|
| `applied` | Se escribió. | Asienta el espejo con `server_data`. |
| `already_applied` | Ese `op_id` ya se había procesado. | Igual que el anterior; el reintento no duplicó nada. |
| `rejected` | Una regla dijo que no: sin existencias, día cerrado, boleta duplicada, permiso ausente. | A la cola de revisión, sin nada que comparar. |
| `conflict` | **Solo** una `base_version` vieja. | A la cola, con las dos versiones lado a lado. |

Esa distinción es la razón de que `conflict` esté reservado: es el único caso que
una persona resuelve mirando dos versiones. Todo lo demás se arregla de otra
manera, y ofrecerle una comparación sería ofrecerle lo único que ahí no sirve.

**`GET /sync/pull`** baja los cambios desde un `cursor` (`sync_seq`) en páginas de
hasta `page_size`. Cada cambio trae `entity`, `id`, `version`, `sync_seq`,
`deleted` y `data`. Una baja viaja como lápida (`deleted: true`), no como
ausencia, para que un dispositivo sepa que algo dejó de existir en vez de
suponerlo.

Ambas respuestas pueden traer `device_directive: "wipe"`: el dispositivo fue
revocado y debe borrar su copia local.

**Lo que no viaja.** El feed enumera columna por columna, no tabla por tabla, y
dos ausencias son deliberadas: el costo de compra de un lote (`unit_cost`) no sale
del servidor, y la hora extra *sugerida* tampoco —el cliente recibe solo los
minutos que una persona confirmó—.

## 8. Imágenes de producto

`PUT /inventory/products/{id}/image` recibe `multipart/form-data`. El formato se
valida por los bytes y no por el nombre del archivo, y el límite lo fija
`MEDIA_MAX_IMAGE_MB`. La ruta que devuelve lleva un digest del contenido, así que
reemplazar la foto cambia la URL y ningún teléfono sigue mostrando la cacheada.
`GET /inventory/products/{id}/image` la devuelve.

## 9. Rutas y permisos

Esta tabla la escribe `python -m scripts.dump_api_reference` leyendo la
aplicación montada, y `tests/unit/test_api_reference.py` falla si el archivo
committeado se separa de lo que el servidor realmente expone. No la edites a
mano.

En la columna de permiso, `—` significa que la ruta es pública y `sesión` que basta
con estar autenticado.

<!-- rutas: generado por scripts/dump_api_reference.py -->

### Authentication

| Método | Ruta | Permiso |
|---|---|---|
| `POST` | `/api/v1/auth/register` | `authorization.users.manage` |
| `POST` | `/api/v1/auth/login` | — |
| `POST` | `/api/v1/auth/refresh` | — |
| `POST` | `/api/v1/auth/forgot-password` | — |
| `POST` | `/api/v1/auth/reset-password` | — |
| `POST` | `/api/v1/auth/logout` | sesión |
| `POST` | `/api/v1/auth/logout-all` | sesión |
| `GET` | `/api/v1/auth/sessions` | sesión |
| `DELETE` | `/api/v1/auth/sessions/{session_id}` | sesión |
| `GET` | `/api/v1/auth/me` | sesión |

### Authorization

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/authorization/permissions` | `authorization.permissions.manage` |
| `GET` | `/api/v1/authorization/roles` | `authorization.roles.manage` |
| `GET` | `/api/v1/authorization/users` | `authorization.users.manage` |
| `POST` | `/api/v1/authorization/permissions` | `authorization.permissions.manage` |
| `POST` | `/api/v1/authorization/roles` | `authorization.roles.manage` |
| `PUT` | `/api/v1/authorization/roles/{role_id}/permissions` | `authorization.roles.manage` |
| `PUT` | `/api/v1/authorization/users/{user_id}/roles` | `authorization.users.manage` |
| `PUT` | `/api/v1/authorization/users/{user_id}/permissions` | `authorization.users.manage` |

### Catalog

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/catalog/service-types` | `catalog.read` |
| `GET` | `/api/v1/catalog/service-types/{service_type_id}` | `catalog.read` |
| `POST` | `/api/v1/catalog/service-types` | `catalog.manage` |
| `PATCH` | `/api/v1/catalog/service-types/{service_type_id}` | `catalog.manage` |
| `GET` | `/api/v1/catalog/service-types/{service_type_id}/prices` | `catalog.read` |
| `POST` | `/api/v1/catalog/service-types/{service_type_id}/prices` | `catalog.manage` |
| `GET` | `/api/v1/catalog/garment-types` | `catalog.read` |
| `POST` | `/api/v1/catalog/garment-types` | `catalog.manage` |
| `PATCH` | `/api/v1/catalog/garment-types/{garment_type_id}` | `catalog.manage` |

### Customers

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/customers` | `customers.read` |
| `POST` | `/api/v1/customers` | `customers.create` |
| `GET` | `/api/v1/customers/{customer_id}` | `customers.read` |
| `PATCH` | `/api/v1/customers/{customer_id}` | `customers.update` |
| `DELETE` | `/api/v1/customers/{customer_id}` | `customers.archive` |

### Daily close

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/daily-close/preview` | `daily_close.read` |
| `GET` | `/api/v1/daily-close` | `daily_close.read` |
| `POST` | `/api/v1/daily-close` | `daily_close.close` |
| `POST` | `/api/v1/daily-close/{closure_id}/reopen` | `daily_close.reopen` |

### Expenses

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/expenses/categories` | `expenses.read` |
| `POST` | `/api/v1/expenses/categories` | `expenses.manage_categories` |
| `PATCH` | `/api/v1/expenses/categories/{category_id}` | `expenses.manage_categories` |
| `GET` | `/api/v1/expenses/summary` | `expenses.read` |
| `GET` | `/api/v1/expenses` | `expenses.read` |
| `POST` | `/api/v1/expenses` | `expenses.create` |
| `PATCH` | `/api/v1/expenses/{expense_id}` | `expenses.update` |
| `DELETE` | `/api/v1/expenses/{expense_id}` | `expenses.void` |

### Inventory

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/inventory/products` | `inventory.read` |
| `POST` | `/api/v1/inventory/products` | `inventory.manage` |
| `GET` | `/api/v1/inventory/products/{product_id}` | `inventory.read` |
| `PATCH` | `/api/v1/inventory/products/{product_id}` | `inventory.manage` |
| `PUT` | `/api/v1/inventory/products/{product_id}/image` | `inventory.manage` |
| `GET` | `/api/v1/inventory/products/{product_id}/image` | `inventory.read` |
| `GET` | `/api/v1/inventory/products/{product_id}/lots` | `inventory.read` |
| `POST` | `/api/v1/inventory/products/{product_id}/lots` | `inventory.manage` |
| `PATCH` | `/api/v1/inventory/lots/{lot_id}` | `inventory.manage` |
| `GET` | `/api/v1/inventory/movements` | `inventory.read` |
| `POST` | `/api/v1/inventory/movements` | `inventory.adjust` |

### Orders

| Método | Ruta | Permiso |
|---|---|---|
| `POST` | `/api/v1/orders` | `orders.create` |
| `GET` | `/api/v1/orders` | `orders.read` |
| `GET` | `/api/v1/orders/daily-summary` | `orders.read` |
| `GET` | `/api/v1/orders/{order_id}` | `orders.read` |
| `PUT` | `/api/v1/orders/{order_id}` | `orders.update` |
| `POST` | `/api/v1/orders/{order_id}/status` | `orders.update` |
| `POST` | `/api/v1/orders/{order_id}/deliver` | `orders.deliver` |
| `POST` | `/api/v1/orders/{order_id}/cancel` | `orders.cancel` |
| `POST` | `/api/v1/orders/{order_id}/payments` | `orders.collect_payment` |

### Promotions

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/promotions` | `promotions.read` |
| `POST` | `/api/v1/promotions` | `promotions.manage` |
| `PATCH` | `/api/v1/promotions/{promotion_id}` | `promotions.manage` |

### Staff

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/api/v1/staff/employees` | `staff.read` |
| `POST` | `/api/v1/staff/employees` | `staff.manage` |
| `PATCH` | `/api/v1/staff/employees/{employee_id}` | `staff.manage` |
| `GET` | `/api/v1/staff/shifts` | `staff.read` |
| `POST` | `/api/v1/staff/shifts` | `staff.manage` |
| `PATCH` | `/api/v1/staff/shifts/{shift_id}` | `staff.manage` |
| `GET` | `/api/v1/staff/rates` | `staff.manage` |
| `POST` | `/api/v1/staff/rates` | `staff.manage` |
| `GET` | `/api/v1/staff/attendance` | `staff.read` |
| `POST` | `/api/v1/staff/attendance` | `attendance.record` |
| `PATCH` | `/api/v1/staff/attendance/{record_id}` | `attendance.record` |

### Supply sales

| Método | Ruta | Permiso |
|---|---|---|
| `POST` | `/api/v1/supply-sales` | `supply_sales.create` |
| `GET` | `/api/v1/supply-sales` | `supply_sales.read` |
| `GET` | `/api/v1/supply-sales/{sale_id}` | `supply_sales.read` |
| `POST` | `/api/v1/supply-sales/{sale_id}/cancel` | `supply_sales.cancel` |

### Sync

| Método | Ruta | Permiso |
|---|---|---|
| `POST` | `/api/v1/sync/devices` | sesión |
| `GET` | `/api/v1/sync/devices` | `sync.devices.manage` |
| `POST` | `/api/v1/sync/devices/{device_id}/revoke` | `sync.devices.manage` |
| `POST` | `/api/v1/sync/push` | sesión |
| `GET` | `/api/v1/sync/pull` | sesión |

### Health

| Método | Ruta | Permiso |
|---|---|---|
| `GET` | `/health` | — |

<!-- fin rutas -->
