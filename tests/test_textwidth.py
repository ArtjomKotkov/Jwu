"""Выравнивание пробелами: значение у правого края, длинное — строками ниже, тоже к краю."""

from jwu.core import textwidth as tw


def _line_width(line: str) -> float:
    import re
    bold = re.search(r"<b>(.*?)</b>", line)
    rest = re.sub(r"<b>.*?</b>", "", line)
    return tw.width(rest) + (tw.width(bold.group(1), bold=True) if bold else 0)


def test_row_reaches_right_edge():
    line = tw.row("✅", "апрув", "Oleg Smirnov")
    assert "\n" not in line and abs(_line_width(line) - tw.CAP_TEXT) < tw.HAIR_W
    assert tw.row("🚀", "готов к мержу", "") == "🚀 <b>готов к мержу</b>"


def test_long_value_wraps_at_arrow_and_stays_right():
    lines = tw.row("🔀", "статус", "Dev at stand → Ready for demo and much more text", cap=10).split("\n")
    assert lines[0] == "🔀 <b>статус</b>" and lines[1].endswith("Dev at stand →")
    assert all(_line_width(x) <= 10 + tw.HAIR_W for x in lines[1:])


def test_html_escaped():
    assert "&lt;b&gt;" in tw.row("", "x", "<b>")
