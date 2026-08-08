# Plan 0006 — PRD de UI: módulos y pantallas de la app (fase 1)

| | |
|---|---|
| **Estado** | 🚧 En progreso |
| **Fecha** | 2026-07-27 |
| **Módulos afectados** | APP Flutter (todas las pantallas de la fase 1); `packages/design_system` |
| **Depende de** | Planes [0001](../0001-pedidos-diarios/PLAN.md) (API pedidos), [0002](../0002-ui-toma-pedido/PLAN.md) (campos de toma de pedido — se integra aquí como una pantalla del mapa), [0003](../0003-escaneo-boleta-ia/PLAN.md) (escaneo IA), [0004](../0004-sincronizacion-offline/PLAN.md) (offline/sync) y [0005](../0005-registro-diario-cierre/PLAN.md) (personal, inventario, gastos, cierre) |

## 1. Contexto

Este es el **PRD de la interfaz** de la fase 1: qué módulos tiene la app, qué pantallas
componen cada módulo, qué campos y controles lleva cada pantalla y a qué endpoint mapean.
Es el documento para maquetar y construir; el estilo visual lo resuelve el design system
existente (`packages/design_system`, diseño atómico: tokens → átomos → moléculas).

**Usuarios y contexto de uso:**

| Persona | Uso | Implicación de diseño |
|---|---|---|
| **Colaborador** | De pie en el mostrador, con prisa, ropa en las manos, a veces sin internet | Controles grandes, mínimo tipeo (steppers y selección > texto), flujos de ≤ 3 pasos, todo lo operativo funciona offline |
| **Admin (dueña/encargado)** | Administra catálogos, precios, personal; cierra el día; revisa números | Pantallas de gestión pueden ser más densas; acciones destructivas con confirmación; cierre online-only |

**Estado actual de la app:** `auth` completo (splash, login, recuperar/restablecer
contraseña), `home` placeholder, `PermissionGate` para ocultar UI por permiso, GoRouter con
auth guard, Drift listo para la BD local del Plan 0004.

**Principio rector (heredado del Plan 0002):** las pantallas se leen como los papeles que
reemplazan — la toma de pedido se lee como la boleta y la pantalla de Caja se lee como la
hoja de Registro Diario — para que el entrenamiento del personal sea mínimo.

## 2. Alcance

**Dentro:** mapa de navegación, especificación pantalla por pantalla (campos, controles,
permisos, mapeo a API/BD local), estados transversales (offline, vacío, error, carga),
inventario de componentes nuevos del design system y orden de implementación.

**Fuera:** diseño visual pixel-perfect (design system), la especificación campo a campo de
la toma de pedido (ya está en el Plan 0002) y del escaneo IA (Plan 0003), apps para
clientes finales (fase 2+), facturación SAT.

## 3. Mapa de navegación

### 3.1 Estructura general

**Shell autenticado** con barra inferior de 5 destinos + rutas apiladas:

```
┌─────────────────────────────────────────────────┐
│  AppBar contextual        [indicador sync] [👤] │
│                                                 │
│                 (pantalla activa)               │
│                                                 │
│  ┌───────┬─────────┬───────┬──────────┬──────┐  │
│  │ Inicio│ Pedidos │ Caja  │ Clientes │ Más  │  │
│  └───────┴─────────┴───────┴──────────┴──────┘  │
└─────────────────────────────────────────────────┘
```

- **Inicio** — el día de un vistazo + accesos rápidos.
- **Pedidos** — el flujo principal (lista → toma/detalle/entrega).
- **Caja** — la hoja de Registro Diario digital: ingresos, gastos, cierre.
- **Clientes** — búsqueda y gestión.
- **Más** — Insumos, Personal, Catálogo, Promociones, Sincronización, Ajustes (con
  `PermissionGate`: cada entrada aparece solo si el rol la puede usar).

El **indicador de sync** vive en el AppBar de todo el shell (§11.1). Diseño **mobile-first
vertical** (teléfono); en tablet las listas maestras pueden mostrar detalle lado a lado
(no bloquea la fase 1).

### 3.2 Árbol de rutas

| Ruta | Pantalla | Permiso mínimo |
|---|---|---|
| `/login`, `/forgot-password`, `/reset-password` | Auth (existentes) | público |
| `/home` | Inicio (§4) | sesión |
| `/orders` | Lista de pedidos (§5.1) | `orders.read` |
| `/orders/new` | Toma de pedido (§5.2 → Plan 0002) | `orders.create` |
| `/orders/scan` | Escaneo de boleta (§5.3 → Plan 0003) | `orders.create` |
| `/orders/:id` | Detalle de pedido (§5.4) | `orders.read` |
| `/orders/:id/deliver` | Entrega (§5.5) | `orders.deliver` |
| — modal | Registrar pago (§5.6) | `orders.collect_payment` |
| — diálogo | Anular pedido (§5.7) | `orders.cancel` |
| `/customers` | Lista de clientes (§6.1) | `customers.read` |
| `/customers/:id` | Detalle de cliente (§6.2) | `customers.read` |
| — sheet | Crear/editar cliente (§6.3) | `customers.create` / `update` |
| `/cash` | Caja del día (§7.1) | `expenses.read` |
| — sheet | Nuevo gasto (§7.2) | `expenses.create` |
| `/cash/supply-sale` | Venta de insumo (§7.3) | `supply_sales.create` |
| `/cash/close` | Cierre del día (§7.4) | `daily_close.close` |
| `/cash/history` | Histórico de cierres (§7.5) | `daily_close.read` |
| `/inventory` | Productos (§8.1) | `inventory.read` |
| `/inventory/:id` | Detalle de producto (§8.2) | `inventory.read` |
| — sheets | Producto / lote / movimiento (§8.3–8.5) | `inventory.manage` / `adjust` |
| `/staff` | Asistencia del día (§9.1) | `attendance.record` |
| `/staff/employees` | Empleados (§9.2) | `staff.read` |
| `/staff/settings` | Turnos y tarifas (§9.3) | `staff.manage` |
| `/catalog` | Catálogo admin (§10.1–10.2) | `catalog.manage` |
| `/promotions` | Promociones admin (§10.3) | `promotions.manage` |
| `/sync` | Estado de sincronización (§11.1) | sesión |
| `/sync/review` | Cola de revisión (§11.2) | sesión |
| `/settings` | Ajustes (§12) | sesión |

