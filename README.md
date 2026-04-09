# Hyperliquid Maker Bot

Hyperliquid Perp 向けの **maker 中心** 板置き bot + **ローカル Web ダッシュボード**。

## このbotについて

**目的は利益最大化ではなく、安全に少額検証できる状態を作ること。**

Hyperliquid 上で bid/ask 両側に小さなポジションを置き、スプレッドを稼ぐ maker 戦略です。
在庫の偏りに応じてクオート価格を傾け、リスクが高まったら自動で停止します。

- v1 対象: **Perp のみ** (BTC, ETH)
- ネットワーク: **testnet での少額検証前提**
- レバレッジ: **初期値 1x**
- 注文方式: **ALO (post-only 相当) のみ**。通常時は taker を使わない
- ダッシュボード: ローカル限定の read-only Web UI (127.0.0.1:8080)

---

## 優先順位

```
破産回避 > 誤発注回避 > 異常時停止 > 少額検証しやすさ > 保守性 > maker優位性
```

---

## 戦略の概要

- 対象: Hyperliquid Perp (v1は BTC, ETH のみ)
- 通常新規注文は **ALO (post-only 相当) のみ**。taker 注文は通常時に使わない
- 中値 (mid) を基準に bid/ask を両側に置く
- 在庫偏りに応じて **inventory skew** をかける
  - ロング寄り → ask を引き気味 (売りを誘発)、bid を遠ざける
  - ショート寄り → bid を引き気味 (買いを誘発)、ask を遠ざける
- 条件が悪い時は **no-quote** を返す (エラーではなく正常動作)
- 緊急時のみ reduce-only + IOC で emergency flatten

---

## やらないこと

- 成行連打
- ナンピン / マーチンゲール
- 無限グリッド
- 高レバレッジ (初期値 1x)
- 通常時のtaker注文
- 未実現益を余力として使うこと

---

## セットアップ

### 必要環境

- Python 3.11+
- インターネット接続 (Hyperliquid API)

### インストール

```bash
git clone <this-repo>
cd hyperliquid-bot

# 依存パッケージインストール
pip install hyperliquid-python-sdk fastapi "uvicorn[standard]" \
    jinja2 "pydantic>=2.7" "pydantic-settings>=2.3" \
    aiofiles python-dotenv

# 開発用 (テスト含む)
pip install pytest pytest-asyncio httpx pytest-cov
```

### 環境確認

```bash
bash scripts/check_env.sh
```

---

## .env の設定

```bash
cp .env.example .env
```

`.env` を編集:

```env
# 必須: Hyperliquidウォレット情報
PRIVATE_KEY=0xyour_private_key_here
WALLET_ADDRESS=0xyour_wallet_address_here

# テストネット (本番前は必ず true のまま)
TESTNET=true

# 取引対象シンボル
SYMBOLS=BTC,ETH

# リスク上限 (USD) — 少額から始める
MAX_ORDER_SIZE_USD=25
MAX_POSITION_USD_PER_SYMBOL=50
MAX_TOTAL_EXPOSURE_USD=100

# 損失上限
MAX_DAILY_LOSS_PCT=2.0
MAX_INTRADAY_DRAWDOWN_PCT=1.0
```

> **警告**: `.env` は絶対に git にコミットしないこと。`.gitignore` に含まれています。

---

## testnet での起動

```bash
# 環境確認
bash scripts/check_env.sh

# bot 起動 (testnet)
bash scripts/start_bot.sh

# または直接
python -m app.main
```

起動時に以下を確認します:

1. `.env` バリデーション (失敗すると即終了)
2. Hyperliquid API 接続
3. asset メタデータ取得 (tick size, sz decimals)
4. leverage 設定
5. 初期ユーザー状態取得 (PnL ベースライン)

いずれかが失敗すると起動を拒否します。

---

## Web ダッシュボードの起動

bot 起動時に `DASHBOARD_ENABLED=true` の場合、自動的にダッシュボードが起動します。

```
http://127.0.0.1:8080
```

### ダッシュボードで確認できる内容

