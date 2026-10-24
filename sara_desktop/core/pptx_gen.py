"""
pptx_gen.py — real PowerPoint generation for SARA.

Builds a .pptx from a structured outline via python-pptx, saves it, then
VERIFY by reopening the file and counting slides. Every function returns
(verified, message) so callers can report failure honestly instead of
pretending a task succeeded.
"""

import re
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

# Clean dark theme (works on any default template)
_BG      = RGBColor(0x0E, 0x14, 0x21)
_TITLE   = RGBColor(0x4F, 0xC3, 0xF7)
_ACCENT  = RGBColor(0x4F, 0xC3, 0xF7)
_TEXT    = RGBColor(0xE2, 0xE8, 0xF0)
_MUTED   = RGBColor(0x8A, 0x97, 0xA8)


def parse_outline(text: str):
    """Parse an outline description into a structured slide list.

    Handles the formats agents actually produce:
      1. **Slide 2: Introduction**      ## Slide 3: Title
      - Subtitle: '...'   - bullets   - Quote: '...'   - Statistic: '...'
      1. **Title Slide**: Title 'Life is...', your name as presenter, ...
    Always returns a list of dicts; never raises.
    """
    slides: list[dict] = []

    def _mk(title):
        return {"title": title.strip(), "subtitle": "", "bullets": []}

    cur = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        # strip one leading bullet marker
        if line.startswith("- ") or line.startswith("• "):
            line = line[2:].strip()
            was_bullet = True
        else:
            was_bullet = False

        low = line.lower()

        # metadata lines
        if low.startswith("subtitle:"):
            if cur:
                cur["subtitle"] = line.split(":", 1)[1].strip().strip("\"'")
            continue
        if low.startswith(("quote:", "statistic:", "warning:", "include a quote:",
                           "closing quote:", "include a statistic:")):
            if cur:
                cur["bullets"].append("💬 " + line.split(":", 1)[1].strip().strip("\"'"))
            continue
        if low in ("content:", "content", "visual:", "visual"):
            continue

        if was_bullet:
            if cur:
                cur["bullets"].append(line)
            continue

        numbered = bool(re.match(r"^\d+[\.\)]\s", line))
        bold     = line.startswith("**")

        # heading: "Slide 2: Introduction", "1. **Slide 2: Introduction**",
        # "## 2. Introduction", "**Title Slide**: Title '...'"
        m = re.match(
            r"^(?:#+\s*)?(?:\d+[\.\)]\s*)?\s*\*{0,2}"
            r"(Slide\s*\d+|Title\s+Slide|Title)?\s*\*{0,2}\s*[:\-–]\s*(.*?)\*{0,2}\s*$",
            line, re.I)
        if m and (m.group(1) or line.startswith("#") or (numbered and bold)):
            head = (m.group(1) or "").strip()
            rest = (m.group(2) or "").strip()
            if head.lower() in ("title slide", "title") and rest:
                rest = re.split(r",\s*your name\b.*$", rest, flags=re.I)[0]
                rest = re.sub(r"^title\s*[:\-]?\s*", "", rest, flags=re.I).strip("\"' ")
                cur = _mk(rest or head)
            else:
                cur = _mk(rest or head)
            slides.append(cur)
            continue

        # generic markdown heading
        if line.startswith("#"):
            cur = _mk(line.lstrip("# "))
            slides.append(cur)
            continue

        # plain continuation paragraph inside the current slide
        if cur and len(line) > 3:
            cur["bullets"].append(line)

    return slides


def build_pptx(outline_text: str, out_path, title: str = "", presenter: str = "SARA",
               date_str: str = ""):
    """Build a .pptx from outline text. Returns (ok, message)."""
    try:
        out = Path(out_path)
        date_str = date_str or datetime.now().strftime("%d %B %Y")
        out.parent.mkdir(parents=True, exist_ok=True)
        slides = parse_outline(outline_text)
        if not slides:
            return False, "Outline produced no slides — nothing to build."

        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]

        def _style(tf, size=18, color=_TEXT, bold=False, align=PP_ALIGN.LEFT):
            tf.word_wrap = True
            for para in tf.paragraphs:
                para.alignment = align
                for run in para.runs:
                    run.font.size = Pt(size)
                    run.font.color.rgb = color
                    run.font.bold = bold
                    run.font.name = "Segoe UI"

        def _bg(slide, color=_BG):
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = color

        for idx, s in enumerate(slides, 1):
            slide = prs.slides.add_slide(blank)
            _bg(slide)

            # Title
            box = slide.shapes.add_textbox(Inches(0.7), Inches(0.45), Inches(12), Inches(1.2))
            tf = box.text_frame
            tf.text = s["title"]
            _style(tf, size=36 if idx == 1 else 30, color=_TITLE, bold=True)

            # Subtitle
            if s["subtitle"]:
                box = slide.shapes.add_textbox(Inches(0.7), Inches(1.6), Inches(12), Inches(0.6))
                tf = box.text_frame
                tf.text = s["subtitle"]
                _style(tf, size=20, color=_ACCENT, bold=False)

            # Bullets
            if s["bullets"]:
                top = 2.4 if (idx == 1 or s["subtitle"]) else 1.9
                box = slide.shapes.add_textbox(Inches(0.7), Inches(top), Inches(12), Inches(4.6))
                tf = box.text_frame
                tf.word_wrap = True
                for i, b in enumerate(s["bullets"]):
                    para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    para.text = "• " + b
                    para.level = 0
                    para.space_after = Pt(10)
                    for run in para.runs:
                        run.font.size = Pt(16)
                        run.font.color.rgb = _TEXT
                        run.font.name = "Segoe UI"

            # Title-slide extras: presenter + date
            if idx == 1 and (presenter or date_str):
                box = slide.shapes.add_textbox(Inches(0.7), Inches(6.3), Inches(12), Inches(0.5))
                tf = box.text_frame
                tf.text = "  |  ".join(x for x in (presenter, date_str) if x)
                _style(tf, size=14, color=_MUTED, align=PP_ALIGN.LEFT)

        prs.save(str(out))

        # ── VERIFY: reopen and count slides ─────────────────────────────
        if not out.exists() or out.stat().st_size < 5000:
            return False, f"PPTX save produced an invalid file at {out}"
        check = Presentation(str(out))
        n = len(check.slides)
        if n != len(slides):
            return False, f"PPTX verification failed: expected {len(slides)} slides, reopened file has {n}."

        return True, (f"Created {out.name} with {n} slides "
                      f"({out.stat().st_size // 1024} KB) — verified by reopening the file.")

    except Exception as e:
        return False, f"PPTX generation failed: {e}"


def describe_pptx(path) -> str:
    """Human-readable summary of an existing PPTX, for verification messages."""
    try:
        prs = Presentation(str(path))
        titles = []
        for i, slide in enumerate(prs.slides, 1):
            t = ""
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text.strip():
                    t = shape.text.strip().splitlines()[0]
                    break
            titles.append(f"  {i}. {t[:60]}")
        return f"{len(prs.slides)} slides:\n" + "\n".join(titles)
    except Exception as e:
        return f"Could not read: {e}"
