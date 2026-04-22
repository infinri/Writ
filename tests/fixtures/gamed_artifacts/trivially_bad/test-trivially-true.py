"""Gamed artifact: trivially-true assertions that test nothing."""


def test_always_passes() -> None:
    assert True


def test_math() -> None:
    assert 1 == 1
    assert 2 + 2 == 4


def test_nothing_fails() -> None:
    x = []
    assert isinstance(x, list)
