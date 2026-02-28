import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dateutil import tz
from huggingface_hub import HfApi


ENDPOINTS = {
    ("ch", "law"): "https://law.moj.gov.tw/api/ch/law/json",
    ("ch", "order"): "https://law.moj.gov.tw/api/ch/order/json",
    ("en", "law"): "https://law.moj.gov.tw/api/en/law/json",
    ("en", "order"): "https://law.moj.gov.tw/api/en/order/json",
}

# 你要的 repo
DEFAULT_REPO_ID = "lianghsun/tw-law"


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def taipei_now_iso() -> str:
    tpe = tz.gettz("Asia/Taipei")
    return datetime.now(tpe).strftime("%Y-%m-%dT%H:%M:%S%z")


def download_zip(url: str, timeout: int = 180, retries: int = 3) -> bytes:
    import time
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            content = r.content
            # Verify it's actually a ZIP before returning
            if not content[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
                raise ValueError(
                    f"Response is not a ZIP file (starts with {content[:4]!r}, HTTP {r.status_code})")
            return content
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"  Attempt {attempt}/{retries} failed: {e}. Retrying in {wait}s...", flush=True)
                time.sleep(wait)
    raise last_exc


def extract_single_json_from_zip(zip_bytes: bytes) -> Tuple[str, bytes]:
    """
    解壓後只取唯一 .json 檔。你的假設：ChLaw.json / ChOrder.json / EngLaw.json... 之類都只有一個 json。
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if len(json_names) != 1:
            raise RuntimeError(
                f"Expected exactly 1 json in zip, got {len(json_names)}: {json_names[:10]}")
        name = json_names[0]
        return name, zf.read(name)


def to_bool_y_n(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        vv = v.strip().upper()
        if vv == "Y":
            return True
        if vv == "N":
            return False
    return None


def map_meta_fields(raw_law_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    依你的規格：去掉 Law 前綴、後面小寫+底線。
    這裡只映射你列的那些欄位；其他欄位不亂放，保持乾淨。
    """
    out = {
        "level": raw_law_obj.get("LawLevel", ""),
        "name": raw_law_obj.get("LawName", ""),
        "url": raw_law_obj.get("LawURL", ""),
        "category": raw_law_obj.get("LawCategory", ""),
        "modified_date": raw_law_obj.get("LawModifiedDate", ""),
        "effective_date": raw_law_obj.get("LawEffectiveDate", ""),
        "effective_note": raw_law_obj.get("LawEffectiveNote", ""),
        "abandon_note": raw_law_obj.get("LawAbandonNote", ""),
        "has_eng_version": to_bool_y_n(raw_law_obj.get("LawHasEngVersion", "")),
        "eng_name": raw_law_obj.get("EngLawName", ""),
        # 原資料拼字常見是 Attachements（你貼的也是），這邊兩個都接
        "attachments": raw_law_obj.get("LawAttachements", raw_law_obj.get("LawAttachments", [])) or [],
        "histories": raw_law_obj.get("LawHistories", ""),
    }
    return out


def normalize_heading_text(s: str) -> str:
    s = (s or "").replace("\r\n", "\n")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s


def update_heading_path(lang: str, heading_path: List[str], c_text: str) -> List[str]:
    """
    做法 A：遇到 ArticleType=C 只更新 heading_path。
    這裡用非常保守的 heuristic：偵測章/節/編/款（中），或 Chapter/Section/Part（英）。
    """
    t = normalize_heading_text(c_text)
    if not t:
        return heading_path

    if lang == "ch":
        # 章：重置
        if "章" in t and t.startswith("第"):
            return [t]
        # 編：也視為大層級，重置
        if "編" in t and t.startswith("第"):
            return [t]
        # 節：在章之下
        if "節" in t and t.startswith("第"):
            if heading_path:
                return [heading_path[0], t]
            return [t]
        # 款/目：更細（保留到第 3 層）
        if any(x in t for x in ["款", "目"]) and t.startswith("第"):
            if len(heading_path) >= 2:
                return [heading_path[0], heading_path[1], t]
            if len(heading_path) == 1:
                return [heading_path[0], t]
            return [t]
        # 其他 C（例如「附則」）就 append（最多 3 層）
        newp = heading_path + [t]
        return newp[-3:]
    else:
        low = t.lower()
        if low.startswith("chapter"):
            return [t]
        if low.startswith("part"):
            return [t]
        if low.startswith("section"):
            if heading_path:
                return [heading_path[0], t]
            return [t]
        newp = heading_path + [t]
        return newp[-3:]


