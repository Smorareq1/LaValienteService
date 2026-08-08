# Plan 0003 — Escaneo de boleta con IA (módulo TEMPORAL)

| | |
|---|---|
| **Estado** | ✅ Implementado (2026-08-08) — falta el set dorado del §9 |
| **Fecha** | 2026-07-22 |
| **Módulos afectados** | Backend: `intake_scan` (nuevo, aislado). APP: pantalla de escaneo |
| **Depende de** | [Plan 0001](../0001-pedidos-diarios/PLAN.md) (pedidos), [Plan 0002](../0002-ui-toma-pedido/PLAN.md) (formulario que se prellena) |
| **Carácter** | ⚠️ **TEMPORAL** — puente mientras conviven papel y sistema; ver plan de retiro (§11) |

## 1. Contexto

Mientras la operación siga llenando la boleta física, la vía más rápida para digitalizar un
pedido es **fotografiarla**: el sistema extrae los campos con un modelo de visión (Gemini) y
prellena el formulario del Plan 0002; la persona solo corrige lo que no se reconoció bien y
guarda. Es el módulo de accesibilidad principal para colaboradores y administradores, así que
el estándar de calidad es alto: extracción confiable, confianza por campo, validación
determinista posterior, y un circuito de mejora continua con datos reales.

**Principio inviolable:** la IA nunca guarda un pedido. Produce un **borrador** que un humano
revisa y confirma. El motor de precios del Plan 0001 (§6) recalcula siempre los montos — los
totales leídos de la foto solo sirven para detectar discrepancias.

## 2. Alcance

**Dentro:** módulo backend `intake_scan` (endpoint, cliente Gemini, prompt versionado,
validación/normalización, matching de cliente, persistencia de trabajos de escaneo y
correcciones), flujo UI de escaneo, evaluación con set dorado y telemetría de correcciones.

**Fuera:** backfill masivo de boletas históricas (posible etapa posterior usando esta misma
infraestructura), entrenamiento de modelos propios.

## 3. Decisiones de diseño

- **D1 — La API key de Gemini vive solo en el backend** (`core/config.py`), nunca en la app
  Flutter. La app sube la foto a nuestra API; el backend habla con Gemini. Esto además permite
  versionar prompts, medir, cachear y limitar costos en un solo lugar.
- **D2 — Módulo aislado y desechable.** Todo vive en `src/modules/intake_scan/` + sus
  endpoints. Ninguna tabla de `orders` depende de él (la referencia va en dirección
  `scan_jobs.order_id → orders`, nullable). Borrar el módulo el día del retiro no toca nada
  del núcleo. Feature flag `SCAN_ENABLED` para apagarlo sin desplegar.
- **D3 — Salida estructurada, no texto libre.** Se usa el modo de salida estructurada de
  Gemini (`responseSchema`) con un JSON Schema estricto (§6). Cada campo trae `value`,
  `confidence` (0–1) y `raw_text` (lo que el modelo leyó literalmente).
- **D4 — Extraer selecciones y cantidades, nunca precios.** Del papel se leen "2 tinas G",
  "T50", "12.5 lbs" — los montos los pone el catálogo vigente vía el motor de §6 del Plan
  0001. Así un precio mal leído jamás contamina un pedido.
- **D5 — Prompt versionado en el repo** (`src/modules/intake_scan/prompts/vN.md`), con
  changelog. Cambiar el prompt exige correr la evaluación (§9) y comparar contra la versión
  anterior. `scan_jobs` registra qué versión de prompt y modelo produjo cada extracción.
- **D6 — Cliente de IA detrás de una interfaz** (`ScanExtractor` protocolo). Gemini es la
  implementación actual; poder cambiar de proveedor o modelo es un archivo, no una reescritura.
- **D7 — Human-in-the-loop medible.** Al crear el pedido desde un escaneo, se guarda el diff
  entre el borrador propuesto y lo que la persona realmente guardó. Ese dataset de correcciones
  es la métrica de calidad real y la materia prima para mejorar el prompt.
- **D8 — Degradación elegante.** Si Gemini falla, tarda o devuelve basura: la persona captura
  manual con el Plan 0002, que funciona 100% sin este módulo. El escaneo es aditivo, jamás un
  punto único de falla. Este módulo es **online-only** (Plan 0004 §7.3): sin conexión el botón
  de escaneo se deshabilita con aviso y la captura manual offline sigue intacta; encolar la
  foto para escaneo diferido queda como mejora futura.