| セクション | 内容 |
|-----------|------|
| **Overview** | bot状態、WS接続、kill switch、今日のPnL |
| **Symbols** | mid価格、spread、imbalance、vol、stale判定 |
| **Orders** | open orders、CLOID、side、価格、サイズ、状態 |
| **Fills** | 直近フィル、価格、サイズ、fee、maker判定 |
| **Positions** | symbol別ポジション、平均建値、在庫skew状態 |
| **PnL** | realized/unrealized/fees/日次損益/損失上限進捗 |
| **Risk** | 全リスク指標の使用率、kill switch状態 |
| **Events** | ボットイベント・エラーの時系列 |

2秒ごとに自動更新されます。

### ダッシュボードで実行できる操作

- **Graceful Stop**: 現在サイクル完了後に停止 (注文キャンセル)
- **Emergency Kill**: 即時停止 + ポジションflatten試行

---

## ダッシュボードを外部公開しない

> ⚠️ **重要な安全注意事項**

デフォルトは `127.0.0.1` (ローカルのみ) にバインドされています。

`DASHBOARD_HOST=0.0.0.0` に変更すると外部からアクセス可能になります。
これを行う場合は:

- 信頼できるネットワーク上でのみ
- ファイアウォールで適切にブロック
- 認証は実装されていないため、外部公開は自己責任

外部バインド時は起動時に `WARNING` ログが出力されます。

---

## ログの見方

```
logs/
  bot.log      — 人間が読む形式 (INFO以上)
  bot.jsonl    — 構造化JSON (DEBUG以上、分析用)
```

重要なログパターン:

```
KILL SWITCH ACTIVATED: reason=daily_loss_exceeded message=...
  → 取引停止。reason で停止理由を確認

Market data marked stale for BTC: ...
  → 市場データが途絶えた。WSが切断されているか確認

Order rejected cloid=0x...: ...
  → 注文拒否。理由を確認してパラメータを見直す
```

---

## 停止条件 (kill switch)

以下のいずれかで自動停止します:

| 条件 | デフォルト閾値 |
|------|--------------|
| 日次損失超過 | 2% (MAX_DAILY_LOSS_PCT) |
| 日中ドローダウン超過 | 1% (MAX_INTRADAY_DRAWDOWN_PCT) |
| 市場データ途絶 | 5秒 (STALE_DATA_THRESHOLD_MS) |
| WS再接続多発 | 60秒以内に5回 |
| 連続注文拒否 | 10回連続 |
| 異常スプレッド | 基準値の5倍超 |
| 急激な価格変動 | 短時間で1%超 |
| 板データ破損 | bid >= ask |
| 手動停止 | ダッシュボード / シグナル |

停止後、`emergency_flatten_enabled=true` かつ手動停止・損失上限超過の場合は reduce-only IOC でポジションをflattenします。

---

## よくある失敗

### `Configuration error: PRIVATE_KEY looks like a placeholder`
`.env` の `PRIVATE_KEY` が `0xyour_private_key_here` のまま。実際の秘密鍵に変更してください。

### `Failed to connect to exchange: ...`
ネットワーク接続またはエンドポイントを確認してください。`TESTNET=true` の場合は testnet に接続します。

### `No asset specs for BTC. Was fetch_meta() called?`
起動時の `fetch_meta()` が失敗しています。APIエンドポイントとネットワークを確認してください。

### Kill switch が頻繁に発動する
- `STALE_DATA_THRESHOLD_MS` を緩めるか、WS接続の安定性を確認
- `BASE_SPREAD_BPS` が市場の実際のスプレッドより小さすぎる可能性

### 注文が通らない (全部 no-quote になる)
- 市場データが stale になっていないか確認 (ダッシュボード > Symbols)
- `MIN_EDGE_BPS` が厳しすぎる可能性

---

## パラメータ説明

