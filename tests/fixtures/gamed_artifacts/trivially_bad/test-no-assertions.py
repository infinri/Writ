"""Gamed artifact: function-named tests with no assertions."""


def test_something_happens() -> None:
    result = compute_something()
    # look at result


def test_other_thing() -> None:
    other = do_other()


def compute_something() -> int:
    return 42


def do_other() -> str:
    return "ok"
