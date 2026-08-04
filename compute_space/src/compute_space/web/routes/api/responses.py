"""Response shapes shared across the API routers, plus the helper that
declares them to OpenAPI. One definition each, so the generated document
doesn't carry three interchangeable copies of the same error body."""

from typing import Any

import attr
from litestar import MediaType
from litestar.openapi.datastructures import ResponseSpec


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


def response_spec(data_container: Any, description: str, media_type: MediaType = MediaType.JSON) -> ResponseSpec:
    """Document one status code. Examples stay off: Litestar generates random
    ones, which would make the committed ``openapi.yaml`` differ every run."""
    return ResponseSpec(
        data_container=data_container,
        description=description,
        media_type=media_type,
        generate_examples=False,
    )


def error_spec(description: str) -> ResponseSpec:
    return response_spec(ErrorResponse, description)
