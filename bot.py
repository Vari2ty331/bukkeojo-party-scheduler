"""Guild party recruitment bot: test manually, then enable timers.

Requires discord.py >= 2.6.2. Creates public threads in an ordinary Discord text channel.
"""
import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from core.calendar import (ENGLISH_KEYS, KOREAN_WEEKDAYS, KST, RAID_TYPES, raid_label,
                           is_starter_message, next_thursday, schedule_for_week,
                           starter_content, title, week_days)
from core.storage import Storage, merge_rosters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('party-bot')


def env_int(name, default=0):
    return int(os.getenv(name, str(default)))


GUILD_ID = env_int('GUILD_ID')
GLENN_TEXT_CHANNEL_ID = env_int('GLENN_TEXT_CHANNEL_ID')
FOURF_TEXT_CHANNEL_ID = env_int('FOURF_TEXT_CHANNEL_ID')
GLENN_PARTICIPANT_ROLE_ID = env_int('GLENN_PARTICIPANT_ROLE_ID')
FOURF_PARTICIPANT_ROLE_ID = env_int('FOURF_PARTICIPANT_ROLE_ID')
ORGANIZER_ROLE_ID = env_int('ORGANIZER_ROLE_ID')
AUTO_CREATE = os.getenv('AUTO_CREATE_ENABLED', 'false').lower() == 'true'
AUTO_CLOSE = os.getenv('AUTO_CLOSE_ENABLED', 'false').lower() == 'true'
OPEN_DAYS_BEFORE = env_int('OPEN_DAYS_BEFORE', 1)
CLOSE_DAYS_BEFORE = env_int('CLOSE_DAYS_BEFORE', 1)
OPEN_TIME = os.getenv('OPEN_TIME', '18:00')
CLOSE_TIME = os.getenv('CLOSE_TIME', '23:00')
DB_PATH = ROOT / os.getenv('DATABASE_PATH', 'data/recruitments.sqlite3')

if not all((GUILD_ID, GLENN_TEXT_CHANNEL_ID, FOURF_TEXT_CHANNEL_ID,
            GLENN_PARTICIPANT_ROLE_ID, FOURF_PARTICIPANT_ROLE_ID)):
    raise SystemExit('.env에 GUILD_ID, 두 던전의 채널 ID와 참여자 역할 ID를 입력해 주세요.')

party = app_commands.Group(name='파티', description='주간 파티 모집을 관리합니다.')
REOPEN_NOTE = '\n\n🔓 재모집 중 — 기존 신청자는 다시 누르지 않아도 됩니다.'


def participant_role_id(raid_type: str) -> int:
    if raid_type == 'glenn':
        return GLENN_PARTICIPANT_ROLE_ID
    if raid_type == '4f':
        return FOURF_PARTICIPANT_ROLE_ID
    raise ValueError(f'지원하지 않는 던전입니다: {raid_type}')


def existing_message_role_id(content: str) -> int | None:
    # Used only when editing a previously published starter (e.g. reopen).
    # An edit must never send a fresh role notification.
    match = re.search(r'<@&(\d+)>', content)
    return int(match.group(1)) if match else None


def organizer(interaction: discord.Interaction) -> bool:
    if not interaction.guild or interaction.guild.id != GUILD_ID:
        return False
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return bool(ORGANIZER_ROLE_ID and any(r.id == ORGANIZER_ROLE_ID for r in member.roles))


async def require_organizer(interaction: discord.Interaction) -> bool:
    if organizer(interaction):
        return True
    await interaction.response.send_message('이 명령어는 운영진만 사용할 수 있습니다.', ephemeral=True)
    return False


class RaidBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned,
                         intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.store = Storage(DB_PATH)
        self.op_lock = asyncio.Lock()

    async def setup_hook(self):
        self.tree.add_command(party, guild=discord.Object(id=GUILD_ID))
        synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
        log.info('Synced %s guild command(s)', len(synced))
        self.check_deadlines.start()

    async def on_ready(self):
        log.info('Ready as %s (%s). Glenn channel: %s; 4F channel: %s',
                 self.user, self.user.id, GLENN_TEXT_CHANNEL_ID, FOURF_TEXT_CHANNEL_ID)

    async def get_emojis(self):
        emoji_by_name = {e.name: e for e in await self.fetch_application_emojis()}
        missing = [day for day in ENGLISH_KEYS
                   if f'party_{day}' not in emoji_by_name and f'raid_{day}' not in emoji_by_name]
        if missing:
            raise RuntimeError('요일 이모지가 없습니다: ' + ', '.join(missing) +
                               '. python -m scripts.upload_emojis 를 실행해 주세요.')
        return [emoji_by_name.get(f'party_{day}') or emoji_by_name[f'raid_{day}']
                for day in ENGLISH_KEYS]

    async def find_unrecorded_thread(self, parent, start, raid_type):
        '''Recover a thread created before the bot crashed while saving its ID.

        Never adopt a thread owned by a person or another application.
        '''
        expected = title(start, raid_type)
        candidates = []
        for thread in await parent.guild.active_threads():
            if (thread.parent_id == parent.id and thread.name == expected
                    and thread.owner_id == self.user.id):
                candidates.append(thread)
        async for thread in parent.archived_threads(limit=100):
            if (thread.name == expected and thread.owner_id == self.user.id
                    and thread.id not in {x.id for x in candidates}):
                candidates.append(thread)
        if len(candidates) > 1:
            raise RuntimeError('여러 개의 동일한 봇 스레드가 발견되었습니다. 직접 확인해 주세요.')
        return candidates[0] if candidates else None

    def ping_target_for(self, parent: discord.TextChannel, raid_type: str) -> discord.Role:
        """Check that the configured role can really be mentioned before creating a thread."""
        role_id = participant_role_id(raid_type)
        role = parent.guild.get_role(role_id)
        if role is None:
            raise RuntimeError(f'{raid_label(raid_type)} 참여자 역할(ID: {role_id})을 서버에서 찾지 못했습니다.')
        me = parent.guild.me
        # Discord silently displays, but does not notify, an unmentionable role
        # unless the sending bot has Mention Everyone in the target channel.
        can_mention_unmentionable = bool(me and parent.permissions_for(me).mention_everyone)
        if not role.mentionable and not can_mention_unmentionable:
            raise RuntimeError(
                f'{raid_label(raid_type)} 참여자 역할이 멘션 가능하지 않습니다. '
                '역할을 멘션 가능으로 변경하거나, 봇에게 해당 채널의 '
                '모든 역할 멘션 권한을 부여해 주세요.')
        return role

    async def create_recruitment(self, start: date, raid_type: str):
        if raid_type not in RAID_TYPES:
            raise ValueError(f'지원하지 않는 던전입니다: {raid_type}')
        async with self.op_lock:
            existing = self.store.by_week(start, raid_type)
            if existing:
                # Repeat create requests repair interrupted reaction seeding,
                # but never create a second thread, including after closing.
                if existing['status'] == 'open':
                    try:
                        channel = await self.fetch_channel(int(existing['thread_id']))
                        message = await channel.fetch_message(int(existing['message_id']))
                        for emoji in await self.get_emojis():
                            if not any(getattr(r.emoji, 'id', None) == emoji.id for r in message.reactions):
                                await message.add_reaction(emoji)
                    except discord.HTTPException:
                        log.exception('Unable to repair reactions for %s', start)
                return existing, False

            emojis = await self.get_emojis()  # fail BEFORE trying to create
            opening, deadline = schedule_for_week(
                start, OPEN_DAYS_BEFORE, OPEN_TIME, CLOSE_DAYS_BEFORE, CLOSE_TIME)
            default_parent_id = GLENN_TEXT_CHANNEL_ID if raid_type == 'glenn' else FOURF_TEXT_CHANNEL_ID
            job = self.store.creation_reservation(start, raid_type)
            parent_id = int(job['parent_channel_id']) if job else default_parent_id
            parent = await self.fetch_channel(parent_id)
            if not isinstance(parent, discord.TextChannel):
                raise RuntimeError('설정한 채널은 일반 텍스트 채널이어야 합니다.')
            mention_role = self.ping_target_for(parent, raid_type)

            claimed = False
            if job is None:
                claimed = self.store.reserve_creation(start, raid_type, parent.id, datetime.now(KST))
                job = self.store.creation_reservation(start, raid_type)
            if not job:
                # A second process may have committed its record meanwhile.
                existing = self.store.by_week(start, raid_type)
                if existing:
                    return existing, False
                raise RuntimeError('다른 봇 프로세스에서 이 주의 모집을 생성하고 있습니다.')

            if job['thread_id']:
                thread = await self.fetch_channel(int(job['thread_id']))
            else:
                if not claimed:
                    # Discord might have successfully created the thread and
                    # then the process crashed before recording its ID.
                    thread = await self.find_unrecorded_thread(parent, start, raid_type)
                    if thread is None:
                        raise RuntimeError(
                            '이전 스레드 생성이 중단되었습니다. 중복 생성을 방지하기 위해 '
                            '새 스레드를 만들지 않습니다. 운영자가 채널과 로그를 확인해 주세요.')
                    self.store.record_created_thread(start, raid_type, thread.id)
                else:
                    try:
                        thread = await parent.create_thread(
                            name=title(start, raid_type), type=discord.ChannelType.public_thread)
                    except (discord.Forbidden, discord.NotFound):
                        # These permission/not-found failures did not create a thread.
                        self.store.release_creation_reservation(start, raid_type)
                        raise
                    # Persist the ID before attempting to send the starter.
                    self.store.record_created_thread(start, raid_type, thread.id)
                job = self.store.creation_reservation(start, raid_type)

            if job['message_id']:
                message = await thread.fetch_message(int(job['message_id']))
            else:
                # Recover from the narrow crash window after sending the starter.
                expected_message = starter_content(start, deadline, raid_type, role_id=mention_role.id)
                previous = []
                async for candidate in thread.history(limit=100, oldest_first=True):
                    if (candidate.author.id == self.user.id
                            and is_starter_message(candidate.content, start, raid_type)):
                        previous.append(candidate)
                if len(previous) > 1:
                    raise RuntimeError('여러 개의 모집 게시물이 발견되었습니다. 직접 확인해 주세요.')
                # Only allow this specific participant role to notify users.
                # bot.allowed_mentions is globally set to none; this is a one-message override.
                allow_this_role = discord.AllowedMentions(
                    everyone=False, users=False, roles=[mention_role], replied_user=False)
                message = (previous[0] if previous else
                           await thread.send(expected_message, allowed_mentions=allow_this_role))
                self.store.record_recruitment_message(start, raid_type, message.id)

            # The database identity is now durable; both the manual command and
            # timer check this same key before attempting another creation.
            ids = {str(i): str(e.id) for i, e in enumerate(emojis)}
            self.store.insert(thread.id, start, message.id, deadline, ids, raid_type)
            self.store.release_creation_reservation(start, raid_type)
            for emoji in emojis:
                if not any(getattr(r.emoji, 'id', None) == emoji.id for r in message.reactions):
                    await message.add_reaction(emoji)
            return self.store.by_thread(thread.id), True

    async def live_roster(self, rec):
        channel = await self.fetch_channel(int(rec['thread_id']))
        message = await channel.fetch_message(int(rec['message_id']))
        ids = json.loads(rec['emoji_ids_json'])
        index_by_id = {int(v): int(k) for k, v in ids.items()}
        roster = {i: [] for i in range(7)}
        for reaction in message.reactions:
            emoji_id = getattr(reaction.emoji, 'id', None)
            index = index_by_id.get(emoji_id)
            if index is None:
                continue
            async for user in reaction.users(limit=None):
                if not user.bot:
                    name = user.display_name
                    if isinstance(user, discord.Member):
                        name = user.display_name
                    roster[index].append((str(user.id), name))
        for users in roster.values():
            users.sort(key=lambda entry: entry[1].casefold())
        if rec['reopened_with_snapshot']:
            return merge_rosters(self.store.final_roster(rec['thread_id']), roster)
        return roster

    def report_embed(self, rec, roster):
        start = date.fromisoformat(rec['week_start'])
        status = '모집 마감' if rec['status'] == 'closed' else (
            '마감 지연 — 운영진 확인 필요' if rec['status'] == 'review_needed' else '모집 중')
        embed = discord.Embed(title=f'{raid_label(rec["raid_type"])} | {start:%m/%d} ~ {(start + timedelta(days=6)):%m/%d} 참여 명단',
                              description=status, color=discord.Color.blurple())
        for i, day in enumerate(week_days(start)):
            names = [name for _uid, name in roster.get(i, [])]
            value = ', '.join(names) if names else '신청 없음'
            # One field per day; Discord embed field max value length is 1024.
            embed.add_field(name=f'{day:%m/%d} ({KOREAN_WEEKDAYS[i]}) — {len(names)}명',
                            value=value[:1020], inline=False)
        embed.set_footer(text='요일별 신청 명단만 표시합니다. 개인 시간 제약은 포함하지 않습니다.')
        return embed

    async def finish(self, rec, *, recovered=False):
        async with self.op_lock:
            rec = self.store.by_thread(rec['thread_id'])
            if rec['status'] != 'closed':
                roster = await self.live_roster(rec)
                self.store.finalize(rec['thread_id'], roster, datetime.now(KST))
            rec = self.store.by_thread(rec['thread_id'])
            if not rec['reactions_cleared']:
                thread = await self.fetch_channel(int(rec['thread_id']))
                msg = await thread.fetch_message(int(rec['message_id']))
                await msg.clear_reactions()  # requires Manage Messages
                clean_body = msg.content.replace(REOPEN_NOTE, '')
                if clean_body.startswith('🔒 모집 마감\n'):
                    closed_body = clean_body
                else:
                    closed_body = '🔒 모집 마감\n' + clean_body
                if msg.content != closed_body:
                    await msg.edit(content=closed_body, allowed_mentions=discord.AllowedMentions.none())
                self.store.mark_reactions_cleared(rec['thread_id'])
            return self.store.by_thread(rec['thread_id'])

    async def reopen_recruitment(self, thread_id, *, extra_minutes=None):
        """Restore weekday reactions while preserving the closed signup snapshot.

        An early reopen uses the original deadline. If the deadline has passed,
        an organizer must provide an explicit extension to avoid an immediate
        automatic re-close. This is safe to retry if a Discord edit fails.
        """
        async with self.op_lock:
            rec = self.store.by_thread(thread_id)
            if rec is None:
                raise ValueError('이 모집 스레드를 데이터베이스에서 찾을 수 없습니다.')
            if rec['status'] == 'review_needed':
                raise ValueError('마감 누락 검토 상태입니다. 먼저 /파티 마감으로 명단을 확정해 주세요.')
            if rec['status'] not in ('open', 'closed'):
                raise ValueError('이 모집 상태에서는 다시 열 수 없습니다.')
            # Repeating /파티 재개 on an already reopened thread repairs
            # missing reactions/text, without resetting its deadline.
            if rec['status'] == 'open' and not rec['reopened_with_snapshot']:
                return rec, False
            now = datetime.now(KST)
            deadline = datetime.fromisoformat(rec['deadline_at'])
            if extra_minutes is not None:
                if not 1 <= extra_minutes <= 10080:
                    raise ValueError('연장 시간은 1~10080분 사이로 입력해 주세요.')
                deadline = max(deadline, now + timedelta(minutes=extra_minutes))
            if deadline <= now:
                raise ValueError('기존 마감 시간이 지났습니다. /파티 재개 연장시간:120처럼 연장 시간을 입력해 주세요.')

            channel = await self.fetch_channel(int(rec['thread_id']))
            message = await channel.fetch_message(int(rec['message_id']))
            emojis = await self.get_emojis()  # verify available before editing anything

            if rec['status'] == 'closed':
                # Remove any post-close reactions before reopening, so late
                # signups made while CLOSED cannot slip into the live roster.
                # Previously saved signups are safely retained in SQLite.
                await message.clear_reactions()
                self.store.mark_reactions_cleared(thread_id)
                for emoji in emojis:
                    await message.add_reaction(emoji)
                new_ids = {str(i): str(e.id) for i, e in enumerate(emojis)}
                self.store.reopen(thread_id, deadline, new_ids)
                just_reopened = True
            else:
                # Retry path for a partially restored reopen operation.
                for emoji in emojis:
                    if not any(getattr(r.emoji, 'id', None) == emoji.id for r in message.reactions):
                        await message.add_reaction(emoji)
                if extra_minutes is not None:
                    self.store.extend_reopened_deadline(thread_id, deadline)
                just_reopened = False

            # Replace only the bot-owned starter text. The thread itself stays
            # open and is not recreated. Also update any extended deadline.
            # Keep a role mention only if the original starter already contained it.
            # Editing/reopening must never re-notify the role or add it retroactively.
            original_role_id = existing_message_role_id(message.content)
            body = (starter_content(date.fromisoformat(rec['week_start']), deadline,
                                    rec['raid_type'], role_id=original_role_id) + REOPEN_NOTE)
            if message.content != body:
                await message.edit(content=body, allowed_mentions=discord.AllowedMentions.none())
            return self.store.by_thread(thread_id), just_reopened

    @tasks.loop(seconds=30)
    async def check_deadlines(self):
        now = datetime.now(KST)
        if AUTO_CREATE:
            start = next_thursday(now.date())
            try:
                opening, closing = schedule_for_week(start, OPEN_DAYS_BEFORE, OPEN_TIME,
                                                     CLOSE_DAYS_BEFORE, CLOSE_TIME)
                if opening <= now < closing:
                    for raid_type in RAID_TYPES:
                        try:
                            if self.store.by_week(start, raid_type) is None:
                                rec, created = await self.create_recruitment(start, raid_type)
                                if created:
                                    log.info('Automatically created %s recruitment: thread %s',
                                             raid_type, rec['thread_id'])
                        except Exception:
                            log.exception('Automatic creation failed for %s', raid_type)
            except Exception:
                log.exception('Automatic thread creation failed')
        if AUTO_CLOSE:
            for rec in self.store.open_recruitments():
                deadline = datetime.fromisoformat(rec['deadline_at'])
                if now < deadline:
                    continue
                # After an outage across the deadline, current reactions may contain late additions.
                if (now - deadline).total_seconds() > 120:
                    self.store.needs_review(rec['thread_id'])
                    log.warning('Deadline missed for %s; use /파티 마감 after reviewing.', rec['thread_id'])
                    continue
                try:
                    await self.finish(rec)
                    log.info('Automatically closed %s', rec['thread_id'])
                except Exception:
                    log.exception('Automatic close failed for %s', rec['thread_id'])

    @check_deadlines.before_loop
    async def before_checks(self):
        await self.wait_until_ready()


