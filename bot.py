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
                           is_starter_message, recruitment_week_start, due_openings, opening_for_week, local_dt,
                           starter_content, title, legacy_thread_title, week_days)
from core.storage import Storage, merge_rosters
from core.calendar_image import render_calendar_png

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
ROLE_MENTIONS_ENABLED = os.getenv('ROLE_MENTIONS_ENABLED', 'false').strip().lower() == 'true'
AUTO_CREATE = os.getenv('AUTO_CREATE_ENABLED', 'false').lower() == 'true'
AUTO_CLOSE = os.getenv('AUTO_CLOSE_ENABLED', 'false').lower() == 'true'
CLOSE_DAYS_BEFORE = env_int('CLOSE_DAYS_BEFORE', 1)
CLOSE_TIME = os.getenv('CLOSE_TIME', '23:00')
DB_PATH = ROOT / os.getenv('DATABASE_PATH', 'data/recruitments.sqlite3')

if not all((GUILD_ID, GLENN_TEXT_CHANNEL_ID, FOURF_TEXT_CHANNEL_ID)):
    raise SystemExit('.env에 GUILD_ID와 두 던전의 채널 ID를 입력해 주세요.')
if ROLE_MENTIONS_ENABLED and not all((GLENN_PARTICIPANT_ROLE_ID, FOURF_PARTICIPANT_ROLE_ID)):
    raise SystemExit('역할 멘션을 사용하려면 .env에 두 던전의 참여자 역할 ID를 입력해 주세요.')

# Discord-side visibility: disabled for ordinary members by default.
# Grant ORGANIZER_ROLE_ID access to /파티 in Server Settings > Integrations.
# The bot token cannot configure custom-role command permission overrides.
party = app_commands.Group(
    name='파티', description='주간 파티 모집을 관리합니다.',
    default_permissions=discord.Permissions.none(),
)
REOPEN_NOTE = '\n\n🔓 재모집 중 — 기존 신청자는 다시 누르지 않아도 됩니다.'


