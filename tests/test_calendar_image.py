import ast
import unittest
from datetime import date, timedelta
from pathlib import Path

from PIL import Image

from core.calendar_image import (calendar_rows, font_paths, render_calendar_png,
                                 korean_public_holidays, date_color)


class PartialCalendarTests(unittest.TestCase):
    def test_sunday_to_saturday_month_boundary(self):
        rows = calendar_rows(date(2026, 11, 26))
        self.assertEqual(rows[0], [date(2026, 11, d) for d in range(22, 29)])
        self.assertEqual(rows[1], [date(2026, 11, 29), date(2026, 11, 30)]
                         + [date(2026, 12, d) for d in range(1, 6)])

    def test_year_boundary_and_leap_day(self):
        for start in (date(2026, 12, 31), date(2024, 2, 29), date(2026, 12, 3)):
            with self.subTest(start=start):
                rows = calendar_rows(start)
                self.assertEqual(len(rows), 2)
                self.assertEqual([len(row) for row in rows], [7, 7])
                self.assertEqual([row[0].weekday() for row in rows], [6, 6])
                self.assertEqual(rows[0][4], start)
                self.assertEqual(rows[1][3], start + timedelta(days=6))
                dates = rows[0] + rows[1]
                self.assertEqual(dates, [dates[0] + timedelta(days=i) for i in range(14)])

    def test_public_holidays_and_year_boundary(self):
        # Christmas, New Year, and the substitute day for Liberation Day 2026.
        for start, holiday in ((date(2026, 12, 24), date(2026, 12, 25)),
                               (date(2026, 12, 31), date(2027, 1, 1)),
                               (date(2026, 8, 13), date(2026, 8, 17))):
            with self.subTest(holiday=holiday):
                public = korean_public_holidays(calendar_rows(start))
                self.assertIn(holiday, public)
                self.assertEqual(date_color(holiday, True, public), '#f2a0a9')
                self.assertEqual(date_color(holiday, False, public), '#a26b76')

    def test_public_holiday_color_overrides_saturday(self):
        saturday = date(2026, 8, 15)
        public = korean_public_holidays(calendar_rows(date(2026, 8, 13)))
        self.assertIn(saturday, public)
        self.assertEqual(date_color(saturday, True, public), '#f2a0a9')
        self.assertEqual(date_color(date(2026, 8, 22), True, public), '#8abcf5')
        self.assertEqual(date_color(date(2026, 8, 16), True, public), '#f2a0a9')
        self.assertEqual(date_color(date(2026, 8, 18), True, public), '#edf1f8')

    def test_reject_non_thursday(self):
        with self.assertRaises(ValueError):
            calendar_rows(date(2026, 11, 25))

    def test_png_and_exact_highlight_cells(self):
        try:
            font_paths()
        except RuntimeError as exc:
            self.skipTest(str(exc))  # Some CI images lack installed Korean fonts.
        for start in (date(2026, 11, 26), date(2026, 12, 31), date(2024, 2, 29)):
            with self.subTest(start=start):
                with render_calendar_png(start) as png:
                    self.assertEqual(png.tell(), 0)
                    with Image.open(png) as image:
                        self.assertEqual(image.format, 'PNG')
                        self.assertEqual(image.size, (960, 379))
                        for r, row in enumerate(calendar_rows(start)):
                            for c, day in enumerate(row):
                                # A point inside the cell away from its label/grid lines.
                                actual = image.getpixel((25 + c * 130 + 15, 152 + r * 100 + 40))
                                expected = ((58, 51, 85) if start <= day <= start + timedelta(days=6)
                                            else (32, 37, 48))
                                self.assertEqual(actual, expected, day)

    def test_attachment_is_part_of_original_signup_message(self):
        # Existing project uses static UI checks; no Discord token required.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
        create = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == 'create_recruitment')
        sends = [n for n in ast.walk(create) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'send'
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'thread']
        self.assertEqual(len(sends), 1)
        self.assertIn('file', {k.arg for k in sends[0].keywords})
        self.assertIn('allowed_mentions', {k.arg for k in sends[0].keywords})
        self.assertEqual(sends[0].args[0].id, 'expected_message')


if __name__ == '__main__':
    unittest.main()