- **D9 — Privacidad.** Las fotos contienen datos personales (nombre, teléfono, NIT). Retención
  limitada y configurable (`SCAN_IMAGE_RETENTION_DAYS`, default 90), almacenamiento fuera del
  árbol público, acceso solo con permiso.

## 4. Flujo completo

```mermaid
sequenceDiagram
    participant APP as App Flutter
    participant API as Backend (intake_scan)
    participant G as Gemini API

    APP->>APP: Cámara con guía de encuadre, compresión
    APP->>API: POST /scans (imagen)
    API->>API: Normalizar (rotación EXIF, reescalar, validar tamaño)
    API->>G: imagen + prompt vN + responseSchema
    G-->>API: JSON: campos + confidence + raw_text
    API->>API: Validación determinista y normalización (§7)
    API->>API: Matching de cliente (sugerencia, nunca auto-crea)
    API->>API: Recalcular montos con el catálogo (motor Plan 0001 §6)
    API-->>APP: Borrador de pedido + confianza por campo + advertencias
    APP->>APP: Formulario 0002 prellenado; campos dudosos marcados
    Note over APP: La persona corrige y confirma
    APP->>API: POST /orders (con scan_id)
    API->>API: Crear pedido + guardar diff borrador→final en scan_job
```

### UX de confianza en el formulario

| Confianza | Tratamiento en UI |
|---|---|
| ≥ 0.85 | Prellenado normal |
| 0.50 – 0.85 | Prellenado con marca visual "revisar" |
| < 0.50 o ilegible | Campo vacío, marcado, y el foco inicial va al primero de estos |

Umbrales configurables. La app muestra un banner "Boleta escaneada — revisa los campos
marcados antes de guardar" y permite ver la foto original lado a lado (split o toggle) durante
la revisión.

## 5. Modelo de datos

**`scan_jobs`** (módulo `intake_scan`)

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `status` | enum `processing` \| `completed` \| `failed` | |
| `image_path` | `String(500)` | Ruta en `SCAN_STORAGE_PATH`; nombre UUID |
| `model` | `String(80)` | Ej. `gemini-2.5-flash` |
| `prompt_version` | `String(20)` | Ej. `v3` |
| `raw_response` | `JSONB` | Respuesta cruda del modelo (auditoría/replay) |
| `extracted` | `JSONB` | Borrador ya validado/normalizado que se envió a la app |
| `warnings` | `JSONB` | Discrepancias detectadas (ej. total leído ≠ total calculado) |
| `error` | `Text` \| null | Si `failed` |
| `latency_ms` | `Integer` \| null | |
| `order_id` | FK → `orders` \| null | Se llena si el borrador terminó en pedido |
| `corrections` | `JSONB` \| null | Diff campo a campo borrador → pedido final (D7) |
| `created_by_id` | FK → `users` | |
| `created_at` / `updated_at` | | |

Índices: `created_at`, `order_id`, `status`.

## 6. Contrato de extracción (responseSchema)

Esquema que Gemini debe devolver — cada hoja es `{value, confidence, raw_text}`:

```jsonc
{
  "header": {
    "date": {"day": …, "month": …, "year": …},   // las X del talonario = null
    "daily_number": …,                            // "No."
    "booklet_serial": …,                          // "#Tomapedido" impreso
    "weight_lbs": …,
    "nit": …                                      // campo impreso como "Factura"
  },
  "customer": { "full_name": …, "phone": …, "address": …, "email": … },
  "garments": [ {"name": …, "quantity": …} ],     // solo filas con cantidad
  "observations": …,
  "services": {
    "wash_by_weight_lbs": …,
    "wash_tub":   {"G": n, "E": n, "P": n},       // cantidad por opción (admite N)
    "dry":        {"T40": n, "T50": n, "T60": n},
    "hand_wash":  {"N2": n, "N3": n, "N4": n},
    "extras":     {"rins": n, "spin": n, "t10_lapses": n, "urgent": n},
    "pickup_amount": …, "delivery_amount": …
  },
  "discounts_marked": [ … ],                      // qué promo marcaron, si se distingue
  "totals_read": { "subtotal": …, "discount": …, "total": … }  // SOLO para cross-check
}
```

