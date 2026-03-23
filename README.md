# 🏇 競馬オッズ急変検知 Bot

JRA中央競馬のオッズ急変（急騰・急落）をリアルタイムで検知し、Discordに通知するBotです。

![Discord](https://img.shields.io/badge/Discord-Bot-5865F2?logo=discord&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

## 特徴

- **リアルタイム検知** — 発走10分前から15秒間隔でオッズを監視
- **オッズ帯別閾値** — 低オッズ(~5倍)/中オッズ(~20倍)/高オッズ(20倍~)で閾値を自動調整
- **急騰・急落の両方を検知** — 大口買い(急落)も人気離散(急騰)もキャッチ
- **完全自動** — レース開催日の9:00〜17:00に自動で監視開始・終了
- **マルチサーバー対応** — 複数のDiscordサーバーで同時利用可能

## 通知イメージ

```
⚡ オッズ急変検知 14:16:19

🔺 急騰　阪神9R [01番] コリカンチャ　17.8 → 23.3 (+30.9%)
🔺 急騰　阪神9R [04番] ライフセービング　31.2 → 47.4 (+51.9%)
🔺 急騰　阪神9R [07番] ハイラント　24.7 → 40.5 (+64.0%)
🔻 急落　阪神9R [08番] サトノソティラス　9.1 → 5.7 (-37.4%)
```

## Discordサーバーに追加する

**無料で使えます。** 以下のリンクからBotをサーバーに追加してください。

**[Botを追加する](https://discord.com/oauth2/authorize?client_id=1484569596198518885&permissions=83968&integration_type=0&scope=bot)**

### 公開Discordサーバー

みんなでオッズ急変を監視するサーバーもあります。お気軽にどうぞ。

**[Discordサーバーに参加する](https://discord.gg/dpj2Vts2)**

## コマンド

| コマンド | 説明 |
|---|---|
| `!help` | コマンド一覧を表示 |
| `!status` | 監視状況を表示（開催場・レース数・アクティブ数） |
| `!odds 阪神 1` | 指定レースの単勝オッズを表示 |
| `!threshold` | 現在の閾値を確認 |
| `!threshold 15 20 25` | 閾値を変更（低/中/高オッズ帯の%） |
| `!setchannel` | アラート送信先チャンネルを変更 |
| `!refresh` | レース一覧を手動で再取得 |
| `!invite` | Bot招待リンクを表示 |

## 監視仕様

| 項目 | 値 |
|---|---|
| 監視時間 | 9:00〜17:00（自動） |
| 対象 | JRA中央競馬 全レース |
| 発走60〜10分前 | 約60秒間隔で監視 |
| 発走10分前〜発走 | **15秒間隔**で高頻度監視 |
| 発走後5分 | 監視終了 |
| デフォルト閾値 | 低オッズ15% / 中オッズ20% / 高オッズ25% |
| ノイズ除去 | 絶対変動幅1.0倍未満は無視 |

## セルフホスト

自分のサーバーで動かしたい場合：

### 必要なもの

- Python 3.11+
- Discord Bot Token（[Discord Developer Portal](https://discord.com/developers/applications)で取得）

### セットアップ

```bash
git clone https://github.com/kyui91/keiba-odds-bot.git
cd keiba-odds-bot
pip install -r requirements.txt
playwright install chromium
```

### 環境変数

`.env` ファイルを作成：

```env
DISCORD_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=your_channel_id
```

### 起動

```bash
python main.py
```

### Docker

```bash
docker build -t keiba-bot .
docker run -d --restart=always --env-file .env --name keiba-bot keiba-bot
```

## 技術スタック

- **Python** + **discord.py** — Bot本体
- **Playwright** — netkeibaからオッズ取得（JS実行後のDOM解析）
- **cloudscraper** — レース一覧取得
- **BeautifulSoup** — HTMLパース

## ライセンス

MIT

## 作者

[@kyui91](https://github.com/kyui91)
