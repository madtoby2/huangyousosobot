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
    async def send_file(self, channel, path, caption, force_document=False, supports_streaming=False, parse_mode=None):
        self.calls.append((channel,path,caption,force_document,supports_streaming,parse_mode))
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
            self.assertEqual(client.calls[1],(-100999,str(path),'Test Game',True,False,None))
            self.assertTrue(client.disconnected)

    async def test_video_upload_is_streamable_media_not_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'daily.mp4'; path.write_bytes(b'video')
            client=FakeClient()
            uploader=ChannelUploader(lambda:client,-100999)
            upload_video=getattr(uploader,'upload_video',None)
            self.assertTrue(callable(upload_video),'ChannelUploader.upload_video must be implemented')
            result=await upload_video(str(path),'Daily video')
            self.assertEqual(result,{'storage_chat_id':-100999,'storage_message_id':66})
            self.assertEqual(client.calls[1],(-100999,str(path),'Daily video',False,True,None))
            self.assertTrue(client.disconnected)

    async def test_mkv_upload_does_not_claim_streaming_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'daily.mkv'; path.write_bytes(b'video')
            client=FakeClient()
            uploader=ChannelUploader(lambda:client,-100999)
            await uploader.upload_video(str(path),'Daily video')
            self.assertFalse(client.calls[1][4])

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
