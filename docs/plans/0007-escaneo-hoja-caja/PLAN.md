# Plan 0007 — Escanear la hoja de caja, y compartir desde WhatsApp

| | |
|---|---|
| **Estado** | ✔️ Implementado |
| **Fecha** | 2026-08-09 |
| **Módulos afectados** | `intake_scan` (backend), `orders` (una consulta nueva), `identity` (un permiso), APP Flutter (`scan`, `orders`, `cash`, `customers`) |
| **Depende de** | [Plan 0003 — Escaneo de boleta con IA](../0003-escaneo-boleta-ia/PLAN.md) y [Plan 0005 — Registro diario y cierre](../0005-registro-diario-cierre/PLAN.md) |

## 1. Contexto

El plan 0003 dejó el escaneo funcionando para **un** documento: la boleta del talonario. Pero el
flujo en papel de la lavandería tiene dos, y el segundo es el que más se teclea: la hoja
**«Registro Diario»** del plan 0005 §1, donde al final del día están las boletas cobradas con su
monto, los gastos con su descripción, y las horas del personal. Meter eso a mano son quince
filas de números escritos a lápiz, todas las tardes.

Y hay un detalle de cómo llega el papel que cambia el diseño: **las fotos llegan por WhatsApp.**
Alguien fotografía el talonario y lo manda al grupo. Hasta ahora eso significaba guardar cada
imagen, abrir la app, entrar al escaneo y buscarla en la galería — cuatro pasos por boleta,
catorce veces.

## 2. Alcance

**Dentro:**

- Lectura de la hoja «Registro Diario» con su propio prompt y su propio esquema
  (`POST /scans/cash-close`), incluidos los tres bloques-día que caben en una página.
- Emparejamiento de cada fila contra la base: la boleta que un `#Tomapedido` nombra y el saldo
  que debe, la categoría a la que apuntan unas palabras, el empleado de unas horas.
- **Importación** de un día confirmado (`POST /scans/cash-close/{id}/apply`): cobros, entregas,
  gastos y jornadas, fila por fila, con su permiso propio.
- Recepción de imágenes compartidas desde otra app en Android, en lote, con la pregunta de qué
  documento son.
- Completar el prellenado de la boleta que el plan 0003 dejó a medias: la fecha leída y el
  cliente —confirmado o dado de alta con lo que la foto ya dice—.
- Una pantalla de espera que dice en qué va la lectura, compartida por los dos escaneos.

**Fuera:**

- **Ventas de insumo.** Las filas azules de la hoja se leen, se marcan y se explican, pero no se
  importan: un `supply_sale` necesita producto y lote (plan 0005 §6.3), y un nombre escrito en
  una columna no alcanza para elegirlos. La fila avisa y manda a la pantalla de venta.
- **iOS.** El filtro de intents es de Android. En iOS compartir exige un *Share Extension* —un
  target nativo aparte, con su App Group— que no se puede añadir editando ficheros del proyecto
  Flutter. El resto de la app funciona igual; lo que falta es la entrada desde la hoja de
  compartir.
- **Set dorado.** El §9 del plan 0003 pide medir un prompt antes de subirle la versión.
  `cash_v1` es la versión inicial y no hay contra qué compararla todavía.

## 3. Glosario

| Término | Significado |
|---|---|
| Hoja / Registro Diario | La página impresa con tres bloques, uno por día |
| Bloque | Un día dentro de la hoja: sus ingresos, sus gastos y sus horas |
| Fila de ingreso | Una boleta cobrada: `#Tomapedido`, cliente, monto y la marca de color |
| Fila de gasto | Dinero que salió: objeto, descripción, monto y observaciones |
| Marca | El resaltador: rosado = transferencia, amarillo = factura, azul = venta de insumo |

## 4. Decisiones de diseño

**D1 — Dos prompts, dos versiones, un módulo.** La hoja no se lee con el prompt de la boleta:
es otro papel, con otra estructura y otro vocabulario. `SCAN_CASH_PROMPT_VERSION` se versiona
aparte de `SCAN_PROMPT_VERSION` para que mejorar una lectura no obligue a revalidar la otra. Lo
que sí comparten es todo lo demás — transporte, reintento, timeout, presupuesto diario y tabla
—, porque nada de eso depende del papel.

