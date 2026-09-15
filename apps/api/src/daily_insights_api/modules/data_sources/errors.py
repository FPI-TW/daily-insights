class DataSourceError(Exception):
    """Base error at the provider boundary."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class DataSourceAuthenticationError(DataSourceError):
    """Provider credentials are missing, invalid, or unauthorized."""


class DataSourceContractError(DataSourceError):
    """The provider response or request contract is incompatible."""


class DataSourceTransientError(DataSourceError):
    """A retryable provider failure exhausted the bounded retry policy."""


class UnsupportedMarketError(DataSourceContractError):
    """No reviewed mapping exists between internal and provider market codes."""
