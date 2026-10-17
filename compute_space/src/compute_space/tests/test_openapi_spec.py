import difflib

from compute_space.web.openapi.functional_spec import SPEC_PATH
from compute_space.web.openapi.functional_spec import _strip_docs
from compute_space.web.openapi.functional_spec import generate_functional_spec
from compute_space.web.openapi.functional_spec import render_spec


def test_checked_in_spec_matches_code() -> None:
    """Fails on any change to how the API is called: paths, methods, parameters, request and response shapes.

    Apps call these endpoints and ship on their own schedule, so a change here can break them.  Make it backwards
    compatible if you can.  If the change is intended, regenerate the spec with `just regenerate-openapi-spec` and
    commit it, so the change shows up in review.
    """
    expected = SPEC_PATH.read_text()
    actual = render_spec(generate_functional_spec())
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True), actual.splitlines(keepends=True), "checked in", "generated"
        )
    )
    assert not diff, (
        f"{SPEC_PATH.name} is out of date with the routes. If this API change is intended, run "
        f"`just regenerate-openapi-spec` and commit the result.\n{diff}"
    )


def test_strip_docs_keeps_properties_named_like_doc_fields() -> None:
    schema = {
        "type": "object",
        "description": "dropped",
        "properties": {"description": {"type": "string", "title": "dropped"}},
    }
    assert _strip_docs(schema) == {"type": "object", "properties": {"description": {"type": "string"}}}