**D2 — Los montos de la hoja sí se leen.** En la boleta está prohibido (plan 0003 D4): los
precios los pone el catálogo. Aquí no hay catálogo del que recalcular el dinero de un día — la
hoja *es* el registro. Lo que sustituye al motor de precios como guardia es la base: cada fila
de ingreso se empareja con la boleta que nombra, y **lo que se puede cobrar está acotado por el
saldo que esa boleta debe**. Un 150 mal leído sobre una boleta de 115 cobra 115.

**D3 — Importar escribe, y por eso tiene puerta propia.** `scans.import_close` no lo tiene el
colaborador. Los permisos que usa por debajo (`orders.collect_payment`, `orders.deliver`,
`expenses.create`) sí los tiene, y eso no es una contradicción: están pensados para cobrar una
boleta con el cliente enfrente. Quince de un tirón desde una fotografía es otra decisión.

**D4 — Lo que se aplica no es lo que se leyó.** `CashSheetApply` no comparte un solo tipo con el
borrador y no lleva ninguna `confidence`: son ids y montos que una persona dejó en la pantalla.
La foto propone, la persona dispone, y el tipo del cuerpo de la petición es lo que hace que esa
frase no dependa de que nadie se salte un paso.

**D5 — Fila por fila, nunca todo o nada.** Una transacción única perdería las catorce filas
buenas por la mala, y quien está en el mostrador tendría que averiguar a mano cuáles pasaron.
Cada fila es su propio intento y la respuesta dice qué fue de cada una: `applied`, `skipped` o
`failed`, con el motivo.

**D6 — Idempotencia por id derivado, y un candado aparte.** Los ids de los pagos y gastos que
crea una importación salen de `uuid5(scan_id, tipo, índice)`, así que un reintento cae sobre las
filas que ya existen y los services contestan «ya está registrada» — que es lo que convierte una
conexión caída en una repetición y no en un cobro doble. El candado de `applied_at` resuelve un
problema distinto: fotografiar **otra vez** la hoja de ayer, donde nada colisionaría porque la
primera importación ya movió los saldos.

**D7 — Nada llega marcado si hay que mirarlo.** Solo se marca por omisión la fila que el
servidor emparejó y cuyo monto cuadra con el saldo. Un desajuste, una boleta que no aparece, una
ya cobrada: desmarcadas. Marcarlas sería esconderlas en una lista de quince.

**D8 — La pregunta de qué documento es se hace por foto.** Un lote compartido es casi siempre de
un solo tipo, pero preguntar una vez por lote dejaría la cola muda después de la primera. Se
pregunta al empezar cada una: un toque de más cada catorce, y a cambio la cola avanza sola.

**D9 — El serial se compara sin ceros de relleno.** El talonario imprime `Nº 000939`; la hoja
escribe `939`. Son la misma boleta. `serial_key` deja caer los ceros a la izquierda **solo** de
un serial todo-dígitos: en `A-0042` nadie dijo que los ceros sobren.

## 5. Modelo de datos

Sin tablas nuevas. Sobre `scan_jobs`:

| Cambio | Por qué |
|---|---|
| `scan_purpose` gana el valor `cash_close` | Tercer documento, mismo registro. Separa la métrica de calidad del §9, que solo tiene sentido sobre lo que tuvo borrador |
| `applied_at` (nullable) | Cuándo se importó la hoja. Es el candado de D6 |
| `applied_result` (JSONB) | Qué hizo esa importación, fila por fila |

Migración `20260809_0015`. El `ALTER TYPE … ADD VALUE` va en su propio `autocommit_block`,
porque Postgres no deja usar un valor nuevo en la misma transacción que lo añade.

## 6. Reglas de negocio

### 6.1 Qué se lee de la hoja

Hasta tres bloques por foto; solo los que tienen algo escrito. Por bloque: fecha, filas de
ingreso, filas de gasto, la línea de entrada/almuerzo/salida y las tres sumas del pie.

Las sumas escritas **solo comparan** (`income_sum_mismatch:1130:1095`). Un desajuste casi
siempre señala una fila que el modelo no vio.

### 6.2 Cómo queda cada fila de ingreso

