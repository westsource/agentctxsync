"""JSON request bodies: parse and validate in one place.

Handlers that read `await request.json()` themselves turn client mistakes
into 500s: a malformed body escapes as an unhandled json.JSONDecodeError,
and a valid non-object body (e.g. `[1]`) explodes on the next `.get()`.
Both are bad requests, so `json_object()` returns the dict or raises the
400 the handlers would otherwise have to repeat at every call site.
"""
from fastapi import HTTPException
from starlette.requests import Request


async def json_object(request: Request) -> dict:
    """Parse a JSON-object request body, or raise HTTPException(400).

    ValueError covers json.JSONDecodeError and the UnicodeDecodeError from a
    non-UTF-8 body.
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body
