#!/usr/bin/env python3
"""Local authenticated management panel for SearchBot paid downloads."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from wallet_store import PaymentMismatch, WalletStore


HTML_PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>黄油搜搜管理面板</title>
<style>:root{--bg:#0b1020;--card:#151d32;--line:#293551;--text:#eef3ff;--muted:#9dabc7;--blue:#6da8ff;--green:#55d69e;--red:#ff6b7a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui}.wrap{max-width:1200px;margin:auto;padding:24px}h1{margin:0 0 18px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}.n{font-size:28px;font-weight:700;color:var(--blue)}.muted{color:var(--muted)}nav{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}button{border:1px solid var(--line);background:#202b45;color:var(--text);border-radius:9px;padding:9px 13px;cursor:pointer}button:hover{border-color:var(--blue)}button.danger{background:#492532;color:#ffdce0}table{width:100%;border-collapse:collapse;display:block;overflow:auto}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted)}.status-ready,.status-delivered{color:var(--green)}.status-failed,.status-refunded{color:var(--red)}#msg{min-height:24px;margin:10px 0;color:var(--muted)}@media(max-width:600px){.wrap{padding:12px}th,td{padding:8px}}</style></head>
<body><div class="wrap"><h1>🎮 黄油搜搜管理面板</h1><div id="cards" class="cards"></div>
<nav><button onclick="loadList('users')">用户余额</button><button onclick="loadList('resources')">资源缓存</button><button onclick="loadList('jobs')">下载任务</button><button onclick="loadList('purchases')">购买/退款</button><button onclick="refresh()">刷新</button></nav>
<div id="msg"></div><div class="panel"><table><thead id="head"></thead><tbody id="body"></tbody></table></div></div>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let current='purchases';async function api(path,opt){const r=await fetch(path,opt);const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}async function refresh(){const x=await api('/api/overview');const labels={users:'用户',resources:'资源',pending_purchases:'待交付',manual_review_purchases:'待人工核对',delivered_purchases:'已交付',refunded_purchases:'已退款',queued_jobs:'排队任务',active_jobs:'处理中'};cards.innerHTML=Object.entries(labels).map(([k,v])=>`<div class=card><div class=n>${x[k]}</div><div class=muted>${v}</div></div>`).join('');await loadList(current)}async function loadList(kind){current=kind;msg.textContent='读取中…';try{const x=await api('/api/'+kind);const rows=x.items||[];let keys=rows.length?Object.keys(rows[0]):[];head.innerHTML='<tr>'+keys.map(k=>`<th>${esc(k)}</th>`).join('')+((kind==='purchases'||kind==='resources')?'<th>操作</th>':'')+'</tr>';body.innerHTML=rows.map(r=>'<tr>'+keys.map(k=>`<td class="status-${esc(r[k])}">${esc(r[k])}</td>`).join('')+(kind==='purchases'?`<td>${r.status==='pending'?`<button class=danger onclick="refund('${esc(r.purchase_id)}')">退款</button>`:r.status==='manual_review'?`<button onclick="resolveDelivery('${esc(r.purchase_id)}')">标记已交付</button> <button class=danger onclick="refund('${esc(r.purchase_id)}')">确认未交付并退款</button>`:''}</td>`:kind==='resources'?`<td><button onclick="setPrice('${esc(r.resource_id)}',${Number(r.price_units)})">改价</button></td>`:'')+'</tr>').join('');msg.textContent=`${rows.length} 条`}catch(e){msg.textContent='错误：'+e.message}}async function setPrice(id,current){const raw=prompt('输入新价格（USDT）',String(current/100000000));if(raw===null)return;const n=Number(raw);if(!Number.isFinite(n)||n<=0)return alert('价格无效');try{await api('/api/resource-price',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource_id:id,price_units:Math.round(n*100000000)})});msg.textContent='价格已更新';await refresh()}catch(e){msg.textContent='改价失败：'+e.message}}async function resolveDelivery(id){const raw=prompt('输入已核对的 Telegram 消息 ID');if(raw===null)return;const mid=Number(raw);if(!Number.isInteger(mid)||mid<=0)return alert('消息 ID 无效');try{await api('/api/resolve-delivery',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({purchase_id:id,telegram_message_id:mid})});msg.textContent='已标记交付';await refresh()}catch(e){msg.textContent='处理失败：'+e.message}}async function refund(id){if(!confirm('确认给该订单退款？'))return;try{const x=await api('/api/refund',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({purchase_id:id,reason:'admin panel refund'})});msg.textContent=x.refunded?'退款成功':'此前已退款';await refresh()}catch(e){msg.textContent='退款失败：'+e.message}}refresh();</script></body></html>'''


def make_handler(store: WalletStore, token: str):
    expected = 'Basic ' + base64.b64encode(('admin:' + token).encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _authorized(self):
            supplied = self.headers.get('Authorization', '')
            return bool(token) and hmac.compare_digest(supplied, expected)

        def _send(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = (json.dumps(body, ensure_ascii=False).encode('utf-8')
                        if content_type.startswith('application/json') else body.encode('utf-8'))
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self):
            if self._authorized():
                return True
            body = json.dumps({'error': 'unauthorized'}).encode()
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="SearchBot Admin"')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return False

        def do_GET(self):
            if not self._require_auth(): return
            path = urlsplit(self.path).path
            try:
                if path == '/':
                    self._send(200, HTML_PAGE, 'text/html; charset=utf-8')
                elif path == '/api/overview':
                    self._send(200, store.admin_overview())
                elif path.startswith('/api/') and path[5:] in ('users','resources','jobs','purchases'):
                    self._send(200, {'items': store.admin_list(path[5:])})
                else:
                    self._send(404, {'error':'not found'})
            except Exception as exc:
                self._send(500, {'error':str(exc)[:200]})

        def do_POST(self):
            if not self._require_auth(): return
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 65536:
                    self._send(413, {'error':'invalid body length'}); return
                data = json.loads(self.rfile.read(length) or b'{}')
                if path == '/api/refund':
                    purchase_id = str(data.get('purchase_id',''))
                    if not purchase_id:
                        self._send(400, {'error':'purchase_id required'}); return
                    refunded = store.refund_purchase(purchase_id,
                                                     data.get('reason') or 'admin refund')
                    self._send(200, {'refunded':refunded})
                elif path == '/api/resolve-delivery':
                    resolved = store.resolve_manual_delivery(
                        str(data.get('purchase_id','')), int(data.get('telegram_message_id',0)))
                    self._send(200, {'resolved':resolved})
                elif path == '/api/resource-price':
                    resource = store.set_resource_price(
                        str(data.get('resource_id','')), int(data.get('price_units',0)))
                    self._send(200, {'resource_id':resource['resource_id'],
                                     'price_units':resource['price_units']})
                else:
                    self._send(404, {'error':'not found'})
            except (ValueError, PaymentMismatch) as exc:
                self._send(400, {'error':str(exc)[:200]})
            except Exception as exc:
                self._send(500, {'error':str(exc)[:200]})

    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--db',default=os.environ.get('WALLET_DB',str(Path(__file__).parent/'wallet.sqlite3')))
    args=parser.parse_args()
    token=os.environ.get('ADMIN_TOKEN','')
    if len(token)<16:
        raise SystemExit('ADMIN_TOKEN must contain at least 16 characters')
    server=ThreadingHTTPServer((args.host,args.port),make_handler(WalletStore(args.db),token))
    print(f'Admin panel listening on http://{args.host}:{args.port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
