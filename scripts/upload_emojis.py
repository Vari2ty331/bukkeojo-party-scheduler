"""Run once: python -m scripts.upload_emojis. Reuses existing emojis safely."""
import asyncio
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

from core.calendar import ENGLISH_KEYS

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')


class EmojiUploader(discord.Client):
    async def on_ready(self):
        try:
            existing = {e.name: e for e in await self.fetch_application_emojis()}
            for day in ENGLISH_KEYS:
                name = f'party_{day}'
                old_name = f'raid_{day}'
                if name in existing:
                    print(f'이미 있음: {name} = {existing[name].id}')
                    continue
                if old_name in existing:
                    print(f'기존 이모지 재사용: {old_name} = {existing[old_name].id}')
                    continue
                img = (ROOT / 'assets' / 'weekdays' / f'party_{day}.png').read_bytes()
                emoji = await self.create_application_emoji(name=name, image=img)
                print(f'새 이모지 생성: {name} = {emoji.id}')
            print('요일 이모지 준비 완료.')
        except Exception as exc:
            print(f'이모지 작업 실패: {type(exc).__name__}: {exc}')
        finally:
            await self.close()


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token or token.startswith('PASTE_'):
        raise SystemExit('.env에 DISCORD_TOKEN을 입력해 주세요.')
    asyncio.run(EmojiUploader(intents=discord.Intents.none()).start(token))
