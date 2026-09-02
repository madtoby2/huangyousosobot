# 黄油搜搜 SearchBot

一个 Telegram 搜索与付费下载 Bot，当前支持双域搜索和 OKPay 钱包充值：

- 🎮 **黄油搜索** — 成人游戏搜索（Ryuugames / OtomiGames），返回下载镜像直链按钮
- 🔍 **BT 搜索** — 磁力资源搜索（Sukebei / JavDB），返回磁力链接并支持一键复制
- 💰 **用户钱包** — 通过 OKPay 充值 USDT，Webhook 与主动查单双通道确认，幂等入账

## 架构

```text
bot.py                  # 主程序：双域路由、分页、详情卡片、钱包与充值交互
search_ryuugames.py     # Ryuugames 实时搜索、processing/AdShrink 最终下载地址解析
search_otomi.py         # OtomiGames 实时搜索
search_bt.py            # Sukebei BT 搜索 + JavDB 番号、封面和发行信息补充
translate.py            # 免费翻译（有道 jsonapi_s + MyMemory 兜底）
okaypay.py              # OKPay HMAC-SHA256 签名、创建支付链接、主动查单
wallet_store.py         # SQLite 用户余额、充值订单和追加式账本
webhook_server.py       # OKPay HTTPS 回调：验签、主动复查、幂等入账和到账通知
```

## 依赖

```bash
pip install python-dotenv python-telegram-bot requests beautifulsoup4 curl_cffi httpx
```

## 配置

创建 `.env`，不要把真实密钥提交到 Git：

```dotenv
BOT_TOKEN=<BotFather token>
OKPAY_SHOP_ID=<OKPay App ID>
OKPAY_API_KEY=<OKPay API key>
PUBLIC_BASE_URL=https://pay.example.com
WALLET_DB=/opt/searchbot/wallet.sqlite3
```

OKPay 回调地址：

```text
https://pay.example.com/api/okpay/notify
```

生产环境应通过 Caddy/Nginx 将该路径反向代理到：

```text
127.0.0.1:8765
```

## 运行

```bash
python bot.py
python webhook_server.py
```

## 部署（systemd）

```bash
sudo cp searchbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now searchbot
sudo systemctl enable --now searchbot-webhook
```

## 已完成功能

- 搜索结果纯文字列表，每页 8 条
- 点击结果后展示封面、简介和下载操作
- Ryuugames + Otomi 多源聚合及标题相关度过滤
- Ryuugames 广告短链二次解析，只保留真实网盘最终地址
- Sukebei 磁力搜索及 Telegram 原生复制按钮
- JavDB 精确番号匹配、封面和发行信息补充
- `/start` 钱包入口、`/wallet` 余额查询和 USDT 充值
- OKPay 请求/响应签名验证
- HTTPS Webhook 验签后再次主动查单
- Webhook 与轮询共用幂等入账函数，避免重复充值
- SQLite 余额缓存与追加式充值账本
- 到账 Telegram 通知

## 下一步需求：黄油付费下载

目标是在黄油详情页增加“余额购买并由 Bot 直接发送文件”，免费镜像下载继续保留。

计划流程：

```text
搜索黄油 → 查看详情 → 选择付费下载
→ 检查资源文件、价格和用户余额
→ 数据库事务内扣款并创建购买记录
→ Bot 直接发送游戏文件
→ 发送成功后记录 Telegram message_id
→ 终局发送失败时生成唯一退款流水，余额原路退回
```

实施要求：

- 为每个可售资源建立稳定 `resource_id`、文件路径、版本、价格和文件大小快照
- 购买扣款、购买记录和账本流水必须在同一数据库事务完成
- 余额不足不得创建扣款流水
- 文件交付在事务提交后执行，避免长时间占用数据库锁
- 发送失败只能通过追加 `refund` 流水退款，不能直接篡改余额
- 同一购买只能成功退款一次
- 扣款前验证文件存在、可读且大小符合 Telegram 实际上传通道限制
- 大文件优先使用本地 Telegram Bot API；超限资源应在扣款前拒绝或采用已验证的分卷方案
- 详情页保留免费网盘按钮，并新增价格明确的付费下载按钮
- 全流程采用 TDD，并进行真实 Telegram 文件交付 E2E

> ⚠️ 仅用于个人学习与技术验证，请遵守当地法律法规及相关平台规则。
