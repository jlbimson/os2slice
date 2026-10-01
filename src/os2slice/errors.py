"""Exceptions that carry a user-facing message, a fix, and a CLI exit code."""

from __future__ import annotations


class Os2sliceError(Exception):
    """Base error. `message` says what went wrong; `fix` says what to do about it."""

    exit_code = 1
    http_status = 500

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix

    def one_line(self) -> str:
        return f"{self.message}. {self.fix}" if self.fix else self.message


class ConfigError(Os2sliceError):
    exit_code = 1
    http_status = 500


class BadRequest(Os2sliceError):
    exit_code = 2
    http_status = 400


class AuthError(Os2sliceError):
    exit_code = 3
    http_status = 500


class NoAccess(AuthError):
    """Onshape says this account can't open the document (403)."""

    http_status = 403


class OnshapeError(Os2sliceError):
    exit_code = 4
    http_status = 502


class SlicerError(Os2sliceError):
    exit_code = 5
    http_status = 500