def build_article_text(name: str, heading_path: List[str], article_no: str, article_content: str) -> str:
    heading = " / ".join([h for h in heading_path if h])
    content = (article_content or "").replace("\r\n", "\n").strip()
    return f"【法規】{name}\n【章節】{heading}\n【條號】{article_no}\n{content}".strip()


def build_full_text(name: str, foreword: str, articles: List[Dict[str, Any]]) -> str:
    lines: List[str] = [name.strip(), ""]
    if foreword:
        lines.append(foreword.replace("\r\n", "\n").strip())
        lines.append("")
    for a in articles:
        t = a.get("ArticleType", "")
        no = (a.get("ArticleNo") or "").strip()
        c = (a.get("ArticleContent") or "").replace("\r\n", "\n").strip()
        if not c:
            continue
        if t == "C":
            lines.append(c)
        else:
            # A：用「條號 + 空格 + 內容」（你要的台灣閱讀習慣）
            if no:
                lines.append(f"{no} {c}")
            else:
                lines.append(c)
    return "\n".join(lines).strip()


def write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> int:
    ensure_dir(os.path.dirname(path))
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_moj_payload(json_bytes: bytes) -> Dict[str, Any]:
    # 檔案通常是 UTF-8；若未來出現 BOM 也能吃
    txt = json_bytes.decode("utf-8-sig")
    return json.loads(txt)