| Estado | Qué pasó | ¿Marcada? |
|---|---|---|
| `matched` | La boleta existe y debe justo eso | Sí |
| `amount_mismatch` | Existe, pero el papel y el saldo no coinciden | No |
| `not_found` | Ninguna boleta lleva ese número | No |
| `settled` | Ya está cobrada | No |
| `cancelled` | Está anulada | No |
| `supply` | Fila azul: venta de insumo | No |
| `unreadable` | El número no se leyó | No |

`amount_suggested` nunca supera el saldo (D2). `can_deliver` distingue cobrar de entregar: la
hoja registra boletas que se pagan y se quedan en la lavandería.

### 6.3 Cómo queda cada fila de gasto

El concepto se arma con las dos columnas escritas, porque ninguna basta sola («gas» no dice
cuánto gas, «2 sac» no dice de qué). La categoría se busca primero por su propio nombre y
después por el vocabulario de la hoja, y se compara **por palabras enteras**: «gas» dentro de
«gastos» no es una compra de gas. Sin categoría la fila igual vuelve —con su monto y su
descripción— para que una persona la archive.

Las observaciones deciden dos cosas: «pago atrasado» o «pendiente» lo dejan `pending` (el papel
lo cuenta para el día aunque el cajón no lo haya visto), y «transferencia» o «depósito» cambian
el método.

### 6.4 La fecha del papel y la fecha del dinero

Importar la hoja de un día pasado **no** retrocede el dinero: `OrdersService.add_payment` sella
el cobro con hoy (plan 0001 D1), así que cae en el cierre de hoy. Se dice antes de confirmar
nada (`sheet_not_today:2026-08-04`) porque es exactamente lo que sorprendería después.

## 7. API v1

| Endpoint | Permiso | Nota |
|---|---|---|
| `POST /scans/cash-close` | `scans.create` | Lee la hoja. No escribe nada |
| `POST /scans/cash-close/{id}/apply` | `scans.import_close` | Un día por petición. 409 si ya se importó |
| `GET /scans/cash-close/{id}` | `scans.read` | La lectura otra vez, con `already_applied` si toca |

Los tres van declarados **antes** de `/{scan_id}`: "cash-close" no es un UUID, pero esa ruta
casaría primero y contestaría un error de validación en vez de una lectura.

## 8. La app

- **Pantalla de espera** (`ScanReadingView`), compartida: un barrido sobre la foto y los pasos
  de la lectura avanzando con su tiempo estimado. Honesta en el orden, estimada en el tiempo —
  la app no puede saber en qué va el servidor, pero sí qué hace y en qué orden.
- **Revisión del cierre** (`CashSheetScanScreen`): una lista de filas con casilla, no un botón
  de importar. Monto editable, método, y «entregar la ropa también» aparte. Abajo, cuánto se va
  a cobrar antes de tocar nada.
- **Compartir**: dos `intent-filter` (`SEND` y `SEND_MULTIPLE`, `image/*`) y una cola que no
  pierde el lote. La elección de qué documento es abre la lectura; terminarla saca la foto de la
  cola y vuelve a preguntar por la siguiente.
- **Prellenado completo de la boleta**: la fecha leída rehace el libro de precios *antes* de
  volcar las líneas —una boleta atrasada se cobra con lo que regía ese día—, y el cliente llega
  confirmado desde la pantalla del escaneo o dado de alta ahí mismo con el nombre, el teléfono y
  la dirección que la foto leyó.

## 9. Preguntas abiertas

1. **Las filas azules.** ¿Vale la pena emparejar el insumo por nombre y proponer la venta, o el
   volumen (una o dos filas al día) no lo justifica?
2. **El set dorado de `cash_v1`.** Hacen falta hojas reales anotadas para poder medir un `v2`.
   Las de `dev/examples/scans/` son el principio.
3. **iOS.** Cuándo se añade el Share Extension, que es trabajo de proyecto nativo y no de Dart.
4. **Horas del personal.** Hoy la jornada se importa con entrada y salida, pero los minutos de
   hora extra siguen confirmándose a mano (plan 0005 §6.2 paso 3). ¿Debería la importación
   proponerlos?

## Historial

| Fecha | Cambio |
|---|---|
| 2026-08-09 | Versión inicial e implementación |
