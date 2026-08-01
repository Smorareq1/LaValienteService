# Plan 0002 — UI de toma de pedido (especificación de campos)

| | |
|---|---|
| **Estado** | 📝 Borrador |
| **Fecha** | 2026-07-22 |
| **Módulos afectados** | APP Flutter (pantalla de captura); consume la API del Plan 0001 |
| **Depende de** | [Plan 0001 — Pedidos diarios](../0001-pedidos-diarios/PLAN.md) |

## 1. Contexto

La pantalla de toma de pedido digitaliza la boleta física. El principio rector: **la pantalla
se lee como la boleta** — mismo orden de secciones, mismos nombres — para que el costo de
entrenamiento de colaboradores sea mínimo. Este documento especifica campos, controles y
comportamiento; el estilo visual lo define el design system existente de la app (no se
especifica aquí).

Usuarios: administradores y colaboradores internos, muchas veces de pie y con prisa. Eso manda:
controles grandes, teclado correcto por campo, mínimo tipeo (steppers y selección > texto libre).

## 2. Alcance

**Dentro:** especificación campo a campo de la pantalla de **toma de pedido**, su mapeo al
payload de `POST /orders`, y el inventario breve de las pantallas hermanas del flujo.

**Fuera:** diseño visual (design system), la pantalla de escaneo con IA
([Plan 0003](../0003-escaneo-boleta-ia/PLAN.md)) — aquí solo se reserva su punto de entrada —
y las pantallas de administración de catálogo/promociones.

## 3. Estructura de la pantalla

Secciones en el orden de la boleta física:

```
[1] Encabezado  →  [2] Cliente  →  [3] Prendas  →  [4] Observaciones
→  [5] Servicios  →  [6] Descuentos  →  [7] Pago inicial (opcional)
→  [8] Resumen fijo (subtotal / descuento / TOTAL) + acciones
```

La sección [8] es un footer persistente: el total se recalcula en vivo con cada cambio.

### 3.1 Encabezado

