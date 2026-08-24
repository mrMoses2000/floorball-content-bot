class FloorballBotError(Exception):
    """Base application error."""


class AuthorizationError(FloorballBotError):
    """Actor is not allowed to perform the requested action."""


class ValidationBlocked(FloorballBotError):
    """Content cannot progress until validation issues are fixed."""


class InvalidTransition(FloorballBotError):
    """Draft workflow transition is not allowed."""


class RetryableProviderError(FloorballBotError):
    """External provider operation may be retried."""


class PermanentProviderError(FloorballBotError):
    """External provider operation must not be retried automatically."""
