"""Map client errors to user-facing messages and process exit codes."""

from __future__ import annotations

from primectl.client import ApiError, ConnectionFailed

EXIT_GENERAL = 1
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 9


def exit_code_for(err: Exception) -> int:
    if isinstance(err, ApiError):
        if err.status == 404:
            return EXIT_NOT_FOUND
        if err.status == 409:
            return EXIT_CONFLICT
    return EXIT_GENERAL


def format_error(err: Exception, *, server: str | None = None) -> str:
    if isinstance(err, ConnectionFailed):
        where = f" ({server})" if server else ""
        return f"cannot reach the Primer server{where}: {err}"
    if isinstance(err, ApiError):
        detail = err.problem.get("detail") or err.problem.get("title") or err.body_text
        if err.status == 401:
            return (
                "not authenticated (401). Set a token via "
                "'primectl config set-context <name> --token ...' or the "
                "PRIMER_API_TOKEN env var."
            )
        if err.status == 403:
            return f"forbidden (403): {detail}"
        if err.status == 404:
            return f"not found (404): {detail}"
        if err.status == 409:
            return f"conflict (409): {detail}"
        if err.status in (400, 422):
            return f"invalid request ({err.status}): {detail}"
        return f"server error ({err.status}): {detail}"
    return str(err)
