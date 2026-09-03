# 黄油搜搜 SearchBot

Telegram 搜索与付费下载 Bot：

- 🎮 黄油搜索：Ryuugames / OtomiGames
- 🔍 BT 搜索：Sukebei / JavDB，磁力复制与番号封面
- 💰 OKPay USDT 钱包充值
- ⚡ 余额付费后按需下载、频道缓存、Bot 交付
- 🖥️ 本地管理面板

## 交互原则

搜索列表保持纯文字；只有点击某条结果下钻后才显示封面、简介和下载操作。
付费下载不是预先囤文件：用户确认并扣余额后才创建下载任务。

```text
查看游戏详情
→ 点击“付费下载”
→ 原子扣余额并创建购买快照
→ 创建或加入该资源的共享下载任务
→ Bot 直接解析 MediaFire / PixelDrain 并下载
→ 使用对应源站密码解压，清理推广文件并重包为无密码 ZIP
→ 验证 ZIP 无加密、CRC 正常
→ 小号上传私有仓库频道
→ Bot copyMessage 给所有等待买家
→ 保存频道消息作为后续缓存
```

同一资源多人同时购买只下载、上传一次。每个下载任务使用带租约（lease）的独立临时目录，崩溃重启后只有过期租约才会被重新排队。下载、上传或单个用户交付失败时使用追加式退款流水，不能直接篡改余额。

交付状态机：`copyMessage` 明确被拒绝（如用户屏蔽 Bot）时自动退款一次；网络超时等结果不确定时进入 `manual_review` 待人工核对状态，不自动退款也不重复扣款，管理员可在面板核对后标记已交付或人工退款。

## 文件结构

```text
bot.py                 Telegram 搜索、钱包、付费按钮和后台 worker
search_ryuugames.py    Ryuugames 搜索及广告短链最终地址解析
search_otomi.py        OtomiGames 搜索
search_bt.py           BT 搜索与 JavDB 元数据补充
translate.py           简介翻译
okaypay.py             OKPay 签名、支付链接和主动查单
webhook_server.py      OKPay 回调验签、复查和幂等入账
wallet_store.py        SQLite 余额、账本、购买、下载任务和频道缓存
downloader.py          MediaFire / PixelDrain 直接下载、大小与磁盘校验
uploader.py            Telethon 小号上传私有频道
delivery.py            Bot copyMessage 交付及失败退款
pipeline.py            下载一次、上传一次、批量交付
artifacts.py           稳定 resource_id
admin_panel.py         零依赖本地管理面板
```

## 配置

复制 `.env.example` 为 `.env`，真实密钥和 session 不得提交 Git。

```dotenv
BOT_TOKEN=<BotFather token>
OKPAY_SHOP_ID=<OKPay App ID>
OKPAY_API_KEY=<OKPay API key>
PUBLIC_BASE_URL=https://pay.example.com
WALLET_DB=/opt/searchbot/wallet.sqlite3

GAME_PRICE_UNITS=100000000
DOWNLOAD_DIR=/opt/searchbot/downloads
STORAGE_CHANNEL_ID=-1001234567890
UPLOADER_SESSION=/opt/searchbot/uploader.session
ADMIN_TOKEN=<至少16字符的随机密码>
```

`GAME_PRICE_UNITS` 使用 USDT 的 8 位最小单位：`100000000` 表示 `1 USDT`。

## 支持的付费下载源

当前自动选源顺序：

1. MediaFire
2. PixelDrain

两者已用真实 Ryuugames 结果验证 Range 下载。Mega、Datanodes、Terabox 等尚未接入时不会作为付费源，也不会在不支持的情况下扣款。项目不依赖 JDownloader。

下载保护：

- 仅允许明确支持的公网域名，每一跳重定向与最终地址都拒绝内网/回环/链路本地地址
- DNS 解析结果先校验为公网地址再固定到连接层（requests 与 curl_cffi 均逐跳校验、IP 固定，消除 DNS rebinding 窗口）
- 下载前检查 Content-Length、最大文件限制和磁盘余量；无长度时按最大上限预留
- 流式下载中持续检查磁盘余量达到保留阈值即中止
- 流式下载到 `.part`，完成后原子改名
- 校验实际字节数并计算 SHA-256
- RAR/ZIP/7z 必须先成功解压并重包为无密码 ZIP；失败时禁止上传原始加密包
- 删除 `.url/.webloc/.desktop/.website` 及文件名含 `ryuu` 的推广文件
- 重包后验证无加密标记及完整 CRC，再重新计算大小与 SHA-256
- 上传频道后删除宿主机临时文件

## 支付和账本

- OKPay Webhook 不直接入账，必须验签后主动查单
- 金额全程使用字符串或整数最小单位，不使用浮点数
- 充值、购买和退款均写入追加式 `balance_ledger`
- 用户重复点击不重复扣款
- 同一资源只存在一个活跃下载任务
- 下载失败时给所有等待买家各退款一次
- `copyMessage` 失败只退款该买家，不破坏已完成频道缓存
- 结果不确定的交付进入待人工核对，不自动退款
- 资源身份包含版本与下载地址，页面改版不会让买家收到旧缓存文件

## 管理面板

面板默认只监听本机：

```bash
ADMIN_TOKEN='your-random-password' \
  ./venv/bin/python admin_panel.py --host 127.0.0.1 --port 8780
```

浏览器访问 `http://127.0.0.1:8780`，HTTP Basic Auth 用户名为 `admin`，密码为 `ADMIN_TOKEN`。

面板包含：

- 用户与余额
- 资源缓存状态
- 下载任务
- 购买、交付和退款
- 待交付订单人工退款（仍使用唯一追加式退款流水）
- 待人工核对订单：核对后标记已交付或确认未交付并退款

生产部署模板：`searchbot-admin.service.example`。需要公网访问时应放在 HTTPS 反向代理和额外访问控制之后，不应直接监听公网地址。

## 运行

```bash
./venv/bin/python bot.py
./venv/bin/python webhook_server.py
./venv/bin/python admin_panel.py --host 127.0.0.1 --port 8780
```

Bot 只有在 `STORAGE_CHANNEL_ID`、已授权的小号 session 和上传认证状态全部存在时才启用付费下载 worker；否则付费按钮点击会明确提示未就绪，并且不会扣款。

## 测试

```bash
PYTHONWARNINGS=error::ResourceWarning ./venv/bin/python -m unittest discover -v
```

覆盖 OKPay、钱包、共享下载任务、重复点击、余额不足、批量退款、直接下载、频道上传、Bot 复制交付、队列重启持久化和管理面板鉴权。

## 真实 E2E 验证

2026-09-03 已完成生产链路验证：

1. 小号 session 已授权并创建私有仓库频道
2. Bot 已加入仓库频道并设置为管理员
3. 真实上传 55,712,342 字节 RAR，频道消息与 SHA-256 已入库
4. `copyMessage` 已实际交付到买家账号并记录买家侧消息 ID
5. 1 USDT 测试账本完整走通：测试入账 → 原子扣款 → 下载 → 上传 → 交付，最终余额归零
6. 下载临时目录已清空；失败退款、租约过期重排和不确定交付重启恢复均有定向回归测试

> 仅用于个人学习与技术验证，请遵守当地法律法规及相关平台规则。
