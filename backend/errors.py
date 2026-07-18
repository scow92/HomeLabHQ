"""Expected application failures and their stable, safe messages."""


class ApplicationError(Exception):
    """A failure an API client can safely be told about."""


class ValidationError(ApplicationError, ValueError):
    pass


class AuthenticationRequired(ApplicationError):
    def __init__(self, message="unauthenticated"):
        super().__init__(message)


class Forbidden(ApplicationError):
    pass


class NotFound(ApplicationError):
    def __init__(self, message="not found"):
        super().__init__(message)


class Conflict(ApplicationError, ValueError):
    pass


class UpstreamUnavailable(ApplicationError):
    pass
