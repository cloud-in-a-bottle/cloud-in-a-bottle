from http import HTTPStatus

from litestar.exceptions import HTTPException


class ConflictException(HTTPException):
    """The request conflicts with the current resource state."""

    status_code = HTTPStatus.CONFLICT


class BadGatewayException(HTTPException):
    """An upstream service returned an invalid response."""

    status_code = HTTPStatus.BAD_GATEWAY
