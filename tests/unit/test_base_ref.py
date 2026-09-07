"""Identidade segura das diferentes representações de uma branch base."""

import pytest

from ai_dev_orchestrator.domain.base_ref import base_refs_equivalent


@pytest.mark.parametrize(
    ("first", "second", "remote_name", "base_branch", "expected"),
    [
        ("main", "refs/remotes/origin/main", "origin", "main", True),
        ("main", "origin/main", "origin", "main", True),
        ("main", "refs/heads/main", "origin", "main", True),
        ("develop", "refs/remotes/upstream/develop", "upstream", "develop", True),
        ("main", "develop", "origin", "main", False),
        ("origin/main", "upstream/main", "origin", "main", False),
        ("origin/main", "upstream/main", "upstream", "main", False),
    ],
)
def test_base_refs_equivalent(
    first: str,
    second: str,
    remote_name: str,
    base_branch: str,
    expected: bool,
) -> None:
    assert base_refs_equivalent(
        first,
        second,
        remote_name=remote_name,
        base_branch=base_branch,
    ) is expected


def test_equal_arbitrary_refs_do_not_gain_aliases() -> None:
    assert base_refs_equivalent(
        "refs/remotes/upstream/main",
        "refs/remotes/upstream/main",
        remote_name="origin",
        base_branch="main",
    )
    assert not base_refs_equivalent(
        "upstream/main",
        "refs/remotes/upstream/main",
        remote_name="origin",
        base_branch="main",
    )
