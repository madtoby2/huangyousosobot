#!/usr/bin/env python3
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from admin_panel import make_handler
from artifacts import game_resource_id
from wallet_store import WalletStore


class AdminPanelTests(unittest.TestCase):
    def setUp(self):
        fd,self.db=tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store=WalletStore(self.db)
        order=self.store.create_topup(101,'2','USDT'); provider='p-'+order['order_id']
        self.store.attach_provider(order['order_id'],provider,'https://pay.example/x')
        self.store.credit_verified({'order_id':order['order_id'],'provider_order_id':provider,
                                    'coin':'USDT','amount':'2'})
        rid=game_resource_id('ryuugames','https://ryuugames.com/test/')
        offer={'resource_id':rid,'title':'Test Game','source':'ryuugames',
               'source_url':'https://ryuugames.com/test/',
               'download_url':'https://pixeldrain.com/u/X','version':'1',
               'price_units':100000000}
        self.purchase,_,_=self.store.create_download_purchase(101,offer)
        handler=make_handler(self.store,'secret-token')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        encoded=base64.b64encode(b'admin:secret-token').decode()
        self.auth={'Authorization':'Basic '+encoded}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        for suffix in ('','-shm','-wal'):
            try: os.unlink(self.db+suffix)
            except FileNotFoundError: pass

    def request(self,path,method='GET',body=None,auth=True):
        data=json.dumps(body).encode() if body is not None else None
        headers={'Content-Type':'application/json'}
        if auth: headers.update(self.auth)
        return urllib.request.urlopen(urllib.request.Request(self.base+path,data=data,
                                      headers=headers,method=method),timeout=5)

    def test_api_rejects_missing_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/overview',auth=False)
        self.assertEqual(caught.exception.code,401)

    def test_overview_and_lists_show_real_database_state(self):
        with self.request('/api/overview') as response:
            data=json.load(response)
        self.assertEqual(data['users'],1)
        self.assertEqual(data['pending_purchases'],1)
        self.assertEqual(data['queued_jobs'],1)
        with self.request('/api/purchases') as response:
            rows=json.load(response)['items']
        self.assertEqual(rows[0]['purchase_id'],self.purchase['purchase_id'])

    def test_resources_table_exposes_price_action(self):
        from admin_panel import HTML_PAGE
        self.assertIn("kind==='resources'?`<td><button onclick=\"setPrice(", HTML_PAGE)
        self.assertIn('setPrice(', HTML_PAGE)

    def test_resource_price_can_be_updated(self):
        rid=self.store.admin_list('resources')[0]['resource_id']
        with self.request('/api/resource-price','POST',
                          {'resource_id':rid,'price_units':125000000}) as response:
            data=json.load(response)
        self.assertEqual(data['price_units'],125000000)
        self.assertEqual(self.store.get_resource(rid)['price_units'],125000000)

    def test_manual_review_can_be_marked_delivered_through_admin(self):
        pid=self.purchase['purchase_id']
        self.store.claim_delivery(pid); self.store.mark_delivery_unknown(pid,'timeout')
        with self.request('/api/resolve-delivery','POST',
                          {'purchase_id':pid,'telegram_message_id':919}) as response:
            self.assertTrue(json.load(response)['resolved'])
        row=self.store.get_purchase(pid)
        self.assertEqual((row['status'],row['telegram_message_id']),('delivered',919))

    def test_manual_refund_is_append_only_and_idempotent(self):
        payload={'purchase_id':self.purchase['purchase_id'],'reason':'admin refund'}
        with self.request('/api/refund','POST',payload) as response:
            self.assertTrue(json.load(response)['refunded'])
        with self.request('/api/refund','POST',payload) as response:
            self.assertFalse(json.load(response)['refunded'])
        self.assertEqual(self.store.get_balance_units(101),200000000)
        self.assertEqual(len([x for x in self.store.ledger_for(101) if x['kind']=='refund']),1)


if __name__=='__main__': unittest.main()
