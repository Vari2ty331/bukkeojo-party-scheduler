> **Oracle + GitHub auto-deploy:** See [ORACLE_DEPLOY.md](ORACLE_DEPLOY.md) for setup and updates.

# 길드 파티 모집 봇 — 글렌 베르나 + 4관

**디스코드 화면에 표시되는 명령어 및 메시지는 한국어로 통일했습니다.** 명령어 표와 기존 버전에서의 업그레이드는 [DISCORD_UI_KO.md](DISCORD_UI_KO.md)를 참고해 주세요.

This is an update of the working **normal Discord text-channel thread** bot. It retains the application-owned weekday emoji reactions, `/파티 명단` and `/파티 마감`, historical data, and the scheduled opening/closing jobs.

## Participant-role ping on new recruitment threads

When `/파티 생성 던전:글렌 베르나` or `/파티 생성 던전:4관` (or the automatic
weekly timer) **first creates** a recruitment, the bot includes the matching
participant role mention at the very top of the first calendar/signup message
**inside the new thread**.

- Glenn mentions `GLENN_PARTICIPANT_ROLE_ID`; 4F mentions `FOURF_PARTICIPANT_ROLE_ID`.
- Only that one configured role is allowed to ping in each initial message.
- Repeating `/파티 생성`, reopening recruitment, closing, or updating the
  message **does not send another role notification**.
- This works for your existing **ordinary text-channel threads**.
- Set both role IDs in `.env`, including in your test server.
- The role must be **Mentionable**, OR the bot must have the channel permission
  **Mention @everyone, @here, and All Roles**. The latter is broader than
  necessary, so making your two participant roles mentionable is usually safer.
  The bot checks this before creating a new thread and reports an error if
  mentions would be suppressed.
- For small roles (fewer than 100 members), Discord can add role members who
  can view the thread and notify them; actual push notifications depend on each
  user's notification preferences.

**Scope:** Two independent recruitments for each Thursday–Wednesday raid week: **Glenn Bearna** and **4F**. The bot runs as a separate Python project on Windows for testing and can later move to Oracle. No N100/website/LLM or PNG rendering is required now.

## Slash commands

- **`/파티 생성 던전:글렌 베르나`** — create the upcoming Glenn recruitment in `GLENN_TEXT_CHANNEL_ID`.
- **`/파티 생성 던전:4관`** — create the upcoming 4F recruitment in `FOURF_TEXT_CHANNEL_ID`.
- `dungeon` is **required**; `/파티 생성` alone is not valid. There is no `both` option.
- **`/파티 명단`** — inside either thread, read live reactions if open; read the final saved roster if closed. The command infers which dungeon/week from the thread ID.
- **`/파티 마감`** — inside either thread, save its roster to SQLite, clear its seven reactions, and leave the thread open for later chat.
- **`/파티 재개`** — inside a previously closed thread, restore the seven reactions without losing any saved signups. New signups are merged with the saved roster when reporting and closing again.
- **`/파티 재개 연장시간:120`** — needed if the original deadline has already passed. Extends the closing deadline to at least two hours from now; the next automatic close uses that updated deadline.
- `/파티 상태`, `/파티 이모지` — unchanged.

## Setup or upgrade (Windows)

1. **Stop the currently running bot**, and back up your `.env` file and SQLite database before upgrading. If a database is open, use SQLite's backup API or stop the bot first and copy its `-wal` and `-shm` files with it if present.
2. Extract this project to a new folder, and copy your existing `.env` and `data/recruitments.sqlite3` from the previous project. Do not replace your existing data with a blank database.
3. Edit `.env` so it includes both required parent channel IDs. They refer to **ordinary Discord text channels**, not thread IDs. You may supply the same channel ID for both if desired.

   ```dotenv
   DISCORD_TOKEN=YOUR_NEW_BOT_TOKEN
   GUILD_ID=YOUR_GUILD_ID
   GLENN_TEXT_CHANNEL_ID=YOUR_GLENN_PARENT_TEXT_CHANNEL_ID
   FOURF_TEXT_CHANNEL_ID=YOUR_4F_PARENT_TEXT_CHANNEL_ID
   GLENN_PARTICIPANT_ROLE_ID=YOUR_GLENN_PARTICIPANTS_ROLE_ID
   FOURF_PARTICIPANT_ROLE_ID=YOUR_4F_PARTICIPANTS_ROLE_ID
   ORGANIZER_ROLE_ID=YOUR_ORGANIZER_ROLE_ID
   DATABASE_PATH=data/recruitments.sqlite3

   AUTO_CREATE_ENABLED=false
   AUTO_CLOSE_ENABLED=false
   OPEN_DAYS_BEFORE=1
   OPEN_TIME=18:00
   CLOSE_DAYS_BEFORE=1
   CLOSE_TIME=23:00
   ```

   The previous `TEXT_CHANNEL_ID`/`FORUM_CHANNEL_ID` fallback is **not used in this version**. Provide `GLENN_TEXT_CHANNEL_ID` and `FOURF_TEXT_CHANNEL_ID` explicitly. The opening/closing values above are *sample times* and are expressed in Korea time (KST).

