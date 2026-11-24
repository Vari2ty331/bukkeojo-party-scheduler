# Weekly partial calendar PNG

Both manual `/파티 생성` and the automatic Monday (4F) / Wednesday (Glenn)
recruitment use the same `create_recruitment()` method. A new recruitment
includes a Pillow-generated, Sunday–Saturday partial calendar attachment
on the original signup message. Only the seven Thursday–Wednesday dates
are highlighted. The title still displays the date range; the separate
text list of dates is omitted. When automatic closing is disabled, no
closing notice is shown. Date calculation, role mentions, reactions,
configured deadlines, duplicate protection and database are unchanged.

## One-time setup on Oracle

```bash
sudo apt update
sudo apt install -y fonts-nanum
```

The renderer prefers NanumBarunGothic (used in the approved preview), then
NanumGothic. On Windows it can use Malgun Gothic (맑은 고딕). No font files
are included in the repository. The exact font appearance may differ.

Make sure the bot has Attach Files (파일 첨부) in both recruitment channels.
The update script will install Pillow and `holidays` into the existing virtual environment
from `requirements.txt`. There are no new `.env` settings.

## Offline preview

On Windows, from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m core.calendar_image 2026-11-26 "$env:TEMP\party-calendar.png"
```

On Oracle, after the updated code is deployed:

```bash
.venv/bin/python -m core.calendar_image 2026-11-26 /tmp/party-calendar.png
```

The date must be the Thursday of the desired play week. This preview
requires no Discord token or running bot; it never posts a message.

## Behavior

- South Korean public holidays (including substitute days) are rendered in
  red, overriding Saturday blue. Holiday data is computed locally for both
  calendar years if the grid crosses New Year; no API or key is needed.
  Newly declared holidays may require a `holidays` package update.

- Existing recruitments are not changed or re-announced. Reopening or closing
  changes the message text without replacing its attachments.
- If generating the PNG fails, a new thread is not created. This prevents
  partial creation when Korean fonts are missing. The same applies to a
  missing Attach Files permission when it can be checked in advance.
- The image is rendered to memory; no generated PNG files accumulate on
  Oracle, and no image-generation service or AI API is needed.
- Tests cover month/year boundaries, leap day, the seven highlighted cells,
  and the signup attachment call. The rendering test skips with a clear
  message if no supported font is installed; install one for a full test.
