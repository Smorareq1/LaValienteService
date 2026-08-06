# Planes de Implementación

Este directorio contiene los planes de diseño e implementación del backend, numerados en orden
cronológico. Cada plan vive en su propio subdirectorio y su documento principal es `PLAN.md`;
material de apoyo (diagramas, notas, decisiones posteriores) se agrega como archivos hermanos
dentro del mismo subdirectorio.

## Convención

```
docs/plans/
├── README.md                     # Este índice (mantener actualizado)
├── TEMPLATE.md                   # Plantilla para nuevos planes
└── NNNN-slug-descriptivo/
    └── PLAN.md                   # Documento principal del plan
```

- **`NNNN`**: correlativo de 4 dígitos (`0001`, `0002`, …). Nunca se reutiliza ni se renumera.
- **`slug`**: nombre corto en kebab-case que describe la capacidad de negocio, no la tabla ni el
  endpoint (ej. `pedidos-diarios`, no `tabla-orders`).
- Todo plan inicia como **Borrador** y avanza de estado conforme se revisa e implementa.
- Un plan aprobado es el contrato de diseño: si durante la implementación algo cambia, se
  actualiza el `PLAN.md` (con una nota en su historial) en el mismo PR que introduce el cambio.

## Estados

| Estado | Significado |
|---|---|
| 📝 Borrador | En redacción o pendiente de revisión |
| ✅ Aprobado | Revisado; listo para implementarse |
| 🚧 En progreso | Implementación en curso |
| ✔️ Implementado | Todo el alcance del plan está en `main` |
| 🗄️ Archivado | Descartado o reemplazado por otro plan (indicar cuál) |

## Índice

| # | Plan | Estado | Módulos | Última actualización |
|---|---|---|---|---|
| [0001](0001-pedidos-diarios/PLAN.md) | Pedidos diarios (toma de pedido) | 🚧 En progreso | `customers`, `catalog`, `orders`, `promotions` | 2026-08-02 |
| [0002](0002-ui-toma-pedido/PLAN.md) | UI de toma de pedido (campos de captura) | 🚧 En progreso | APP Flutter | 2026-08-03 |
| [0003](0003-escaneo-boleta-ia/PLAN.md) | Escaneo de boleta con IA (⚠️ temporal) | 📝 Borrador | `intake_scan`, APP Flutter | 2026-07-22 |
| [0004](0004-sincronizacion-offline/PLAN.md) | Offline-first y sincronización (fundacional — precede a 0001) | 🚧 En progreso | `sync`, APP Flutter | 2026-08-03 |
| [0005](0005-registro-diario-cierre/PLAN.md) | Registro diario: personal, inventario, gastos y cierre (+ orden consolidado de migraciones de la fase 1) | 📝 Borrador | `staff`, `inventory`, `expenses`, `daily_close` | 2026-07-27 |
| [0006](0006-ui-app-fase1/PLAN.md) | PRD de UI: módulos y pantallas de la app (fase 1) | 🚧 En progreso | APP Flutter, `design_system` | 2026-08-03 |

### Planes previstos (aún sin documento)

- Nómina / planilla completa (el plan 0005 solo cubre asistencia y horas extra).
- Facturación / NIT ante SAT (hoy solo se registra el NIT del cliente).
