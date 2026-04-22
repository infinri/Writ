"""Gamed artifact: tests that only verify mock internals, never real behavior."""
from unittest.mock import Mock


def test_mock_gets_called() -> None:
    m = Mock()
    m.do_thing(42)
    m.do_thing.assert_called_with(42)


def test_mock_return_value() -> None:
    m = Mock()
    m.return_value = "hello"
    assert m() == "hello"


def test_mock_attribute() -> None:
    m = Mock()
    m.x = 5
    assert m.x == 5
