from compute_space.web.openapi.functional_spec import SPEC_PATH
from compute_space.web.openapi.functional_spec import generate_functional_spec
from compute_space.web.openapi.functional_spec import render_spec


def main() -> None:
    SPEC_PATH.write_text(render_spec(generate_functional_spec()))
    print(f"wrote {SPEC_PATH}")


if __name__ == "__main__":
    main()
