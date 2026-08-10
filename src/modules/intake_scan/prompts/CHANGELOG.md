# Historial de prompts (Plan 0003 D5)

Cambiar el prompt **exige** correr `python -m scripts.eval_scan` contra el set
dorado y comparar con la versión anterior. Regla del §9: no se sube una versión
que empeore la exactitud global.

Cada `scan_job` guarda `prompt_version` y `model`, así que una caída de calidad
se puede atribuir al cambio que la causó en vez de adivinarla.

Son **dos series independientes**, una por documento, y se versionan aparte
(`SCAN_PROMPT_VERSION` y `SCAN_CASH_PROMPT_VERSION`): mejorar la lectura de la
boleta no debe obligar a revalidar la del cierre, ni al revés.

## Boleta de talonario — `v*`

| Versión | Fecha | Cambio | Exactitud global |
|---|---|---|---|
| `v1` | 2026-08-08 | Versión inicial: descripción zona por zona de la boleta, lista cerrada de los 21 tipos de prenda, semántica de G/E/P, T40–T60, N2–N4 y R/S/T10/SU, la X como campo anulado, confusiones de manuscrito (1/7, 4/9, 5/S, 0/6) y calibración explícita de la confianza. | — (sin set dorado todavía) |

## Hoja «Registro Diario» — `cash_v*`

| Versión | Fecha | Cambio | Exactitud global |
|---|---|---|---|
| `cash_v1` | 2026-08-09 | Versión inicial: la hoja como lista de hasta tres bloques-día, las dos mitades independientes (ingresos por `#Tomapedido`, gastos con `#Factura` = objeto y `Proveedor` = descripción), el significado de las marcas de color (rosado = transferencia, amarillo = factura, azul = venta de insumo), las horas de entrada/almuerzo/salida, y los totales como comprobación. | — (sin set dorado todavía) |

## Pendiente para v2

Lo que el §6 del plan ya anticipa: **few-shot** con dos o tres fotos anotadas
dentro del propio prompt multimodal. Se deja para cuando el set dorado exista y
pueda medirse si mejora, que es justamente lo que D5 pide.