def configured_deadline(start: date, raid_type: str) -> datetime | None:
    # The two opening schedules do not depend on whether automatic closing is enabled.
    if not AUTO_CLOSE:
        return None
    deadline = local_dt(start - timedelta(days=CLOSE_DAYS_BEFORE), CLOSE_TIME)
    if deadline <= opening_for_week(start, raid_type):
        raise ValueError('자동 마감 시각은 모집 시작 시각보다 늦어야 합니다.')
    return deadline


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
        log.info('Participant role mentions enabled: %s', ROLE_MENTIONS_ENABLED)

    async def get_emojis(self):
        emoji_by_name = {e.name: e for e in await self.fetch_application_emojis()}
        missing = [day for day in ENGLISH_KEYS
                   if f'party_{day}' not in emoji_by_name and f'raid_{day}' not in emoji_by_name]
        if missing:
            raise RuntimeError('요일 이모지가 없습니다: ' + ', '.join(missing) +
                               '. python -m scripts.upload_emojis 를 실행해 주세요.')
        return [emoji_by_name.get(f'party_{day}') or emoji_by_name[f'raid_{day}']
                for day in ENGLISH_KEYS]

    async def find_unrecorded_thread(self, parent, start, raid_type, *, archive_limit=100):
        '''Recover a thread created before the bot crashed while saving its ID.

        Never adopt a thread owned by a person or another application.
        '''
        expected = title(start, raid_type)
        previous_title = legacy_thread_title(start, raid_type)
        reservation = self.store.creation_reservation(start, raid_type)
        if reservation is None:
            raise RuntimeError('생성 예약을 찾을 수 없습니다. 운영진 확인이 필요합니다.')
        reserved_at = datetime.fromisoformat(reservation['created_at'])
        # Discord snowflake timestamps have millisecond precision. Never adopt
        # an older same-named thread, including one from a previous year.
        reserved_at = reserved_at.replace(microsecond=reserved_at.microsecond // 1000 * 1000)
        other_type = '4f' if raid_type == 'glenn' else 'glenn'
        other_job = self.store.creation_reservation(start, other_type)
        ambiguous_parent = (other_job is not None and not other_job['thread_id']
                            and int(other_job['parent_channel_id']) == parent.id)
        candidates = []

        def consider(thread):
            if (thread.parent_id != parent.id or thread.owner_id != self.user.id
                    or thread.name not in (expected, previous_title)
                    or thread.created_at is None or thread.created_at < reserved_at
                    or self.store.thread_is_recorded(thread.id)
                    or any(x.id == thread.id for x in candidates)):
                return
            if thread.name == expected and ambiguous_parent:
                raise RuntimeError('같은 채널에 두 던전의 미완료 생성 예약이 있습니다. 운영진 확인이 필요합니다.')
            candidates.append(thread)

        for thread in await parent.guild.active_threads():
            consider(thread)
        async for thread in parent.archived_threads(limit=archive_limit):
            consider(thread)
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

    async def create_recruitment(self, start: date, raid_type: str, *, allow_deleted_replacement=False):
        if raid_type not in RAID_TYPES:
            raise ValueError(f'지원하지 않는 던전입니다: {raid_type}')
        async with self.op_lock:
            if not allow_deleted_replacement and self.store.is_cancelled(start, raid_type):
                return None, False
            existing = self.store.by_week(start, raid_type)
            missing_thread_id = None
            if existing:
                # Fetch the thread itself, separately from its starter message.
                # A deleted message or API permission/network error is NOT proof
                # that the recruitment thread was deleted.
                try:
                    channel = await self.fetch_channel(int(existing['thread_id']))
                except discord.NotFound as exc:
                    if exc.code != 10003:  # Unknown Channel
                        raise
                    if not allow_deleted_replacement:
                        raise RuntimeError('기존 모집 스레드가 없습니다. 운영진이 직접 /파티 생성으로 확인해 주세요.') from exc
                    if (existing['status'] not in ('open', 'review_needed')
                            or existing['closed_at'] is not None or existing['reopened_with_snapshot']):
                        raise RuntimeError('기존 모집 스레드가 삭제되었지만 마감 또는 재개 기록이 있어 보존합니다. 운영진 확인이 필요합니다.') from exc
                    missing_thread_id = existing['thread_id']
                else:
                    if existing['status'] == 'open':
                        try:
                            message = await channel.fetch_message(int(existing['message_id']))
                        except discord.NotFound as exc:
                            raise RuntimeError('기존 모집 스레드는 있지만 모집 메시지를 찾을 수 없습니다. 운영진 확인이 필요합니다.') from exc
                        for emoji in await self.get_emojis():
                            if not any(getattr(r.emoji, 'id', None) == emoji.id for r in message.reactions):
                                await message.add_reaction(emoji)
                    return existing, False

            emojis = await self.get_emojis()  # fail BEFORE trying to create
            deadline = configured_deadline(start, raid_type)
            default_parent_id = GLENN_TEXT_CHANNEL_ID if raid_type == 'glenn' else FOURF_TEXT_CHANNEL_ID
            job = self.store.creation_reservation(start, raid_type)
            parent_id = int(job['parent_channel_id']) if job else default_parent_id
            parent = await self.fetch_channel(parent_id)
            if not isinstance(parent, discord.TextChannel):
                raise RuntimeError('설정한 채널은 일반 텍스트 채널이어야 합니다.')
            mention_role = self.ping_target_for(parent, raid_type) if ROLE_MENTIONS_ENABLED else None
            me = parent.guild.me
            if me is not None and not parent.permissions_for(me).attach_files:
                raise RuntimeError('모집 채널에서 봇에게 파일 첨부 권한을 부여해 주세요.')
            # Generate before reserving or creating a new thread. A missing font
            # or rendering failure cannot leave an empty recruitment behind.
            calendar_png = await asyncio.to_thread(render_calendar_png, start)

            claimed = False
            if missing_thread_id is not None:
                # Preflight (emojis, parent channel, role permissions) succeeded.
                # Atomically remove the stale record and claim the replacement.
                claimed = self.store.reserve_deleted_thread_replacement(
                    start, raid_type, missing_thread_id, parent.id, datetime.now(KST))
                if not claimed:
                    raise RuntimeError('기존 모집 기록 또는 생성 예약이 변경되었거나 저장된 명단이 있습니다. 기존 기록을 보존합니다.')
                log.info('Replacing deleted recruitment thread %s for %s %s',
                         missing_thread_id, raid_type, start)
                job = self.store.creation_reservation(start, raid_type)
            elif job is None:
                claimed = self.store.reserve_creation(
                    start, raid_type, parent.id, datetime.now(KST),
                    allow_cancelled=allow_deleted_replacement)
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
                expected_message = starter_content(
                    start, deadline, raid_type, role_id=mention_role.id if mention_role else None)
                previous = []
                async for candidate in thread.history(limit=100, oldest_first=True):
                    if (candidate.author.id == self.user.id
                            and is_starter_message(candidate.content, start, raid_type)):
                        previous.append(candidate)
                if len(previous) > 1:
                    raise RuntimeError('여러 개의 모집 게시물이 발견되었습니다. 직접 확인해 주세요.')
                # Testing mode suppresses every mention. Production allows only
                # the configured dungeon role, never everyone or other users.
                allow_this_role = (discord.AllowedMentions(
                    everyone=False, users=False, roles=[mention_role], replied_user=False)
                    if mention_role else discord.AllowedMentions.none())
                if previous:
                    # Reuse an already-sent starter after a crash; never send
                    # a second notification or a second recruitment message.
                    message = previous[0]
                else:
                    # The PNG and text share the same message ID. All signup
                    # reactions, reports and close/reopen operations stay there.
                    attachment = discord.File(
                        calendar_png, filename='party-calendar.png',
                        description=f'{start:%m/%d} ~ {start + timedelta(days=6):%m/%d} 파티 모집 달력')
                    message = await thread.send(
                        expected_message, file=attachment, allowed_mentions=allow_this_role)
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

    async def delete_party(self, start, raid_type, *, invoking_channel_id, expected_thread_id=None):
        """Delete a confirmed bot-managed thread before removing the DB records.

        Recovery from the configured parent is allowed for a missing thread or
        interrupted creation. Existing recorded threads must be deleted from
        inside the thread, to reduce the risk of selecting the wrong party.
        """
        async with self.op_lock:
            rec = self.store.by_week(start, raid_type)
            job = self.store.creation_reservation(start, raid_type)
            if rec is None and job is None:
                return False
            if expected_thread_id is not None and (
                    rec is None or rec['thread_id'] != str(expected_thread_id)):
                raise RuntimeError('모집 정보가 변경되었습니다. 다시 확인해 주세요.')
            if rec is not None and job is not None and (
                    job['thread_id'] != rec['thread_id']
                    or job['message_id'] != rec['message_id']):
                raise RuntimeError('모집 기록과 생성 예약이 일치하지 않습니다. 운영진 확인이 필요합니다.')

            thread_id = rec['thread_id'] if rec is not None else job['thread_id']
            parent_id = (int(job['parent_channel_id']) if job is not None else
                         (GLENN_TEXT_CHANNEL_ID if raid_type == 'glenn' else FOURF_TEXT_CHANNEL_ID))
            if expected_thread_id is None and invoking_channel_id != parent_id:
                raise RuntimeError('해당 던전의 모집 채널에서 사용해 주세요.')
            thread = None
            if thread_id is not None:
                try:
                    thread = await self.fetch_channel(int(thread_id))
                except discord.NotFound as exc:
                    if exc.code != 10003:  # Only a confirmed missing channel is safe to clear.
                        raise
            else:
                # A failed Discord create might have succeeded before its ID
                # could be saved. Inspect active and all visible public archived
                # threads before releasing this reservation.
                parent = await self.fetch_channel(parent_id)
                if not isinstance(parent, discord.TextChannel) or parent.guild.id != GUILD_ID:
                    raise RuntimeError('기존 모집 채널을 확인할 수 없습니다.')
                candidate = await self.find_unrecorded_thread(parent, start, raid_type, archive_limit=None)
                if candidate is not None:
                    # A title alone cannot prove the candidate is the intended
                    # thread (old years can have matching month/day titles).
                    # Do not destroy anything based on this uncertain match.
                    raise RuntimeError(
                        f'일치하는 스레드(<#{candidate.id}>)가 발견되었습니다. '
                        '스레드와 생성 기록을 확인한 후 다시 시도해 주세요.')

            if thread is not None:
                if (not isinstance(thread, discord.Thread) or thread.guild.id != GUILD_ID
                        or thread.owner_id != self.user.id):
                    raise RuntimeError('봇이 생성한 모집 스레드인지 확인할 수 없어 삭제하지 않습니다.')
                if expected_thread_id is None and thread.parent_id != parent_id:
                    raise RuntimeError('모집 스레드의 상위 채널이 일치하지 않습니다.')
                if rec is not None and expected_thread_id is None:
                    raise RuntimeError(f'모집 스레드가 아직 존재합니다. <#{thread.id}> 안에서 /파티 삭제를 사용해 주세요.')
                try:
                    await thread.delete(reason=f'Organizer confirmed party deletion: {raid_type} {start}')
                except discord.NotFound as exc:
                    if exc.code != 10003:
                        raise

            # Never remove the DB record if Discord deletion/verification failed.
            # The DB transaction also clears saved rosters and creation claims.
            deleted = self.store.delete_recruitment(
                start, raid_type, datetime.now(KST),
                expected_record=rec, expected_reservation=job)
            if deleted:
                log.info('Organizer deleted recruitment %s %s (thread %s)', raid_type, start, thread_id)
            return deleted

    async def resolve_roster_names(self, guild, roster):
        """Resolve current server nicknames once per user, preserving IDs and signups."""
        names = {}
        result = {i: [] for i in range(7)}
        for i in range(7):
            for uid, fallback in roster.get(i, []):
                uid = str(uid)
                if uid not in names:
                    member = guild.get_member(int(uid))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(uid))
                        except discord.HTTPException:
                            # A departed member or failed lookup must not remove a signup.
                            member = None
                    names[uid] = member.display_name if member is not None else None
                result[i].append((uid, names[uid] if names[uid] is not None else fallback))
            result[i].sort(key=lambda entry: (entry[1].casefold(), entry[0]))
        return result

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
                    roster[index].append((str(user.id), user.display_name))
        if rec['reopened_with_snapshot']:
            roster = merge_rosters(self.store.final_roster(rec['thread_id']), roster)
        return await self.resolve_roster_names(channel.guild, roster)

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
            if rec is None:
                raise ValueError('이미 삭제된 모집입니다.')
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

        Manual-only recruitment reopens without a deadline. When automatic
        closing is enabled, an expired deadline needs an explicit extension.
        This is safe to retry if a Discord edit fails.
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
            deadline = (datetime.fromisoformat(rec['deadline_at'])
                        if AUTO_CLOSE and rec['deadline_at'] else None)
            if extra_minutes is not None:
                if not AUTO_CLOSE:
                    raise ValueError('자동 마감이 꺼져 있습니다. 연장시간 없이 /파티 재개를 사용해 주세요.')
                if not 1 <= extra_minutes <= 10080:
                    raise ValueError('연장 시간은 1~10080분 사이로 입력해 주세요.')
                extension = now + timedelta(minutes=extra_minutes)
                deadline = max(deadline, extension) if deadline else extension
            if deadline is not None and deadline <= now:
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
                if extra_minutes is not None or not AUTO_CLOSE:
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
            for start, raid_type in due_openings(now):
                try:
                    deadline = configured_deadline(start, raid_type)
                    if deadline is not None and now >= deadline:
                        continue
                    if (self.store.by_week(start, raid_type) is None
                            and not self.store.is_cancelled(start, raid_type)):
                        rec, created = await self.create_recruitment(start, raid_type)
                        if created:
                            log.info('Automatically created %s recruitment: thread %s',
                                     raid_type, rec['thread_id'])
                except Exception:
                    log.exception('Automatic creation failed for %s', raid_type)
        if AUTO_CLOSE:
            for rec in self.store.open_recruitments():
                if not rec['deadline_at']:
                    continue  # recruitment created without an automatic deadline
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
    if not await require_organizer(interaction):
        return
    mention_status = '켜짐' if ROLE_MENTIONS_ENABLED else '꺼짐'
    await interaction.response.send_message(
        f'✅ 파티 모집 봇이 정상 작동 중입니다.\n참여자 역할 멘션: {mention_status}', ephemeral=True)


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
    start = recruitment_week_start(datetime.now(KST))
    try:
        rec, created = await bot.create_recruitment(start, dungeon, allow_deleted_replacement=True)
        verb = '새 모집 스레드를 생성했습니다.' if created else '이미 모집 스레드가 있습니다. 기존 스레드를 안내합니다.'
        await interaction.followup.send(
            f'**{raid_label(dungeon)}** — {verb}\n'
            f'https://discord.com/channels/{GUILD_ID}/{rec["thread_id"]}',
            ephemeral=True)
    except Exception as exc:
        log.exception('Manual create failed for %s', dungeon)
        await interaction.followup.send(
            f'**{raid_label(dungeon)}** 파티 모집을 생성하지 못했습니다: {exc}', ephemeral=True)


