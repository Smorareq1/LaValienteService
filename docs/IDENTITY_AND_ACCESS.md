# Identidad y acceso

El módulo `src/modules/identity` mantiene la autorización normalizada:

- `users` conserva las credenciales y estado de la cuenta. El login acepta `username`
  o `email` (campo `identifier`); el email y el teléfono son opcionales y se usan para
  la recuperación de contraseña.
- `roles` y `permissions` son catálogos independientes; un permiso se identifica por
  `resource.action`.
- `user_roles` y `role_permissions` son relaciones muchos-a-muchos, con claves primarias
  compuestas para impedir duplicados.
- `user_permissions` permite excepciones por persona. `deny` tiene precedencia sobre un
  permiso concedido por un rol o directamente.
- `auth_sessions` representa una sesión por dispositivo/login, con metadatos (`user_agent`,
  `ip_address`, `last_used_at`) y un vencimiento absoluto (30 días por defecto). Revocarla
  invalida todos sus refresh tokens.
- `refresh_tokens` guarda el hash SHA-256 de cada paso de rotación del refresh token de una
  sesión. En cada `refresh` el token presentado se marca como usado y se emite uno nuevo
  (vida deslizante de 7 días, nunca más allá del vencimiento absoluto de la sesión).
- `password_reset_tokens` guarda el hash del código de recuperación de 6 dígitos, con
  expiración (10 min), límite de intentos (5) y canal (`email` o `sms`).

### Sesiones y tokens

- Los access tokens JWT son breves (15 minutos por defecto) y llevan claims completos:
  `iss`, `aud`, `sub`, `sid` (sesión), `jti`, `iat`, `nbf`, `exp`, `typ`. La validación exige
  firma, emisor, audiencia y presencia de todos los claims, con `leeway` configurable.
- **Rotación con detección de robo**: si alguien presenta un refresh token que ya fue rotado
  fuera de la ventana de gracia (`REFRESH_REUSE_GRACE_SECONDS`, 30 s por defecto, pensada para
  reintentos de red), se asume que el token fue robado y se revoca la sesión completa.
- En cada endpoint protegido se comprueba la sesión y se recalculan los permisos desde la base
  de datos. Por ello, retirar un rol o permiso tiene efecto sin esperar a que expire el token.
- `GET /auth/sessions` lista las sesiones activas del usuario (marcando la actual),
  `DELETE /auth/sessions/{id}` cierra una en particular y `POST /auth/logout-all` cierra todas.

## Endpoints iniciales

- `POST /api/v1/auth/login`, `refresh`, `logout` y `GET /me` cubren el ciclo de autenticación.
- `POST /api/v1/auth/register` crea cuentas, pero requiere el permiso
  `authorization.users.manage` (no hay registro público).
- `POST /api/v1/auth/forgot-password` envía un código de recuperación (siempre responde 202
  para no revelar si la cuenta existe) y `POST /auth/reset-password` lo canjea por una nueva
  contraseña, revocando todas las sesiones activas.
- `GET/POST /api/v1/authorization/permissions` lista y crea permisos dinámicos.
- `GET/POST /api/v1/authorization/roles` y `PUT /roles/{role_id}/permissions` administran roles.
- `GET /api/v1/authorization/users`, `PUT /users/{user_id}/roles` y `.../permissions` asignan
  acceso.

Los endpoints administrativos requieren los permisos correspondientes. El script de bootstrap
(`python -m scripts.create_superuser`) crea el primer `system_admin` tras aplicar la migración
inicial.

## Recuperación de contraseña

El envío del código se delega en `src/core/notifications.py`. El backend `console` (por
defecto) imprime el código en el log del servidor — útil mientras no exista un proveedor
real. Cuando se contrate AWS, se agrega un backend `aws` (SES para email, SNS para SMS) y se
selecciona con la variable `NOTIFICATIONS_BACKEND`.
