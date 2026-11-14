import re

# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3). A release candidate adds "-rc.N"
# (v1.2.0-rc.1). Any other v-prefixed tag is ignored so version parsing can't crash on it. Shared by the instance
# updater (update.py, apply_after_checkout.py) and app releases (compute_space.core.releases).
RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")
RC_TAG = re.compile(r"v\d+(?:\.\d+)*-rc\.\d+")


def version_key(tag_name: str) -> tuple[tuple[int, ...], int, int]:
    version, _, rc = tag_name.lstrip("v").partition("-rc.")
    # A release sorts after its own candidates: v1.2.0-rc.1 < v1.2.0-rc.2 < v1.2.0.
    return tuple(int(x) for x in version.split(".")), 0 if rc else 1, int(rc or 0)


def is_version_tag(name: str) -> bool:
    return bool(RELEASE_TAG.fullmatch(name) or RC_TAG.fullmatch(name))


def newest_release(tag_names: list[str]) -> str | None:
    """The highest release tag (candidates excluded) among ``tag_names``, or None."""
    return max((t for t in tag_names if RELEASE_TAG.fullmatch(t)), key=version_key, default=None)