## 4. Módulo Inicio

### 4.1 Pantalla Inicio (`/home`)

**Propósito:** responder "¿cómo va el día?" en 5 segundos y lanzar las 4 acciones más
frecuentes sin buscar en tabs.

| Elemento | Contenido / control | Fuente |
|---|---|---|
| Header | Saludo + nombre + avatar (existente) | sesión |
| Banner de revisión | Solo si hay ops en cola de revisión: "N capturas necesitan tu decisión" → `/sync/review` | BD local |
| Resumen del día | 3 cifras grandes: **Ingresos / Gastos / Neto** de hoy (en vivo) + "ver Caja" | `GET /daily-close/preview` (offline: cálculo local) |
| Pedidos de hoy | Chips con contador por estado: Recibidos · En proceso · Listos · Entregados; tap filtra la lista | BD local |
| Listos para entregar | Lista corta (≤ 5): cliente, # pedido, total, saldo; tap → detalle | BD local |
| Acciones rápidas | 4 botones grandes: **Nuevo pedido**, **Registrar gasto**, **Venta de insumo**, **Asistencia** | — |

Visibilidad: cada card respeta permisos (un colaborador sin `daily_close.read` no ve el
resumen de dinero — ver matriz §13).

## 5. Módulo Pedidos

### 5.1 Lista de pedidos (`/orders`)

| Elemento | Control | Notas |
|---|---|---|
| Filtro de fecha | Chip de fecha (default hoy) + date picker | `order_date` |
| Filtro de estado | Chips: Todos · Recibido · En proceso · Listo · Entregado · Anulado | |
| Búsqueda | Campo búsqueda: cliente, # boleta, # diario | `GET /orders?search=` / BD local |
| Card de pedido | **# diario** (o folio provisional `P-n` + ícono pendiente de sync), cliente, hora, total, badge de estado, badge de saldo pendiente si > 0 | |
| Footer | Total del día (Σ pedidos listados) | Semilla visual del cierre |
| FAB | "+ Pedido" → `/orders/new` | `orders.create` |

Estado vacío: "Sin pedidos este día" + CTA nuevo pedido.

### 5.2 Toma de pedido (`/orders/new`)

Especificada campo a campo en el **[Plan 0002](../0002-ui-toma-pedido/PLAN.md)** (es la
pantalla más crítica del sistema). Resumen de secciones: Encabezado (fecha, # boleta,
peso, NIT) → Cliente (buscador + alta rápida) → Prendas (steppers por tipo, total de
piezas en vivo) → Observaciones → Servicios (dinámicos desde catálogo) → Descuentos
(promociones vigentes) → Pago inicial (colapsado) → Footer fijo subtotal/descuento/TOTAL
con **Guardar**, **Escanear boleta** y **Limpiar**.

### 5.3 Escaneo de boleta (`/orders/scan`)

Punto de entrada desde la toma de pedido; especificado en el
**[Plan 0003](../0003-escaneo-boleta-ia/PLAN.md)**. Online-only: sin conexión el botón se
deshabilita con aviso (Plan 0004 §13).

### 5.4 Detalle de pedido (`/orders/:id`)

| Sección | Contenido |
|---|---|
| Encabezado | # diario grande, # boleta, fecha, badge de estado, cliente (tap → detalle) |
| Prendas | Lista tipo × cantidad (y entregadas si aplica, resaltando diferencias) |
| Cargos y descuentos | Líneas con descripción y monto (snapshot) |
| Pagos | Lista de pagos (fecha, monto, método) + **Saldo** destacado |
| Observaciones / auditoría | Texto + quién recibió/entregó/anuló y cuándo |
| Acciones (según estado × rol, Plan 0001 §7) | **Avanzar estado** (recibido→en proceso→listo), **Entregar** (§5.5), **Registrar pago** (§5.6), **Editar** (reabre la pantalla 0002 precargada), **Anular** (§5.7) |

Un pedido de fecha ya cerrada (Plan 0005 D9) muestra candado y oculta toda acción de
edición.

### 5.5 Entrega (`/orders/:id/deliver`)

| Elemento | Control | Notas |
|---|---|---|
| Conciliación de prendas | Stepper por tipo, default = recibidas; diferencia resaltada en rojo con nota | `quantity_delivered` |
| Saldo | Cifra grande | `total − Σ pagos` |
| Cobro final | Colapsable: monto (default = saldo), método (Efectivo/Transferencia), referencia si transferencia | |
| Confirmar entrega | Botón primario; si queda saldo > 0 exige permiso `orders.deliver_unpaid` (si no lo tiene: aviso "solo un admin puede entregar con saldo") | `POST /orders/{id}/deliver` |

### 5.6 Registrar pago (modal)

