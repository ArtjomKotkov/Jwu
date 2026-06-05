"""Копирование в системный буфер обмена (через pyperclip)."""

from __future__ import annotations


def copy_to_clipboard(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)
