#!/usr/bin/env python3
import os
import tempfile
import unittest

from artifacts import game_resource_id
from wallet_store import WalletStore


class QueuePersistenceTests(unittest.TestCase):
    def setUp(self):
        fd,self.db=tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store=WalletStore(self.db)
        order=self.store.create_topup(1,'2','USDT'); provider='p-'+order['order_id']
        self.store.attach_provider(order['order_id'],provider,'https://pay.example/x')
        self.store.credit_verified({'order_id':order['order_id'],'provider_order_id':provider,
                                    'coin':'USDT','amount':'2'})
        rid=game_resource_id('ryuugames','https://ryuugames.com/test/')
        offer={'resource_id':rid,'title':'T','source':'ryuugames',
               'source_url':'https://ryuugames.com/test/',
               'download_url':'https://pixeldrain.com/u/ABC','version':'1',
               'price_units':100000000}
        self.store.create_download_purchase(1,offer)
        self.rid=rid

    def tearDown(self):
        for suffix in ('','-shm','-wal'):
            try: os.unlink(self.db+suffix)
            except FileNotFoundError: pass

    def test_queued_job_survives_store_restart_and_claim_removes_it(self):
        reopened=WalletStore(self.db)
        queued=reopened.queued_download_jobs(limit=10)
        self.assertEqual(len(queued),1)
        self.assertEqual(queued[0]['resource_id'],self.rid)
        reopened.claim_download(queued[0]['job_id'])
        self.assertEqual(reopened.queued_download_jobs(limit=10),[])


    def test_restart_requeues_interrupted_download(self):
        job=self.store.job_for_resource(self.rid)
        self.store.claim_download(job['job_id'])
        reopened=WalletStore(self.db)
        claimed = reopened.get_job(job['job_id'])
        self.assertEqual(reopened.reset_interrupted_downloads(now=claimed['lease_expires_at'] + 1),1)
        queued=reopened.queued_download_jobs()
        self.assertEqual([x['job_id'] for x in queued],[job['job_id']])


    def test_unexpired_lease_is_not_stolen_by_second_instance(self):
        job=self.store.job_for_resource(self.rid)
        claimed=self.store.claim_download(job['job_id'])
        reopened=WalletStore(self.db)
        self.assertEqual(reopened.reset_interrupted_downloads(now=claimed['lease_expires_at'] - 1),0)
        self.assertEqual(reopened.get_job(job['job_id'])['status'],'downloading')


if __name__=='__main__': unittest.main()
