"""선택 실행: 기존 앱 이모지 raid_* 이름을 party_*로 변경합니다.

사용법: python -m scripts.rename_emojis
변경되는 것은 표시 이름뿐이며 Discord 이모지 ID와 기존 반응은 유지됩니다.
"""
import asyncio
import os
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

from core.calendar import ENGLISH_KEYS

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')


class EmojiRenamer(discord.Client):
    async def on_ready(self):
        try:
            app_id = self.application_id or (await self.application_info()).id
            emojis = {e.name: e for e in await self.fetch_application_emojis()}
            token = os.environ['DISCORD_TOKEN']
            headers = {'Authorization': f'Bot {token}'}
            async with aiohttp.ClientSession(headers=headers) as session:
                for day in ENGLISH_KEYS:
                    old_name, new_name = f'raid_{day}', f'party_{day}'
                    if new_name in emojis:
                        print(f'이름 확인 완료: {new_name}')
                        continue
                    existing = emojis.get(old_name)
                    if not existing:
                        print(f'해당 이모지 없음: {old_name} (새 설치는 upload_emojis 사용)')
                        continue
                    url = f'https://discord.com/api/v10/applications/{app_id}/emojis/{existing.id}'
                    async with session.patch(url, json={'name': new_name}) as response:
                        if response.status != 200:
                            error = (await response.text())[:300]
                            raise RuntimeError(f'{old_name} 이름 변경 실패 ({response.status}): {error}')
                    emojis[new_name] = existing
                    print(f'{old_name} → {new_name} (ID 유지: {existing.id})')
            print('이모지 이름 확인이 끝났습니다.')
        except Exception as exc:
            print(f'이모지 이름 변경 실패: {type(exc).__name__}: {exc}')
        finally:
            await self.close()


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token or token.startswith('PASTE_'):
        raise SystemExit('.env에 DISCORD_TOKEN을 입력해 주세요.')
    asyncio.run(EmojiRenamer(intents=discord.Intents.none()).start(token))
