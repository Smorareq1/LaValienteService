# Plan 0004 — Offline-first y sincronización

| | |
|---|---|
| **Estado** | 📝 Borrador |
| **Fecha** | 2026-07-22 |
| **Módulos afectados** | Backend: `sync` (nuevo) + columnas en toda entidad sincronizable. APP: base de datos local, outbox y motor de sync |
| **Depende de** | `identity` (RBAC). **Precede** a la implementación de pedidos del [Plan 0001](../0001-pedidos-diarios/PLAN.md) (ver §13) |
| **Carácter** | Fundacional — toda entidad futura nace sincronizable |

## 1. Contexto

La operación no puede detenerse cuando se cae el internet: la toma de pedido, el cobro y la
entrega deben funcionar **siempre**, y sincronizarse solas al volver la conexión. Este plan
define la arquitectura offline-first del sistema completo: la app deja de hablar con la API en
el camino crítico y pasa a leer/escribir **su base de datos local**, con un motor de
sincronización detrás.

Escala real del negocio: pocos dispositivos (1–5), decenas de pedidos al día, un solo local.
El diseño debe ser riguroso donde importa (idempotencia, conflictos, dinero, unicidad) y
austero donde no (no necesitamos CRDTs ni streaming en tiempo real — ver §4.1).

## 2. Alcance

**Dentro:** protocolo de sincronización push/pull, columnas y tablas de soporte en el backend,
aplicador de operaciones que reutiliza los services de dominio, base local (SQLite) + outbox +
motor de sync en Flutter, taxonomía de conflictos y cola de revisión, seguridad offline
(sesión, cifrado, dispositivos), estrategia de pruebas multi-dispositivo, y la re-secuenciación
de los planes 0001–0003.

**Fuera:** colaboración en tiempo real (dos personas editando el mismo pedido en vivo),
sincronización peer-to-peer entre dispositivos, y el modo offline del escaneo IA (el Plan 0003
es online-only por naturaleza; con mala señal se captura manual).

## 3. Glosario

| Término | Significado |
|---|---|
| **Fuente de verdad local** | La BD SQLite del dispositivo. La UI **solo** lee/escribe ahí; nunca espera a la red. |
| **Fuente de verdad global** | PostgreSQL en el servidor. Gana todo conflicto salvo regla explícita en contra. |
| **Operación (op)** | Mutación de negocio registrada localmente: `create_order`, `add_payment`… Con `op_id` UUID propio. |
| **Outbox** | Cola local append-only de operaciones pendientes de push, en el orden en que ocurrieron. |
| **Cursor** | Posición del dispositivo en el feed de cambios del servidor (`sync_seq` global). |
| **Tombstone** | Registro marcado como eliminado (`deleted_at`) que sigue viajando en el pull para que los dispositivos lo borren/oculten. |
| **Cola de revisión** | Bandeja en la app con las operaciones que el servidor rechazó y requieren decisión humana. |

## 4. Decisiones de diseño

- **D1 — Offline-first real, no "modo offline".** La app funciona igual con o sin red: la UI
  opera contra SQLite local siempre. "Online" solo significa que el motor de sync está
  vaciando el outbox y trayendo cambios. No existe un camino de código "con conexión" distinto.
- **D2 — El servidor es la autoridad.** Toda op pasa por los **mismos services de dominio**
  que la API REST (motor de precios, validaciones, RBAC). La sincronización no es un bypass:
  es otro transporte hacia la misma lógica. Consecuencia estructural: los services del Plan
  0001 deben ser invocables desde el endpoint REST **y** desde el aplicador de ops.
- **D3 — IDs generados por el cliente (UUID v4).** Ya es la convención del esquema. Permite
  crear entidades offline y referenciarlas entre sí (pedido → cliente nuevo) sin esperar al
  servidor.
- **D4 — Idempotencia absoluta por `op_id`.** Cada op se aplica exactamente una vez: el
  servidor guarda el resultado en `sync_operations` (único por `op_id`) y ante un reintento
  devuelve el resultado registrado. Un crash a media sincronización nunca duplica un pedido ni
  un pago.
- **D5 — Push de comandos, pull de estado.** Suben **operaciones de negocio** (intención);
  baja **estado materializado** (filas con versión). Esto deja al servidor validar invariantes
  (correlativo diario, serie única, transiciones de estado) que un merge de filas jamás podría
  garantizar.
- **D6 — Concurrencia optimista por versión.** Toda entidad sincronizable lleva `version`
  (int incremental). Las ops de update llevan `base_version`; si no coincide con la actual →
  conflicto → cola de revisión. No hay merge automático de campos en entidades de dinero.
- **D7 — Feed de cambios por secuencia global.** Toda escritura toca `sync_seq` (secuencia
  global de PostgreSQL). El pull es `WHERE sync_seq > :cursor ORDER BY sync_seq` paginado —
  simple, ordenado, sin relojes distribuidos.
- **D8 — Borrado = tombstone.** Nunca `DELETE` físico en entidades sincronizables (ya era
  D9 del Plan 0001); `deleted_at` viaja en el pull.
