"""One shared PDF renderer for preview, print jobs and reprints."""

import io
from pathlib import Path

from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.schemas import LabelConfig, valid_gtin

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("Promo", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("PromoBold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))


def fit_text(c, text, x, y, width, size, max_lines=1, align="left"):
    words = text.split()
    for font_size in range(size, 7, -1):
        lines, line = [], ""
        for word in words:
            # Break long unspaced strings without discarding content.
            parts = []
            while pdfmetrics.stringWidth(word, "PromoBold", font_size) > width:
                cut = max(
                    1, int(len(word) * width / pdfmetrics.stringWidth(word, "PromoBold", font_size)) - 1
                )
                parts.append(word[:cut])
                word = word[cut:]
            parts.append(word)
            for part in parts:
                candidate = (line + " " + part).strip()
                if pdfmetrics.stringWidth(candidate, "PromoBold", font_size) > width and line:
                    lines.append(line)
                    line = part
                else:
                    line = candidate
        if line:
            lines.append(line)
        if len(lines) <= max_lines:
            break
    if len(lines) > max_lines:
        raise ValueError("Description cannot fit safely; shorten text or choose landscape")
    c.setFont("PromoBold", font_size)
    for i, line in enumerate(lines):
        if align == "right":
            c.drawRightString(x + width, y - i * font_size * 1.25, line)
        elif align == "center":
            c.drawCentredString(x + width / 2, y - i * font_size * 1.25, line)
        else:
            c.drawString(x, y - i * font_size * 1.25, line)


def render_pdf(rows, config=None):
    config = LabelConfig.model_validate(config or {})
    pagesize = A4 if config.orientation == "portrait" else landscape(A4)
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=pagesize, invariant=1, pageCompression=1)
    c.setTitle("Mieszko promotion labels")
    w, h = pagesize
    red = HexColor(config.red)
    for row in rows:
        p = row["product"]
        margin = 20  # 7 mm print-safe inset
        c.setStrokeColor(red)
        c.setFillColor(white)
        c.setLineWidth(3)
        c.roundRect(margin, margin, w - margin * 2, h - margin * 2, 8, stroke=1, fill=1)
        header_y = h - 45
        logo_width = 0
        if row.get("logo_file"):
            c.drawImage(
                ImageReader(row["logo_file"]),
                32,
                h - 113,
                70,
                78,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
            logo_width = 75
        c.setFillColor(red)
        fit_text(c, "POLSKIE SUPERMARKETY", 33 + logo_width, header_y - 9, w * 0.51 - 34 - logo_width, 15)
        fit_text(c, "MIESZKO", 33 + logo_width, header_y - 48, w * 0.51 - 34 - logo_width, 39)
        date_x = w * 0.54
        c.setFillColor(red)
        c.roundRect(date_x, h - 116, w - date_x - 33, 80, 12, stroke=0, fill=1)
        c.setFillColor(white)
        fit_text(c, "Promotion Period", date_x + 10, h - 65, w - date_x - 53, 20, align="center")
        fit_text(
            c,
            f"{row['start_date']} - {row['end_date']}",
            date_x + 10,
            h - 94,
            w - date_x - 53,
            13,
            align="center",
        )
        # Body and footer have independent boxes, protecting barcode quiet zones.
        body_bottom, body_top = 158, h - 138
        body_h = body_top - body_bottom
        image_box = (34, body_bottom + 10, w * 0.54 - 34, body_h - 20)
        ix, iy, iw, ih = image_box
        scale = config.image_scale
        iw *= scale
        ih *= scale
        ix += (image_box[2] - iw) / 2 + config.image_x * image_box[2]
        iy += (image_box[3] - ih) / 2 + config.image_y * image_box[3]
        # Clamp adjustable image to reserved body rectangle.
        ix = max(34, min(ix, w * 0.54 - iw))
        iy = max(body_bottom, min(iy, body_top - ih))
        image_file = row.get("image_file")
        if image_file and Path(image_file).is_file():
            c.drawImage(
                ImageReader(image_file), ix, iy, iw, ih, preserveAspectRatio=True, anchor="c", mask="auto"
            )
        else:
            c.setFillColor(HexColor("#ECEFF2"))
            c.roundRect(ix + 12, iy + ih * 0.25, iw - 24, ih * 0.5, 12, fill=1, stroke=0)
            c.setFillColor(HexColor("#64748B"))
            fit_text(c, "Product image required", ix + 22, iy + ih / 2, iw - 44, 14, 2, "center")
        radius = min(w * 0.183, body_h * 0.37)
        cx = w * 0.765
        cy = body_bottom + body_h * 0.58
        c.setFillColor(HexColor("#FFE827"))
        c.setStrokeColor(red)
        c.setLineWidth(3)
        c.circle(cx, cy, radius, fill=1, stroke=1)
        c.setFillColor(HexColor("#111111"))
        fit_text(c, "£" + row["price"], cx - radius + 12, cy - 7, radius * 2 - 24, 64, align="center")
        if row.get("previous_price"):
            c.setFont("Promo", 11)
            c.drawCentredString(cx, cy + radius * 0.50, "Was £" + row["previous_price"])
        c.setFillColor(red)
        c.roundRect(cx - radius - 5, cy - radius + 8, radius * 2 + 10, 39, 5, fill=1, stroke=0)
        c.setFillColor(white)
        fit_text(
            c, p.get("unit") or "EACH", cx - radius + 2, cy - radius + 20, radius * 2 - 4, 21, align="center"
        )
        barcode = p["barcode"]
        kind = "EAN13" if len(barcode) == 13 and valid_gtin(barcode) else "Code128"
        drawing = createBarcodeDrawing(
            kind, value=barcode, barHeight=39, humanReadable=True, fontName="Promo", fontSize=9
        )
        max_w = w * 0.30
        factor = min(1, max_w / drawing.width)
        # Extremely long codes cannot be scannable inside this reserved area.
        if factor < 0.62:
            raise ValueError("Barcode too long for a scannable A4 label; select a shorter product barcode")
        c.saveState()
        c.translate(33, 53)
        c.scale(factor, factor)
        drawing.drawOn(c, 0, 0)
        c.restoreState()
        c.setFillColor(HexColor("#151B26"))
        fit_text(c, p["name"], w * 0.37, 124, w * 0.63 - 34, 22, 4, "right")
        fit_text(
            c,
            " / ".join(v for v in (p.get("weight"), p.get("brand")) if v),
            w * 0.37,
            48,
            w * 0.63 - 34,
            14,
            1,
            "right",
        )
        c.showPage()
    c.save()
    return stream.getvalue()
