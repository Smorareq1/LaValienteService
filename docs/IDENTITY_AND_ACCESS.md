# Identidad y acceso

El módulo `src/modules/identity` mantiene la autorización normalizada:

- `users` conserva las credenciales y estado de la cuenta.
- `roles` y `permissions` son catálogos independientes; un permiso se identifica por
  `resource.action`.
- `user_roles` y `role_permissions` son relaciones muchos-a-muchos, con claves primarias
  compuestas para impedir duplicados.
- `user_permissions` permite excepciones por persona. `deny` tiene precedencia sobre un
  permiso concedido por un rol o directamente.
- `auth_sessions` guarda únicamente el hash SHA-256 de cada refresh token y permite su
  revocación inmediata.

Los access tokens JWT son breves (15 minutos por defecto) e incluyen el identificador de sesión.
En cada endpoint protegido se comprueba la sesión y se recalculan los permisos desde la base de
datos. Por ello, retirar un rol o permiso tiene efecto sin esperar a que expire el token.

## Endpoints iniciales

- `POST /api/v1/auth/register`, `login`, `refresh`, `logout` y `GET /me` cubren el ciclo de
  autenticación.
- `POST /api/v1/authorization/permissions` crea permisos dinámicos.
- `POST /api/v1/authorization/roles` y `PUT /roles/{role_id}/permissions` administran roles.
- `PUT /api/v1/authorization/users/{user_id}/roles` y `.../permissions` asignan acceso.

Los endpoints administrativos requieren los permisos correspondientes. El script de bootstrap
crea el primer `system_admin` tras aplicar la migración inicial.
