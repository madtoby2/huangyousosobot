# 黄油搜搜 SearchBot

一个 Telegram 搜索 Bot，双域搜索：

- 🎮 **黄油搜索**  — 成人游戏搜索（Ryuugames / OtomiGames），返回下载镜像直链按钮
- 🔍 **BT 搜索**   — 磁力资源搜索（Sukebei / JavDB），返回磁力链接可一键复制

## 架构

```
bot.py                  # 主程序（python-telegram-bot，双域路由 / 分页 / 详情卡片）
search_ryuugames.py     # Ryuugames 实时搜索（curl_cffi 过 CF + age_gate cookie + processing 按钮解析）
search_otomi.py         # OtomiGames 实时搜索（requests + bs4）
search_bt.py            # BT 搜索（sukebei 磁力直出 + javdb 番号补充）
translate.py            # 免费翻译（有道 jsonapi_s + MyMemory 兜底）
```

## 依赖

```bash
pip install python-dotenv python-telegram-bot requests beautifulsoup4 curl_cffi
```

## 配置

创建 `.env`（从 `.env.example` 复制）：

```
BOT_TOKEN=<BotFather 生成的 token>
```

## 运行

```bash
python bot.py
```

## 部署（systemd）

```bash
sudo cp searchbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now searchbot
```

## 功能说明

- 搜索结果列表（纯文字，每页 8 条，翻页）
- 点击结果下钻详情（带封面缩略图 + 中文简介 + 下载/磁力按钮）
- 黄油搜索：Ryuugames + Otomi 多源聚合，标题相关度过滤
- BT 搜索：Sukebei 磁力直出 + JavDB 补充
- 免费翻译：英文描述 → 中文（有道 / MyMemory）

> ⚠️ 仅用于个人学习与技术验证，请遵守当地法律法规。
