import asyncio
from collections.abc import Awaitable, Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        maximum: int = 65536,
        on_rejected: Callable[[Scope, int], Awaitable[int]] | None = None,
    ) -> None:
        self.app = app
        self.maximum = maximum
        self.on_rejected = on_rejected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.maximum:
                        status = await self.on_rejected(scope, 413) if self.on_rejected else 413
                        await JSONResponse(
                            {"detail": "درخواست بیش از حد بزرگ است."}, status_code=status
                        )(scope, receive, send)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            status = await self.on_rejected(scope, 408) if self.on_rejected else 408
            await JSONResponse({"detail": "زمان دریافت درخواست پایان یافت."}, status_code=status)(
                scope, receive, send
            )
            return
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
