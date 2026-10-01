from typing import Any, Optional


class AppException(Exception):
    """Base application exception."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Optional[Any] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFoundError(AppException):
    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            code=f"{resource.upper()}_NOT_FOUND",
            message=f"{resource} '{identifier}' was not found",
            status_code=404,
        )


class StockNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Stock", identifier=identifier)


class CompanyNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Company", identifier=identifier)


class PriceNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Price", identifier=identifier)


class EventNotFoundError(NotFoundError):
    def __init__(self, identifier: Any):
        super().__init__(resource="Event", identifier=identifier)


class DuplicateResourceError(AppException):
    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            code=f"DUPLICATE_{resource.upper()}",
            message=f"{resource} '{identifier}' already exists",
            status_code=409,
        )


class DuplicateStockError(DuplicateResourceError):
    def __init__(self, symbol: str):
        super().__init__(resource="Stock", identifier=symbol)


class DuplicatePriceError(DuplicateResourceError):
    def __init__(self, stock_id: int, date: str):
        super().__init__(resource="Price", identifier=f"stock_id={stock_id}, date={date}")


class DuplicateEventError(DuplicateResourceError):
    def __init__(self, detail: str):
        super().__init__(resource="Event", identifier=detail)


class AppValidationError(AppException):
    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=422,
            details=details,
        )


class ProviderError(AppException):
    def __init__(self, provider: str, message: str):
        super().__init__(
            code=f"{provider.upper()}_PROVIDER_ERROR",
            message=f"Provider '{provider}' error: {message}",
            status_code=502,
        )


class DatabaseError(AppException):
    def __init__(self, message: str = "A database error occurred"):
        super().__init__(
            code="DATABASE_ERROR",
            message=message,
            status_code=500,
        )
