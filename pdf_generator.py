"""
PDF generator — converts the digest markdown text to a styled PDF.
Uses reportlab with DejaVu fonts for proper Cyrillic support.
"""

import io
import re
import os
from datetime import datetime, timezone, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    Table, TableStyle, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

DUBAI_TZ = timezone(timedelta(hours=4))

# ── Color palette ──────────────────────────────────────────────────────────
COLOR_BG_HEADER   = HexColor("#1a1a2e")
COLOR_ACCENT      = HexColor("#7c3aed")  # purple
COLOR_ACCENT_LIGHT= HexColor("#a78bfa")
COLOR_TEXT        = HexColor("#1f2937")
COLOR_MUTED       = HexColor("#6b7280")
COLOR_SECTION_BG  = HexColor("#f3f0ff")
COLOR_DIVIDER     = HexColor("#e5e7eb")
COLOR_WHITE       = white

# ── Font registration ──────────────────────────────────────────────────────
# DejaVu fonts support full Unicode including Cyrillic and emoji fallback
_FONTS_REGISTERED = False

def _register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return

    # Possible locations for DejaVu fonts
    font_search_paths = [
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
        "/usr/share/fonts/truetype",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.join(os.path.dirname(__file__), "fonts"),
    ]

    font_files = {
        "DejaVu": "DejaVuSans.ttf",
        "DejaVu-Bold": "DejaVuSans-Bold.ttf",
    }

    found = {}
    for name, filename in font_files.items():
        for path in font_search_paths:
            full = os.path.join(path, filename)
            if os.path.exists(full):
                found[name] = full
                break

    if "DejaVu" in found:
        # Register normal font
        pdfmetrics.registerFont(TTFont("DejaVu", found["DejaVu"]))
        # Register bold font (fallback to normal if not found)
        bold_path = found.get("DejaVu-Bold", found["DejaVu"])
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold_path))
        # Register font family — map bold/italic ALL to registered names
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            "DejaVu",
            normal="DejaVu",
            bold="DejaVu-Bold",
            italic="DejaVu",
            boldItalic="DejaVu-Bold",
        )
        _FONTS_REGISTERED = True
        return True

    return False


def _font(variant="normal"):
    """Return font name: DejaVu if available, else fallback to Helvetica."""
    if _FONTS_REGISTERED:
        mapping = {
            "normal": "DejaVu",
            "bold": "DejaVu-Bold",
            "italic": "DejaVu",       # no separate italic — use normal
            "bolditalic": "DejaVu-Bold",
        }
        return mapping.get(variant, "DejaVu")
    else:
        mapping = {
            "normal": "Helvetica",
            "bold": "Helvetica-Bold",
            "italic": "Helvetica-Oblique",
            "bolditalic": "Helvetica-BoldOblique",
        }
        return mapping.get(variant, "Helvetica")


def _strip_emoji(text: str) -> str:
    """Remove emoji characters that reportlab cannot render."""
    # Remove common emoji ranges
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\U00002600-\U000026FF"
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002300-\U000023FF"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text).strip()


def get_styles() -> dict:
    _register_fonts()

    styles = {
        "title": ParagraphStyle(
            "DigestTitle",
            fontName=_font("bold"),
            fontSize=22,
            textColor=COLOR_WHITE,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "DigestSubtitle",
            fontName=_font("normal"),
            fontSize=11,
            textColor=HexColor("#c4b5fd"),
            alignment=TA_CENTER,
            spaceAfter=0,
        ),
        "section_header": ParagraphStyle(
            "SectionHeader",
            fontName=_font("bold"),
            fontSize=13,
            textColor=COLOR_ACCENT,
            spaceBefore=16,
            spaceAfter=6,
        ),
        "item_title": ParagraphStyle(
            "ItemTitle",
            fontName=_font("bold"),
            fontSize=11,
            textColor=COLOR_TEXT,
            spaceBefore=8,
            spaceAfter=2,
        ),
        "item_body": ParagraphStyle(
            "ItemBody",
            fontName=_font("normal"),
            fontSize=9.5,
            textColor=COLOR_TEXT,
            leading=14,
            spaceAfter=2,
            alignment=TA_JUSTIFY,
        ),
        "item_link": ParagraphStyle(
            "ItemLink",
            fontName=_font("italic"),
            fontSize=8.5,
            textColor=COLOR_ACCENT,
            spaceAfter=4,
        ),
        "footer": ParagraphStyle(
            "Footer",
            fontName=_font("normal"),
            fontSize=8,
            textColor=COLOR_MUTED,
            alignment=TA_CENTER,
        ),
    }
    return styles