| 変数 | 説明 | 推奨開始値 |
|------|------|-----------|
| `MAX_ORDER_SIZE_USD` | 1注文の最大USD額 | 25 |
| `MAX_POSITION_USD_PER_SYMBOL` | symbol別最大ポジション | 50 |
| `MAX_TOTAL_EXPOSURE_USD` | 全symbol合計の最大エクスポージャ | 100 |
| `MAX_DAILY_LOSS_PCT` | 日次損失上限 (%) | 2.0 |
| `MAX_INTRADAY_DRAWDOWN_PCT` | 日中ドローダウン上限 (%) | 1.0 |
| `BASE_SPREAD_BPS` | 基本スプレッド (bps) | 10 |
| `MIN_EDGE_BPS` | クオートに必要な最低エッジ (bps) | 5 |
| `VOL_MULTIPLIER` | ボラティリティによるスプレッド拡張係数 | 2.0 |
| `INVENTORY_SKEW_MAX_BPS` | 在庫skewの最大値 (bps) | 20 |
| `QUOTE_REFRESH_MS` | クオート更新間隔 (ms) | 5000 |
| `MAX_QUOTE_AGE_MS` | クオートの最大寿命 (ms) | 30000 |
| `PRICE_REPLACE_THRESHOLD_BPS` | 価格変化がこれ以上でreplace (bps) | 2.0 |
| `STALE_DATA_THRESHOLD_MS` | この時間データがないとstale扱い (ms) | 5000 |
| `ABNORMAL_SPREAD_MULTIPLIER` | 基準の何倍で異常スプレッド扱い | 5.0 |
| `EMERGENCY_FLATTEN_ENABLED` | kill switch時にflattenするか | true |

---

## 少額検証の進め方

### Step 1: testnet で動作確認
1. `TESTNET=true` で起動
2. ダッシュボードで市場データが更新されているか確認
3. クオートが出ているか確認 (Symbols セクション)
4. フィルが入るか確認 (Fills セクション)

### Step 2: mainnet 少額テスト
mainnet 前のチェックリストを完了してから実施すること。

1. `MAX_ORDER_SIZE_USD=10` など最小額から
2. `MAX_POSITION_USD_PER_SYMBOL=20`
3. `MAX_TOTAL_EXPOSURE_USD=40`
4. 毎日 PnL を確認

### Step 3: パラメータ調整
少なくとも数日のデータを見てから調整すること。

---

## mainnet 前チェックリスト

以下を全て確認してから `TESTNET=false` に変更すること。

- [ ] `TESTNET=true` で最低24時間の連続稼働を確認
- [ ] kill switch が正常に発動することを確認 (手動テスト)
- [ ] ダッシュボードで fills / positions が正しく表示されること
- [ ] ログに異常なエラーがないこと
- [ ] `PRIVATE_KEY` と `WALLET_ADDRESS` が正しいこと
- [ ] mainnet ウォレットに少額のみ入金していること
- [ ] `MAX_DAILY_LOSS_PCT` と `MAX_INTRADAY_DRAWDOWN_PCT` を確認
- [ ] `EMERGENCY_FLATTEN_ENABLED=true` であること
- [ ] ダッシュボードが `127.0.0.1` にバインドされていること
- [ ] `.env` が git にコミットされていないこと

---

## 手動停止 / manual kill

### Graceful stop (推奨)
```bash
# ダッシュボード > "Graceful Stop" ボタン
# または Ctrl+C (SIGINT)
```

現在のサイクルを完了してから全注文をキャンセルして停止します。

### Emergency kill
```bash
# ダッシュボード > "Emergency Kill" ボタン (確認ダイアログあり)
```

即時停止 + ポジション flatten を試行します。

---

## 安全上の注意

1. **秘密鍵を共有・公開しないこと** — ログにも表示されません
2. **testnet で十分に検証してからmainnetへ** — 資産を失うリスクがあります
3. **ダッシュボードを外部公開しないこと** — 認証がありません
4. **少額から始めること** — パラメータが意図通りに動くか確認してから増やす
5. **未実現益を頼らないこと** — bot はリスク計算に unrealized PnL を含みません
6. **kill switch が発動したら原因を調査してから再起動すること**

---

## アーキテクチャ概要

```
asyncio event loop (bot core)
  ├── ws_client        — WebSocket 接続・再接続
  ├── market_data      — mid/spread/vol/stale計算
  ├── quote_engine     — bid/ask算出・no-quote判定
  ├── order_manager    — CLOID管理・状態機械
  ├── inventory_manager — symbol別在庫・skew
  ├── risk_manager     — 全注文前チェック
  ├── kill_switch      — 発動条件監視
  ├── pnl_manager      — realized/unrealized/fees
  └── persistence      — SQLite (fills/orders/events)

exchange/ adapter layer  ← UNVERIFIED HL SDK仕様を隔離
  ├── client.py        — Exchange/Info ラッパー
  ├── ws_client.py     — WS 接続管理
  └── normalizer.py    — 生データ → 内部モデル変換

Dashboard (別スレッド, FastAPI)
  └── 127.0.0.1:8080  — read-only + graceful stop/kill
```

