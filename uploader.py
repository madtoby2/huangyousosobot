#!/usr/bin/env python3
"""Telethon user-session uploader for the private storage channel."""
from __future__ import annotations

import json
import os
from pathlib import Path


class UploaderUnavailable(RuntimeError):
    pass


class ChannelUploader:
    def __init__(self, client_factory, channel_id: int):
        self.client_factory = client_factory
        self.channel_id = int(channel_id)

    async def upload(self, path: str, caption: str):
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            raise UploaderUnavailable('upload file is missing or empty')
        client = self.client_factory()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise UploaderUnavailable('uploader session is not authorized')
            message = await client.send_file(
                self.channel_id, str(file_path), caption=str(caption)[:1000],
                force_document=True)
            if not getattr(message, 'id', None):
                raise UploaderUnavailable('Telegram returned no storage message id')
            return {'storage_chat_id': self.channel_id,
                    'storage_message_id': int(message.id)}
        finally:
            await client.disconnect()


def build_telethon_uploader(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    channel = os.environ.get('STORAGE_CHANNEL_ID', '').strip()
    if not channel:
        raise UploaderUnavailable('STORAGE_CHANNEL_ID is not configured')
    auth_path = root / '.uploader_auth.json'
    if not auth_path.exists():
        raise UploaderUnavailable('uploader authorization is missing')
    state = json.loads(auth_path.read_text())
    if not state.get('authorized'):
        raise UploaderUnavailable('uploader session is not authorized')
    session = os.environ.get('UPLOADER_SESSION', str(root / 'uploader_351961666576.session'))
    if not Path(session).exists():
        raise UploaderUnavailable('uploader session file is missing')
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise UploaderUnavailable('Telethon is not installed') from exc

    def factory():
        return TelegramClient(session, int(state['api_id']), state['api_hash'])

    return ChannelUploader(factory, int(channel))
