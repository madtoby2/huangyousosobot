#!/usr/bin/env python3
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from uploader import ChannelUploader, UploaderUnavailable


class FakeClient:
    def __init__(self, authorized=True):
        self.authorized=authorized; self.calls=[]; self.disconnected=False
    async def connect(self): self.calls.append('connect')
    async def is_user_authorized(self): return self.authorized
    async def send_file(self, channel, path, caption, force_document):
        self.calls.append((channel,path,caption,force_document))
        return SimpleNamespace(id=66)
    async def disconnect(self): self.disconnected=True


class UploaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_returns_channel_message_and_disconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'game.zip'; path.write_bytes(b'x')
            client=FakeClient()
            uploader=ChannelUploader(lambda:client,-100999)
            result=await uploader.upload(str(path),'Test Game')
            self.assertEqual(result,{'storage_chat_id':-100999,'storage_message_id':66})
            self.assertEqual(client.calls[1],(-100999,str(path),'Test Game',True))
            self.assertTrue(client.disconnected)

    async def test_unauthorized_session_never_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'game.zip'; path.write_bytes(b'x')
            client=FakeClient(authorized=False)
            uploader=ChannelUploader(lambda:client,-100999)
            with self.assertRaises(UploaderUnavailable):
                await uploader.upload(str(path),'Test Game')
            self.assertEqual(client.calls,['connect'])
            self.assertTrue(client.disconnected)


if __name__=='__main__': unittest.main()