| Campo | Control | Req. | Default | Payload | Notas |
|---|---|---|---|---|---|
| Fecha del pedido | Date picker | ✔ | Hoy | `order_date` | Editable para capturas atrasadas |
| No. diario | Texto solo lectura | — | "Se asigna al guardar" | — | El sistema lo genera; se muestra en la confirmación |
| Serie de imprenta (#boleta) | Campo numérico | ✖ | vacío | `booklet_serial` | Teclado numérico. Si la API responde duplicado: "esta boleta ya fue registrada" |
| Peso (lbs) | Campo decimal | ✖ | vacío | `weight_lbs` | Sincronizado bidireccional con el cargo "Lavado por peso" (§3.5) |
| NIT | Campo texto | ✖ | NIT del cliente | `nit` | Botón rápido "CF". Se prellena al seleccionar cliente, editable |

### 3.2 Cliente

| Campo | Control | Req. | Notas |
|---|---|---|---|
| Buscador de cliente | Autocomplete (nombre o teléfono) | ✔ | `GET /customers?search=`; resultados muestran nombre + teléfono |
| Cliente seleccionado | Chip/card con nombre, teléfono, NIT | — | Acción "cambiar" |
| Nuevo cliente | Botón → bottom sheet | — | Nombre (req.), teléfono (recomendado — hint "casi siempre se pide"), NIT, correo, dirección, notas (opcionales). Crea vía `POST /customers` o inline en el payload (`customer`) |

### 3.3 Prendas

| Campo | Control | Req. | Notas |
|---|---|---|---|
| Lista de tipos de prenda | Stepper (−/+) por tipo | Al menos 1 pieza total | `GET /catalog/garment-types`, orden por `sort_order`. Los 21 tipos de la boleta |
| Nota por prenda | Icono → campo texto por línea | ✖ | "camisa blanca manchada" → `garments[].notes` |
| **Total piezas** | Contador en vivo, siempre visible | — | Suma automática; es el "No. Piezas" de la boleta — no se digita nunca |

### 3.4 Observaciones

Campo multilinea opcional → `observations`.

### 3.5 Servicios (cargos)

La lista se pinta **dinámicamente** desde `GET /catalog/service-types` (con opciones y precio
vigente): agregar un servicio en catálogo lo hace aparecer aquí sin tocar la app. Cada tipo de
`pricing_mode` tiene su control:

| Servicio | Control | Comportamiento |
|---|---|---|
| Lavado por peso | Toggle + campo libras | Ligado al Peso del encabezado. Muestra en vivo `lbs × Q2.50 = Qxx.xx` |
| Lavado por tina | Stepper por opción: G, E, P | Combinables (1 G + 1 P). Cada opción muestra su precio; línea muestra `N × precio` |
| Secado | Stepper por opción: T40, T50, T60 | Igual que tinas |
| Nivel (lavado a mano) | Stepper por opción: N2, N3, N4 | Hint del rango ("N3: 5–9 piezas"). Advertencia no bloqueante si las piezas no caen en el rango |
| Rins (suavizante) | Stepper | Q10 c/u, default 1 al activar, admite N (D3.1 del Plan 0001) |
| Spin (centrifugado) | Stepper | Q5 c/u, admite N |
| Tiempo extra secado (T10) | Stepper de lapsos | "lapsos de 10 min", Q5 c/u |
| Servicio urgente (SU) | Stepper | Q10 c/u, default 1 al activar, admite N |
| Recepción | Campo monto Q | Variable: lo que cobró el motorista. Vacío = no aplica |
| Entrega | Campo monto Q | Igual |

Todo stepper en 0 significa "servicio no incluido" y no genera línea en el payload.

### 3.6 Descuentos

| Campo | Control | Visibilidad | Notas |
|---|---|---|---|
| Promociones vigentes | Chips seleccionables | Todos | `GET /promotions?active_on=<fecha>`; al seleccionar, la app estima el monto y lo muestra; el server confirma al guardar |
| Descuento manual | Monto + descripción | Solo con permiso `orders.manual_discount` (admin) | Va como descuento sin `promotion_code` |

### 3.7 Pago inicial (opcional, colapsado por defecto)

| Campo | Control | Notas |
|---|---|---|
| Monto | Campo decimal | `advance_payment.amount` |
| Método | Segmented: Efectivo / Transferencia | `method` |
| Referencia | Campo texto | Solo si transferencia |
| Es anticipo | Se marca solo | `is_advance = true` si monto < total |

Recordatorio de negocio: normalmente se paga al entregar; el anticipo es raro. Por eso la
sección vive colapsada.

### 3.8 Resumen y acciones (footer fijo)

- **Subtotal**, **Descuento**, **TOTAL** (grande). Cálculo en vivo del lado de la app como
  *preview*; la cifra oficial siempre es la que devuelve `POST /orders` (D5 del Plan 0001) —
  si difieren, se muestra la del server.
- Botones: **Guardar** (`POST /orders`), **Escanear boleta** (entrada al Plan 0003),
  **Limpiar**.
- Tras guardar: pantalla de confirmación con el **No. diario asignado**, total, saldo, y
  acciones "Nuevo pedido" / "Ver detalle".

## 4. Validaciones en la UI (espejo del backend, §6.3 del Plan 0001)

La UI valida antes de enviar para dar feedback inmediato; el backend revalida siempre.

- Cliente seleccionado o creado — bloqueante.
- Al menos un cargo — bloqueante.
- Al menos una pieza en prendas — bloqueante.
- Si hay "Lavado por peso", el campo libras es obligatorio — bloqueante.
- Rango de piezas del Nivel — advertencia, no bloquea.
- Errores del server (serie de imprenta duplicada, promoción vencida) se muestran anclados al
  campo correspondiente, no como toast genérico.

## 5. Pantallas hermanas del flujo (inventario para maquetas futuras)

| Pantalla | Contenido esencial |
|---|---|
| **Pedidos del día** | Lista con filtros (fecha, estado, búsqueda por cliente/boleta), badge de estado, total del día al pie (semilla del cierre diario) |
| **Detalle de pedido** | Todo el pedido en modo lectura + acciones según estado y rol (§7.3 del Plan 0001) |
| **Entrega** | Conciliación de prendas (recibidas vs entregadas, resaltando faltantes), saldo pendiente y cobro final |
| **Registrar pago** | Monto, método, referencia; muestra saldo restante |

## 6. Offline

Resuelto por el [Plan 0004 — Offline-first](../0004-sincronizacion-offline/PLAN.md): la
pantalla lee y escribe la **BD local** siempre (con o sin red). Implicaciones para la maqueta:

- El total del footer es el cálculo local; cuando el pedido sincroniza, el server confirma la
  cifra oficial (si difiere, la app la actualiza y lo señala — 0004 D10).
- Sin conexión, la confirmación muestra el **folio provisional** (`P-3`) en lugar del No.
  diario, más un indicador de "pendiente de sincronizar"; el No. definitivo llega al sincronizar.
- La app necesita un indicador global de estado de sync (pendientes / última sincronización) y
  la pantalla de **cola de revisión** (0004 §8) en el inventario de pantallas hermanas.

## 7. Preguntas abiertas

1. ¿Se imprime o comparte un comprobante (ticket/WhatsApp) al guardar? Definiría una acción más
   en la confirmación.

## Historial

| Fecha | Cambio |
|---|---|
| 2026-07-22 | Versión inicial |
| 2026-07-22 | Offline resuelto por el Plan 0004: la pantalla opera contra la BD local, folio provisional e indicadores de sync (§6). |
| 2026-07-27 | El [Plan 0006](../0006-ui-app-fase1/PLAN.md) (PRD de UI completo) integra esta pantalla en el mapa de navegación general; las "pantallas hermanas" de §5 quedan especificadas allí (§5.1, §5.4–5.7). |
