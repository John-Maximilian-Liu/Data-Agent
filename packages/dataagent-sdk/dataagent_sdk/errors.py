"""Typed SDK exceptions with request IDs."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str | None = None, request_id: str | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"{status_code} {code}: {message or ''} (request_id={request_id})")


class ForbiddenError(ApiError):
    pass


class NotFoundError(ApiError):
    pass


class ConflictError(ApiError):
    pass


class RateLimitedError(ApiError):
    pass


def raise_for_status(status: int, body: dict, request_id: str | None) -> None:
    if status < 400:
        return
    code = body.get("code") or (body.get("detail") or {}).get("code") or "error"
    message = body.get("message") or str(body.get("detail"))
    if status == 403:
        raise ForbiddenError(status, code, message, request_id)
    if status == 404:
        raise NotFoundError(status, code, message, request_id)
    if status == 409:
        raise ConflictError(status, code, message, request_id)
    if status == 429:
        raise RateLimitedError(status, code, message, request_id)
    raise ApiError(status, code, message, request_id)
