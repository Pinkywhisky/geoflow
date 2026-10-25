"""One heavy analysis at a time in this Uvicorn process."""

from collections.abc import Callable
import re
from typing import TypeVar

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


T = TypeVar("T")
analysis_limiter = anyio.CapacityLimiter(1)
admission_limiter = anyio.CapacityLimiter(2)
_ANALYSIS_PATH = re.compile(
    r"/(?:analyze|profils/analyse|profils/[^/]+/tester|"
    r"dossiers/[^/]+/import|analyses/[^/]+/profil)/?"
)


class AnalysisAdmissionMiddleware:
    """Bound uploads before multipart parsing: one running, one waiting.

    A thread limit alone leaves unlimited queued upload files on disk.
    This process-local admission limit covers the entire request lifetime.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not _ANALYSIS_PATH.fullmatch(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        try:
            admission_limiter.acquire_nowait()
        except anyio.WouldBlock:
            await JSONResponse(
                {"detail": "Une analyse est en cours et une autre attend. Réessayez dans quelques instants."},
                status_code=429,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            admission_limiter.release()


async def run_analysis(function: Callable[..., T], *args: object) -> T:
    # Do not abandon the worker on cancellation: upload/conversion workspaces
    # must remain available until the synchronous function has finished.
    return await anyio.to_thread.run_sync(function, *args, limiter=analysis_limiter)
