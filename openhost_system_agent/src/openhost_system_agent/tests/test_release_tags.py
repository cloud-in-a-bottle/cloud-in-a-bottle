from openhost_system_agent.release_tags import newest_release


def test_newest_release_is_numeric_and_skips_candidates_and_other_tags() -> None:
    tags = ["v1.9.0", "v1.10.0", "v2.0.0-rc.1", "v2-beta", "latest", "1.11.0"]
    assert newest_release(tags) == "v1.10.0"


def test_newest_release_of_nothing() -> None:
    assert newest_release(["v2.0.0-rc.1", "latest"]) is None