Monto (default = saldo) · método segmented · referencia (solo transferencia) · saldo
restante en vivo → `POST /orders/{id}/payments`.

### 5.7 Anular pedido (diálogo)

Motivo obligatorio (multilinea) + advertencia "esta acción no se puede deshacer; para
corregir, anula y crea de nuevo" → `POST /orders/{id}/cancel`.

## 6. Módulo Clientes

### 6.1 Lista (`/customers`)

Búsqueda por nombre/teléfono (misma fuente que el autocomplete de la toma), cards con
nombre + teléfono + NIT, FAB "+ Cliente".

### 6.2 Detalle (`/customers/:id`)

Datos completos · pedidos recientes (card compacta con estado y total) · saldo pendiente
acumulado si existe · acciones: **Editar**, **Archivar** (admin, con confirmación).

### 6.3 Crear / editar (bottom sheet — la misma del Plan 0002 §3.2)

| Campo | Control | Req. |
|---|---|---|
| Nombre completo | Texto | ✔ |
| Teléfono | Teléfono | Recomendado (hint "casi siempre se pide") |
| NIT | Texto + botón rápido "CF" | ✖ |
| Correo / dirección / notas | Texto | ✖ |

## 7. Módulo Caja — la hoja de Registro Diario en pantalla

### 7.1 Caja del día (`/cash`)

**Propósito:** replicar la hoja física: ingresos a la izquierda, gastos a la derecha,
total del día — aquí como resumen + dos tabs.