---

## 未確認事項 (UNVERIFIED)

以下の仕様は公式ドキュメント・SDK調査に基づく仮定であり、
実際の動作で確認が必要です:

- WebSocket メッセージの正確な shape (`exchange/normalizer.py`)
- `userEvents` で fill と order update がどのように届くか (`main.py`)
- `cancel_by_cloid` の引数形式 (SDK 0.22.0)
- IOC flatten の価格指定方法 (現在 mid±5% で代用)
- `update_leverage` のメソッド名とシグネチャ

これらは全て `app/exchange/` adapter 層に隔離されており、
修正が必要な場合はそこだけ変更すれば bot 本体への影響は最小限です。

---

## テストの実行

```bash
# 全ユニットテスト (network不要)
python3 -m pytest tests/ -m "not integration" -q

# カバレッジ付き
python3 -m pytest tests/ -m "not integration" --cov=app --cov-report=term-missing

# integration テスト (testnet接続が必要)
python3 -m pytest tests/ -m "integration" -v

# 特定ファイルのみ
python3 -m pytest tests/test_quote_engine.py -v
python3 -m pytest tests/test_risk_manager.py -v
```

**現在のテスト状況**: 142 passed, 2 skipped (integration)

| テストファイル | 内容 |
|--------------|------|
| `test_settings.py` | config バリデーション・placeholder拒否 |
| `test_models.py` | (CLOID等の基本型) |
| `test_state.py` | BotState・kill switch・snapshot隔離 |
| `test_telemetry.py` | SecretFilter・秘密情報ログ非出力 |
| `test_market_data.py` | 板データ処理・stale検知 |
| `test_quote_engine.py` | quote生成・skew・tick丸め・no-quote |
| `test_order_manager.py` | 注文ライフサイクル・partial fill |
| `test_inventory_manager.py` | 在庫追跡・skew算出・限度判定 |
| `test_risk_manager.py` | 全リスクチェック・emergency bypass |
| `test_kill_switch.py` | kill switch発動・flatten条件 |
| `test_pnl_manager.py` | realized PnL・fee追跡 |
| `test_persistence.py` | SQLite CRUD |
| `test_api.py` | dashboard API・秘密情報非含有 |
| `test_reconnect_recovery.py` | 再接続嵐検知・状態回復設計 |
| `test_edge_cases.py` | 安全機構の境界条件 |

### 未テスト項目 (integration / network依存)

| 項目 | 理由 |
|------|------|
| WS実際の接続・切断・再接続 | live testnet接続が必要 |
| 注文送信と取引所レスポンスの実パース | live API必要 |
| emergency flatten の実約定確認 | live API必要 |
| ダッシュボード + bot 同時稼働 E2E | uvicorn/asyncio E2Eテスト相当 |

---

## 開発者向けメモ

### UNVERIFIED 仕様の扱い方

`app/exchange/` adapter 層のコードに以下のタグでコメントが付いています:

```python
# FACT:        公式仕様で確認済み
# UNVERIFIED:  未確認。実動作で要確認
# ASSUMPTION:  合理的な仮定。違ったら adapter 層だけ修正
# TODO:        実装が必要
```

UNVERIFIED 箇所を修正する際は `app/exchange/client.py` と `app/exchange/normalizer.py` のみ変更すれば bot 本体に影響しません。

### ディレクトリ構成

```
app/
  exchange/        ← HL SDK依存。UNVERIFIED仕様をここに隔離
  api/             ← ダッシュボード。bot本体に影響を与えない
  main.py          ← エントリポイント
  settings.py      ← 全設定。起動拒否判定
  state.py         ← asyncio.Lock付き共有状態
  ...

templates/         ← Jinja2 HTML
static/            ← JS/CSS (no framework)
tests/             ← 142テスト
data/              ← SQLite DB (.gitignore)
logs/              ← ログファイル (.gitignore)
```

---

## ライセンス

自己責任でご使用ください。取引による損失について作者は責任を負いません。