- **D9 — El correlativo diario lo asigna el servidor al sincronizar.** Dos dispositivos
  offline no pueden repartirse el "No." del día sin coordinarse. Offline, el pedido muestra un
  folio provisional local (`P-3`); al sincronizar recibe su `daily_number` definitivo. El
  ancla operativa con el papel siempre es la serie de imprenta (`booklet_serial`).
- **D10 — Los montos se recalculan al aplicar.** La app calcula totales con su catálogo
  cacheado (preview); el servidor recalcula con el catálogo vigente a `order_date` al aplicar
  la op (D5 del Plan 0001 sobrevive al offline). Si difieren, la respuesta trae las cifras
  definitivas y la app actualiza su copia local.
- **D11 — Dispositivos registrados y revocables.** Cada instalación se registra
  (`sync_devices`); un admin puede revocar un dispositivo (robo/baja) y en su siguiente
  contacto el servidor ordena el borrado de datos locales.
- **D12 — BD local cifrada.** SQLCipher (vía drift), llave en Keystore/Keychain. La boleta
  contiene datos personales; un teléfono perdido no puede regalarlos.

### 4.1 Alternativas consideradas y descartadas

| Alternativa | Por qué no |
|---|---|
| CRDTs / merge automático (Automerge, etc.) | Los invariantes que importan aquí (correlativo, serie única, totales, transiciones) son reglas de negocio centralizadas; un CRDT converge pero no valida. Complejidad enorme para 1–5 dispositivos. |
| Plataformas de sync de terceros (PowerSync, ElectricSQL, Firebase) | Dependencia externa y de pago para un problema acotado; además el requisito D2 (ops por los services propios) es incompatible con replicar filas directo. |
| LWW por campo (last-write-wins) | Peligroso con dinero: perder silenciosamente un cargo o un pago es inaceptable. Preferimos rechazar y que un humano decida (§8). |
| WebSocket / tiempo real | El pull por lotes al reconectar y periódico cubre el caso de uso; el local es uno solo. Se puede añadir después sin cambiar el protocolo. |
| Relojes híbridos (HLC) | Con servidor autoritativo y `sync_seq` global no hay ordenamiento distribuido que resolver. El `seq` local por dispositivo ordena el outbox; el servidor ordena el mundo. |

## 5. Arquitectura

```mermaid
flowchart LR
    subgraph DEV["App Flutter (cada dispositivo)"]
        UI["UI (Plan 0002)"] --> LDB[("SQLite cifrada<br/>fuente de verdad local")]
        UI --> OB["Outbox<br/>(ops pendientes)"]
        SYNC["Motor de sync<br/>(background)"] --> OB
        SYNC --> LDB
    end
    SYNC -->|"POST /sync/push (ops)"| API["Backend FastAPI<br/>módulo sync"]
    SYNC -->|"GET /sync/pull (cursor)"| API
    API --> APPL["Aplicador de ops"]
    APPL --> SVCS["Services de dominio<br/>(los mismos del REST)"]
    SVCS --> PG[("PostgreSQL<br/>fuente de verdad global")]
```

Ciclo del motor: **push → pull → reconciliar**. Disparadores: recuperar conectividad, app a
foreground, mutación local (debounced), y periódico. Reintentos con backoff exponencial +
jitter; nunca dos ciclos concurrentes.

## 6. Modelo de datos

### 6.1 Servidor — columnas en toda entidad sincronizable (`SyncableMixin`)

| Columna | Tipo | Notas |
|---|---|---|
| `version` | `Integer`, default 1 | Incrementa en cada escritura (D6) |
| `sync_seq` | `BigInteger`, índice | Asignado de la secuencia global en cada escritura (D7) |
| `deleted_at` | `DateTime` \| null | Tombstone (D8) |

Aplica a: `customers`, `garment_types`, `service_types`, `service_options`, `service_prices`,
`promotions`, `orders`, `order_garments`, `order_charges`, `order_discounts`,
`order_payments`. (Las tablas hijas de pedido viajan embebidas en su pedido en el pull, pero
llevan las columnas para el feed.) `identity` no se sincroniza: la gestión de usuarios es
online-only; la app cachea lo mínimo (§10).

> **Actualización 2026-07-27:** el [Plan 0005](../0005-registro-diario-cierre/PLAN.md)
> §6.4 extiende esta lista con sus entidades (`staff`, `inventory`, `expenses`,
> `daily_close`) y agrega conflictos nuevos a la tabla de §8.

### 6.2 Servidor — tablas nuevas (módulo `sync`)

**`sync_devices`**

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | Generado por el dispositivo en el primer login |
| `user_id` | FK → `users` | Último usuario que lo usó |
| `name` | `String(80)` | "Tablet mostrador" |
| `platform`, `app_version` | `String` | Diagnóstico |
| `last_seen_at`, `last_push_at`, `last_pull_cursor` | | Observabilidad |
| `revoked_at` | `DateTime` \| null | D11: al contactar, recibe orden de wipe |

