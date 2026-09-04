#!/usr/bin/env python3
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from artifacts import game_resource_id
from pipeline import process_download_job
from wallet_store import WalletStore


class FakeUploader:
    def __init__(self): self.calls=[]
    async def upload(self, path, caption):
        self.calls.append((path, caption))
        return {'storage_chat_id': -100999, 'storage_message_id': 55}


class FakeBot:
    def __init__(self): self.calls=[]
    async def copy_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=7000 + len(self.calls))


class DownloadPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WalletStore(self.db)
        self.resource_id = game_resource_id('ryuugames', 'https://ryuugames.com/test/')
        self.offer = {'resource_id': self.resource_id, 'title': '[Ryuugames] Test Game 已解密',
                      'source': 'ryuugames', 'source_url': 'https://ryuugames.com/test/',
                      'download_url': 'https://www.mediafire.com/file/test/file',
                      'version': '1.0', 'price_units': 100000000}
        for user in (101, 202):
            order=self.store.create_topup(user,'2','USDT')
            provider='p-'+order['order_id']
            self.store.attach_provider(order['order_id'],provider,'https://pay.example/x')
            self.store.credit_verified({'order_id':order['order_id'],
                'provider_order_id':provider,'coin':'USDT','amount':'2'})
            self.store.create_download_purchase(user,self.offer)
        self.job = self.store.job_for_resource(self.resource_id)

    def tearDown(self):
        self.tmp.cleanup()
        for suffix in ('','-shm','-wal'):
            try: os.unlink(self.db+suffix)
            except FileNotFoundError: pass

    async def test_one_download_one_upload_delivers_all_waiting_buyers(self):
        download_calls=[]
        def fake_download(url, destination_dir, progress=None):
            download_calls.append(url)
            path=Path(destination_dir)/'game.zip'; path.write_bytes(b'game-data')
            return {'path':str(path),'file_size':9,'checksum':'sha256:abc','final_url':url}
        uploader,bot=FakeUploader(),FakeBot()
        stages=[]
        async def stage(name, data=None): stages.append(name)
        result=await process_download_job(self.store,self.job['job_id'],fake_download,
                                          uploader,bot,self.tmp.name,stage_callback=stage)
        self.assertEqual(download_calls,[self.offer['download_url']])
        self.assertEqual(len(uploader.calls),1)
        self.assertEqual(len(bot.calls),2)
        self.assertEqual(result['delivered'],2)
        self.assertEqual(stages,['downloading','uploading','delivering','ready'])
        self.assertFalse(Path(uploader.calls[0][0]).exists())
        self.assertEqual(self.store.get_resource(self.resource_id)['cache_status'],'ready')
        self.assertEqual(self.store.pending_purchases_for_resource(self.resource_id), [])

    async def test_ambiguous_telegram_failure_is_reported_for_manual_review(self):
        def fake_download(url,destination_dir,progress=None):
            path=Path(destination_dir)/'game.zip'; path.write_bytes(b'x')
            return {'path':str(path),'file_size':1,'checksum':'sha256:x','final_url':url}
        class TimeoutBot:
            async def copy_message(self,**kwargs): raise TimeoutError('unknown result')
        stages=[]
        async def stage(name,data=None): stages.append(name)
        result=await process_download_job(self.store,self.job['job_id'],fake_download,
                                          FakeUploader(),TimeoutBot(),self.tmp.name,
                                          stage_callback=stage)
        self.assertEqual(result['manual_review'],2)
        self.assertEqual(stages[-1],'manual_review')
        self.assertEqual(self.store.get_balance_units(101),100000000)
        self.assertEqual(self.store.get_balance_units(202),100000000)

    async def test_download_failure_refunds_every_waiting_buyer(self):
        def failed_download(*args,**kwargs): raise RuntimeError('HTTP 403')
        uploader,bot=FakeUploader(),FakeBot()
        result=await process_download_job(self.store,self.job['job_id'],failed_download,
                                          uploader,bot,self.tmp.name)
        self.assertEqual(result['refunded'],2)
        self.assertEqual(uploader.calls,[])
        self.assertEqual(bot.calls,[])
        self.assertEqual(self.store.get_balance_units(101),200000000)
        self.assertEqual(self.store.get_balance_units(202),200000000)


    async def test_prepares_archive_before_upload(self):
        prepared_calls = []
        def fake_download(url, destination_dir, progress=None):
            path = Path(destination_dir) / 'source.rar'
            path.write_bytes(b'encrypted-source')
            return {'path': str(path), 'file_size': path.stat().st_size,
                    'checksum': 'sha256:source', 'final_url': url}
        def fake_prepare(path, source, title):
            prepared_calls.append((path, source, title))
            output = Path(path).with_name('Test Game.zip')
            output.write_bytes(b'plain-zip')
            return {'path': str(output), 'file_size': output.stat().st_size,
                    'checksum': 'sha256:plain'}
        uploader, bot = FakeUploader(), FakeBot()
        stages = []
        async def stage(name, data=None): stages.append(name)

        result = await process_download_job(
            self.store, self.job['job_id'], fake_download, uploader, bot,
            self.tmp.name, stage_callback=stage, prepare_callable=fake_prepare)

        self.assertEqual(len(prepared_calls), 1)
        self.assertEqual(prepared_calls[0][1:], ('ryuugames', '[Ryuugames] Test Game 已解密'))
        self.assertTrue(uploader.calls[0][0].endswith('Test Game.zip'))
        self.assertEqual(uploader.calls[0][1], 'Test Game')
        resource = self.store.get_resource(self.resource_id)
        self.assertEqual(resource['checksum'], 'sha256:plain')
        self.assertEqual(stages, ['downloading', 'preparing', 'uploading',
                                  'delivering', 'ready'])
        self.assertEqual(result['delivered'], 2)


if __name__=='__main__': unittest.main()
