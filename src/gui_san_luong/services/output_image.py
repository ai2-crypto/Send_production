"""Draw an employee's daily output as coloured images, for sending over Zalo.

Zalo text cannot be bold or coloured, so this is the way to make item codes, machines and
quantities stand out. Rows are split between images so none grows too tall to read on a
phone, and every file stays under the 1 MB Zalo accepts.
"""

import io
import math
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gui_san_luong.services.worker_output import (
    DetailLine,
    EmployeeDay,
    EmployeeLine,
    day_totals,
    department_title,
    format_quantity,
    share_calculation,
)

ROWS_PER_IMAGE = 10
MAX_IMAGE_BYTES = 1_000_000
JPEG_QUALITY = 85
WIDTH = 900
PAD = 36
CARD_TEXT_X = PAD + 74
CARD_RIGHT = WIDTH - PAD - 24
# Drawn at twice the size so text stays sharp on phone screens.
SCALE = 2

# Segoe UI on Windows; DejaVu (both have Vietnamese) on Linux, e.g. the Docker image,
# because Microsoft's fonts may not be copied there.
FONT_SETS = (
    (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        {"regular": "segoeui.ttf", "bold": "segoeuib.ttf", "mono": "consolab.ttf"},
    ),
    (
        Path("/usr/share/fonts/truetype/dejavu"),
        {
            "regular": "DejaVuSans.ttf",
            "bold": "DejaVuSans-Bold.ttf",
            "mono": "DejaVuSansMono-Bold.ttf",
        },
    ),
)


def _installed_font_set() -> tuple[Path, dict[str, str]]:
    return next(
        (
            (folder, files)
            for folder, files in FONT_SETS
            if all((folder / name).is_file() for name in files.values())
        ),
        FONT_SETS[0],
    )


FONT_DIR, FONT_FILES = _installed_font_set()

BACKGROUND = "#F4F6FA"
CARD = "#FFFFFF"
INK = "#1F2937"
MUTED = "#6B7280"
BLUE = "#0068FF"
BLUE_SOFT = "#E6F0FF"
HEADER_SUBTEXT = "#DCE8FF"
GREEN = "#0F9D58"
ORANGE = "#C2410C"
ORANGE_SOFT = "#FFEDD5"
PURPLE = "#6D28D9"
PURPLE_SOFT = "#EDE9FE"
RED = "#DC2626"
RED_SOFT = "#FEE2E2"


class OutputImageError(Exception):
    """A user-facing reason why the images could not be drawn."""


@dataclass(frozen=True)
class OutputImage:
    data: bytes
    file_name: str
    caption: str


def fonts_available() -> bool:
    return all((FONT_DIR / name).is_file() for name in FONT_FILES.values())


@cache
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / FONT_FILES[kind]), size * SCALE)
    except OSError as error:
        raise OutputImageError(
            f"Không tìm thấy phông chữ {FONT_FILES[kind]} trong {FONT_DIR}."
        ) from error


def _entries(employee: EmployeeDay) -> list[tuple[EmployeeLine, DetailLine | None]]:
    """One entry per Excel row; a department with no rows still shows its heading."""
    entries: list[tuple[EmployeeLine, DetailLine | None]] = []
    for line in employee.lines:
        entries.extend((line, detail) for detail in line.details)
        if not line.details:
            entries.append((line, None))
    return entries


def image_count(employee: EmployeeDay) -> int:
    return max(1, math.ceil(len(_entries(employee)) / ROWS_PER_IMAGE))


def image_captions(employee: EmployeeDay) -> list[str]:
    """The text sent with each image: the total stays readable once Zalo drops the image."""
    count = image_count(employee)
    title = (
        f"Sản lượng ngày {employee.day:%d/%m/%Y} – {employee.employee_name} – "
        f"MS {employee.employee_code}"
    )
    total = f"TỔNG: {day_totals(employee)}"
    return [
        f"{title} (ảnh {number}/{count})\n{total}" if count > 1 else f"{title}\n{total}"
        for number in range(1, count + 1)
    ]