**`sync_operations`** — bitácora idempotente (D4)

| Campo | Tipo | Notas |
|---|---|---|
| `op_id` | UUID, **único** | Idempotencia |
| `device_id` | FK → `sync_devices` | |
| `user_id` | FK → `users` | Quién la originó (RBAC se evalúa con este usuario) |
| `entity`, `op_type`, `entity_id` | `String` / UUID | `order` + `create`, etc. |
| `payload` | `JSONB` | El comando tal como llegó |
| `status` | enum `applied` \| `already_applied` \| `rejected` \| `conflict` | |
| `result` | `JSONB` | Lo que se respondió (se re-sirve ante replay) |
| `reason` | `Text` \| null | Si rechazada/conflicto |
| `client_ts`, `applied_at` | | Skew de reloj se loguea si excede umbral |

### 6.3 Cliente (drift/SQLite)

- **Espejo de las entidades sincronizables** + metadatos por fila: `version` conocida,
  `pending` (tiene cambios locales sin confirmar), `sync_status`
  (`synced` \| `pending` \| `rejected`).
- **`outbox`**: `op_id`, `seq` local monotónico, `entity`, `op_type`, `entity_id`, `payload`,
  `created_at`, `attempts`, `last_error`.
- **`sync_state`**: cursor de pull, timestamps del último ciclo, `device_id`.
- **`review_queue`**: ops rechazadas/en conflicto con su razón y el estado servidor adjunto.

## 7. Protocolo

### 7.1 Push — `POST /api/v1/sync/push`

```jsonc
{
  "device_id": "…",
  "operations": [
    {
      "op_id": "1f0c…",             // UUID nuevo por op — clave de idempotencia
      "seq": 41,                     // orden local del dispositivo (se aplican en orden)
      "entity": "order",
      "op_type": "create",           // create | update | change_status | deliver | cancel | add_payment …
      "entity_id": "9a2b…",          // UUID generado por el cliente (D3)
      "base_version": null,          // requerido en updates (D6)
      "payload": { /* mismo contrato que el endpoint REST equivalente (Plan 0001 §6.1) */ },
      "client_ts": "2026-07-22T14:03:22-06:00"
    }
  ]
}
```

Respuesta — un resultado por op, en orden:

```jsonc
{
  "results": [
    {
      "op_id": "1f0c…",
      "status": "applied",           // applied | already_applied | rejected | conflict
      "entity_id": "9a2b…",
      "server_version": 1,
      "server_data": { "daily_number": 7, "subtotal": "116.25", "total": "108.75" },
      "warnings": [],
      "reason": null                  // en rejected/conflict: motivo legible + estado actual
    }
  ]
}
```

Reglas: lote máx. 200 ops; cada op se aplica en su **propia transacción** (una op mala no
tumba el lote); las ops de un dispositivo se aplican en orden de `seq`; permisos RBAC se
evalúan con el `user_id` de la op — un permiso retirado entre la captura offline y el sync
produce `rejected` (a revisión, §8).

### 7.2 Pull — `GET /api/v1/sync/pull?cursor=184023&page_size=500`

```jsonc
{
  "changes": [
    { "entity": "customer", "id": "…", "version": 7, "sync_seq": 184031,
      "deleted": false, "data": { /* fila completa */ } },
    { "entity": "order", "id": "…", "version": 3, "sync_seq": 184102,
      "deleted": false, "data": { /* pedido con prendas, cargos, descuentos, pagos */ } }
  ],
  "next_cursor": 184530,
  "has_more": false,
  "server_time": "2026-07-22T20:10:00Z",
  "device_directive": null            // "wipe" si el dispositivo fue revocado (D11)
}
```

Reglas: orden estricto por `sync_seq`; el cliente aplica la página y **luego** persiste el
cursor (crash-safe: re-aplicar una página es inocuo porque el upsert es por `id` + `version`);
una fila local con `pending` no se pisa con el pull — se resuelve cuando su op se confirme o
caiga a revisión.

**Bootstrap** (primer arranque o post-wipe): pull desde `cursor=0` pagina el snapshot completo
(catálogo primero, luego clientes, luego pedidos de los últimos N días — configurable). El
dispositivo queda operable offline desde ese momento.

### 7.3 Qué sincroniza cada entidad

| Entidad | Dirección | Ops offline permitidas |
|---|---|---|
| Catálogo (servicios, opciones, precios, prendas) | Servidor → app | Ninguna (admin edita online) |
| Promociones | Servidor → app | Ninguna |
| Clientes | Bidireccional | `create`, `update` |
| Pedidos (+ hijas) | Bidireccional | `create`, `update`, `change_status`, `deliver`, `cancel` |
| Pagos | App → servidor (append-only) | `add_payment` |
| Usuarios/roles | No sincroniza | — (online-only) |
| Escaneos (Plan 0003) | No sincroniza | — (requiere red por definición) |

## 8. Conflictos: taxonomía y resolución