### Lineamientos del prompt (v1)

El prompt describe la boleta **zona por zona** (es un formato fijo — eso es una ventaja
enorme): dónde está cada campo, qué significan G/E/P, T40–T60, N2–N4, R/S/T10/SU, que una
**X** significa campo anulado/no usado, que la letra es manuscrita en español de Guatemala,
moneda Q, confusiones típicas de manuscrito (1/7, 4/9, 5/S, 0/6), y la lista cerrada de los
21 tipos de prenda para restringir `garments[].name`. Instrucción explícita: si un campo no se
lee con certeza, bajar `confidence` — **nunca inventar**. Mejora prevista v2: few-shot con 2–3
fotos anotadas dentro del prompt multimodal.

## 7. Validación y normalización (determinista, en nuestro service)

Después de Gemini y antes de responder a la app:

1. **Formatos:** teléfono (8 dígitos GT), NIT (patrón `\d+-?[\dKk]` o "CF"), fecha plausible
   (± tolerancia respecto a hoy; default = hoy si venía en blanco/X).
2. **Prendas:** mapear `garments[].name` a `garment_types` por normalización (minúsculas, sin
   acentos) + alias ("pants" → Pants/Pijama). Lo no mapeable se descarta con warning.
3. **Coherencia:** si hay `wash_by_weight_lbs` debe haber peso; suma de prendas vs. piezas;
   nivel vs. rango de piezas (solo warning, como en captura manual).