bot = RaidBot()


@party.command(name='상태', description='파티 모집 봇의 작동 상태를 확인합니다.')
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message('✅ 파티 모집 봇이 정상 작동 중입니다.', ephemeral=True)


@party.command(name='이모지', description='요일별 모집 이모지가 준비되었는지 확인합니다.')
async def emojis(interaction: discord.Interaction):
    if not await require_organizer(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        es = await bot.get_emojis()
        text = '✅ 요일 이모지 준비 완료: ' + ' '.join(str(e) for e in es)
    except Exception as exc:
        text = f'이모지 확인 실패: {exc}'
    await interaction.followup.send(text, ephemeral=True)


@party.command(name='생성', description='선택한 던전의 다음 주 파티 모집 스레드를 생성합니다.')
@app_commands.rename(dungeon='던전')
@app_commands.describe(dungeon='모집을 생성할 던전을 선택해 주세요.')
@app_commands.choices(dungeon=[
    app_commands.Choice(name='글렌 베르나', value='glenn'),
    app_commands.Choice(name='4관', value='4f'),
])
async def create(interaction: discord.Interaction, dungeon: str):
    if not await require_organizer(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    start = next_thursday(datetime.now(KST).date())
    try:
        rec, created = await bot.create_recruitment(start, dungeon)
        verb = '새 모집 스레드를 생성했습니다.' if created else '이미 모집 스레드가 있습니다. 기존 스레드를 안내합니다.'
        await interaction.followup.send(
            f'**{raid_label(dungeon)}** — {verb}\n'
            f'https://discord.com/channels/{GUILD_ID}/{rec["thread_id"]}',
            ephemeral=True)
    except Exception as exc:
        log.exception('Manual create failed for %s', dungeon)
        await interaction.followup.send(
            f'**{raid_label(dungeon)}** 파티 모집을 생성하지 못했습니다: {exc}', ephemeral=True)


@party.command(name='명단', description='현재 스레드의 요일별 신청자 명단을 보여 줍니다.')
async def report(interaction: discord.Interaction):
    if not await require_organizer(interaction):
        return
    rec = bot.store.by_thread(interaction.channel_id)
    if not rec:
        await interaction.response.send_message('해당 파티 모집 스레드 안에서 사용해 주세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        roster = bot.store.final_roster(rec['thread_id']) if rec['status'] == 'closed' else await bot.live_roster(rec)
        await interaction.followup.send(embed=bot.report_embed(rec, roster), ephemeral=True)
    except Exception as exc:
        log.exception('Report failed')
        await interaction.followup.send(f'명단 조회 실패: {exc}', ephemeral=True)


@party.command(name='재개', description='현재 스레드의 모집을 다시 열고 기존 신청자를 유지합니다.')
@app_commands.rename(extra_minutes='연장시간')
@app_commands.describe(extra_minutes='마감 시각이 지난 경우 현재부터 추가로 모집할 시간(분). 선택 사항')
async def reopen(interaction: discord.Interaction, extra_minutes: int | None = None):
    if not await require_organizer(interaction):
        return
    rec = bot.store.by_thread(interaction.channel_id)
    if not rec:
        await interaction.response.send_message('해당 파티 모집 스레드 안에서 사용해 주세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        rec, reopened = await bot.reopen_recruitment(rec['thread_id'], extra_minutes=extra_minutes)
        if reopened:
            msg = ('모집을 다시 열었습니다. **기존 신청자는 그대로 유지**되며, '
                   '새 이모지 반응으로 추가 신청을 받습니다.')
        elif rec['reopened_with_snapshot']:
            msg = '이미 다시 열린 모집입니다. 이모지와 안내문을 확인했습니다.'
        else:
            msg = '이미 모집 중입니다.'
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as exc:
        log.exception('Reopen failed')
        await interaction.followup.send(f'다시 열기 실패: {exc}', ephemeral=True)


@party.command(name='마감', description='현재 스레드의 신청 명단을 저장하고 반응을 제거합니다.')
async def close(interaction: discord.Interaction):
    if not await require_organizer(interaction):
        return
    rec = bot.store.by_thread(interaction.channel_id)
    if not rec:
        await interaction.response.send_message('해당 파티 모집 스레드 안에서 사용해 주세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        rec = await bot.finish(rec)
        roster = bot.store.final_roster(rec['thread_id'])
        await interaction.followup.send(embed=bot.report_embed(rec, roster), ephemeral=True)
    except Exception as exc:
        log.exception('Close failed')
        await interaction.followup.send(f'마감 처리 실패: {exc}. 재시도할 수 있습니다.', ephemeral=True)


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token or token.startswith('PASTE_'):
        raise SystemExit('.env에 DISCORD_TOKEN을 입력해 주세요.')
    bot.run(token)
