class AntigravityError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class AntigravityApiError(AntigravityError):
    def __init__(self, status_code: int, message: str, *, retryable: bool | None = None) -> None:
        self.status_code = status_code
        is_retryable = (
            retryable
            if retryable is not None
            else (status_code == 429 or 500 <= status_code < 600)
        )
        super().__init__(message, retryable=is_retryable)


class AntigravityCliError(AntigravityError):
    pass


def is_retryable_antigravity_error(exc: BaseException) -> bool:
    return isinstance(exc, AntigravityError) and exc.retryable
