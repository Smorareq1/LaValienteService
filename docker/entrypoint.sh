#!/bin/sh
# Punto de entrada de la imagen de producción.
#
# Hace tres cosas y ninguna más: deja `/data` escribible para el usuario sin
# privilegios, suelta privilegios, y traduce un verbo corto (`api`, `migrate`,
# `seed`) al comando real. Que el arranque sea un verbo y no una línea de
# uvicorn de 90 caracteres es lo que permite que el comando de inicio en
# Railway, en Compose y en la terminal de alguien sean el mismo.
#
# Lo que NO hace es migrar antes de servir. Las migraciones van en el
# pre-deploy de Railway (`entrypoint.sh migrate`): ahí corren una sola vez,
# antes de que la versión nueva reciba tráfico, y si fallan el despliegue se
# aborta y la versión anterior sigue viva. Metidas en el arranque, cada
# réplica las intentaría a la vez y una migración rota se llevaría el
# servicio entero.
set -eu

: "${APP_USER:=app}"
: "${HOST:=0.0.0.0}"
: "${PORT:=8000}"
: "${WEB_CONCURRENCY:=1}"
: "${MEDIA_DIR:=/data/media}"
: "${SCAN_STORAGE_PATH:=/data/media/scans}"

# El volumen persistente se monta sobre `/data` y llega con dueño root, así que
# el `chown` del Dockerfile queda tapado y hay que rehacerlo en cada arranque.
# Sin esto, la primera foto de producto falla con EACCES y el error aparece en
# el mostrador, no en el despliegue.
#
# No es recursivo a propósito: lo único que llega con dueño ajeno son estos
# directorios; los archivos que escribe la API ya nacen suyos, y un `-R` sobre
# meses de escaneos alargaría cada arranque sin arreglar nada.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${MEDIA_DIR}" "${SCAN_STORAGE_PATH}"
    chown "${APP_USER}:${APP_USER}" "${MEDIA_DIR}" "${SCAN_STORAGE_PATH}"
fi

# `exec` en los dos caminos: el proceso de la aplicación tiene que ser el PID 1
# para recibir el SIGTERM del despliegue y cerrar las conexiones abiertas en vez
# de morir de un SIGKILL 30 segundos después, a media petición.
run_as_app() {
    if [ "$(id -u)" = "0" ]; then
        exec gosu "${APP_USER}" "$@"
    fi
    exec "$@"
}

action="${1:-api}"
shift || true

case "${action}" in
api)
    # `--proxy-headers` con `--forwarded-allow-ips=*`: detrás del proxy de
    # Railway, sin esto todas las peticiones parecen venir del propio proxy y
    # los registros pierden la IP del cliente. El comodín es seguro aquí porque
    # al contenedor sólo llega tráfico del borde de Railway.
    #
    # `--no-server-header` quita el `server: uvicorn` de cada respuesta: no le
    # sirve a nadie salvo a quien busca versiones con bugs conocidos.
    run_as_app uvicorn src.main:app \
        --host "${HOST}" \
        --port "${PORT}" \
        --workers "${WEB_CONCURRENCY}" \
        --proxy-headers \
        --forwarded-allow-ips='*' \
        --no-server-header \
        --timeout-graceful-shutdown 20 \
        "$@"
    ;;
migrate)
    run_as_app alembic upgrade head
    ;;
seed)
    # Los seeders del README, en orden y todos idempotentes: correrlos de nuevo
    # no duplica nada, así que esto es seguro de repetir en un entorno vivo.
    # `create_superuser` NO está aquí: crea una persona con contraseña y eso se
    # hace a mano, una vez (ver docs/DEPLOYMENT.md).
    for module in \
        seed_permissions \
        seed_catalog \
        seed_promotions \
        seed_staff \
        seed_expense_categories \
        seed_products; do
        echo "→ scripts.${module}"
        if [ "$(id -u)" = "0" ]; then
            gosu "${APP_USER}" python -m "scripts.${module}"
        else
            python -m "scripts.${module}"
        fi
    done
    ;;
*)
    # Cualquier otra cosa se ejecuta tal cual, como usuario sin privilegios.
    # Sirve para las tareas de una sola vez (`python -m scripts.prune_scans`) y
    # además hace inofensivo que Railway anteponga el ENTRYPOINT a un comando
    # que ya empieza por esta misma ruta: se re-entra una vez y se resuelve.
    run_as_app "${action}" "$@"
    ;;
esac