def _scaled(value: float) -> int:
    return round(value * SCALE)


class _Canvas:
    def __init__(self, height: int) -> None:
        self.image = Image.new("RGB", (_scaled(WIDTH), _scaled(height)), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)

    def width_of(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        return self.draw.textlength(text, font=font) / SCALE

    def text(
        self, x: float, y: float, text: str, font: ImageFont.FreeTypeFont, fill: str
    ) -> float:
        self.draw.text((_scaled(x), _scaled(y)), text, font=font, fill=fill)
        return self.width_of(text, font)

    def box(
        self, x0: float, y0: float, x1: float, y1: float, fill: str, radius: float = 14
    ) -> None:
        self.draw.rounded_rectangle(
            (_scaled(x0), _scaled(y0), _scaled(x1), _scaled(y1)),
            _scaled(radius),
            fill=fill,
        )

    def chip(self, x: float, y: float, text: str, fill: str, background: str) -> float:
        font = _font("bold", 19)
        width = self.width_of(text, font) + 24
        self.box(x, y, x + width, y + 34, background, 17)
        self.text(x + 12, y + 3, text, font, fill)
        return width

    def wrapped(
        self, text: str, font: ImageFont.FreeTypeFont, width: float
    ) -> list[str]:
        """Break at spaces, or anywhere in a long code that has none."""
        lines: list[str] = []
        current = ""
        for word in text.split(" "):
            candidate = f"{current} {word}".strip()
            if self.width_of(candidate, font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = ""
            for char in word:
                if self.width_of(current + char, font) > width and current:
                    lines.append(current)
                    current = ""
                current += char
        if current:
            lines.append(current)
        return lines or [""]


def _draw_header(
    canvas: _Canvas, employee: EmployeeDay, number: int, count: int
) -> float:
    title = f"SẢN LƯỢNG NGÀY {employee.day:%d/%m/%Y}"
    if count > 1:
        title += f"  ({number}/{count})"
    canvas.box(0, 0, WIDTH, 150, BLUE, 0)
    canvas.text(PAD, 26, title, _font("bold", 30), "#FFFFFF")
    canvas.text(
        PAD,
        78,
        f"{employee.employee_name}  ·  MS {employee.employee_code}",
        _font("regular", 24),
        HEADER_SUBTEXT,
    )
    canvas.box(PAD, 120, WIDTH - PAD, 230, CARD, 18)
    canvas.text(PAD + 28, 140, "TỔNG SẢN LƯỢNG", _font("bold", 18), MUTED)
    canvas.text(PAD + 28, 162, day_totals(employee), _font("bold", 40), GREEN)
    return 258


def _draw_heading(canvas: _Canvas, y: float, line: EmployeeLine) -> float:
    unit = f" {line.unit}" if line.unit else ""
    text = f"{department_title(line)}: {format_quantity(line.quantity)}{unit}"
    canvas.box(PAD, y, PAD + 8, y + 34, BLUE, 4)
    canvas.text(PAD + 22, y - 2, text, _font("bold", 24), INK)
    return y + 56


def _draw_row(
    canvas: _Canvas, y: float, number: int, line: EmployeeLine, detail: DetailLine
) -> float:
    """A card with chips for shift and machine, the item code, then the sum."""
    regular, bold, mono = _font("regular", 20), _font("bold", 24), _font("mono", 21)
    text_width = CARD_RIGHT - CARD_TEXT_X
    item_lines = canvas.wrapped(detail.detail.item or "", mono, text_width)
    step_lines = (
        canvas.wrapped(detail.detail.step, regular, text_width)
        if detail.detail.step
        else []
    )
    calculation = f"{share_calculation(detail)}  =  "
    unit = f" {line.unit}" if line.unit else ""
    result = f"{format_quantity(detail.quantity)}{unit}"
    fits = (
        canvas.width_of(calculation, regular) + canvas.width_of(result, bold)
        <= text_width
    )
    calculation_lines = canvas.wrapped(calculation.strip(), regular, text_width)
    height = (
        70
        + 32 * len([part for part in item_lines if part])
        + 30 * len(step_lines)
        + (34 if fits else 30 * len(calculation_lines) + 36)
        + 18
    )
    canvas.box(PAD, y, WIDTH - PAD, y + height, CARD, 16)
    canvas.box(PAD + 18, y + 18, PAD + 58, y + 58, BLUE_SOFT, 20)
    label = str(number)
    canvas.text(
        PAD + 38 - canvas.width_of(label, _font("bold", 20)) / 2,
        y + 22,
        label,
        _font("bold", 20),
        BLUE,
    )
    x: float = CARD_TEXT_X
    if detail.detail.shift:
        x += (
            canvas.chip(x, y + 21, f"Ca {detail.detail.shift}", PURPLE, PURPLE_SOFT)
            + 10
        )
    if detail.detail.machine:
        x += (
            canvas.chip(x, y + 21, f"Máy {detail.detail.machine}", ORANGE, ORANGE_SOFT)
            + 10
        )
    if detail.detail.overtime_quantity:
        overtime = format_quantity(detail.overtime_share)
        canvas.chip(x, y + 21, f"Tăng ca {overtime}{unit}", RED, RED_SOFT)
    elif detail.detail.overtime:
        canvas.chip(x, y + 21, "Tăng ca", RED, RED_SOFT)
    cursor = y + 66
    for part in item_lines:
        if part:
            canvas.text(CARD_TEXT_X, cursor, part, mono, BLUE)
            cursor += 32
    for part in step_lines:
        canvas.text(CARD_TEXT_X, cursor, part, regular, MUTED)
        cursor += 30
    if fits:
        offset = canvas.text(CARD_TEXT_X, cursor + 3, calculation, regular, MUTED)
        canvas.text(CARD_TEXT_X + offset, cursor, result, bold, GREEN)
    else:
        for part in calculation_lines:
            canvas.text(CARD_TEXT_X, cursor, part, regular, MUTED)
            cursor += 30
        canvas.text(CARD_TEXT_X, cursor, f"= {result}", bold, GREEN)
    return y + height + 16


def _encode(image: Image.Image) -> tuple[bytes, str]:
    """PNG keeps text crisp; a JPEG or a smaller copy only when PNG is over the limit."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    if buffer.tell() <= MAX_IMAGE_BYTES:
        return buffer.getvalue(), "png"
    for scale in (1.0, 0.75, 0.5):
        resized = image.resize(
            (round(image.width * scale), round(image.height * scale))
        )
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        if buffer.tell() <= MAX_IMAGE_BYTES:
            return buffer.getvalue(), "jpg"
    raise OutputImageError("Ảnh sản lượng lớn hơn 1 MB, Zalo không nhận.")


def render_output_images(employee: EmployeeDay) -> list[OutputImage]:
    entries = _entries(employee)
    count = image_count(employee)
    captions = image_captions(employee)
    images = []
    for index in range(count):
        chunk = entries[index * ROWS_PER_IMAGE : (index + 1) * ROWS_PER_IMAGE]
        # Generous room first, cropped to what was drawn at the end.
        canvas = _Canvas(400 + 330 * max(len(chunk), 1))
        y: float = _draw_header(canvas, employee, index + 1, count)
        previous: EmployeeLine | None = None
        for offset, (line, detail) in enumerate(chunk):
            if line is not previous:
                y = _draw_heading(canvas, y, line)
                previous = line
            if detail is not None:
                number = index * ROWS_PER_IMAGE + offset + 1
                y = _draw_row(canvas, y, number, line, detail)
        canvas.text(
            PAD,
            y + 8,
            "Sai số liệu? Nhắn lại cho OA để được kiểm tra.",
            _font("regular", 18),
            MUTED,
        )
        cropped = canvas.image.crop((0, 0, canvas.image.width, _scaled(y + 60)))
        data, extension = _encode(cropped)
        images.append(
            OutputImage(
                data,
                f"san-luong-{employee.employee_code}-{employee.day:%Y%m%d}-{index + 1}"
                f".{extension}",
                captions[index],
            )
        )
    return images
