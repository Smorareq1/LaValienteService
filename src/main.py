from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.v1.router import api_router
from src.core.config import get_settings
from src.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from src.core.logging import configure_logging

settings = get_settings()
configure_logging()

# The three URLs move together on purpose: hiding `/docs` while leaving
# `/openapi.json` up publishes the same map, only less comfortably.
app = FastAPI(
    title=settings.app_name,
    docs_url="/docs" if settings.serve_docs else None,
    redoc_url="/redoc" if settings.serve_docs else None,
    openapi_url="/openapi.json" if settings.serve_docs else None,
)

# Only when someone configured origins. An API that no browser calls answers
# better with no CORS headers at all than with permissive ones nobody needed.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # The session travels in the `Authorization` header and never in a
        # cookie, so the browser has no ambient credential to attach and
        # allowing them would only widen what a hostile page can do.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.api_v1_prefix)


def error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(_: Request, exc: AuthenticationError) -> JSONResponse:
    return error_response(status.HTTP_401_UNAUTHORIZED, str(exc))


@app.exception_handler(AuthorizationError)
async def authorization_error_handler(_: Request, exc: AuthorizationError) -> JSONResponse:
    return error_response(status.HTTP_403_FORBIDDEN, str(exc))


@app.exception_handler(NotFoundError)
async def not_found_error_handler(_: Request, exc: NotFoundError) -> JSONResponse:
    return error_response(status.HTTP_404_NOT_FOUND, str(exc))


@app.exception_handler(ConflictError)
async def conflict_error_handler(_: Request, exc: ConflictError) -> JSONResponse:
    return error_response(status.HTTP_409_CONFLICT, str(exc))


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
