"""Discord 모듈을 설치하지 않은 CI 환경에서도 UI 명령어 선언을 검사합니다."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_role_mentions_default_to_off(self):
        self.assertIn("os.getenv('ROLE_MENTIONS_ENABLED', 'false')", self.source)
        self.assertIn('if ROLE_MENTIONS_ENABLED else None', self.source)
        self.assertIn('role_id=mention_role.id if mention_role else None', self.source)
        self.assertIn('if mention_role else discord.AllowedMentions.none()', self.source)
        self.assertIn('if ROLE_MENTIONS_ENABLED and not all', self.source)

    def test_korean_subcommands(self):
        expected = {'상태', '이모지', '생성', '명단', '마감', '재개', '삭제', '글렌설정'}
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

    def test_party_group_has_restrictive_discord_default_permissions(self):
        group = next(n.value for n in self.tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'party' for t in n.targets))
        self.assertEqual(ast.unparse(group.func), 'app_commands.Group')
        permissions = next(k.value for k in group.keywords if k.arg == 'default_permissions')
        self.assertEqual(ast.unparse(permissions), 'discord.Permissions.none()')

    def test_every_party_command_checks_organizer_before_work(self):
        checked = set()
        for node in self.tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if not any(isinstance(d, ast.Call) and ast.unparse(d.func) == 'party.command'
                       for d in node.decorator_list):
                continue
            first = node.body[0]
            self.assertIsInstance(first, ast.If, node.name)
            self.assertEqual(ast.unparse(first.test), 'not await require_organizer(interaction)', node.name)
            self.assertIsInstance(first.body[0], ast.Return, node.name)
            checked.add(node.name)
        self.assertEqual(checked, {'ping', 'emojis', 'create', 'delete', 'report', 'reopen', 'close', 'glenn_settings'})

    def test_organizer_role_and_administrator_access(self):
        # Exercise the existing authorization function without connecting to Discord.
        class Member:
            def __init__(self, roles, administrator=False):
                self.roles = [SimpleNamespace(id=role) for role in roles]
                self.guild_permissions = SimpleNamespace(administrator=administrator)

        fn = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'organizer')
        namespace = {'discord': SimpleNamespace(Member=Member, Interaction=object),
                     'GUILD_ID': 100, 'ORGANIZER_ROLE_ID': 200}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<organizer>', 'exec'), namespace)
        organizer = namespace['organizer']
        def interaction(member, guild_id=100):
            return SimpleNamespace(user=member, guild=SimpleNamespace(id=guild_id))
        self.assertTrue(organizer(interaction(Member([200]))))
        self.assertTrue(organizer(interaction(Member([], administrator=True))))
        self.assertFalse(organizer(interaction(Member([300]))))
        self.assertFalse(organizer(interaction(Member([200]), guild_id=999)))
        self.assertFalse(organizer(interaction(SimpleNamespace())))
        self.assertFalse(organizer(SimpleNamespace(user=Member([200]), guild=None)))

    def test_manual_create_and_default_delete_share_week_boundary(self):
        functions = {n.name: n for n in self.tree.body if isinstance(n, ast.AsyncFunctionDef)}
        for name in ('create', 'delete'):
            fn = functions[name]
            self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                and n.func.id == 'recruitment_week_start'
                                for n in ast.walk(fn)), name)

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
