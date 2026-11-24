"""Discord 모듈을 설치하지 않은 CI 환경에서도 UI 명령어 선언을 검사합니다."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CommandAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / 'bot.py').read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.source)

    def test_main_command_is_korean_party(self):
        self.assertIn("name='파티'", self.source)
        self.assertNotIn("name='party'", self.source)
        self.assertNotIn("name='raid'", self.source)
        self.assertIn('self.tree.add_command(party', self.source)

    def test_korean_subcommands(self):
        expected = {'상태', '이모지', '생성', '명단', '마감', '재개'}
        actual = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == 'party' and dec.func.attr == 'command'):
                    names = [k.value.value for k in dec.keywords
                             if k.arg == 'name' and isinstance(k.value, ast.Constant)]
                    actual.update(names)
        self.assertEqual(actual, expected)

    def test_required_dungeon_is_korean_choice(self):
        self.assertIn("rename(dungeon='던전')", self.source)
        self.assertIn("Choice(name='글렌 베르나', value='glenn')", self.source)
        self.assertIn("Choice(name='4관', value='4f')", self.source)
        self.assertIn("rename(extra_minutes='연장시간')", self.source)
        self.assertNotIn('/party ', self.source)
        self.assertNotIn('/raid ', self.source)
        self.assertIn('/파티 마감', self.source)
        self.assertNotIn('/raid reopen extra_minutes', self.source)


if __name__ == '__main__':
    unittest.main()