4. Install dependencies in your virtual environment if needed, and run tests:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   .\.venv\Scripts\python.exe bot.py
   ```

5. The bot syncs slash commands to the configured Discord guild at startup. You shouldn't need to upload the seven application-owned emojis again if they are already installed on this Discord application.

## Reopening after closing (important details)

**Reopen uses the same thread, message, and SQLite record.** The organizer runs `/파티 재개` in the closed Glenn or 4F thread; no dungeon field is needed. The other raid is unaffected.

At closing, the bot **saved all user signups** and removed the seven reactions. Discord cannot re-create a reaction on behalf of a member. Therefore:

1. `/파티 재개` first discards any late reactions that appeared while closed, then re-adds the seven bot-owned weekday reactions.
2. All signups saved at the earlier close **remain valid**. Members who were already registered do **not** need to react again.
3. `/파티 명단` while reopened shows the **union** of saved signups and new reaction users, deduplicated by Discord user ID.
4. `/파티 마감` again saves the combined roster and clears the reactions again. This also works after multiple reopen/close cycles or a bot restart.
5. The original deadline remains in effect if it is still in the future. If it has passed, the command requires `extra_minutes` to prevent the automatic timer from immediately re-closing the recruitment. Example: `/파티 재개 연장시간:120`.

**Limitation:** Discord's original user reactions cannot be restored. A member cannot withdraw an earlier, saved signup merely by removing a newly added reaction; the saved signup remains. If an organizer needs to correct or remove a previously archived signup, that will require a separate editing/withdrawal feature in a future iteration. The bot will not silently discard these registrations.

For robust recovery, `/파티 재개` can be safely retried if an emoji/message editing operation failed partway through. If already open, it won't reset the existing participation list. The bot needs `Manage Messages` to clear reactions and `Add Reactions` to put the seven back.

## Exactly how duplicate protection works

The bot stores a record for each `(dungeon, week_start)` pair. For example:

- `(glenn, 2026-11-26)` → Glenn thread ID
- `(4f, 2026-11-26)` → 4F thread ID

When **manual `/파티 생성`** or the **automatic creation timer** runs, both invoke the **same `create_recruitment()` function**. If that dungeon/week already exists (even if it is closed), the command returns the existing thread URL instead of creating a new thread. A bot-level lock and a database uniqueness constraint prevent ordinary duplicate requests.

A `creation_reservations` table further protects against a bot crash *during* a creation attempt. The bot reserves the dungeon/week before contacting Discord, and records the thread ID as soon as it receives it. If restarted after the thread was created, it attempts to resume the unfinished operation rather than creating another thread.

**Rare failure case:** If Discord created a thread but the bot lost the response *and* cannot find that thread, the bot deliberately **stops and reports a recovery error** instead of risking a duplicate. Inspect the parent channel and the bot logs, and reconcile the pending reservation before trying again. Do not simply delete the reservation without checking whether the thread exists. Run only one live instance of this application for the production guild.

The weekly auto-creation task attempts **both** dungeons during the configured opening window, independently. If you manually created Glenn first, the timer will skip Glenn and create only 4F (if needed).

## Automatic schedules

Both timers are **off by default** for local testing:

- `AUTO_CREATE_ENABLED=true` enables automatic creation in the opening window defined by `OPEN_DAYS_BEFORE`, `OPEN_TIME`.
- `AUTO_CLOSE_ENABLED=true` enables automatic snapshots/clearing reactions at the configured deadline.
- For each raid week, the bot uses the upcoming Thursday–Wednesday date range and the configured KST opening/closing times.
- If the bot missed a closing deadline by over two minutes due to downtime, it marks that recruitment `review_needed` for organizer verification rather than claiming the current reactions were the exact historical snapshot.

**Closing one raid does not close the other**; `/파티 명단` or `/파티 마감` always uses the current thread ID.

## Suggested personal-server test

1. Leave both automatic settings `false` and restart the bot.
2. Run `/파티 생성 던전:글렌 베르나` and `/파티 생성 던전:4관`; verify each appears in its intended parent text channel.
3. Run the **same Glenn create command twice**; it should return the original Glenn thread URL both times.
4. Confirm Glenn pinged only the Glenn role and 4F only the 4F role; repeat `/파티 생성` to confirm there is no second ping. Then react on different days and run `/파티 명단` in both to confirm independent rosters.
5. Close Glenn with `/파티 마감`, then invoke `/파티 생성 던전:글렌 베르나` again; it must **not** make a replacement thread. 4F stays open.
6. Restart the bot; confirm reports still work for both threads.
7. Only after these tests, turn on the automatic jobs and confirm they do not duplicate manually created threads.

**Deferred:** generating a PNG partial calendar with Pillow on Oracle. For now, keep the existing date text in the recruitment starter message. This change does not introduce image-generation dependencies or modify the existing reaction mapping.
