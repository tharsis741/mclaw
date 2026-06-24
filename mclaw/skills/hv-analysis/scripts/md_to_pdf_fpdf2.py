"""
fpdf2-based PDF converter for hv-analysis reports.
Windows-compatible alternative to md_to_pdf.py (which requires WeasyPrint/GTK3).

Usage:
    python md_to_pdf_fpdf2.py input.md output.pdf --title "报告标题" --author "作者名"
"""

import os
import re
import sys
import argparse
from fpdf import FPDF

FONT_DIR = 'C:/Windows/Fonts'


class ReportPDF(FPDF):
    """A4 report PDF with Chinese font support."""

    def __init__(self):
        super().__init__('P', 'mm', 'A4')
        self.set_auto_page_break(auto=True, margin=20)
        self._register_chinese_font()

    def _register_chinese_font(self):
        candidates = [
            ('C:/Windows/Fonts/Noto Sans SC (TrueType).otf', None),
            ('C:/Windows/Fonts/NotoSansSC-Regular.otf', None),
            ('C:/Windows/Fonts/msyh.ttc', None),
            ('C:/Windows/Fonts/simsun.ttc', None),
        ]
        bold_candidates = [
            ('C:/Windows/Fonts/Noto Sans SC Bold (TrueType).otf', None),
            ('C:/Windows/Fonts/NotoSansSC-Bold.otf', None),
            ('C:/Windows/Fonts/msyhbd.ttc', None),
            ('C:/Windows/Fonts/simsun.ttc', None),
        ]
        font_path = None
        bold_path = None
        for p, _ in candidates:
            if os.path.exists(p):
                font_path = p
                break
        for p, _ in bold_candidates:
            if os.path.exists(p):
                bold_path = p
                break
        if font_path:
            self.add_font('CN', '', font_path)
            self.add_font('CN', 'B', bold_path or font_path)
        else:
            raise RuntimeError('No Chinese font found in C:/Windows/Fonts/')

    def header(self):
        if self.page_no() > 1:
            self.set_font('CN', '', 7)
            self.set_text_color(150, 150, 150)
            self.cell(0, 8, self._title, align='C')
            self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font('CN', '', 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, str(self.page_no()), align='C')

    def add_cover(self, title, author='数字生命卡兹克'):
        self._title = title
        self.add_page()
        self.ln(30)
        self.set_font('CN', 'B', 26)
        self.set_text_color(20, 60, 120)
        self.multi_cell(0, 13, title, align='C')
        self.ln(8)
        self.set_font('CN', '', 11)
        self.set_text_color(100, 100, 100)
        self.cell(0, 8, '横纵分析研究报告', align='C')
        self.ln(10)
        import datetime
        self.cell(0, 8, datetime.date.today().strftime('%Y年%m月'), align='C')
        self.ln(30)
        self.set_font('CN', '', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, '方法论：横纵分析法 (Horizontal-Vertical Analysis)', align='C')
        self.ln(6)
        self.cell(0, 6, f'作者：{author}', align='C')

    def chapter_title(self, num, title):
        self.set_font('CN', 'B', 16)
        self.set_text_color(20, 60, 120)
        self.cell(0, 12, f'{num}. {title}')
        self.ln(14)

    def sub_title(self, title):
        self.set_font('CN', 'B', 12)
        self.set_text_color(60, 60, 60)
        self.cell(0, 10, title)
        self.ln(12)

    def sub_sub_title(self, title):
        self.set_font('CN', 'B', 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 8, title)
        self.ln(10)

    def body_text(self, text):
        self.set_font('CN', '', 9)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, text)
        self.ln(2)

    def bold_text(self, text):
        self.set_font('CN', 'B', 9)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, text)
        self.ln(2)

    def add_table(self, headers, rows, col_widths=None):
        page_w = self.w - 2 * self.l_margin
        if col_widths is None:
            col_widths = [page_w / len(headers)] * len(headers)
        self.set_font('CN', 'B', 7.5)
        self.set_fill_color(20, 60, 120)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=1, fill=True, align='C')
        self.ln()
        self.set_font('CN', '', 7)
        self.set_text_color(40, 40, 40)
        for ri, row in enumerate(rows):
            if ri % 2 == 1:
                self.set_fill_color(240, 245, 250)
                fill = True
            else:
                fill = False
            for i, cell in enumerate(row):
                self.cell(col_widths[i], 7, cell, border=1, fill=fill, align='C')
            self.ln()
        self.ln(6)


def convert(md_path, pdf_path, title=None, author='数字生命卡兹克'):
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Extract first H1 as title if not provided
    if not title:
        m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        title = m.group(1).strip() if m else '研究报告'

    pdf = ReportPDF()
    pdf.add_cover(title, author)

    sections = re.split(r'(?=^##\s+)', content, flags=re.MULTILINE)
    for section in sections:
        lines = section.strip().split('\n')
        if not lines or not lines[0].strip():
            continue
        first = lines[0].strip()
        if not first.startswith('## '):
            continue

        # Chapter title
        cn = re.match(r'##\s+([一二三四五六七八九十]+)[、.．]\s*(.*)', first)
        if cn:
            pdf.add_page()
            pdf.chapter_title(cn.group(1), cn.group(2))
        else:
            pdf.add_page()
            pdf.chapter_title('', first[3:].strip())

        i = 1
        table_data = []
        in_table = False
        while i < len(lines):
            line = lines[i].rstrip()

            if line.startswith('### ') and '### ' in line:
                pdf.sub_title(line[4:].strip())
                i += 1; continue
            if line.startswith('#### '):
                pdf.sub_sub_title(line[5:].strip())
                i += 1; continue

            # Table rows
            if line.startswith('|') and '---' not in line and '|' in line:
                cells = [c.strip() for c in line.split('|') if c.strip()]
                table_data.append(cells)
                in_table = True
                i += 1; continue
            if '|---' in line:
                i += 1; continue

            if in_table and table_data:
                if len(table_data) >= 2:
                    h = table_data[0]
                    r = table_data[1:]
                    w = (pdf.w - 2 * pdf.l_margin) / max(len(h), 1)
                    pdf.add_table(h, r, [w] * len(h))
                table_data = []
                in_table = False

            clean = line.strip()
            if not clean or clean in ('---', '___', '***'):
                i += 1; continue
            if clean.startswith('> '):
                i += 1; continue
            if clean.startswith('---'):
                i += 1; continue

            # Bold lines
            if clean.startswith('**') and clean.endswith('**'):
                pdf.bold_text(clean.replace('**', ''))
            elif clean:
                pdf.body_text(re.sub(r'\*\*(.*?)\*\*', r'\1', clean))
            i += 1

    pdf.output(pdf_path)
    print(f'[OK] {len(pdf.pages)} pages -> {pdf_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='Input Markdown file')
    parser.add_argument('output', help='Output PDF file')
    parser.add_argument('--title', help='Report title (default: from first H1)')
    parser.add_argument('--author', default='数字生命卡兹克')
    args = parser.parse_args()
    convert(args.input, args.output, args.title, args.author)
