from __future__ import annotations

from io import BytesIO


PDF_FONT = "STSong-Light"


def render_interview_review_pdf(title: str, markdown: str) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise ValueError("PDF 导出依赖未安装，请运行 pip install -r requirements.txt。") from exc

    pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))
    buffer = BytesIO()
    document = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    document.setTitle(f"{title or '面试复盘'} - 面试复盘")
    document.setAuthor("简历投递 Agent")
    page_width, page_height = A4
    left = 48
    right = 48
    top = page_height - 52
    bottom = 48
    y = top

    def ensure_space(height: float) -> None:
        nonlocal y
        if y - height < bottom:
            document.showPage()
            y = top

    def draw_wrapped(text: str, size: int, leading: int) -> None:
        nonlocal y
        for line in wrap_pdf_line(text, page_width - left - right, PDF_FONT, size, pdfmetrics):
            ensure_space(leading)
            document.setFont(PDF_FONT, size)
            document.drawString(left, y, line)
            y -= leading

    document.setFont(PDF_FONT, 18)
    document.drawString(left, y, f"{title or '面试复盘'} 面试复盘")
    y -= 30

    for raw_line in (markdown or "暂无面试复盘内容。").splitlines():
        line = raw_line.strip()
        if not line:
            y -= 7
            continue
        if line.startswith("## "):
            y -= 4
            draw_wrapped(line[3:], 14, 22)
            y -= 3
            continue
        if line.startswith("# "):
            draw_wrapped(line[2:], 16, 24)
            continue
        draw_wrapped(line, 10, 16)

    document.save()
    return buffer.getvalue()


def wrap_pdf_line(text: str, max_width: float, font_name: str, size: int, pdfmetrics) -> list[str]:
    source = text or ""
    lines: list[str] = []
    current = ""
    for char in source:
        candidate = current + char
        if current and pdfmetrics.stringWidth(candidate, font_name, size) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current or not lines:
        lines.append(current)
    return lines
