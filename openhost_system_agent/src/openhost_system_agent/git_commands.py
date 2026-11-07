import os

import git

_HOOKS_CONFIG = "core.hooksPath=/dev/null"


def git_command(*args: str) -> list[str]:
    """Disable hooks at command scope, including recursive Git subprocesses."""
    return ["git", "-c", _HOOKS_CONFIG, *args]


class HooklessGit(git.Git):
    def __init__(self, working_dir: str | os.PathLike[str] | None = None) -> None:
        super().__init__(working_dir)
        self.set_persistent_git_options(c=_HOOKS_CONFIG)


class HooklessRepo(git.Repo):
    GitCommandWrapperType = HooklessGit
