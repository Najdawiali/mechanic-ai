"""Draws a synthetic connector picture where the test points exist ONLY as graphics.

Why synthetic: the real procedure PDFs are licensed client data and must not
travel with the repo. This picture copies the property that matters: the pin
numbers are drawn, not text, so only a vision model can read them. It is an
EASIER case than the real PDFs. A pass here is necessary, not sufficient;
the real test is the P0010 page (use --real-image).
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

RED = (200, 30, 30)
BLACK = (20, 20, 20)
WHITE = (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _cell(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, label: str, marked: bool) -> None:
    draw.rectangle([x, y, x + w, y + h], fill=RED if marked else WHITE, outline=BLACK, width=2)
    font = _font(int(h * 0.5))
    box = draw.textbbox((0, 0), label, font=font)
    tx = x + (w - (box[2] - box[0])) / 2 - box[0]
    ty = y + (h - (box[3] - box[1])) / 2 - box[1]
    draw.text((tx, ty), label, fill=WHITE if marked else BLACK, font=font)


def make_pin_image(valve_pins: list[int], pcm_pin: int) -> bytes:
    """valve_pins: which of pins 1/2 on the 2P connector are marked. pcm_pin: 1..80."""
    img = Image.new("RGB", (1500, 620), WHITE)
    draw = ImageDraw.Draw(img)

    draw.text((40, 30), "VTC OIL CONTROL SOLENOID VALVE A - 2P CONNECTOR", fill=BLACK, font=_font(26))
    draw.text((40, 66), "Terminal side of male terminals", fill=BLACK, font=_font(18))
    for i, pin in enumerate((1, 2)):
        _cell(draw, 60 + i * 110, 110, 110, 90, str(pin), pin in valve_pins)

    draw.text((40, 260), "PCM CONNECTOR E (80P)", fill=BLACK, font=_font(26))
    draw.text((40, 296), "Wire side of female terminals", fill=BLACK, font=_font(18))
    cols, rows, w, h = 20, 4, 68, 60
    for r in range(rows):
        for c in range(cols):
            pin = r * cols + c + 1
            _cell(draw, 60 + c * w, 340 + r * h, w, h, str(pin), pin == pcm_pin)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    with open("pin_test.png", "wb") as fh:
        fh.write(make_pin_image([1, 2], 64))
    print("wrote pin_test.png")
