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

# {repo_id}

Data generated from MOJ National Laws Database Open API (JSON ZIP).

- UpdateDate (from source payload): **{update_date}**
- Generated at (Asia/Taipei): **{taipei_now_iso()}**

## Configs

Eight configs:

- ch_law_articles
- ch_law_full_text
- ch_order_articles
- ch_order_full_text
- en_law_articles
- en_law_full_text
- en_order_articles
- en_order_full_text

## Record formats

### *_articles

- `text` uses labeled format:
  - 【法規】... / 【章節】... / 【條號】... / content
- Headings (`ArticleType = C`) are **not** emitted as standalone records; they only update `heading_path`.

### *_full_text

- One record per law/order.
- Preserves headings as lines, and concatenates articles.

> Note: Please review licensing/terms of use for downstream redistribution.
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