4. **Recalcular:** construir los cargos con el catálogo vigente y comparar contra
   `totals_read`. Discrepancia > Q1 ⇒ warning visible ("el total de la boleta dice Q108.75,
   el sistema calcula Q98.75 — revisa cantidades") — suele delatar una cantidad mal leída.
5. **Cliente:** buscar por teléfono exacto y nombre fuzzy. Respuesta incluye
   `customer_match: {customer_id, score} | null`; la app muestra "¿Es María López,
   5512-3456?" con opciones usar / crear nuevo / buscar. **Nunca** se auto-crea.

## 8. API y configuración

### Endpoints

| Método y ruta | Permiso | Descripción |
|---|---|---|
| `POST /scans` (multipart) | `scans.create` | Síncrono (timeout `SCAN_TIMEOUT_S`, default 30 s). Devuelve el borrador completo + `scan_id`. 503 con mensaje claro si `SCAN_ENABLED=false` o falla el proveedor |
| `GET /scans/{id}` | `scans.read` | Re-consultar un escaneo (y su foto) |
| `POST /orders` (existente) | — | Acepta `scan_id` opcional: enlaza el pedido y dispara el cálculo del diff de correcciones (D7). Cambio retrocompatible al Plan 0001 |

Permisos `scans.create` / `scans.read`: admin ✔, colaborador ✔ (es el módulo de accesibilidad
de todos).

### Settings nuevos (`core/config.py`)

| Setting | Default | Notas |
|---|---|---|
| `SCAN_ENABLED` | `false` | Feature flag global |
| `GEMINI_API_KEY` | — | Secreto; solo backend (D1) |
| `SCAN_MODEL` | `gemini-2.5-flash` | Configurable sin desplegar código |
| `SCAN_PROMPT_VERSION` | `v1` | Debe existir en `prompts/` |
| `SCAN_TIMEOUT_S` | `30` | |
| `SCAN_MAX_IMAGE_MB` | `8` | Se reescala server-side a ≤ ~2 MP antes de enviar |
| `SCAN_DAILY_LIMIT` | `200` | Tope de escaneos/día (control de costos) |
| `SCAN_STORAGE_PATH` | — | Directorio de imágenes |
| `SCAN_IMAGE_RETENTION_DAYS` | `90` | Limpieza vía script en `scripts/` (D9) |

Resiliencia: 1 reintento con backoff ante error transitorio del proveedor; presupuesto de
latencia total dentro del timeout; logging estructurado (scan_id, modelo, prompt, latencia,
resultado) con el logger de `core/logging.py`.

## 9. Evaluación y mejora continua

Esto es lo que separa "demo que a veces funciona" de un módulo confiable:

- **Set dorado:** `tests/fixtures/scan_golden/` con fotos reales de boletas + su JSON esperado
  anotado a mano. Arrancar con ≥ 20 boletas variadas (letras distintas, fotos torcidas, con
  sombra, campos con X). Crece con los casos que la operación reporte como mal leídos.
- **Script de evaluación:** `scripts/eval_scan.py` — corre el set dorado contra el proveedor
  real y reporta **exactitud por campo** (header, cliente, prendas, servicios), comparando
  además contra la última corrida guardada. Se ejecuta manualmente al cambiar prompt o modelo
  (no en CI: requiere key y cuesta dinero). Regla: no se sube una versión de prompt que
  empeore la exactitud global.
- **Métrica de producción (D7):** de `scan_jobs.corrections` salen las dos cifras que
  importan: *% de campos corregidos por pedido* y *campos más corregidos* (ranking). Consulta
  SQL simple al inicio; dashboard después si lo amerita.
- **Tests:** unit del mapeo/validación/normalización con fixtures JSON (sin red); integración
  del endpoint con un `ScanExtractor` fake (D6). El proveedor real solo se toca en la eval.

## 10. Plan de implementación

| Fase | Contenido | Depende de |
|---|---|---|
| **PR A — esqueleto** | Módulo `intake_scan` (models, migración `scan_jobs`, settings, feature flag, storage de imágenes, permisos) | Plan 0001 PR 3 |
| **PR B — extracción** | `ScanExtractor` + implementación Gemini, prompt v1, responseSchema, endpoint `POST /scans` con normalización de imagen | PR A |
| **PR C — inteligencia de dominio** | Validación determinista (§7), matching de cliente, recálculo y warnings de discrepancia | PR B |
| **PR D — cierre del loop** | `scan_id` en `POST /orders`, cálculo del diff de correcciones, `GET /scans/{id}` | PR C |
| **PR E — calidad** | Set dorado inicial, `eval_scan.py`, script de retención de imágenes | PR B (paralelo a C/D) |
| **APP** | Pantalla de cámara con guía, envío, formulario 0002 prellenado con badges de confianza y vista de foto lado a lado | PR C |

## 11. Plan de retiro (por eso es TEMPORAL)

- **Criterio de retiro:** cuando la operación abandone el talonario físico (o el % de pedidos
  originados por escaneo caiga sostenidamente), se apaga con `SCAN_ENABLED=false` y en la
  siguiente ventana se elimina el módulo.
- **Qué se conserva:** la tabla `scan_jobs` se archiva (histórico de correcciones = evidencia
  de calidad de datos) o se exporta y se elimina, a decisión de ese momento.
- **Qué garantiza el diseño:** por D2, el retiro es borrar `src/modules/intake_scan/`, sus
  endpoints, sus settings y una migración de limpieza. Cero impacto en `orders`.

## 12. Preguntas abiertas

1. **Almacenamiento de imágenes:** ¿disco del servidor basta (con backup) o desde ya un bucket
   S3-compatible? Afecta `SCAN_STORAGE_PATH` y el script de retención.
   → **Disco, por ahora.** `storage.py` guarda bajo `SCAN_STORAGE_PATH` archivando por día
   (`2026/08/08/…`), que es lo que hace que la retención de D9 borre un rango de directorios en
   vez de recorrer cada archivo mirando su fecha. Mudarlo a un bucket es reescribir ese módulo
   —cuatro funciones— sin tocar la columna, igual que la foto de producto del plan 0005 D10.
2. **Presupuesto mensual** para Gemini: define `SCAN_DAILY_LIMIT` real (con Flash el costo por
   escaneo es de centavos, pero conviene fijar tope).
   → **Sigue abierta**, y es la única que necesita una respuesta de la dueña. El tope está
   implementado y puesto en 200/día, contado sobre toda la lavandería y no por usuario —el
   presupuesto es del negocio, no de quien esté en el mostrador—. Ese número es un marcador
   hasta que haya una cifra real.
3. ¿Interesa el **backfill** de boletas históricas en lote cuando el módulo esté estable?
   → **No entra en la fase 1** y no se implementó nada hacia ahí. El §2 ya lo dejaba fuera;
   se anota que la infraestructura que haría falta —extractor, validación, `scan_jobs`— ya
   existe, así que el día que interese es un script, no un módulo.
4. ¿La foto se toma solo desde la cámara o también se acepta **galería** (fotos que mandan por
   WhatsApp)? Recomendado: ambas.
   → **Ambas**, como recomendaba. La pantalla ofrece los dos botones y `TicketPhotoPicker` los
   trata igual. Obligar a re-fotografiar la pantalla del teléfono para una boleta que ya llegó
   por WhatsApp habría sido trabajo inventado.

## Historial

| Fecha | Cambio |
|---|---|
| 2026-07-22 | Versión inicial |
| 2026-07-22 | D8 ampliada: módulo online-only según el Plan 0004; sin red se deshabilita el escaneo y la captura manual offline sigue funcionando. |
| 2026-08-08 | **Implementado de punta a punta**, del PR A al PR D más la pantalla: módulo `intake_scan` con `scan_jobs` (migración `20260808_0013`), los nueve ajustes `SCAN_*`, el prompt `v1` versionado con su changelog, `ScanExtractor` con la implementación de Gemini, la validación determinista del §7, el matching de cliente, los endpoints `POST /scans`, `GET /scans/{id}` y `GET /scans/{id}/image`, el `scan_id` de `POST /orders` con su diff de correcciones, y los scripts `eval_scan.py` y `prune_scans.py`. En la app: la pantalla de escaneo, el volcado sobre la toma de pedido y los avisos traducidos. Verificado: 398 pruebas de backend y 414 de la app en verde, `mypy` sin errores nuevos, y la key de `.env` validada contra Google (`gemini-2.5-flash` disponible). **Nueve decisiones y desviaciones.** **(a)** El cliente de Gemini va por **HTTP con `httpx`** y no por el SDK de Google: lo que se necesita del API es un POST con un esquema adjunto —la parte estable— y el SDK sería una dependencia más que el retiro del §11 tendría que deshacer. **(b)** `scan_jobs` **no lleva `SyncableMixin`**, contra lo que haría cualquier otra tabla del sistema: un escaneo pertenece al minuto en que ocurrió y el teléfono que lo tomó ya tiene la respuesta; meterlo en el feed empujaría fotos del nombre, teléfono y NIT de alguien a todos los demás aparatos de la lavandería. **(c)** Las cajas de opciones del `responseSchema` son **listas cerradas** (G/E/P, T40–T60, N2–N4, R/S/T10/SU) y no mapas libres, porque la salida estructurada de Gemini no admite `additionalProperties` — y porque un código que el catálogo no conoce es una mala lectura, no un servicio nuevo. **(d)** El §7.3 pedía comprobar que el peso del encabezado y las libras cobradas cuadren; el motor del plan 0001 §6 **rechaza** un pedido donde no cuadran, y aquí eso sería un callejón sin salida —nadie puede discutir con una fotografía—, así que el encabezado **sigue a las libras cobradas** y la discrepancia se reporta. **(e)** Los avisos viajan **codificados** (`total_mismatch:108.75:98.75`), como los del cierre y los del motor de sincronización: el servidor es el único que puede detectarlos y la app es la que habla español. **(f)** El `scan_id` viaja en `OrderCreate` y se consume **una capa más arriba** —en el endpoint y en el handler de sync—, nunca dentro de `OrdersService`, que es lo que mantiene la dirección de D2. Y tuvo que entrar también al camino de **sincronización**, porque la app captura local y empuja: `POST /orders` no es la ruta que toma una boleta escaneada, así que sin eso la métrica de D7 solo habría visto las boletas que nadie escaneó. Sync lo hace contra un `Protocol` que declara él mismo (`ScanLinker`), así que no importa nada de este módulo. **(g)** Un fallo del proveedor **conserva la fila** en `failed` en vez de deshacerla: es lo que distingue «el proveedor está caído» de «nadie está escaneando». **(h)** La foto se pide a 2560 px y calidad 90, no a los 1440/82 de la foto de producto: lo que hay que distinguir es un 1 de un 7 a lápiz sobre papel de talonario. **(i)** Se acepta **galería además de cámara**, que es lo que la pregunta abierta 4 recomendaba: llegan boletas fotografiadas y mandadas por WhatsApp. **Lo que no entró: el set dorado del §9.** `eval_scan.py` está escrito y corre, pero `tests/fixtures/scan_golden/` está vacío porque son **fotos reales de boletas** —veinte, variadas, anotadas a mano— y eso no se puede escribir desde aquí. Hasta que exista, la regla de D5 («no se sube un prompt que empeore la exactitud») no tiene contra qué medirse, y `v1` no tiene número en su changelog. |