| Elemento | Contenido | Fuente |
|---|---|---|
| Selector de fecha | Default hoy; fechas pasadas en solo lectura si están cerradas (candado visible) | |
| Resumen | **Ingresos · Gastos · Neto** en vivo + desglose efectivo/transferencia | `GET /daily-close/preview?date=` (offline: cálculo local) |
| **Tab Ingresos** | Lista unificada del día: pagos de pedidos (hora, # pedido, cliente, monto, ícono método) y ventas de insumos (badge "Insumo" — el azul de la hoja). Transferencias con marca visual (el rosado de la hoja) | pagos + ventas del día |
| **Tab Gastos** | Lista: chip de categoría, concepto, monto, badge "Pendiente" si `status=pending`; total al pie | `GET /expenses?date=` |
| Acciones | **+ Gasto** (§7.2) · **+ Venta de insumo** (§7.3) · **Cerrar día** (§7.4, admin, deshabilitado offline con hint "requiere conexión") | |

### 7.2 Nuevo gasto (bottom sheet)

| Campo | Control | Req. | Payload |
|---|---|---|---|
| Fecha | Date picker (default hoy; bloqueada si cerrada) | ✔ | `expense_date` |
| Categoría | Dropdown (administrable) | ✔ | `category_id` |
| Concepto | Texto ("Gas — 2 sacos") | ✔ | `concept` |
| Monto | Decimal Q | ✔ | `amount` |
| Método | Segmented Efectivo/Transferencia (default efectivo) | ✔ | `payment_method` |
| Estado | Switch "Queda pendiente de pago" (default pagado) | — | `status` |
| Observaciones | Multilinea | ✖ | `observations` |

**Campos condicionales por categoría:**

- **Horas extra** → selector de empleado (req.) + jornada del día (opcional); al elegir
  jornada con minutos extra, el monto se **sugiere** (`minutos × tarifa / 60`, editable) →
  `employee_id`, `attendance_record_id`.
- **Compra de insumos** → toggle "Crear lote de inventario" (visible solo con
  `inventory.manage`): producto, cantidad, costo unitario, precio de venta → crea lote +
  gasto vinculados (`product_lot_id`, Plan 0005 §6.3).

### 7.3 Venta de insumo (`/cash/supply-sale`)

| Elemento | Control | Notas |
|---|---|---|
| Líneas de venta | Selector de producto (card con **imagen**, nombre, stock disponible, precio) + stepper de cantidad; precio unitario visible (del lote FIFO, solo lectura); subtotal por línea | El service asigna lote; offline usa stock/precio local como preview |
| Advertencia de stock | Si cantidad > stock local: warning no bloqueante ("el servidor validará al sincronizar") | Plan 0005 D12 |
| Cliente | Buscador opcional ("venta de mostrador" si vacío) | `customer_id` |
| NIT | Texto + "CF", opcional | `nit` |
| Pago | Método segmented + referencia si transferencia | Pago inmediato, sin crédito |
| TOTAL | Cifra grande en footer fijo | Server recalcula al aplicar |
| Guardar | → `POST /supply-sales` (u op offline) | Confirmación con total |

### 7.4 Cierre del día (`/cash/close`) — online-only, admin

Pantalla-acta con los números de §6.1 del Plan 0005:

| Sección | Contenido |
|---|---|
| Totales | Ingresos por pedidos · ingresos por insumos · **Ingresos** · Gastos · **NETO** |
| Arqueo | Efectivo vs. transferencia (para cuadrar caja física) |
| Advertencias | Pedidos listos sin entregar · gastos pendientes · ops sin sincronizar de otros dispositivos (no bloquean, se muestran) |
| Notas | Multilinea opcional |
| Acción | **Cerrar día** con confirmación ("la fecha quedará bloqueada") → `POST /daily-close` |

Día ya cerrado: la misma pantalla en modo acta (solo lectura) + **Reabrir** (admin,
`daily_close.reopen`, con motivo).

### 7.5 Histórico de cierres (`/cash/history`)

Lista por fecha (neto, ingresos, gastos, quién cerró) → tap abre el acta.

## 8. Módulo Insumos (inventario)

### 8.1 Productos (`/inventory`)

Grid de cards: **imagen** (placeholder si no tiene), nombre, unidad, **stock total**,
precio de venta vigente. Búsqueda por nombre. FAB "+ Producto" (admin). Los productos
inactivos solo se ven con `inventory.manage` (toggle "mostrar archivados").

### 8.2 Detalle de producto (`/inventory/:id`)

| Sección | Contenido |
|---|---|
| Cabecera | Imagen grande, nombre, unidad, descripción, stock total |
| Lotes | Lista: **# lote**, disponible / recibido, costo unitario ("—" si desconocido), precio de venta, fecha de recepción |
| Kardex | Movimientos (tipo con ícono, cantidad, fecha, quién, notas), filtro por tipo |
| Acciones (admin) | **Editar**, **Cambiar imagen**, **+ Lote**, **Registrar uso interno / ajuste** |

### 8.3 Crear / editar producto (sheet, admin)

Nombre (req.) · unidad (req., sugerencias: bote, bolsa, galón, saco) · descripción ·
**imagen** (cámara o galería, preview, límite 5 MB — `PUT
/inventory/products/{id}/image`).

### 8.4 Alta de lote (sheet, admin)

| Campo | Control | Req. | Notas |
|---|---|---|---|
| Cantidad recibida | Decimal | ✔ | |
| Costo unitario | Decimal Q | Recomendado | Hint: "con esto sabrás tu ganancia" (Plan 0005 D3) |
| Precio de venta | Decimal Q | ✖ | Vacío = solo uso interno |
| Fecha de recepción | Date picker (hoy) | ✔ | |
| Registrar gasto | Toggle: crea el gasto de la compra (monto = cantidad × costo, editable) | — | Plan 0005 §6.3 |

El **# de lote lo asigna el sistema** y se muestra en la confirmación.

### 8.5 Movimiento manual (sheet, admin)

Tipo (Uso interno / Ajuste, segmented) · lote (selector) · cantidad · notas (req. en
ajuste — "¿por qué?") → `POST /inventory/movements`.

## 9. Módulo Personal

### 9.1 Asistencia del día (`/staff`)

**Propósito:** sustituir el "Entró 6:50 / Salió 13:00" que hoy se anota en observaciones.

| Elemento | Control | Notas |
|---|---|---|
| Fecha | Selector (default hoy) | |
| Lista de empleados activos | Card por empleado con estado del día: **Sin marcar** / **Trabajando desde HH:MM** / **Jornada completa (turno X)** | |
| Marcar entrada | Botón en card → time picker (default ahora) + turno sugerido por hora (editable) | `POST /staff/attendance` |
| Marcar salida | Botón → time picker (default ahora); si excede el turno, muestra **minutos extra sugeridos** (editable, puede quedar 0) | `PATCH /staff/attendance/{id}` |
| Pago de hora extra | Si hay minutos extra confirmados: CTA "Registrar pago (Q sugerido)" → abre Nuevo gasto (§7.2) precargado | Plan 0005 §6.2 |

Funciona offline (ops de asistencia, Plan 0005 §6.4).

### 9.2 Empleados (`/staff/employees`, admin)

Lista (nombre, teléfono, ¿tiene acceso al sistema?) · form: nombre (req.), teléfono,
**usuario vinculado** (dropdown de usuarios, opcional — Plan 0005 D6), notas, activo.

### 9.3 Turnos y tarifas (`/staff/settings`, admin)

- **Turnos:** lista editable (código, nombre, hora inicio/fin).
- **Tarifa de hora extra:** vigente destacada + historial; "Nueva tarifa" (monto + desde
  cuándo) cierra la anterior — mismo patrón UI que precios de catálogo (§10.1).

## 10. Módulo Catálogo y promociones (admin)

### 10.1 Servicios y precios (`/catalog`)

Lista de servicios (nombre, modo de precio, precio vigente, activo) → detalle: datos +
opciones (para `tiered`) + **historial de precios** con "Nuevo precio" (monto + vigente
desde → cierra el anterior). La UI deja claro que **los pedidos históricos no cambian**.

### 10.2 Tipos de prenda

Lista CRUD simple: nombre, activo, orden (drag). Los 21 de la boleta vienen sembrados.

### 10.3 Promociones (`/promotions`)

Lista con badge de vigencia (Activa / Programada / Vencida) · form: código, nombre,
descripción, tipo (Porcentaje / Monto fijo / Precio especial — segmented con hint de
ejemplo por tipo), valor, servicios a los que aplica (chips multiselección), vigencia
(desde/hasta, "hasta" opcional), activa.

## 11. Módulo Sincronización

### 11.1 Indicador global + estado (`/sync`)

**Indicador en el AppBar del shell** (siempre visible): ✓ sincronizado · ⟳ sincronizando ·
**N** pendientes (naranja) · **!** en revisión (rojo). Tap → `/sync`:

Última sincronización · pendientes por tipo (pedidos, pagos, gastos…) · **Sincronizar
ahora** · nombre del dispositivo · aviso de reloj desfasado si aplica (Plan 0004 §9).

### 11.2 Cola de revisión (`/sync/review`)

Lista de ops rechazadas/en conflicto: tipo (ícono), entidad afectada, razón legible
("esta boleta ya fue registrada como pedido #7"), fecha. Detalle: **lado a lado local vs.
servidor**, y acciones concretas por caso (Plan 0004 §8): reintentar corregido ·
descartar duplicado · ver el pedido existente · pedir a un admin. Nada desaparece solo.

## 12. Módulo Ajustes (`/settings`)

- **Perfil:** nombre, usuario, roles (solo lectura) · cambiar contraseña · cerrar sesión.
- **Dispositivos** (admin): lista de `sync_devices` (nombre, último contacto, usuario) ·
  **Revocar** con confirmación fuerte (Plan 0004 D11).
- **Usuarios y roles** (admin): alta de usuario, asignar roles, activar/desactivar —
  online-only. *(Depende de exponer endpoints CRUD en `identity`; si no están listos,
  esta entrada se oculta y la gestión se hace por script — pregunta abierta #4.)*

## 13. Matriz pantalla × rol

| Pantalla | Colaborador | Admin |
|---|---|---|
| Inicio (sin card de dinero si no tiene `daily_close.read`… ver nota) | ✔ | ✔ |
| Pedidos: lista, toma, escaneo, detalle, entrega, pagos | ✔ | ✔ |
| Anular pedido | ✔ | ✔ |
| Editar pedido en estado `ready` | solo estado/pagos | ✔ |
| Clientes: lista, detalle, crear, editar | ✔ | ✔ |
| Archivar cliente | ✖ | ✔ |
| Caja: ver día, + gasto, + venta de insumo | ✔ | ✔ |
| Editar/anular gasto · cancelar venta de insumo | ✖ | ✔ |
| Cerrar / reabrir día · histórico de cierres | ✖ | ✔ |
| Insumos: ver productos, lotes, kardex | ✔ | ✔ |
| Insumos: crear/editar producto, lotes, ajustes, imagen | ✖ | ✔ |
| Asistencia: marcar entrada/salida | ✔ | ✔ |
| Empleados, turnos y tarifas | ✖ | ✔ |
| Catálogo, prendas, promociones (gestión) | ✖ | ✔ |
| Sync: estado y cola de revisión | ✔ (sus capturas) | ✔ |
| Ajustes: perfil | ✔ | ✔ |
| Ajustes: dispositivos, usuarios | ✖ | ✔ |

Nota: la matriz deriva de los permisos RBAC (Planes 0001 §9 y 0005 §7); la UI usa
`PermissionGate` (ya existe) — **ocultar**, no deshabilitar, lo que el rol no puede usar
(excepto acciones contextuales donde el porqué importa, ej. "entregar con saldo": se
muestra deshabilitada con explicación).

Las columnas de esta matriz no se codifican como roles: la UI pregunta siempre por
permisos. La columna "Admin" sale del **permiso comodín `*.*`** que llevan los roles
`admin` y `system_admin` (implementado el 2026-08-02), y por eso un módulo nuevo aparece
para ellos el día que se publica, sin tener que otorgar nada. Un `deny` por usuario le
gana al comodín, y viaja al cliente en `denied_permissions` para que la app aplique la
misma regla que la API: ocultar algo que el servidor permitiría es un fastidio, pero
mostrar algo que va a rechazar con 403 es un error.

## 14. Estados transversales

| Estado | Regla |
|---|---|
| **Cargando** | Skeletons en listas; nunca spinner de pantalla completa tras el splash |
| **Vacío** | Ilustración ligera + texto + CTA de la acción natural ("Sin pedidos hoy → + Pedido") |
| **Error de red** | La UI opera contra BD local (Plan 0004 D1); los errores de red solo aparecen en acciones online-only (escaneo, cierre, administración) como banner con "reintentar" |
| **Error de validación** | Anclado al campo, no toast genérico (Plan 0002 §4) |
| **Offline** | Badge "pendiente de sincronizar" por registro + folio provisional `P-n` en pedidos; indicador global §11.1; acciones online-only deshabilitadas con hint |
| **Cifra oficial del servidor** | Cuando el recálculo difiere del preview local, la app actualiza y lo señala sutilmente (Plan 0004 D10) |
| **Fecha cerrada** | Candado visible; formularios de esa fecha en solo lectura (Plan 0005 D9) |
| **Sin permiso** | El elemento no se renderiza (`PermissionGate`) |

## 15. Design system — componentes a construir

Existen: tokens completos, `AppButton`, `AppTextField`, `AppChip`, `AppBadge`,
`AppAvatar`, `AppFormField`. Faltan (átomos/moléculas nuevos, en orden de necesidad):

| Componente | Tipo | Lo usan |
|---|---|---|
| `AppStepper` (− n +, táctil grande) | Átomo | Prendas, servicios, venta de insumo, entrega |
| `AppMoneyText` (formato Q#,##0.00, tamaños) | Átomo | Todas las pantallas de dinero |
| `AppSegmented` (2–3 opciones) | Átomo | Método de pago, tipos, filtros binarios |
| `AppSearchField` (con debounce y clear) | Átomo | Pedidos, clientes, productos |
| `AppStatusBadge` (estados de pedido/gasto/sync con color semántico) | Átomo | Listas y detalles |
| `AppDateChip` / `AppDateField` | Átomo | Filtros de fecha, formularios |
| `AppTimeField` | Átomo | Asistencia |
| `AppEmptyState` | Molécula | Todas las listas |
| `AppListCard` (entidad: título, subtítulo, trailing, badges) | Molécula | Pedidos, clientes, gastos, lotes… |
| `AppSummaryBar` (footer fijo subtotal/total + acción primaria) | Molécula | Toma de pedido, venta de insumo |
| `AppStatTile` (cifra grande + etiqueta) | Molécula | Inicio, Caja, cierre |
| `AppBottomSheetScaffold` (título, contenido scroll, acciones) | Molécula | Todos los formularios en sheet |
| `AppConfirmDialog` (destructivo vs normal) | Molécula | Anular, revocar, cerrar día |
| `AppImagePicker` (cámara/galería + preview + compresión) | Molécula | Imagen de producto |
| `SyncStatusIndicator` | Molécula | AppBar del shell |
| `AppTabBar` | Átomo | Caja, detalle de producto |

## 16. Orden de implementación (fases de UI)

Alineado con los PRs de backend (Planes 0001 §11, 0004 §13, 0005 §9). Cada fase es
integrable y demostrable.

| Fase | Contenido | Requiere de backend |
|---|---|---|
| **UI 1 — Shell y base** | Bottom nav + rutas + guards por permiso, componentes §15 núcleo (`AppStepper`, `AppMoneyText`, `AppListCard`, `AppEmptyState`, sheets), Inicio esqueleto | identity (ya está) |
| **UI 2 — Clientes y catálogo (lectura)** | Lista/detalle/form de clientes; catálogo en lectura para alimentar la toma | PR 1–2 |
| **UI 3 — Toma de pedido** | Pantalla 0002 completa con cálculo local en vivo | PR 3 + S2 (BD local) |
| **UI 4 — Ciclo del pedido** | Lista, detalle, estados, entrega, pagos, anulación | PR 4 |
| **UI 5 — Promociones en la toma** | Chips de descuento + admin de promociones | PR 5 |
| **UI 6 — Caja** | Caja del día (tabs), nuevo gasto, venta de insumo | PR 8–9 |
| **UI 7 — Insumos y Personal** | Productos + imagen, lotes, kardex; asistencia, empleados, turnos/tarifas | PR 7–8 |
| **UI 8 — Cierre y Dashboard** | Cierre del día, histórico, Inicio completo con resumen en vivo | PR 10 |
| **UI 9 — Sync visible** | Indicador global, pantalla de estado, cola de revisión, folios provisionales | S2–S5 |
| **UI 10 — Admin y pulido** | Catálogo/precios/prendas admin, dispositivos, escaneo IA (0003), estados vacíos/errores finales | PR 12, 0003 |

> El orden UI 6–7 puede invertirse; la venta de insumo (UI 6) necesita al menos productos
> y lotes sembrados (PR 8).

## 17. Preguntas abiertas

1. **¿Ticket/comprobante al guardar o entregar?** (impresora térmica, compartir por
   WhatsApp) — hereda la pregunta del Plan 0002; agregaría una acción en las
   confirmaciones de pedido y venta.
2. **¿El colaborador ve las cifras de dinero del día** (resumen de Inicio/Caja) **o solo
   el admin?** La matriz §14 hoy asume que sí las ve (necesita cuadrar caja al operar);
   restringirlas es cambiar `daily_close.read`/`expenses.read` en el seeder RBAC.
3. **Dispositivo objetivo del mostrador:** ¿teléfono o tablet? El diseño es mobile-first;
   si habrá tablet fija, la fase de pulido debería priorizar el layout a dos paneles.
4. **Usuarios y roles desde la app** (§13): ¿se exponen endpoints CRUD de `identity` en la
   fase 1, o la gestión de usuarios sigue por script (`create_superuser.py`) hasta la
   fase 2?
5. **Modo oscuro:** el design system tiene tokens para tema claro; ¿se requiere oscuro en
   fase 1 o después?

## Historial

| Fecha | Cambio |
|---|---|
| 2026-07-27 | Versión inicial: mapa de navegación de 5 tabs, especificación de pantallas de todos los módulos de la fase 1 (pedidos, clientes, caja, insumos, personal, catálogo, sync, ajustes), matriz pantalla × rol, estados transversales, inventario de componentes del design system y orden de implementación alineado a los PRs de backend. |
| 2026-08-02 | §13 aclarada: la visibilidad por rol se resuelve con el permiso comodín `*.*` para `admin`/`system_admin` y con `denied_permissions` viajando al cliente, no con nombres de rol en la UI. El seeder pasó a sembrar también los permisos del Plan 0005 (`expenses`, `inventory`, `staff`, `attendance`, `daily_close`), sin los cuales la matriz no se podía expresar y un colaborador no veía ni Caja ni Insumos ni Personal. Guard del router extendido a `/inventory`, `/staff`, `/catalog`, `/promotions` y `/cash/history`: ocultar el destino no basta si un deep link entra igual. |
| 2026-08-02 | **UI 2 implementada**: lista (§6.1), detalle (§6.2) y formulario en sheet (§6.3) de clientes, todo contra la BD local. Dos notas sobre lo que el plan pedía y no se pudo dar tal cual: los pedidos recientes y el saldo del §6.2 no tienen fuente hasta UI 4, así que esa sección va con estado vacío que lo dice en vez de quedar omitida; y **archivar dejó de ser un `update` con `is_active: false`**, porque el §13 lo reserva al admin y como update el servidor le habría exigido `customers.update`, que todo colaborador tiene — ahora viaja como operación `customer.archive` con su propio permiso. Del §15 se construyeron `AppSearchField` y `AppConfirmDialog`. |
| 2026-08-03 | **UI 3 implementada**: la toma de pedido (§5.2) completa, con cálculo local en vivo, contra la BD local. Las desviaciones campo a campo quedan anotadas en el [Plan 0002](../0002-ui-toma-pedido/PLAN.md); aquí importan tres cosas del mapa. La ruta `/orders/new` **no cuelga del shell** sino del nivel superior, porque el footer con el TOTAL ocupa el sitio de la barra de cinco destinos; el guard la protege con `orders.create` **además** del `orders.read` que hereda de `/orders`. Del §15 se construyeron `AppSegmented`, `AppSummaryBar` y `AppDateField`, y `AppTextField`/`AppFormField` ganaron `inputFormatters` para que un campo de dinero no acepte lo que después habría que rechazar con un mensaje. Queda pendiente de UI 4 el único punto de entrada que el plan pide y todavía no existe: el FAB de la lista de pedidos (§5.1); por ahora se llega desde la acción rápida de Inicio. |
| 2026-08-03 | **UI 4 implementada**: lista del día (§5.1), detalle (§5.4), entrega (§5.5), registrar pago (§5.6) y anulación (§5.7), todo contra la BD local con escritura optimista más su operación de sync. Tres cosas del plan **no** se pudieron dar y una se agregó. No están: **(a) Editar** en el detalle, porque `PUT /orders/{id}` es del PR 6 del Plan 0001 y las reglas de §7.3 dependen de un permiso que §9 no define —corregir es anular y volver a capturar, y así lo dice el diálogo de anulación—; **(b) el *quién*** de la auditoría: se muestra cuándo se recibió, entregó y anuló, pero no por quién, porque los usuarios no viajan por el feed y el dispositivo solo tiene ids (aparecerá cuando `identity` entre a `FEED_ENTITIES`); **(c) el candado de fecha cerrada**, que depende de `daily_close` (Plan 0005). Se agregó `created_at` al feed de `order` para poder mostrar la hora que pide la tarjeta de §5.1. Dos notas de navegación: `/orders/new` se declara **antes** del shell porque dentro de la rama vive `/orders/:id`, que también casaría con "new"; y el guard por prefijos literales no puede expresar `/orders/:id/deliver`, así que entregar se protege con el `PermissionGate` del botón y con el RBAC del servidor al aplicar la operación —que es donde el plan 0004 §10 dice que se evalúa. Entregar con saldo se muestra **deshabilitado con su explicación** en vez de esconderse, que es la excepción que §13 contempla para las acciones contextuales. |
| 2026-08-04 | **UI 5 implementada**: los chips de descuento de la toma (§5.2 → plan 0002 §3.6) y la administración de promociones (§10.3). El espejo local suma la entidad `promotion` del feed —pull-only como el catálogo—, así que **los chips funcionan sin señal**, que es la mitad del punto: una promoción que solo se puede ofrecer con internet no sirve en el mostrador. `/promotions`, en cambio, es **online-only** y así se comporta: sin red dice que esta pantalla necesita conexión y ofrece reintentar (§14), en vez de fingir una lista vieja que no se puede editar. Cuatro notas. **(a)** La lista lee de la API y no del espejo, porque aquí se escribe y una promoción editada tiene que verse tal como quedó en el servidor. **(b)** El **código no se edita**: es a lo que apuntan los pedidos ya tomados. **(c)** El badge de vigencia del plan gana un cuarto valor, **Apagada**, separado de «Vencida»: una promoción dentro de sus fechas pero desactivada a mano no está vencida, y confundirlas haría buscar el error en el calendario. Prender y apagar está en la propia tarjeta, sin abrir el formulario, porque es lo que se hace a diario. **(d)** `/promotions` se declara **fuera del shell**, apilada como las demás entradas de «Más», y el guard sigue exigiendo `promotions.manage`. Del §15 no hizo falta ningún componente nuevo; `decimalInputFormatters` se mudó de la toma de pedido al design system, que es donde podía usarlo también el formulario de promociones. |
| 2026-08-04 | **«Editar» en el detalle** (§5.4), que era la desviación (a) de la entrada de UI 4: `PUT /orders/{id}` llegó con el PR 6 y con él el permiso que faltaba. El botón se decide por estado y por permiso a la vez: un pedido `recibido` o `en proceso` lo corrige quien tenga `orders.update`, uno `listo` exige `orders.update_ready` —ya está contado, lavado y doblado— y uno entregado o anulado no ofrece el botón a nadie, que es lo que el §7.3 del plan 0001 dice y lo que el diálogo de anulación ya prometía. Se **oculta**, no se deshabilita, como manda el §13. La ruta `/orders/:id/edit` se declara **fuera del shell** por lo mismo que `/orders/new`: el footer del total ocupa el sitio de la barra de navegación. Sigue pendiente de UI 9 lo que pasa cuando la corrección **choca**: el servidor la rechaza por `base_version` y la operación cae a la cola de revisión, que todavía no tiene pantalla. |
| 2026-08-04 | **UI 9 implementada**: la sincronización deja de ser invisible. El indicador global (§11.1), la pantalla de estado y los folios provisionales ya existían desde el PR S2 y UI 3; lo que llegó ahora es la **cola de revisión** (§11.2) —que hasta hoy era un enlace roto: el banner de Inicio empujaba a `/sync/review`, una ruta que no estaba declarada— más el desglose de pendientes por tipo que §11.1 pide. Cinco decisiones. **(a)** La «razón legible» no se traduce del servidor: el motivo llega en inglés y en prosa libre (`Booklet A-4410 has already been registered.`), así que la explicación la escribe la app a partir de **qué operación era** —eso sí lo sabe con certeza— y el texto del servidor se muestra literal y aparte, rotulado como lo que es. Colgar la interfaz de una frase que mañana se reescribe habría sido construir sobre arena; el precio es que no se puede decir «ya fue registrada como pedido #7» a partir del motivo. **(b)** Pero el «#7» sí aparece, por otro camino: la app busca en su propio espejo qué pedido tiene ya esa serie de imprenta. El pedido bueno bajó por el feed, así que el duplicado se nombra sin preguntarle nada más al servidor. **(c)** El «lado a lado» del plan no compara dos versiones del mismo formulario, porque no lo son: lo local es un **comando** (lo que se capturó) y lo del servidor es **estado** (cómo quedó). Se muestran como dos bloques con sus campos naturales. Y el lado del servidor no sale de la respuesta del push —un conflicto vuelve sin `server_data`— sino del espejo local, que es el estado de ahora y no el de aquel intento. **(d)** Las acciones salen del tipo de operación, no del motivo. Una corrección que chocó ofrece **corregir de nuevo** sobre lo que el servidor tiene, no reintentar: reenviar el mismo cuerpo con la versión refrescada pisaría lo que el otro dispositivo escribió, que es justo lo que el D6 del plan 0004 llama adivinar. **(e)** Descartar un **alta** retira además la fila local, porque es lo único que el feed no puede corregir —del otro lado nunca existió— y dejarla sería un pedido fantasma en la lista del día o un pago que descuadra el saldo para siempre. Descartar cualquier otra cosa solo cierra la entrada. Del §15 no hizo falta ningún componente nuevo. |
| 2026-08-04 | **Corregir una boleta que el servidor rechazó** vuelve a darla de alta en vez de actualizarla. Salió de UI 9: la salida natural de una serie de imprenta repetida es cambiarla y volver a mandar la boleta, pero la pantalla de corrección encolaba siempre un `order/update`, y el servidor no conoce ese pedido —lo habría rechazado otra vez, ahora por inexistente. Se decide con dos señales juntas: la fila está `rejected` y sigue en la versión `0`, o sea que ninguna copia del servidor bajó nunca. No basta con la versión: una boleta que solo espera turno también está en `0`, pero su `create` va delante en el outbox. El alta reemitida se lleva el anticipo ya registrado, con su id original —el dinero recibido no es un campo editable y sin él la boleta subiría sin el pago que el cliente sí hizo. |
| 2026-08-07 | **UI 6 implementada**: la Caja del día (§7.1) con sus dos pestañas, el gasto en sheet (§7.2) y la venta de insumo (§7.3), todo contra la BD local. Con ella entran al espejo local siete entidades del registro diario —categorías de gasto, gastos, productos, lotes, ventas de insumo con sus líneas y el acta del cierre— en el **esquema Drift v7**; del §15 se construyeron `AppStatTile` y `AppTabBar`. La maqueta `Caja del día.dc.html` describe una caja que este sistema no tiene: **fondo inicial, apertura de caja, retiro a banco y un método «Tarjeta»**, ninguno de los cuales existe en el plan 0005 ni en la API — implementarlos habría sido inventar un proceso de negocio y un método de pago, así que se tomó su lenguaje visual y la única cifra suya que el modelo sí sostiene: «efectivo en caja ahora», que es el arqueo (`cash_income − cash_expenses`) que el backend ya calcula. Ocho notas. **(a)** Los dos bloques condicionales del §7.2 no están. El de **horas extra** necesita los espejos de `employee` y `attendance_record`, que son de UI 7, y su punto de entrada natural es el §9.1. El de **compra de insumos** está descrito al revés respecto de la API: el §6.3 del plan 0005 y `POST /inventory/products/{id}/lots` hacen que el **lote cree el gasto**, no que el gasto cree el lote, así que su sitio es el sheet de alta de lote (§8.4), también en UI 7. **(b) «Cerrar día» no está en el footer**: su pantalla es UI 8, y un botón que abre un marcador en medio del flujo del mostrador es peor que no tenerlo. **(c)** La tarjeta de producto muestra una inicial y **no la foto**: la imagen se sirve autenticada desde el servidor y necesita caché local para existir sin señal, que es lo que llega con `AppImagePicker` en UI 7. **(d)** La categoría del gasto se elige con **chips** y no con un desplegable: son seis y caben; abrir un menú para decir «gas» es un paso de más de pie. **(e)** Los gastos del día **no se pueden ordenar por hora** —el feed no manda el `created_at` de un gasto— así que se agrupan por categoría, que es como los agrupa el papel, en vez de quedar en el orden arbitrario en que el pull los insertó. **(f)** El aviso de stock **no bloquea** (plan 0005 D12) y el stock que se muestra ya viene descontado de las ventas capturadas sin señal: la fila del lote sigue diciendo lo que el servidor sabía —el pull no pisa filas `pending`— y sin ese descuento el último bote se podría vender dos veces sin que la pantalla dijera nada. **(g)** Las líneas de una venta capturada aquí son una **vista previa**: el mostrador elige productos y cantidades, y qué lote las cubre y a qué precio lo decide el FIFO del servidor, que crea sus propias filas con ids suyos. Por eso `SupplySaleMirror.settle` las retira cuando la venta se aplica; dejarlas dejaría cada línea dos veces. **Eso destapó el mismo defecto en pedidos, donde no está resuelto**: una boleta capturada en el teléfono termina con cada prenda y cada cargo duplicados después de sincronizar. Comprobado y anotado aparte; no se arregló aquí porque toca cinco tablas y el camino de corrección. **(h)** `/cash/supply-sale` era un **enlace roto**: Inicio empujaba a esa ruta desde UI 1 y nunca se había declarado, igual que le pasó a `/sync/review` hasta UI 9. Dos mudanzas de paso: `PaymentMethod` se fue a `core/money` (lo comparten pagos, gastos y ventas) y `CustomerSection` es ahora `customers/ui/widgets/customer_picker.dart`, para que la Caja no tenga que importar del módulo de pedidos para preguntar por un cliente. |
