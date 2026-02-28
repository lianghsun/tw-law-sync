# tw-law-sync

自動從台灣法務部[全國法規資料庫](https://law.moj.gov.tw/)下載法律與命令資料，轉換後推送至 [Hugging Face Hub](https://huggingface.co/datasets/lianghsun/tw-law) 的同步工具。

## 資料集

**[lianghsun/tw-law](https://huggingface.co/datasets/lianghsun/tw-law)** — 共 8 個 config：

| Config | 說明 |
|--------|------|
| `ch_law_articles` | 中文法律，每條文一筆 |
| `ch_law_full_text` | 中文法律，每部法律一筆全文 |
| `ch_order_articles` | 中文命令，每條文一筆 |
| `ch_order_full_text` | 中文命令，每部命令一筆全文 |
| `en_law_articles` | 英文法律，每條文一筆 |
| `en_law_full_text` | 英文法律，每部法律一筆全文 |
| `en_order_articles` | 英文命令，每條文一筆 |
| `en_order_full_text` | 英文命令，每部命令一筆全文 |

## 資料流程

```
MOJ API (ZIP/JSON)
    ↓ 下載 × 4 個端點
    ↓ 解壓、UTF-8-sig 解碼
    ↓ 比對 UpdateDate（去重）
    ↓ 轉換為 *_articles / *_full_text 兩種格式
    ↓ 寫入 JSONL + 自動生成 dataset card
    ↓ 推送至 Hugging Face Hub
```

**資料來源端點：**

- `https://law.moj.gov.tw/api/ch/law/json`
- `https://law.moj.gov.tw/api/ch/order/json`
- `https://law.moj.gov.tw/api/en/law/json`
- `https://law.moj.gov.tw/api/en/order/json`

## 輸出欄位

### `*_articles`

| 欄位 | 說明 |
|------|------|
| `text` | `【法規】名稱\n【章節】章/節\n【條號】第N條\n條文內容` |
| `article_no` | 條號（如「第一條」） |
| `article_type` | 通常為 `A`（`C` 為章節標題，不輸出為獨立記錄） |
| `heading_path` | 章節層級路徑（list，最多 3 層） |
| `level` / `name` / `url` / `category` | 法規基本資訊 |
| `modified_date` / `effective_date` | 日期資訊 |
| `has_eng_version` / `eng_name` | 英文版資訊 |
| `update_date` / `doc_type` / `language` | 資料集元資料 |

### `*_full_text`

`text` 為完整法規文字（法規名稱 + 序文 + 所有條文），其餘欄位同上。

## 執行方式

### 本地開發（不推送）

```bash
pip install -r requirements.txt
python sync_moj_law.py --workdir ./data
```

### 本地執行並推送

```bash
export HF_TOKEN=hf_xxxxx
python sync_moj_law.py --repo_id lianghsun/tw-law --workdir ./data --push
```

### CLI 參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--repo_id` | `lianghsun/tw-law` | Hugging Face 資料集 repo ID |
| `--workdir` | `/data` | 本地工作目錄 |
| `--push` | `False` | 加上此旗標才會推送 |

### Docker（cron 模式，每週五 00:30 Asia/Taipei 自動執行）

```bash
docker build -t tw-law-sync .

docker run -d \
  -e HF_TOKEN=hf_xxxxx \
  -v tw-law-data:/data \
  tw-law-sync
```

### Docker（立即執行一次）

```bash
docker run --rm \
  -e HF_TOKEN=hf_xxxxx \
  -v tw-law-data:/data \
  tw-law-sync --once
```

> `/data` 需掛載為持久化 volume，否則重啟後去重快取消失，會導致重複推送。

## GitHub Actions

`.github/workflows/daily-sync.yml` 每天 UTC 16:30（= 台北時間 00:30）觸發，並支援手動執行（`workflow_dispatch`）。

去重快取（`data/meta_cache/`）以 `actions/cache` 跨 run 保存，MOJ 無更新時會自動跳過推送。

**需設定的 Secret：**

| Secret | 說明 |
|--------|------|
| `HF_TOKEN` | Hugging Face 存取 Token（需有 `lianghsun/tw-law` 寫入權限） |

## 專案結構

```
tw-law-sync/
├── sync_moj_law.py          # 主程式
├── Dockerfile               # python:3.11-slim + cron
├── entrypoint.sh            # Docker 啟動腳本
├── crontab                  # cron 排程設定
├── requirements.txt
└── .github/
    └── workflows/
        └── daily-sync.yml   # GitHub Actions 排程
```

執行時產生（容器內 `/data`）：

```
/data/
├── out/                     # 每次清空重建
│   ├── ch_law_articles/train.jsonl
│   ├── ch_law_full_text/train.jsonl
│   ├── ...（共 8 個 config）
│   ├── meta/last_update.json
│   └── README.md            # 自動生成的 dataset card
└── meta_cache/
    └── last_update.json     # 持久化去重快取
```

## 依賴

```
huggingface_hub==0.24.6
requests==2.32.3
python-dateutil==2.9.0
```

Python 3.11+