def gen_configs(payload: Dict[str, Any], lang: str, doc_type: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    產 2 個 subset：
      - *_articles：每條一筆（C 只提供 heading_path）
      - *_full_text：每篇一筆（保留 C 做標題行）
    """
    update_date = payload.get("UpdateDate", "")
    laws = payload.get("Laws", []) or []
    articles_rows: List[Dict[str, Any]] = []
    full_rows: List[Dict[str, Any]] = []

    for law in laws:
        meta = map_meta_fields(law)
        base = {
            **meta,
            "update_date": update_date,
            "doc_type": doc_type,
            "language": lang,
        }

        name = meta.get("name", "")
        foreword = law.get("LawForeword", "") or ""
        law_articles = law.get("LawArticles", []) or []

        # full_text（一篇一筆）
        full_text = build_full_text(
            name=name, foreword=foreword, articles=law_articles)
        full_rows.append(
            {
                "text": full_text,
                **base,
            }
        )

        # articles（每條一筆；做法 A）
        heading_path: List[str] = []
        for a in law_articles:
            at = a.get("ArticleType", "")
            if at == "C":
                heading_path = update_heading_path(
                    lang, heading_path, a.get("ArticleContent", ""))
                continue

            article_no = (a.get("ArticleNo") or "").strip()
            article_content = a.get("ArticleContent", "") or ""
            if not article_no and not article_content:
                continue

            row = {
                "text": build_article_text(name=name, heading_path=heading_path, article_no=article_no, article_content=article_content),
                "article_no": article_no,
                "article_type": at or "A",
                "heading_path": heading_path,
                **base,
            }
            articles_rows.append(row)

    return {
        f"{lang}_{doc_type}_articles": articles_rows,
        f"{lang}_{doc_type}_full_text": full_rows,
    }


def read_last_update(local_dir: str) -> Optional[str]:
    p = os.path.join(local_dir, "meta", "last_update.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f).get("update_date")


def write_last_update(local_dir: str, update_date: str) -> None:
    ensure_dir(os.path.join(local_dir, "meta"))
    p = os.path.join(local_dir, "meta", "last_update.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {
                "update_date": update_date,
                "generated_at": taipei_now_iso(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def write_readme(local_dir: str, repo_id: str, update_date: str) -> None:
    ensure_dir(local_dir)
    p = os.path.join(local_dir, "README.md")
    content = f"""---
license: other
language:
- zh
- en
tags:
- law
- taiwan
- legal
- nlp
pretty_name: Taiwan Laws & Orders (MOJ)
configs:
- config_name: ch_law_articles
  data_files:
  - split: train
    path: ch_law_articles/train.jsonl

- config_name: ch_law_full_text
  data_files:
  - split: train
    path: ch_law_full_text/train.jsonl

- config_name: ch_order_articles
  data_files:
  - split: train
    path: ch_order_articles/train.jsonl

- config_name: ch_order_full_text
  data_files:
  - split: train
    path: ch_order_full_text/train.jsonl

- config_name: en_law_articles
  data_files:
  - split: train
    path: en_law_articles/train.jsonl

- config_name: en_law_full_text
  data_files:
  - split: train
    path: en_law_full_text/train.jsonl

- config_name: en_order_articles
  data_files:
  - split: train
    path: en_order_articles/train.jsonl

- config_name: en_order_full_text
  data_files:
  - split: train
    path: en_order_full_text/train.jsonl
---

# Dataset Card for tw-law

## Dataset Details

### Dataset Description

**tw-law** 是從台灣法務部[全國法規資料庫（MOJ）](https://law.moj.gov.tw/)開放 API 自動同步的結構化法律資料集，涵蓋所有現行有效的**法律（Law）**與**命令（Order）**，並同時提供繁體中文及英文版本。

本資料集每週自動更新，以 `UpdateDate` 進行去重，確保僅在 MOJ 來源有實際異動時才重新推送，避免無意義的重複版本。

- **資料來源：** [全國法規資料庫開放 API](https://law.moj.gov.tw/api)（法務部）
- **最後更新（UpdateDate）：** {update_date}
- **本次生成時間（Asia/Taipei）：** {taipei_now_iso()}
- **策劃者：** [Liang Hsun Huang](https://www.linkedin.com/in/lianghsunhuang/?locale=en_US)
- **語言：** 繁體中文、英文
- **授權：** Other（請參閱下方授權說明）

### Dataset Sources

- **資料集頁面：** [lianghsun/tw-law](https://huggingface.co/datasets/lianghsun/tw-law)
- **同步工具原始碼：** [lianghsun/tw-law-sync](https://github.com/lianghsun/tw-law-sync)
- **MOJ 原始 API：**
  - `https://law.moj.gov.tw/api/ch/law/json`（中文法律）
  - `https://law.moj.gov.tw/api/ch/order/json`（中文命令）
  - `https://law.moj.gov.tw/api/en/law/json`（英文法律）
  - `https://law.moj.gov.tw/api/en/order/json`（英文命令）

---

## Uses

### Direct Use

本資料集適合用於：

- **法律 NLP 研究**：命名實體辨識、法條擷取、法律問答、語意搜尋等任務
- **大型語言模型預訓練或持續預訓練**：提供高品質、結構清晰的繁體中文正式文本
- **法律資訊系統**：建立台灣法規的語意索引、知識圖譜或 RAG 應用
- **中英法律對照研究**：藉由 `en_*` 與 `ch_*` config 的對應，進行雙語法律文本比對

### Out-of-Scope Use

- 本資料集**不提供法律建議**，任何以此資料集做出的推論或生成內容均不構成法律諮詢
- 不應用於任何規避或誤導台灣司法程序的目的
- 英文版法律翻譯由 MOJ 官方提供，非所有法規均有英文對照版本，請留意欄位 `has_eng_version`

---

## Dataset Structure

### Configurations（8 個 config）

| Config | 說明 | 粒度 |
|--------|------|------|
| `ch_law_articles` | 繁體中文法律，逐條展開 | 每筆 = 一個條文 |
| `ch_law_full_text` | 繁體中文法律，完整全文 | 每筆 = 一部法律 |
| `ch_order_articles` | 繁體中文命令，逐條展開 | 每筆 = 一個條文 |
| `ch_order_full_text` | 繁體中文命令，完整全文 | 每筆 = 一部命令 |
| `en_law_articles` | 英文法律，逐條展開 | 每筆 = 一個條文 |
| `en_law_full_text` | 英文法律，完整全文 | 每筆 = 一部法律 |
| `en_order_articles` | 英文命令，逐條展開 | 每筆 = 一個條文 |
| `en_order_full_text` | 英文命令，完整全文 | 每筆 = 一部命令 |

> **法律（Law）vs 命令（Order）：** 法律（含憲法、法、律、條例、通則）由立法院通過；命令（含規程、規則、細則、辦法、綱要、標準、準則）由行政機關發布，法律位階較高。

### `*_articles` Config 欄位說明

| 欄位 | 型別 | 說明 |
|------|------|------|
| `text` | string | 格式化條文，見下方「text 格式」說明 |
| `article_no` | string | 條號，如「第 1 條」 |
| `article_type` | string | 條文類型，`A`＝一般條文；`C` 類型（章節標題）不輸出為獨立記錄 |
| `heading_path` | list[string] | 章節層級路徑，最多 3 層，如 `["第一章 總則", "第一節 通則"]` |
| `level` | string | 法規位階，如「憲法」、「法律」、「命令」 |
| `name` | string | 法規名稱 |
| `url` | string | MOJ 法規頁面網址 |
| `category` | string | 法規類別（含 270+ 種分類） |
| `modified_date` | string | 最後修正日期（YYYYMMDD） |
| `effective_date` | string | 施行日期 |
| `effective_note` | string | 施行備註 |
| `abandon_note` | string | 廢止備註 |
| `has_eng_version` | boolean | 是否有英文版本 |
| `eng_name` | string | 英文法規名稱 |
| `histories` | string | 沿革記錄 |
| `attachments` | list[object] | 附件（含 `FileName`、`FileURL`） |
| `update_date` | string | MOJ 資料庫更新日期（來源 `UpdateDate`） |
| `doc_type` | string | `law` 或 `order` |
| `language` | string | `ch` 或 `en` |

### `*_full_text` Config 欄位說明

欄位與 `*_articles` 相同，但不含 `article_no`、`article_type`、`heading_path`、`attachments`。`text` 欄位為完整法規全文（法規名稱 + 序文 + 所有條文連接）。

### `text` 欄位格式

**`*_articles`（逐條格式）：**

```
【法規】{法規名稱}
【章節】{heading_path 以 / 連接}
【條號】{條號}
{條文內容}
```

**`*_full_text`（完整全文格式）：**

```
{法規名稱}

{序文（若有）}

{第一章 章節標題}
第 N 條
條文內容
...
```

### heading_path 解析邏輯

`ArticleType = "C"` 的條文為章節標題（如「第一章 總則」），**不輸出為獨立記錄**，僅用以更新後續條文的 `heading_path`。

- **中文版**：識別「章、節、編、款、目」關鍵字判斷層級
- **英文版**：識別「Chapter、Section、Part」關鍵字判斷層級
- 同層或更高層的新標題出現時，自動截斷舊路徑

### 資料範例

```json
{{
  "text": "【法規】中華民國憲法\\n【章節】第 一 章 總綱\\n【條號】第 1 條\\n中華民國基於三民主義，為民有民治民享之民主共和國。",
  "article_no": "第 1 條",
  "article_type": "A",
  "heading_path": ["第 一 章 總綱"],
  "level": "憲法",
  "name": "中華民國憲法",
  "url": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=A0000001",
  "category": "憲法",
  "modified_date": "19470101",
  "has_eng_version": true,
  "eng_name": "Constitution of the Republic of China (Taiwan)",
  "update_date": "{update_date}",
  "doc_type": "law",
  "language": "ch"
}}
```

---

## Dataset Creation

### Curation Rationale

台灣全國法規資料庫是台灣最完整的法律文本公開來源，但原始 API 提供的是高度壓縮的 JSON 結構，不適合直接用於 NLP 任務。本資料集將原始資料轉換為對語言模型友善的格式：

1. **`*_articles`**：保留細粒度的條文上下文（所屬法規、章節路徑、條號），適合語意搜尋與問答任務
2. **`*_full_text`**：保留完整法規結構，適合摘要、分類與全文索引任務

### Source Data

#### Data Collection and Processing

本資料集的原始資料直接來自法務部 MOJ 的官方開放 API，每週五 00:30（Asia/Taipei）自動執行同步：

1. 從 4 個 API 端點各下載一個 ZIP 壓縮檔
2. 解壓取出 JSON 檔（UTF-8-sig 解碼）
3. 比對 `UpdateDate` 去重，若與上次同步相同則跳過推送
4. 將 JSON 轉換為 JSONL 格式，分別產出 8 個 config
5. 透過 Hugging Face Hub API 上傳

#### Who are the source data producers?

本資料集內容由**法務部全國法規資料庫**（[law.moj.gov.tw](https://law.moj.gov.tw/)）生產與維護。英文版法律翻譯由法務部官方提供。

### Annotations

本資料集不含人工標註，所有欄位均直接來自 MOJ 原始資料，或透過確定性規則推導（如 `heading_path` 的章節層級解析）。

---

## Bias, Risks, and Limitations

- **覆蓋範圍**：僅含現行有效法規，已廢止法規不在此資料集中
- **英文版完整性**：非所有法規均有官方英文翻譯，`en_*` config 的規模明顯小於 `ch_*`
- **更新頻率**：MOJ 資料庫雙週更新，本資料集每週同步，但可能存在短暫落差
- **條文格式**：部分法規條文含有表格、附圖或公式，以純文字呈現時資訊可能不完整
- **法律時效**：本資料集反映同步當時的法規狀態，不追蹤歷史版本

### Recommendations

使用者應注意本資料集僅供研究與技術用途，不構成法律建議。如需確認法規的最新現行版本，請以 [全國法規資料庫](https://law.moj.gov.tw/) 官方網站為準。

---

## License

本資料集內容來源為台灣政府機關發布之公開法律文件。根據[政府資料開放授權條款](https://data.gov.tw/license)，允許各界以商業或非商業目的進行蒐集、處理、利用及重製，惟需**標示來源**為「全國法規資料庫，法務部」。

同步工具原始碼（[lianghsun/tw-law-sync](https://github.com/lianghsun/tw-law-sync)）採 **MIT License**。

---

## Citation

```bibtex
@misc{{tw-law,
  title        = {{tw-law: Taiwan Laws and Orders Dataset (MOJ)}},
  author       = {{Liang Hsun Huang}},
  year         = {{2025}},
  howpublished = {{\\url{{https://huggingface.co/datasets/lianghsun/tw-law}}}},
  note         = {{Automatically synced from the Ministry of Justice National Laws Database Open API.}}
}}
```

## Dataset Card Authors

[Liang Hsun Huang](https://www.linkedin.com/in/lianghsunhuang/?locale=en_US)

## Dataset Card Contact

[Liang Hsun Huang](https://www.linkedin.com/in/lianghsunhuang/?locale=en_US)
"""
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def push_to_hub(repo_id: str, local_dir: str, commit_message: str) -> None:
    """
    你要的 push_to_hub：用 huggingface_hub 直接把產物資料夾上傳到指定 repo。
    需要環境變數 HF_TOKEN（或 HUGGINGFACE_HUB_TOKEN）。
    """
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)
    api.upload_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=local_dir,
        path_in_repo=".",
        commit_message=commit_message,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo_id", default=DEFAULT_REPO_ID)
    ap.add_argument("--workdir", default="/data")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    out_dir = os.path.join(args.workdir, "out")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    ensure_dir(out_dir)

    # 下載 + 產出
    all_rows_by_config: Dict[str, List[Dict[str, Any]]] = {}
    source_update_dates: List[str] = []

    for (lang, doc_type), url in ENDPOINTS.items():
        print(
            f"[{taipei_now_iso()}] Download: lang={lang} doc_type={doc_type} url={url}", flush=True)
        zip_bytes = download_zip(url)
        json_name, json_bytes = extract_single_json_from_zip(zip_bytes)
        payload = load_moj_payload(json_bytes)
        update_date = payload.get("UpdateDate", "")
        source_update_dates.append(update_date)

        configs = gen_configs(payload, lang=lang, doc_type=doc_type)
        all_rows_by_config.update(configs)

        print(
            f"  - extracted {json_name}, UpdateDate={update_date}", flush=True)

    # 用「最大的一個 UpdateDate」當本次版本（四個來源理論上應一致；若不一致也能合理選最大）
    update_date_final = sorted(
        [d for d in source_update_dates if d])[-1] if any(source_update_dates) else ""
    last_update = read_last_update(out_dir)  # out_dir 每次都清，這裡只是防呆
    # 以 repo 內 meta/last_update.json 來去重：先讀本地 workdir/meta（保留於 /data）
    stable_meta_dir = os.path.join(args.workdir, "meta_cache")
    ensure_dir(stable_meta_dir)
    stable_meta_file = os.path.join(stable_meta_dir, "last_update.json")
    prev = None
    if os.path.exists(stable_meta_file):
        with open(stable_meta_file, "r", encoding="utf-8") as f:
            prev = json.load(f).get("update_date")

    if prev and update_date_final and prev == update_date_final:
        print(
            f"[{taipei_now_iso()}] No update (UpdateDate unchanged: {update_date_final}). Skip.", flush=True)
        return

    # 寫檔：每個 config 一個 train.jsonl
    for config_name, rows in all_rows_by_config.items():
        cfg_dir = os.path.join(out_dir, config_name)
        ensure_dir(cfg_dir)
        n = write_jsonl(os.path.join(cfg_dir, "train.jsonl"), rows)
        print(f"  - wrote {config_name}/train.jsonl rows={n}", flush=True)

    # meta + README
    write_last_update(out_dir, update_date_final)
    write_readme(out_dir, args.repo_id, update_date_final)

    # 把 update_date 存到 stable cache（存在 /data，容器重啟還在）
    with open(stable_meta_file, "w", encoding="utf-8") as f:
        json.dump({"update_date": update_date_final, "generated_at": taipei_now_iso(
        )}, f, ensure_ascii=False, indent=2)

    print(f"[{taipei_now_iso()}] Prepared dataset artifacts. UpdateDate={update_date_final}", flush=True)

    if args.push:
        msg = f"sync: UpdateDate={update_date_final}"
        push_to_hub(args.repo_id, out_dir, commit_message=msg)
        print(f"[{taipei_now_iso()}] Pushed to hub: {args.repo_id}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
