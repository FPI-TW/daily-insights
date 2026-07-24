class DataSourceError(Exception):
    """Base error at the provider boundary."""


class DataSourceAuthenticationError(DataSourceError):
    """Provider credentials are missing, invalid, or unauthorized."""


class DataSourceContractError(DataSourceError):
    """The provider response or request contract is incompatible."""


class DataSourceTransientError(DataSourceError):
    """A retryable provider failure exhausted the bounded retry policy."""


class UnsupportedMarketError(DataSourceContractError):
    """No reviewed mapping exists between internal and provider market codes."""