@party.command(name='삭제', description='모집 스레드와 해당 주의 신청 기록을 삭제합니다.')
@app_commands.rename(confirmed='확인', dungeon='던전', week='시작일')
@app_commands.describe(confirmed='정말 삭제하려면 참(True)을 선택해 주세요.',
                       dungeon='스레드가 이미 삭제된 경우 모집 채널에서 선택해 주세요.',
                       week='모집 주간의 목요일 날짜(YYYY-MM-DD). 생략하면 다음 모집 주간입니다.')
@app_commands.choices(dungeon=[
    app_commands.Choice(name='글렌 베르나', value='glenn'),
    app_commands.Choice(name='4관', value='4f'),
])
async def delete(interaction: discord.Interaction, confirmed: bool = False,
                 dungeon: str | None = None, week: str | None = None):
    if not await require_organizer(interaction):
        return
    rec = bot.store.by_thread(interaction.channel_id)
    if rec is not None:
        if dungeon is not None or week is not None:
            await interaction.response.send_message(
                '모집 스레드 안에서는 던전과 시작일을 입력하지 않아도 됩니다.', ephemeral=True)
            return
        start, raid_type = date.fromisoformat(rec['week_start']), rec['raid_type']
        expected_thread_id = rec['thread_id']
    else:
        if dungeon not in RAID_TYPES or interaction.channel_id not in (GLENN_TEXT_CHANNEL_ID, FOURF_TEXT_CHANNEL_ID):
            await interaction.response.send_message(
                '모집 스레드 안에서 사용하거나, 스레드가 이미 삭제되었다면 해당 모집 채널에서 던전을 선택해 주세요.',
                ephemeral=True)
            return
        try:
            start = date.fromisoformat(week) if week else recruitment_week_start(datetime.now(KST))
            if start.weekday() != 3:
                raise ValueError('Not Thursday')
        except ValueError:
            await interaction.response.send_message('시작일은 목요일 날짜(YYYY-MM-DD)로 입력해 주세요.', ephemeral=True)
            return
        raid_type = dungeon
        expected_thread_id = None
        job = bot.store.creation_reservation(start, raid_type)
        parent_id = int(job['parent_channel_id']) if job else (
            GLENN_TEXT_CHANNEL_ID if raid_type == 'glenn' else FOURF_TEXT_CHANNEL_ID)
        if interaction.channel_id != parent_id:
            await interaction.response.send_message('해당 던전의 모집 채널에서 사용해 주세요.', ephemeral=True)
            return
        if bot.store.by_week(start, raid_type) is None and job is None:
            await interaction.response.send_message('해당 주간의 모집 기록이나 생성 예약이 없습니다.', ephemeral=True)
            return

    label = f'{raid_label(raid_type)} | {start:%Y/%m/%d} ~ {(start + timedelta(days=6)):%m/%d}'
    if not confirmed:
        await interaction.response.send_message(
            f'**{label}**\n⚠️ 모집 스레드(있는 경우), 모든 신청 명단 및 해당 생성 기록을 영구 삭제합니다. '
            '복구할 수 없으며 자동 재생성되지 않습니다.\n'
            '삭제하려면 같은 명령어에서 확인을 참(True)으로 선택해 주세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        removed = await bot.delete_party(start, raid_type, invoking_channel_id=interaction.channel_id,
                                         expected_thread_id=expected_thread_id)
    except Exception as exc:
        log.exception('Party deletion failed for %s %s', raid_type, start)
        await interaction.followup.send(f'삭제 실패: {exc}', ephemeral=True)
        return
    try:
        await interaction.followup.send(
            f'**{label}** — 모집을 삭제했습니다. /파티 생성으로 다시 만들 수 있습니다.'
            if removed else '이미 삭제되었거나 해당 모집 기록이 없습니다.', ephemeral=True)
    except discord.HTTPException:
        # The interaction's channel itself may now be gone. Deletion has already
        # committed; do not report a false failure or try to recreate anything.
        log.info('Deletion result could not be sent after thread deletion: %s', label)


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
        if rec['status'] == 'closed':
            roster = await bot.resolve_roster_names(
                interaction.guild, bot.store.final_roster(rec['thread_id']))
        else:
            roster = await bot.live_roster(rec)
        await interaction.followup.send(embed=bot.report_embed(rec, roster), ephemeral=True)
    except Exception as exc:
        log.exception('Report failed')
        await interaction.followup.send(f'명단 조회 실패: {exc}', ephemeral=True)


@party.command(name='재개', description='현재 스레드의 모집을 다시 열고 기존 신청자를 유지합니다.')
@app_commands.rename(extra_minutes='연장시간')
@app_commands.describe(extra_minutes='자동 마감 사용 시에만 필요한 연장 시간(분). 선택 사항')
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
        roster = await bot.resolve_roster_names(
            interaction.guild, bot.store.final_roster(rec['thread_id']))
        await interaction.followup.send(embed=bot.report_embed(rec, roster), ephemeral=True)
    except Exception as exc:
        log.exception('Close failed')
        await interaction.followup.send(f'마감 처리 실패: {exc}. 재시도할 수 있습니다.', ephemeral=True)


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token or token.startswith('PASTE_'):
        raise SystemExit('.env에 DISCORD_TOKEN을 입력해 주세요.')
    bot.run(token)
