## Documento de Definición de Arquitectura
**Enfoque:** Clean Architecture + Domain-Driven Design
**Versión del Estándar:** 1.0.0

---

### 1. Blueprint del Sistema de Archivos
Esta estructura de directorios es mandatoria. Todo el código debe residir dentro del directorio `src/` para evitar problemas de importación y mantener una estricta separación de contextos.

    [PROJECT_ROOT]/ 
    ├── .github/workflows/        # Workflows de CI/CD de GitHub Actions (Linting, Testing, Build)
    ├── docker/                   # Dockerfiles (Dev, Prod) y scripts de inicialización
    ├── docs/                     # Documentación de arquitectura (ADRs) y Swagger estático
    ├── migrations/               # Versiones de migraciones de base de datos (Cambios en DDL)
    ├── scripts/                  # Scripts de mantenimiento (seeders, cleanups)
    ├── src/                      # Código fuente principal
    │   ├── api/                  # CAPA DE INTERFAZ (Interface Adapters)
    │   │   ├── __init__.py
    │   │   ├── dependencies.py   # Dependencias inyectables globales (Auth, CurrentUser)
    │   │   └── v1/               # Versión de la API
    │   │       ├── __init__.py   # Centralizador de rutas
    │   │       ├── router.py
    │   │       └── endpoints/    # Controladores HTTP (conectan HTTP -> Service)
    │   │           └── entidad.py
    │   ├── core/                 # NÚCLEO TRANSVERSAL (Infraestructura Global)
    │   │   ├── config.py         # Settings de variables de entorno
    │   │   ├── database.py       # Factory de conexión a BD con conexiones async
    │   │   ├── exceptions.py     # Clases de Excepciones Base del sistema
    │   │   ├── logging.py        # Configuración de loggers
    │   │   └── security.py       # Lógica criptográfica (JWT, Hashing)
    │   └── modules/              # CAPA DE DOMINIO Y NEGOCIO (El Corazón)
    │       ├── __init__.py
    │       └── [nombre_modulo]/  # Ej: 'dominio_a', 'dominio_b'
    │           ├── __init__.py
    │           ├── models.py     # Entidades de Base de Datos (ORM)
    │           ├── schemas.py    # DTOs / Modelos de validación (Contratos de datos)
    │           ├── repository.py # Patrón Repositorio (Abstracción de datos)
    │           ├── service.py    # Lógica de Negocio (Casos de Uso)
    │           └── exceptions.py # Excepciones específicas del módulo
    ├── tests/                    # Estrategia de Testing
    │   ├── integration/          # Pruebas de integración (con BD real)
    │   ├── unit/                 # Pruebas unitarias (Mocks)
    │   └── conftest.py           # Fixtures base para las pruebas
    ├── alembic.ini               # Configuración de migraciones de BD
    ├── pyproject.toml            # Gestión de dependencias y configuración de herramientas
    └── ruff.toml                 # Configuración de linter y formatter estricto

---

### 2. Tecnologías y Estándares de Soporte
Para garantizar que esta arquitectura se mantenga robusta y escalable, el proyecto debe adherirse a los siguientes estándares:

* **Lenguaje:** Python 3.14.
* **Framework de API:** FastAPI.
* **Gestión de Dependencias:** Uso exclusivo de Poetry para garantizar dependencias deterministas mediante lockfiles.
* **Linting y Formatting:** Ruff, configurado con reglas estrictas para el control de importaciones y la complejidad ciclomática.
* **Gestión de Entorno:** Queda estrictamente prohibido el uso de archivos `.env.example`. La configuración debe gestionarse dinámicamente según el entorno de despliegue.
* **Type Checking:** MyPy con el modo estricto activado (sin permitir tipos implícitos).
* **Testing:** Pytest junto con Testcontainers para ejecutar pruebas de integración reales.

---

### 3. Manifiesto de Responsabilidades

**A. Capa API (`src/api/vX/endpoints/`)**
Es la puerta de entrada de la aplicación; su única responsabilidad es gestionar el protocolo HTTP.
* **DEBE contener:** Definiciones de rutas de los endpoints, inyección de dependencias iniciales, validación de esquemas de entrada, llamadas a la capa de Service y retorno de códigos de estado HTTP.
* **NO DEBE contener:** Lógica de negocio (condicionales complejos), consultas directas a la base de datos o reglas de validación de negocio.
* **Dependencias:** Conoce a las capas Core y Modules (específicamente Service y Schemas). Nunca se comunica de forma directa con el Repository.

**B. Capa de Dominio/Módulos (`src/modules/`)**
Representa el núcleo del sistema y debe ser completamente agnóstica al framework web (FastAPI).

**Service (`service.py`)**
Actúa como el orquestador principal de la lógica.
* **DEBE contener:** Reglas de negocio core, validaciones lógicas complejas y orquestación de llamadas al Repository.
* **NO DEBE contener:** Objetos de petición (Request) o respuesta (Response) del framework, ni códigos HTTP. Debe lanzar excepciones nativas puras de Python.
* **Dependencias:** Conoce a Repository y Schemas.

**Repository (`repository.py`)**
Es el guardián de los datos y la capa de abstracción hacia la base de datos.
* **DEBE contener:** Consultas a la base de datos (mediante ORM o sentencias crudas), filtros y lógicas de paginación a nivel de BD.
* **NO DEBE contener:** Lógica de negocio. El repositorio es pasivo; su único fin es persistir y recuperar información.
* **Dependencias:** Conoce a los Models.

**Schemas (`schemas.py`)**
Definen los contratos de transferencia de datos.
* **DEBE contener:** Clases de validación estructurada para las entradas y salidas del sistema.
* **NO DEBE contener:** Ningún tipo de lógica de conexión a bases de datos.

---

### 4. Flujo de Trabajo de una Petición
El ciclo de vida de una solicitud HTTP debe ser siempre unidireccional y predecible:

1. **Entrada:** El cliente realiza una petición HTTP hacia un endpoint específico de la API.
2. **Interfaz (API):** El controlador correspondiente recibe la solicitud y el framework valida automáticamente la estructura del cuerpo contra el Schema definido.
3. **Delegación:** El endpoint invoca el método correspondiente dentro del Service, pasando los datos validados.
4. **Lógica (Service):** Se ejecutan las validaciones de negocio y cualquier transformación requerida de los datos, para finalmente invocar el método de guardado en el Repository.
5. **Persistencia (Repo):** El Repository ejecuta la operación en la base de datos y retorna la entidad resultante al Service.
6. **Transformación Final:** El Service procesa la entidad devuelta y la retorna en un formato útil para la capa de negocio.
7. **Salida:** El Endpoint recibe el resultado final, lo serializa utilizando el Schema de respuesta correspondiente y emite el código de estado HTTP adecuado hacia el cliente.