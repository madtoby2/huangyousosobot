#!/usr/bin/env python3
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from artifacts import game_resource_id
from bot import _state, buy_download
from wallet_store import WalletStore


class BuyDownloadHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd,self.db=tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store=WalletStore(self.db)
        self.user=101
        order=self.store.create_topup(self.user,'2','USDT')
        provider='p-'+order['order_id']
        self.store.attach_provider(order['order_id'],provider,'https://pay.example/x')
        self.store.credit_verified({'order_id':order['order_id'],'provider_order_id':provider,
                                    'coin':'USDT','amount':'2'})
        rid=game_resource_id('ryuugames','https://ryuugames.com/test/')
        self.offer={'resource_id':rid,'title':'Test Game','source':'ryuugames',
                    'source_url':'https://ryuugames.com/test/',
                    'download_url':'https://www.mediafire.com/file/x/game/file',
                    'version':'1.0','price_units':100000000}
        _state(self.user)['paid_offers']={rid:self.offer}
        self.q=SimpleNamespace(data='buy_'+rid,from_user=SimpleNamespace(id=self.user),
            answer=AsyncMock(),message=SimpleNamespace(reply_text=AsyncMock()))
        self.update=SimpleNamespace(callback_query=self.q)
        self.context=SimpleNamespace(application=SimpleNamespace(bot=SimpleNamespace()))

    def tearDown(self):
        _state(self.user).clear()
        for suffix in ('','-shm','-wal'):
            try: os.unlink(self.db+suffix)
            except FileNotFoundError: pass

    async def test_unconfigured_delivery_never_charges(self):
        with patch('bot._wallet_store',self.store),patch('bot._delivery_configured',return_value=False):
            await buy_download(self.update,self.context)
        self.assertEqual(self.store.get_balance_units(self.user),200000000)
        self.assertEqual(self.store.purchases_for(self.user),[])
        self.assertIn('暂未就绪',self.q.message.reply_text.await_args.args[0])

    async def test_configured_delivery_charges_and_queues(self):
        with patch('bot._wallet_store',self.store),patch('bot._delivery_configured',return_value=True):
            await buy_download(self.update,self.context)
        self.assertEqual(self.store.get_balance_units(self.user),100000000)
        self.assertEqual(len(self.store.purchases_for(self.user)),1)
        self.assertEqual(self.store.job_for_resource(self.offer['resource_id'])['status'],'queued')
        self.assertIn('下载队列',self.q.message.reply_text.await_args.args[0])

    async def test_insufficient_balance_does_not_queue(self):
        poor=202
        _state(poor)['paid_offers']={self.offer['resource_id']:self.offer}
        q=SimpleNamespace(data='buy_'+self.offer['resource_id'],from_user=SimpleNamespace(id=poor),
            answer=AsyncMock(),message=SimpleNamespace(reply_text=AsyncMock()))
        try:
            with patch('bot._wallet_store',self.store),patch('bot._delivery_configured',return_value=True):
                await buy_download(SimpleNamespace(callback_query=q),self.context)
            self.assertEqual(self.store.purchases_for(poor),[])
            self.assertIn('余额不足',q.message.reply_text.await_args.args[0])
        finally:
            _state(poor).clear()


if __name__=='__main__': unittest.main()