Principio: **nada de dinero se fusiona en silencio**. Lo automático se limita a lo seguro; el
resto va a la cola de revisión con contexto completo.

| Caso | Detección | Resolución |
|---|---|---|
| Mismo `booklet_serial` desde dos dispositivos | Unique en servidor | 2ª op → `rejected` ("boleta ya registrada, pedido X"). Casi siempre es doble captura: la app ofrece descartar el duplicado o editar la serie |
| Dos pedidos offline el mismo día | — | No es conflicto: el servidor asigna `daily_number` por orden de llegada (D9) |
| Update con `base_version` vieja | D6 | `conflict`: la app muestra ambas versiones lado a lado; la persona decide qué conservar y re-emite la op |
| Cliente duplicado (dos altas offline de la misma persona) | Teléfono exacto al aplicar `create` | Se aplica **con warning** `possible_duplicate_of`; queda en revisión para fusión manual (endpoint de merge: plan futuro) |
| Transición de estado inválida (ej. entregar un pedido ya anulado desde otro dispositivo) | Service de dominio | `rejected` con estado actual; a revisión |
| Pago duplicado por reintento | `op_id` | Imposible por D4 (`already_applied`) |
| Entrega offline con saldo pendiente según el servidor | Service (`orders.deliver_unpaid`) | Si el usuario no tiene el permiso → `rejected` a revisión; el pedido queda entregado físicamente pero el sistema exige regularizar el cobro |
| Promoción vencida al momento de aplicar | Motor de descuentos | `rejected` con recálculo sugerido sin la promo; la persona confirma |

La **cola de revisión** es una pantalla de la app (badge con contador): cada entrada muestra
qué pasó, el estado local vs. el del servidor, y acciones concretas (reintentar corregido,
descartar, pedir a un admin). Nada desaparece solo.

## 9. Reglas de dominio bajo offline

- **Folio provisional:** hasta sincronizar, el pedido muestra `P-<n>` del dispositivo y un
  ícono de estado. El ticket/confirmación usa la serie de imprenta como referencia ante el
  cliente.
- **Catálogo cacheado:** los precios que ve la app son los del último pull. D10 garantiza que
  el servidor tiene la última palabra; si el recálculo difiere del preview, la app actualiza y
  lo señala.
- **`order_date`:** la pone el dispositivo (fecha de negocio local). Si el skew de reloj
  contra `server_time` supera el umbral, el motor lo reporta y la app avisa al usuario que
  revise la fecha/hora del equipo.
- **Búsqueda de clientes:** contra la BD local (completa tras bootstrap). Un cliente creado en
  otro dispositivo aparece tras el siguiente pull.

## 10. Seguridad offline

- **Sesión con gracia offline:** el refresh token existente (`identity`) mantiene la sesión;
  sin red, la app permite operar con la sesión cacheada hasta `OFFLINE_GRACE_DAYS` (default 7)
  desde la última autenticación exitosa; vencida la gracia, bloquea la captura hasta
  reconectar (los datos locales no se pierden).
- **Cifrado local** (D12): SQLCipher, llave por dispositivo en almacenamiento seguro del SO.
- **Revocación** (D11): admin marca el dispositivo; el siguiente push/pull devuelve
  `device_directive: "wipe"` → la app borra BD local y llaves. Ops pendientes de un
  dispositivo revocado se rechazan.
- **RBAC en el servidor siempre** (§7.1): el permiso se evalúa al aplicar, no al capturar.

## 11. Observabilidad y límites

