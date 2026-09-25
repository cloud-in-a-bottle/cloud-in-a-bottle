from __future__ import annotations

from unittest.mock import patch

import pytest

from openhost_system_agent import reclaim


class TestReclaimHostOwnership:
    def test_refuses_when_not_root(self) -> None:
        with patch("os.geteuid", return_value=1000):
            with pytest.raises(RuntimeError, match="must be run as root"):
                reclaim.reclaim_host_ownership()

    def test_chowns_existing_paths_as_root(self) -> None:
        # Traverse both trees physically, changing only mismatched ownership.
        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_host_ownership()

        called_paths = [call.args[0] for call in mock_run.call_args_list]
        assert [cmd[:2] for cmd in called_paths] == [
            ["find", "/home/host/openhost"],
            ["find", "/home/host/.pixi"],
        ]
        for cmd in called_paths:
            assert cmd[2:-6] == ["(", "!", "-user", "host", "-o", "!", "-group", "host", ")"]
            assert cmd[-6:] == ["-exec", "chown", "-h", "host:host", "{}", "+"]
        for call in mock_run.call_args_list:
            assert call.kwargs["check"] is True

    def test_skips_missing_paths(self) -> None:
        # Only the pixi tree exists -> only it is chowned; a fresh host without
        # the repo yet must not error.
        def fake_exists(path: str) -> bool:
            return path == "/home/host/.pixi"

        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", side_effect=fake_exists),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_host_ownership()

        called_paths = [call.args[0] for call in mock_run.call_args_list]
        assert [cmd[:2] for cmd in called_paths] == [["find", "/home/host/.pixi"]]

    def test_noop_when_no_paths_exist(self) -> None:
        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_host_ownership()
        mock_run.assert_not_called()