SECTION_MARKERS = [
    ("🔥", "ТОП-НОВОСТИ ДНЯ"),
    ("⚡", "НОВЫЕ ТЕХНОЛОГИИ"),
    ("🏢", "AI В НЕДВИЖИМОСТИ"),
    ("🍕", "AI В ОБЩЕПИТЕ"),
    ("💡", "КАК ЛЮДИ ИСПОЛЬЗУЮТ AI"),
    ("🔮", "ПРОГНОЗЫ И ПЕРСПЕКТИВЫ"),
    ("💎", "ЖЕМЧУЖИНА ДНЯ"),
]

# Text replacements for emoji section markers
SECTION_EMOJI_REPLACE = {
    "🔥": "[TOP]",
    "⚡": "[NEW]",
    "🏢": "[RE]",
    "🍕": "[QSR]",
    "💡": "[HOW]",
    "🔮": "[FORECAST]",
    "💎": "[GEM]",
    "🗞": "[NEWS]",
    "🔗": "->",
}

def _clean_text(text: str) -> str:
    """Replace emoji with text equivalents and strip remaining ones."""
    for emoji, replacement in SECTION_EMOJI_REPLACE.items():
        text = text.replace(emoji, replacement)
    return _strip_emoji(text)


def parse_digest(text: str) -> list[dict]:
    """
    Parse the digest text into structured items.
    Returns list of dicts: {type: 'section'|'item', ...}
    """
    elements = []
    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Section header
        is_section = False
        for emoji, keyword in SECTION_MARKERS:
            if emoji in line and keyword[:6] in line.upper():
                is_section = True
                clean = re.sub(r"\*+", "", line).strip()
                clean = _clean_text(clean)
                elements.append({"type": "section", "text": clean})
                break

        if is_section:
            continue

        # Numbered item title
        m = re.match(r"^\*{0,2}(\d+)[.)]\s+(.+)", line)
        if m:
            num = m.group(1)
            rest = re.sub(r"\*+", "", m.group(2)).strip()
            rest = _clean_text(rest)
            elements.append({"type": "item_title", "num": num, "text": rest})
            continue

        # URL line
        if line.startswith("http") or re.search(r"https?://", line):
            elements.append({"type": "link", "text": line})
            continue

        # Regular body text
        clean = re.sub(r"\*+", "", line).strip()
        clean = _clean_text(clean)
        if clean:
            elements.append({"type": "body", "text": clean})

    return elements


def build_header(styles: dict, date_str: str) -> list:
    """Build the PDF header block."""
    header_content = [
        Paragraph("AI-ДАЙДЖЕСТ", styles["title"]),
        Paragraph(date_str, styles["subtitle"]),
        Spacer(1, 8),
        Paragraph("Ежедневный обзор мира искусственного интеллекта", styles["subtitle"]),
    ]
    table = Table([[header_content]], colWidths=[A4[0] - 4*cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_HEADER),
        ("TOPPADDING", (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 20),
        ("LEFTPADDING", (0, 0), (-1, -1), 20),
        ("RIGHTPADDING", (0, 0), (-1, -1), 20),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
    ]))
    return [table, Spacer(1, 16)]


def generate_pdf(digest_text: str) -> io.BytesIO:
    """Generate a styled PDF from the digest text. Returns BytesIO."""
    buf = io.BytesIO()
    now_dubai = datetime.now(DUBAI_TZ)
    date_str = now_dubai.strftime("%d %B %Y • %H:%M Dubai Time")

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2*cm,
        rightMargin=2*cm,
        topMargin=2*cm,
        bottomMargin=2*cm,
        title=f"AI Digest {now_dubai.strftime('%Y-%m-%d')}",
        author="AI Digest Bot",
    )

    styles = get_styles()
    story = []

    # Header
    story.extend(build_header(styles, date_str))

    # Parse and render content
    elements = parse_digest(digest_text)

    for el in elements:
        if el["type"] == "section":
            story.append(Spacer(1, 8))
            story.append(HRFlowable(
                width="100%", thickness=1,
                color=COLOR_ACCENT_LIGHT, spaceAfter=4
            ))
            story.append(Paragraph(el["text"], styles["section_header"]))

        elif el["type"] == "item_title":
            title_text = f'{el["num"]}. {el["text"]}'
            story.append(Spacer(1, 6))
            story.append(Paragraph(title_text, styles["item_title"]))

        elif el["type"] == "body":
            story.append(Paragraph(el["text"], styles["item_body"]))

        elif el["type"] == "link":
            link_text = el["text"]
            if len(link_text) > 90:
                link_text = link_text[:87] + "..."
            story.append(Paragraph(f"-> {link_text}", styles["item_link"]))

    # Footer
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=COLOR_DIVIDER))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"AI Digest Bot • Сгенерировано {date_str} • Источники: 50+ каналов",
        styles["footer"]
    ))

    doc.build(story)
    buf.seek(0)
    return buf
