"""Outgoing-text sanitizer — strips Markdown/LaTeX Telegram can't render."""
from agentzero.telegram_io import _to_plain


def test_strips_markdown_emphasis():
    assert _to_plain("the *current* rate") == "the current rate"
    assert _to_plain("**bold** and *italic*") == "bold and italic"
    assert _to_plain("price is *₵278.31*") == "price is ₵278.31"


def test_strips_latex_math():
    raw = r"\[ 22.99 \, \text{USD} \times 12.09 \approx 278.31 \]"
    out = _to_plain(raw)
    assert "\\" not in out
    assert "text{" not in out
    assert "USD" in out
    assert "×" in out
    assert "≈" in out


def test_real_world_example():
    raw = (
        "The current exchange rate is approximately *1 USD = 12.09 GHS*.\n\n"
        "So, for the *Xbox Game Pass priced at $22.99*, that would be:\n\n"
        r"\[ 22.99 \, \text{USD} \times 12.09 \, \text{GHS/USD} \approx 278.31 \, \text{GHS} \]"
        "\n\nIn summary, it would cost about *₵278.31*."
    )
    out = _to_plain(raw)
    assert "*" not in out
    assert "\\" not in out
    assert "₵278.31" in out
    assert "1 USD = 12.09 GHS" in out
    assert "$22.99" in out  # currency dollar sign is preserved


def test_converts_markdown_links():
    assert _to_plain("[Apply here](https://x.com/job)") == "Apply here (https://x.com/job)"


def test_plain_text_untouched():
    plain = "Got it — I'll remind you at 14:32. Anything else?"
    assert _to_plain(plain) == plain
