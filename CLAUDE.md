# CLAUDE.md

## 專案概述

**tw-law-sync** 是一個自動化資料同步工具，定期從台灣法務部全國法規資料庫（MOJ）下載法律與命令資料，轉換後推送至 Hugging Face Hub 作為公開資料集。

- **資料集目標**：`lianghsun/tw-law`（HuggingFace Datasets）
- **資料來源**：MOJ 開放 API（ZIP 格式，內含 JSON）
- **更新排程**：每週五 00:30（Asia/Taipei），搭配 `UpdateDate` 去重避免重複推送

---

## 專案結構

```
tw-law-sync/
├── sync_moj_law.py   # 主程式（下載、轉換、推送）
├── Dockerfile        # 容器定義（python:3.11-slim + cron）
├── entrypoint.sh     # Docker 啟動腳本
├── crontab           # cron 排程設定
├── requirements.txt  # Python 依賴
└── .gitignore
```

執行時會在 `/data`（容器內）產生以下目錄：

```
/data/
├── out/              # 每次同步的輸出目錄（每次清空重建）
│   ├── ch_law_articles/train.jsonl
│   ├── ch_law_full_text/train.jsonl
│   ├── ch_order_articles/train.jsonl
│   ├── ch_order_full_text/train.jsonl
│   ├── en_law_articles/train.jsonl
│   ├── en_law_full_text/train.jsonl
│   ├── en_order_articles/train.jsonl
│   ├── en_order_full_text/train.jsonl
│   ├── meta/last_update.json
│   └── README.md     # 自動生成的 HuggingFace dataset card
└── meta_cache/
    └── last_update.json  # 持久化的去重快取（容器重啟後仍保留）
```

---

## 資料流程

1. **下載**：對 4 個 MOJ API 端點各下載一個 ZIP 檔
   - `https://law.moj.gov.tw/api/ch/law/json`
   - `https://law.moj.gov.tw/api/ch/order/json`
   - `https://law.moj.gov.tw/api/en/law/json`
   - `https://law.moj.gov.tw/api/en/order/json`

2. **解壓**：從 ZIP 取出唯一的 `.json` 檔案（UTF-8-sig 解碼）

3. **去重**：比對 `UpdateDate` 與 `meta_cache/last_update.json`，若相同則跳過

4. **轉換**：每個端點產生兩種 subset：
   - `*_articles`：每條法條一筆，含 `heading_path`（章節層級路徑）
   - `*_full_text`：每部法規/命令一筆完整全文

5. **輸出**：寫入 JSONL 格式，並自動生成 HuggingFace dataset card（README.md）

6. **推送**：使用 `HfApi.upload_folder()` 上傳至 HuggingFace Hub

---

## 輸出格式

### `*_articles` 欄位

| 欄位 | 說明 |
|------|------|
| `text` | 格式：`【法規】名稱\n【章節】章/節\n【條號】第N條\n條文內容` |
| `article_no` | 條號（如「第一條」） |
| `article_type` | 通常為 `A`（一般條文），`C` 類型不輸出為獨立記錄 |
| `heading_path` | 章節層級路徑（list，最多 3 層） |
| `level` / `name` / `url` / `category` | 法規基本資訊 |
| `modified_date` / `effective_date` | 日期資訊 |
| `has_eng_version` / `eng_name` | 英文版資訊 |
| `update_date` / `doc_type` / `language` | 資料集元資料 |

### `*_full_text` 欄位

| 欄位 | 說明 |
|------|------|
| `text` | 完整法規文字（法規名稱 + 序文 + 所有條文） |
| 其餘欄位 | 同上（法規元資料） |

---

## 環境變數

| 變數 | 必要 | 說明 |
|------|------|------|
| `HF_TOKEN` 或 `HUGGINGFACE_HUB_TOKEN` | 推送時必要 | HuggingFace 存取 Token |

---

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

### Docker（cron 模式，每週五自動執行）

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

---

## CLI 參數

```
python sync_moj_law.py [--repo_id REPO_ID] [--workdir WORKDIR] [--push]
```

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--repo_id` | `lianghsun/tw-law` | HuggingFace 資料集 repo ID |
| `--workdir` | `/data` | 本地工作目錄 |
| `--push` | `False` | 加上此旗標才會推送至 HuggingFace |

---

## 關鍵設計決策

### 章節層級解析（`heading_path`）

`ArticleType = "C"` 的條文是章節標題（如「第一章 總則」），不輸出為獨立記錄，但用來更新 `heading_path`。中文版辨識「章/節/編/款/目」，英文版辨識「Chapter/Section/Part」。

### 去重機制

`UpdateDate` 存於 `/data/meta_cache/last_update.json`（持久化 volume），避免 MOJ 無更新時重複推送。使用四個來源的最大 `UpdateDate` 作為本次版本號。

### 排程設定

cron 設定為每週五 00:30（Asia/Taipei），MOJ 資料庫雖標示雙週更新，但因有去重機制，每週執行是安全的。

---

## 依賴

```
huggingface_hub==0.24.6   # HuggingFace Hub 上傳
requests==2.32.3           # HTTP 下載（timeout=180s）
python-dateutil==2.9.0     # Asia/Taipei 時區處理
```

Python 版本要求：**3.11+**（Dockerfile 使用 `python:3.11-slim`）

---

## 注意事項

- `/data` 目錄需掛載為持久化 volume，否則 `meta_cache` 每次容器重啟後會消失，導致重複推送
- `out/` 目錄每次執行時會完整清空重建
- HuggingFace Token 需有對應 repo 的寫入權限
- MOJ API 下載 timeout 設為 180 秒，檔案較大時請確保網路穩定
