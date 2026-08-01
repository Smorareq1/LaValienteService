# Plan NNNN — Título del plan

| | |
|---|---|
| **Estado** | 📝 Borrador |
| **Fecha** | AAAA-MM-DD |
| **Módulos afectados** | `modulo_a`, `modulo_b` |
| **Depende de** | Plan NNNN (si aplica) |

## 1. Contexto

Qué problema de negocio resuelve este plan y de dónde viene la información (documentos físicos,
entrevistas, planes previos).

## 2. Alcance

**Dentro:** qué se implementa con este plan.

**Fuera:** qué queda explícitamente para planes futuros (enlazarlos si existen).

## 3. Glosario de dominio

Términos del negocio y su significado exacto. Evita ambigüedad entre lo que dice el papel/el
cliente y lo que modela el sistema.

## 4. Decisiones de diseño

Lista numerada `D1, D2, …` de decisiones con su justificación. Es la sección que se consulta
cuando alguien pregunta "¿por qué se hizo así?".

## 5. Modelo de datos

Tablas, campos (tipo, nulabilidad), restricciones e índices. Incluir diagrama ER (mermaid) si
hay más de dos entidades.

## 6. Reglas de negocio

Cálculos, validaciones y flujos de estado, con ejemplos numéricos cuando aplique.

## 7. API

Endpoints, verbos, permisos requeridos y forma de los payloads relevantes.

## 8. Migraciones y seeders

Qué migraciones se crean y qué datos semilla se cargan.

## 9. Plan de implementación

Fases/PRs en orden, cada una integrable y testeable por separado.

## 10. Preguntas abiertas

Lo pendiente de confirmar con el negocio. Al resolverse, mover la respuesta a la sección que
corresponda y dejar constancia en el historial.

## Historial

| Fecha | Cambio |
|---|---|
| AAAA-MM-DD | Versión inicial |
