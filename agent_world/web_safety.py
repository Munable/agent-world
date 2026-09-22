from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def add_web_safety(app):
    @app.middleware("http")
    async def private_responses(request, call_next):
        # Basic-auth browser forms must not accept cross-origin mutation requests.
        if request.method in {"POST", "PATCH", "DELETE", "PUT"} and not request.url.path.endswith(
            "/join/exchange"
        ):
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin and origin != expected:
                return JSONResponse(
                    {"error": "CrossOriginRequest", "message": "cross-origin writes are not allowed"},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(
            {"error": "InvalidArguments", "message": "invalid request shape", "retryable": False},
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )
