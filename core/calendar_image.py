"""Pillow renderer for the Sunday–Saturday partial recruitment calendar."""
from datetime import date, timedelta
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.calendar import week_days


@lru_cache(maxsize=1)
def font_paths() -> tuple[Path, Path]:
    """Use installed Korean fonts; no font files are stored in the repository."""
    nanum = Path('/usr/share/fonts/truetype/nanum')
    windows = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    candidates = [
        (nanum / 'NanumBarunGothic.ttf', nanum / 'NanumBarunGothicBold.ttf'),
        (nanum / 'NanumGothic.ttf', nanum / 'NanumGothicBold.ttf'),
        (windows / 'malgun.ttf', windows / 'malgunbd.ttf'),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise RuntimeError(
        '달력에 필요한 한글 폰트를 찾을 수 없습니다. '
        'Ubuntu: sudo apt install fonts-nanum / Windows: 맑은 고딕 설치 확인')


def calendar_rows(start: date) -> list[list[date]]:
    """Only the two ordinary calendar weeks containing the raid dates."""
    week_days(start)  # Reject dates other than the Thursday start.
    first = start - timedelta(days=(start.weekday() + 1) % 7)
    return [[first + timedelta(days=r * 7 + c) for c in range(7)] for r in range(2)]


def render_calendar(start: date) -> Image.Image:
    rows = calendar_rows(start)
    end = start + timedelta(days=6)
    first = rows[0][0]
    weeks = len(rows)
    regular, bold_path = font_paths()
    margin, cw, rh = 25, 130, 100
    x0, y0 = margin, 152
    width, height = 2*margin + 7*cw, y0 + weeks*rh + 27
    bg, panel, grid = '#171a22', '#202530', '#343b48'
    muted, bright = '#7e899b', '#edf1f8'
    accent, selected = '#b9a7f5', '#3a3355'
    weekend = {0: '#f2a0a9', 6: '#8abcf5'}
    im = Image.new('RGB', (width, height), bg)
    d = ImageDraw.Draw(im)
    def font(n, bold=False):
        return ImageFont.truetype(str(bold_path if bold else regular), n)
    def center(pos, value, f, color):
        box = d.textbbox((0, 0), value, font=f)
        x, y = pos
        d.text((x-(box[0]+box[2])/2, y-(box[1]+box[3])/2), value, font=f, fill=color)
    period = f'{start.month:02d}.{start.day:02d} ~ {end.month:02d}.{end.day:02d}'
    d.text((margin+4, 19), f'{period}  파티 모집', font=font(37, True), fill=bright)
    if start.year == end.year:
        month_label = f'{start.year}년 {start.month}월' if start.month == end.month else f'{start.year}년 {start.month}–{end.month}월'
    else:
        month_label = f'{start.year}년 {start.month}월 – {end.year}년 {end.month}월'
    d.text((margin+4, 67), month_label, font=font(22), fill=muted)
    d.rounded_rectangle((width-margin-176, 40, width-margin-158, 58), radius=4, fill=selected, outline=accent, width=2)
    d.text((width-margin-146, 38), '모집 기간', font=font(21), fill=accent)
    d.rounded_rectangle((x0, 109, width-margin, y0+weeks*rh), radius=14, fill=panel, outline=grid, width=2)
    for i, label in enumerate('일월화수목금토'):
        center((x0+(i+.5)*cw, 130), label, font(26, True), weekend.get(i, bright))
    for r in range(weeks):
        for c in range(7):
            x, y = x0+c*cw, y0+r*rh
            if c: d.line((x,y0,x,y0+weeks*rh), fill=grid, width=1)
            d.line((x0,y,width-margin,y), fill=grid, width=1)
        # One rounded highlight per contiguous selected segment of this row.
        indices = [c for c in range(7) if start <= rows[r][c] <= end]
        if indices:
            l, rr = indices[0], indices[-1]
            d.rounded_rectangle((x0+l*cw+4,y0+r*rh+5,x0+(rr+1)*cw-4,y0+(r+1)*rh-5), radius=13, fill=selected, outline=accent, width=3)
            for c in range(l+1,rr+1):
                x = x0+c*cw
                d.line((x,y0+r*rh+8,x,y0+(r+1)*rh-8), fill='#5d5278', width=1)
        for c in range(7):
            day = rows[r][c]
            active = start <= day <= end
            x, y = x0+(c+.5)*cw, y0+r*rh
            color = weekend.get(c, bright) if active else ({0:'#a26b76',6:'#6184ae'}.get(c, muted))
            # Show the month change in its true calendar cell, not in a separate row.
            if day.day == 1 or day == first:
                center((x,y+23), f'{day.month}월', font(17, active), accent if active else muted)
            center((x,y+57), str(day.day), font(43 if active else 37, active), color)
    return im


def render_calendar_png(start: date) -> BytesIO:
    """PNG stream ready to pass to discord.File; no temporary files required."""
    output = BytesIO()
    with render_calendar(start) as image:
        image.save(output, format='PNG')
    output.seek(0)
    return output


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Preview a Thursday–Wednesday recruitment calendar.')
    parser.add_argument('start', type=date.fromisoformat, help='Thursday (YYYY-MM-DD)')
    parser.add_argument('output', type=Path, help='Output PNG path')
    args = parser.parse_args()
    args.output.write_bytes(render_calendar_png(args.start).getvalue())
