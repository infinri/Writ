"""Normalize CLI output for content assertions.

CI forces color (Rich styles the '--' prefix apart from the option name, so
'--dest' is not a contiguous substring of the raw output) and wraps help boxes
at 80 columns (splitting long literals across box-border lines). Assert against
plain(output) to check content, never layout.
"""
import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """ANSI stripped; box borders and newlines collapsed to single spaces."""
    return " ".join(_ANSI.sub("", text).replace("│", " ").split())