- Métricas mínimas: ops pendientes por dispositivo y su antigüedad (la señal de "algo no está
  sincronizando"), tasa de rechazo/conflicto por tipo, latencia de push/pull, dispositivos sin
  contacto > 48 h. Primera versión: consultas SQL sobre `sync_operations`/`sync_devices`;
  dashboard después.
- Límites: lote de push 200 ops, página de pull 500 cambios, payload máx. configurable.
- Logging estructurado por ciclo de sync (device, ops, resultados, duración) con
  `core/logging.py`.

## 12. Estrategia de pruebas

- **Servidor:** unit del aplicador (idempotencia, orden, base_version) con fixtures;
  integración con Testcontainers: replay de lotes, op malformada dentro de lote válido, revoke.
- **App:** tests del motor con servidor fake — crash a media página de pull, crash post-push
  pre-ack (el replay debe devolver `already_applied`), outbox con 500 ops.
- **Escenarios dorados multi-dispositivo** (integración end-to-end, dos clientes simulados
  contra el backend real; son la especificación ejecutable del sistema):
  1. A y B capturan pedidos offline el mismo día → sincronizan → correlativos distintos y
     estables.
  2. A y B capturan la **misma boleta física** → el segundo cae a revisión como duplicado.
  3. A edita un pedido que B ya modificó → conflicto por versión → resolución manual.
  4. Crash de red a media sincronización → reintento → cero duplicados (pagos incluidos).
  5. Cambio de precio en catálogo mientras A está offline → el pedido de A se recalcula al
     aplicar y la app refleja las cifras del servidor.
  6. Dispositivo revocado con ops pendientes → wipe y rechazo de sus ops.
  7. Dispositivo 30 días offline → bootstrap parcial + drenado de outbox por lotes sin timeout.

## 13. Plan de implementación y re-secuenciación

| Fase | Contenido | Depende de |
|---|---|---|
| **PR S1 — núcleo servidor** | `SyncableMixin` (version, sync_seq, deleted_at) + secuencia global, tablas `sync_devices`/`sync_operations`, endpoints push/pull, registro de aplicadores por entidad, registro/revocación de dispositivos | — |
| **PR S2 — núcleo app** | drift + SQLCipher, esquema espejo base, outbox, motor de sync (ciclo, backoff, disparadores), bootstrap, indicadores de estado en UI | S1 |
| **PR S3 — primeras entidades** | Catálogo/prendas/promos (pull-only) y clientes (bidireccional) end-to-end; es la prueba de fuego del framework con datos reales | S1, S2 + PR 1–2 del Plan 0001 |
| **PR S4 — pedidos sync-aware** | Aplicadores de `orders` (se construyen **junto con** los PR 3–4 del Plan 0001: mismo service, dos transportes) | S3 |
| **PR S5 — conflictos y revisión** | Cola de revisión en app, flujo de duplicados de cliente/boleta, folio provisional | S4 |
| **PR S6 — calidad** | Suite de escenarios dorados (§12), métricas, límites | S4 |

**Re-secuenciación de los demás planes** (esto es lo que cambia por ser fundacional):

- **Plan 0001:** PR 1–2 (`catalog`, `customers`) pueden ir en paralelo a S1–S2, pero sus
  modelos nacen con `SyncableMixin`. Los PR 3–4 (`orders`) se implementan junto con S4: el
  service es único y lo consumen REST y el aplicador (D2).
- **Plan 0002:** la pantalla lee y escribe la BD local desde el día uno (su pregunta abierta
  #1 queda resuelta por este plan). El "total oficial del server" llega vía sync, no vía
  respuesta HTTP inmediata, cuando se está offline.
- **Plan 0003:** online-only. Sin red, el botón de escaneo se deshabilita con aviso y la
  captura manual offline sigue intacta. Encolar la foto para escaneo diferido queda como
  mejora futura de ese plan.

## 14. Settings nuevos

| Setting | Default | Notas |
|---|---|---|
| `SYNC_PUSH_MAX_OPS` | `200` | |
| `SYNC_PULL_PAGE_SIZE` | `500` | |
| `SYNC_BOOTSTRAP_ORDER_DAYS` | `90` | Pedidos históricos que baja un dispositivo nuevo |
| `SYNC_CLOCK_SKEW_WARN_S` | `300` | Umbral de aviso de reloj |
| `OFFLINE_GRACE_DAYS` | `7` | §10 |

## 15. Preguntas abiertas

1. ¿PIN/bloqueo local en la app además del login (dispositivo compartido en mostrador)?
2. ¿Cuántos días de pedidos históricos necesita ver un dispositivo offline?
   (`SYNC_BOOTSTRAP_ORDER_DAYS` — 90 es hipótesis.)
3. El endpoint de **fusión de clientes duplicados** (admin) — ¿se adelanta a esta etapa o se
   deja como plan propio cuando haya datos reales de duplicación?

## Historial

| Fecha | Cambio |
|---|---|
| 2026-07-22 | Versión inicial |
| 2026-07-27 | El Plan 0005 extiende las entidades sincronizables (§6.1/§7.3) y la taxonomía de conflictos (§8) con los módulos del registro diario. |
| 2026-07-31 | **PR S1 (núcleo servidor) implementado**: `SyncableMixin`, secuencia global `sync_seq_global`, `sync_devices`, `sync_operations`, endpoints `POST /sync/devices`, `POST /sync/devices/{id}/revoke`, `POST /sync/push` y `GET /sync/pull`. Decisiones tomadas al implementar: (a) el catálogo viaja como **entidades separadas** en el feed (`service_type`, `service_option`, `service_price`, `garment_type`), no embebido, porque la app espeja tabla por tabla (§6.3); (b) el estampado de `version`/`sync_seq` vive en un hook `before_flush` de SQLAlchemy para que un módulo nuevo no pueda olvidarlo; (c) `sync_seq` se asigna como expresión SQL (`nextval`), así que no debe leerse en Python después de escribir. Falta PR S2 (núcleo app). |
| 2026-08-01 | **PR S2 (núcleo app) implementado**: BD local cifrada con SQLCipher (llave de 256 bits por dispositivo en `flutter_secure_storage`), tablas `outbox` / `sync_state` / `review_queue` / `deferred_changes`, mixin `SyncedColumns` como base del esquema espejo, repositorio con el ciclo push → pull → reconciliar emitiendo progreso por Stream, motor con los cuatro disparadores del §5 y backoff exponencial con jitter, bootstrap paginado y pantalla `/sync`. Decisiones tomadas al implementar: (a) se abandonó `drift_flutter` — su opener no expone `isolateSetup`, necesario para cargar SQLCipher **dentro** del isolate de la BD, y su `sqlite3_flutter_libs` choca con `sqlcipher_flutter_libs` por declarar la misma clase de plugin Android; (b) los cambios del feed cuya entidad aún no tiene espejo se guardan en `deferred_changes` en vez de descartarse, porque el cursor es único y saltárselos los perdería para siempre — el PR S3 los drenará al registrar sus espejos; (c) el wipe por revocación (D11) borra las filas, hace `VACUUM` y re-cifra con `PRAGMA rekey`, ya que borrar filas deja páginas legibles dentro del archivo; (d) la app quedó **native-only**: el opener usa `dart:io` y `path_provider`, así que ya no compila para web. Falta PR S3 (primeras entidades end-to-end). |
| 2026-08-01 | **Verificado end-to-end sobre emulador Android + backend real** (`APP/integration_test/`). Se comprobó que la BD local queda ilegible en disco, que un dispositivo nuevo se registra y bootstrapea el catálogo sembrado, y que un cliente capturado offline llega a PostgreSQL con `version=1` y su operación en `applied`. La corrida destapó un bug del PR S1: `_warn_on_clock_skew` restaba un `client_ts` sin zona horaria de un `datetime` con zona y respondía **500 al push completo** — un diagnóstico tumbando la operación que observaba. Corregido en dos lados: el servidor asume UTC cuando la marca llega sin offset (una app de terceros no debería poder tirar el endpoint) y la app manda `client_ts` en UTC, porque `toIso8601String()` sobre una fecha local no lleva offset y el servidor no podía situarla en el tiempo. |
| 2026-08-02 | **PR S3 (primeras entidades) implementado**: tablas espejo de `service_type`, `service_option`, `service_price`, `garment_type` y `customer` sobre el mixin `SyncedColumns`, con `TableMirror` concentrando las dos reglas del §7.2 (tombstone en vez de borrado; una fila `pending` no se pisa con el pull). Los espejos se registran en `syncMirrorsProvider` y el ciclo arranca drenando `deferred_changes`, así que lo que llegó antes de que la entidad tuviera espejo entra sin rebobinar el cursor ni pedirle nada extra al servidor. El contrato `SyncEntityMirror` creció con `settle(entityId, rejected:)`: sin ese aviso al reconciliar, una fila capturada local se quedaba `pending` para siempre y el feed nunca podía corregirla. Catálogo y prendas se leen con `CatalogRepository` (incluye el precio vigente a una fecha, D10 como vista previa); clientes con `CustomersRepository`, que escribe la fila espejo y encola su operación **en una sola transacción** — un corte entre ambas dejaría un cliente que nadie sube o una operación sobre un cliente inexistente. Verificado contra el backend real: el catálogo sembrado aterriza en sus tablas (`deferred_changes` en 0) y un cliente capturado sin red sube y vuelve por el feed con la versión del servidor. |
| 2026-08-02 | **PR S4 (pedidos sync-aware) implementado**, junto con el PR 4 del Plan 0001 como pide §13. Servidor: las cinco tablas de `orders` entran a `FEED_ENTITIES` y las operaciones `order/create`, `order/status`, `order/deliver`, `order/cancel` y `order_payment/create` a `OPERATION_PERMISSIONS`, cada una replicada por el mismo service que usa REST (D2). App: tablas espejo de las cinco entidades y sus `TableMirror`. Decisiones tomadas al implementar: (a) **no hay un `order/update` genérico** — cada cosa que le puede pasar a un pedido exige un permiso distinto (§9 del Plan 0001), y una sola operación habría dejado que cualquiera que puede editar también entregue; (b) el pago es **su propia entidad de operación** (`order_payment/create`) y no `order/payment`, porque lo que la op crea es un pago y `entity_id` es el id que el dispositivo minteó para él — que es lo que hace idempotente el reintento; (c) un pedido viaja como **cinco filas y no como documento anidado**: cada tabla tiene su `sync_seq`, así que un pago cobrado hoy llega sin reenviar el pedido entero; (d) la tabla espejo de pedidos **no lleva `searchIndex` propio** como la de clientes — buscar por nombre se resuelve uniendo con `customer_entries`, porque copiarlo obligaría a reindexar todos los pedidos de alguien cada vez que corrigen su nombre; (e) la migración local sube a la v4 y **rebobina el cursor de pull**: `deferred_changes` cubre lo que llegó sin espejo, no lo que quedó debajo del cursor y por lo tanto nunca se mandó. Verificado contra el backend real: un pedido capturado con id propio sube por `push` con su pago, el servidor le asigna correlativo, y el reintento de la misma op responde `already_applied`. |
| 2026-08-02 | **Bug preexistente encontrado al verificar el PR S4**: cualquier respuesta que leyera `updated_at` justo después de un `UPDATE` daba **500**. La columna la llena la base (`onupdate=func.now()`), así que tras el flush el atributo queda expirado y leerlo dispara un SELECT diferido —fuera del greenlet, o sea `MissingGreenlet`, no una consulta lenta—. Afectaba también a `PATCH /customers/{id}`, que nunca se había ejercido en vivo porque la app edita clientes por el outbox. Corregido con `eager_defaults=True` en el `Base`: los valores que genera el servidor vuelven por `RETURNING` en la misma sentencia. Va en la base y no en cada modelo porque todos tienen un `updated_at` así y un esquema de respuesta que lo lee. |
| 2026-08-02 | **Permiso comodín `*.*`** en `identity`: los roles `admin` y `system_admin` lo llevan y pasan cualquier chequeo, salvo un `deny` explícito por usuario, que sigue ganando. Resuelve que un administrador perdiera acceso a cada módulo nuevo hasta que alguien le otorgara su permiso a mano — `system_admin` tenía 3 permisos y veía la app casi vacía. `CurrentUserRead` viaja ahora con `denied_permissions` para que la app aplique exactamente la misma regla que la API y no pueda mostrar una sección que el servidor va a rechazar. |
| 2026-08-03 | **`created_at` entra al feed de `order`** (y como columna anulable en el espejo local, esquema Drift v5). Es el único campo de auditoría que viaja, y viaja porque la lista del día del Plan 0006 §5.1 muestra la hora en cada tarjeta: en el mostrador los pedidos se distinguen por la hora tanto como por el número. Se aprovechó para dejar anotada una consecuencia del §8 que la app ya ejerce: un cambio de ciclo de vida que el servidor no acepta vuelve como **`conflict`**, no como `rejected` —es una regla de negocio, no un cuerpo inválido—, y ambos casos van a la cola de revisión. La fila espejo queda `rejected` y por lo tanto **deja de estar protegida del pull**, que es exactamente lo que hace que el estado optimista equivocado se corrija solo en el siguiente ciclo. |
| 2026-08-04 | **`promotion` entra al feed** (servidor → app, sin operaciones de subida, como pide §7.3). Viaja con `code`, `name`, `description`, `discount_type`, `value`, `applies_to_service_codes`, `valid_from`, `valid_to` e `is_active`: el dispositivo tiene que poder decidir **offline** qué chips ofrecer y cuánto descuenta cada uno, y esa decisión la toma con la vigencia y el tipo, no con un monto ya calculado. La promoción vencida al momento de aplicar del §8 ya la reporta el motor con su propio mensaje ("no está vigente el <fecha>"), que es lo que la cola de revisión necesita mostrar; el recálculo sugerido sin la promo es de la app y llega con UI 5 / PR S5. |
| 2026-08-04 | **`order/update` entra al push** (PR 6 del plan 0001). El registro decía que un pedido no tiene `update` genérico; la razón sigue en pie pero estaba mal nombrada: lo que no puede existir es un `update` que sirva *también* para entregar o anular, porque cada uno pide su permiso. La corrección de la boleta (§7.3 del plan 0001) es una operación propia, pide `orders.update` —y `orders.update_ready` además si el pedido ya está listo, que el service evalúa al aplicar— y es la primera operación de pedido que viaja con **`base_version`**: una boleta corregida en el mostrador mientras el teléfono estaba sin señal es exactamente el conflicto que D6 describe, y fusionar dos versiones de una boleta es adivinar. La operación no se ha usado todavía desde la app: el botón «Editar» del detalle (§5.4 del plan 0006) sigue pendiente de su fase de UI, pero el servidor ya la acepta. |
| 2026-08-04 | **PR S5 (conflictos y revisión) implementado** en la app, junto con la fase UI 9 del plan 0006. La cola de revisión del §8 deja de ser una tabla que solo se llenaba: `review_entries` ya tenía todo lo que hacía falta desde el PR S2, pero nada la leía, así que un rechazo se veía como una fila en rojo sin explicación y sin salida. Ahora `/sync/review` lista lo que espera decisión y `/sync/review/{op_id}` muestra las dos versiones y las acciones. Cuatro decisiones al implementar. **(a)** La razón legible del §8 la **escribe la app** a partir de `entity` + `op_type`, no del `reason` del servidor: ese llega en inglés y en prosa libre, y colgar de él la decisión de qué botones ofrecer habría atado la interfaz a frases que se reescriben. El texto del servidor se muestra literal y aparte, como diagnóstico. El precio es que el ejemplo del plan («esta boleta ya fue registrada como pedido #7») no se puede armar desde el motivo — haría falta que el servidor devolviera un **código** y el id del pedido que ya tiene la serie. **(b)** Ese «#7», sin embargo, sí aparece: la app lo busca en su propio espejo por `booklet_serial`. El pedido bueno ya bajó por el feed, así que el duplicado se nombra sin pedirle nada nuevo al servidor. **(c)** El estado del servidor de la comparación **no sale de la respuesta del push**. Un `ConflictError` se registra con el motivo y nada más —`server_data` viaja vacío cuando la operación ni siquiera se pudo aplicar—, así que la versión buena es la que bajó por el feed después: el espejo de una fila `rejected` deja de estar protegido justo para eso, y además es el estado de ahora y no el de aquel intento. **(d)** El contrato `SyncEntityMirror` crece con `discard(entityId)`: descartar un **alta** rechazada tiene que retirar la fila local, porque es lo único que el feed no puede corregir —del otro lado nunca existió—. Va como lápida y no como `DELETE`, que es la misma regla del D8 aplicada adentro, y en pedidos cascadea a las cinco filas para que el anticipo no quede contado en una boleta que ya no está. Reintentar, en cambio, no toca la fila: reenvía el comando y deja que el estado baje por el feed (D5), y siempre con un `op_id` **nuevo**, porque el viejo ya tiene recibo y el replay solo devolvería el mismo rechazo (D4). |
| 2026-08-05 | **`conflict` vuelve a significar lo que dice el §8**, encontrado al implementar el PR 11 del plan 0005. El aplicador mapeaba **todo** `ConflictError` a `conflict`, y esa tabla reserva la palabra para un solo caso: `base_version` vieja. Los demás que la tabla marca `rejected` —boleta duplicada, transición de estado inválida, promoción vencida, entrega con saldo— llegaban al teléfono etiquetados como choque de versión. Ahora hay `StaleVersionError`, subclase de `ConflictError` (el HTTP sigue contestando 409), y es la única que produce `conflict`. Esto **corrige la nota del 2026-08-03**: un cambio de ciclo de vida que el servidor no acepta vuelve como `rejected`. La razón que daba aquella nota —"es una regla de negocio, no un cuerpo inválido"— describía bien el hecho y eligió mal la etiqueta: `rejected` es exactamente "una regla de dominio dijo que no", y `conflict` es "otro dispositivo llegó antes". Lo que **no** cambia: los dos siguen yendo a la misma cola y `_reconcile` nunca los distinguió, así que la fila espejo se sigue soltando del pull igual. Lo que mejora: la pantalla de detalle ya ramificaba en `ReviewOutcome.conflict` para decir «alguien corrigió esta boleta mientras estabas sin señal», y ese texto se le estaba mostrando a quien no había chocado con nadie. |
| 2026-08-07 | **Prendas y cargos duplicados tras sincronizar**, que la nota (g) de UI 6 dejó comprobada y sin arreglar. `order/create` manda cantidades y elecciones, nunca ids de línea (D5): prendas, cargos y descuentos los arma el servidor con ids propios, así que cuando el feed los baja el `insertOrReplace` del §7.2 no encuentra a quién pisar e inserta filas **nuevas** — la boleta terminaba con cada línea dos veces, la vista previa y la de verdad. `OrderMirror.settle` retira ahora las que minteó el dispositivo, igual que `SupplySaleMirror` ya hacía con las líneas de una venta de insumo. Las dos condiciones del retiro hacen falta las dos: `pending` porque es lo que se capturó aquí, y `version = 0` porque sin eso **entregar** un pedido se llevaría por delante las líneas buenas (`markDelivered` las marca `pending` para escribirles el conteo, y `order/deliver` se resuelve por esta misma entidad). Una boleta **rechazada** no se toca: del otro lado no existe, no va a bajar nada, y sus líneas son lo único que dice qué se había capturado. El anticipo queda fuera porque sí viaja con el id del dispositivo y el feed lo encuentra. |
| 2026-08-07 | **El disparador de mutación local del §5 no existía.** Estaba escrito —`SyncEngine.capture()` encolaba y llamaba a `syncSoon()`— pero **ningún repositorio lo llamaba nunca**: los seis puntos de captura hablan con `SyncRepository.enqueue` directamente, porque la operación tiene que entrar en la **misma transacción** que las filas espejo, y `capture()` no puede estar dentro de ella. El resultado es que toda captura del mostrador esperaba hasta dos minutos al `Timer.periodic` para salir del dispositivo, con el indicador en «pendiente» todo ese rato. Se resolvió al revés de como estaba planteado: el motor **observa el outbox** (`watchPendingCount`) y pide ciclo cuando la cuenta **crece**. Dos razones. La de capas: las dependencias de un módulo apuntan hacia adentro (UI → State → Data), así que un repositorio no puede llamar al motor sin invertir esa flecha —y por eso `capture()` era una trampa además de código muerto: se borró—. La práctica: mirar la cola no se puede olvidar, y sirve igual para el módulo que se sume mañana. Tres precisiones. **(a)** Solo cuando crece: que baje es el propio push sacando lo que ya subió, y sin esa comprobación cada ciclo dispararía el siguiente para siempre. **(b)** La cuenta la publica drift al **confirmar** la transacción, así que el disparo nunca adelanta a la escritura. **(c)** Si el motor viene de fallar **manda el backoff**: seguir capturando durante un corte no es razón para golpear al servidor cada dos segundos, y esas operaciones saldrán en el reintento ya agendado. El `syncSoon()` explícito de la cola de revisión se queda: reintentar a mano es una decisión de una persona y sí debe saltarse el backoff, que es justamente de donde viene esa entrada. |
