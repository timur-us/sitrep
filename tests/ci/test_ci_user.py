"""The tests run as a non-root user, locally and in CI (ubuntu-24.04 runners do).

As root, tests/test_data_dirs.py skips its unreadable-directory case, because root
reads any directory. This keeps that skip from going unnoticed.
"""

from __future__ import annotations

import os


def test_tests_do_not_run_as_root() -> None:
    assert os.geteuid() != 0, "the tests run as root; test_unreadable_directory_is_reported skips"
