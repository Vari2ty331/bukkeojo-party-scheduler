"""Date calculations and Korean message text for Thursday–Wednesday party weeks."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
KOREAN_WEEKDAYS = ("목", "금", "토", "일", "월", "화", "수")
ENGLISH_KEYS = ("thu", "fri", "sat", "sun", "mon", "tue", "wed")
RAID_LABELS = {
    "glenn": "글렌 베르나",
    "4f": "4관",
    "legacy": "기존 파티",  # Recruitment records made with the previous single-raid bot.
}
RAID_TYPES = ("glenn", "4f")
# Number of days before the Thursday raid week, and local opening time.
RECRUITMENT_OPENINGS = {"4f": (3, "11:00"), "glenn": (1, "11:00")}
WEEKLY_RESET_TIME = time(7, 0)  # Thursday 07:00 KST


def raid_label(raid_type: str) -> str:
    try:
        return RAID_LABELS[raid_type]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 던전입니다: {raid_type}") from exc


def next_thursday(today: date) -> date:
    """First Thursday STRICTLY AFTER today; matches the original bot behavior."""
    days = (3 - today.weekday()) % 7
    return today + timedelta(days=days or 7)


def recruitment_week_start(now: datetime) -> date:
    """Target Thursday for creation, rolling forward at Thursday 07:00 KST.

    Before the reset on Thursday morning, the recruitment still targets that
    same Thursday. After the reset, it targets the following Thursday.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")
    local = now.astimezone(KST)
    if local.weekday() == 3 and local.time() < WEEKLY_RESET_TIME:
        return local.date()
    return next_thursday(local.date())


def week_days(thursday: date):
    if thursday.weekday() != 3:
        raise ValueError("파티 모집 주간은 목요일에 시작해야 합니다.")
    return [thursday + timedelta(days=i) for i in range(7)]


def title(thursday: date, raid_type: str = "legacy") -> str:
    final = thursday + timedelta(days=6)
    return f"{thursday:%m.%d} ~ {final:%m.%d}"


def legacy_thread_title(thursday: date, raid_type: str) -> str:
    """Previous title, kept for recovery of interrupted creations."""
    return f"{title(thursday, raid_type)} {raid_label(raid_type)} 모집"


def starter_content(thursday: date, deadline: datetime | None, raid_type: str = "legacy",
                    role_id: int | None = None) -> str:
    final = week_days(thursday)[-1]
    role_mention = f"<@&{role_id}>\n" if role_id else ""
    return (
        role_mention + f"**{raid_label(raid_type)} 파티 모집 | {thursday:%m/%d} ~ {final:%m/%d}**"
        + "\n\n참여 가능한 날의 **요일 이모지**를 모두 눌러 주세요."
        + "\n시간 제한이 있다면 이 스레드에 댓글로 남겨 주세요."
        + ("\n평일은 저녁, 주말/공휴일은 점심부터 출발" if raid_type == "glenn" else "")
        + (f"\n⏰ 신청 마감: **{deadline.astimezone(KST):%m/%d %H:%M} (한국 시간)**"
           if deadline is not None else "")
    )



def is_starter_message(content: str, thursday: date, raid_type: str) -> bool:
    """Recognize the week's starter even if role IDs or deadline settings change.

    Used only within an already identified, bot-owned recruitment thread,
    so editing configuration cannot cause the role notification to fire twice
    after a crash between message send and database persistence.
    """
    final = week_days(thursday)[-1]
    # New and previous party titles already contain both dates. The PNG now
    # replaces the separate date list, so it is no longer needed for recovery.
    header = f"**{raid_label(raid_type)} 파티 모집 | {thursday:%m/%d} ~ {final:%m/%d}**"
    if header in content.splitlines():
        return True
    # Retain compatibility with the original, older recruitment format.
    return (
        f"**{raid_label(raid_type)} — 이번 주 참여 가능 요일 조사**" in content
        and f"{thursday:%m/%d}({KOREAN_WEEKDAYS[0]})" in content
        and f"{final:%m/%d}({KOREAN_WEEKDAYS[6]})" in content
    )


def local_dt(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=KST)


def schedule_for_week(thursday: date, open_days_before: int, open_at: str,
                      close_days_before: int, close_at: str):
    opening = local_dt(thursday - timedelta(days=open_days_before), open_at)
    closing = local_dt(thursday - timedelta(days=close_days_before), close_at)
    if not opening < closing:
        raise ValueError("모집 시작 시간은 마감 시간보다 빨라야 합니다.")
    return opening, closing


def opening_for_week(thursday: date, raid_type: str) -> datetime:
    """4F opens Monday 11:00 KST; Glenn opens Wednesday 11:00 KST."""
    if thursday.weekday() != 3:
        raise ValueError("파티 모집 주간은 목요일에 시작해야 합니다.")
    days_before, hhmm = RECRUITMENT_OPENINGS[raid_type]
    return local_dt(thursday - timedelta(days=days_before), hhmm)


def due_openings(now: datetime):
    """Eligible dungeon/week pairs; catch up until Thursday 07:00 KST.

    Storage separately prevents a second creation for the same dungeon/week.
    The opening window is not a recruitment deadline.
    """
    now = now.astimezone(KST)
    start = recruitment_week_start(now)
    return [(start, raid_type) for raid_type in RAID_TYPES
            if now >= opening_for_week(start, raid_type)]
