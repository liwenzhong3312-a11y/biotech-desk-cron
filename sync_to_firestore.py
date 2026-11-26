#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将本地 db/ 目录与 Firebase Cloud Firestore (biotech-desk-neo664) 双向同步：
- 上传：全量同步、--only col/docId 按需增量同步
- 拉取：--pull 从云端拉取最新编辑（tickers/macro/benchlib/mktlib/proposals）写回本地 db/
- 鉴权：支持本地 ~/.config/configstore/firebase-tools.json、遇 401 自动续期，以及 CI 环境 FIREBASE_REFRESH_TOKEN 无头鉴权。
"""
import os
import sys
import json
import math
import socket
import subprocess
import urllib.request
import urllib.parse

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(ROOT_DIR, "db")
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
DATABASE_ID = "(default)"
COLLECTIONS = ("tickers", "macro", "benchlib", "mktlib", "prices", "glossary", "proposals")
PULL_COLLECTIONS = COLLECTIONS
IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"
_CACHED_TOKEN = ""


def _exchange_oauth_refresh(client_id, client_secret, refresh_token):
    """纯 Python 标准库直接向 Google OAuth2 端点刷新 access_token（0.3s 零依赖）。"""
    _ensure_proxy()
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    tok = payload.get("access_token") or ""
    if not tok:
        raise RuntimeError("OAuth2 刷新响应未包含 access_token")
    return tok


def _local_proxy_open(host="127.0.0.1", port=20808):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _ensure_proxy():
    if "HTTPS_PROXY" not in os.environ and "https_proxy" not in os.environ:
        proxies = urllib.request.getproxies()
        proxy_url = proxies.get("https") or proxies.get("http")
        if not proxy_url and _local_proxy_open():
            proxy_url = "http://127.0.0.1:20808"
        if proxy_url:
            os.environ["HTTPS_PROXY"] = proxy_url
            os.environ["HTTP_PROXY"] = proxies.get("http", proxy_url)


def _ensure_ci_firebase_config():
    """若 CI 环境中不存在 firebase-tools.json 但设置了 FIREBASE_REFRESH_TOKEN，则生成最小配置以便 CLI 换取 access_token。"""
    cfg_path = os.path.expanduser("~/.config/configstore/firebase-tools.json")
    if os.path.isfile(cfg_path):
        return cfg_path
    ref_tok = (os.environ.get("FIREBASE_REFRESH_TOKEN") or "").strip()
    if not ref_tok:
        return cfg_path
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "tokens": {
                "refresh_token": ref_tok,
                "access_token": "",
                "expires_at": 0,
            }
        }, f, ensure_ascii=False, indent=2)
    return cfg_path


def get_firebase_token():
    global _CACHED_TOKEN
    if _CACHED_TOKEN:
        return _CACHED_TOKEN
    oauth_raw = (os.environ.get("FIREBASE_OAUTH_JSON") or "").strip()
    if oauth_raw:
        cred = json.loads(oauth_raw)
        _CACHED_TOKEN = _exchange_oauth_refresh(
            cred["client_id"], cred["client_secret"], cred["refresh_token"]
        )
        return _CACHED_TOKEN

    cfg_path = _ensure_ci_firebase_config()
    if not os.path.isfile(cfg_path):
        raise RuntimeError("未找到 Firebase CLI 配置，请先运行 firebase login 或配置 FIREBASE_OAUTH_JSON")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    tokens = cfg.get("tokens") or {}
    tok = tokens.get("access_token")
    if not tok:
        if tokens.get("refresh_token"):
            return refresh_firebase_token()
        raise RuntimeError("未找到 Firebase CLI access_token，请先运行 firebase login")
    _CACHED_TOKEN = tok
    return tok


def refresh_firebase_token():
    """刷新过期 access_token：优先纯 Python OAuth2，回退本地 firebase-tools CLI。"""
    global _CACHED_TOKEN
    oauth_raw = (os.environ.get("FIREBASE_OAUTH_JSON") or "").strip()
    if oauth_raw:
        cred = json.loads(oauth_raw)
        _CACHED_TOKEN = _exchange_oauth_refresh(
            cred["client_id"], cred["client_secret"], cred["refresh_token"]
        )
        return _CACHED_TOKEN

    _ensure_proxy()
    _ensure_ci_firebase_config()
    npx_cmd = "npx.cmd" if os.name == "nt" else "npx"
    res = subprocess.run(
        [npx_cmd, "-y", "firebase-tools@latest", "projects:list"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if res.returncode != 0:
        raise RuntimeError(f"刷新 Firebase Token 失败: {res.stderr.strip()[:300]}")
    cfg_path = os.path.expanduser("~/.config/configstore/firebase-tools.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    tok = (cfg.get("tokens") or {}).get("access_token")
    if not tok:
        raise RuntimeError("Firebase CLI 刷新后仍未获得 access_token")
    _CACHED_TOKEN = tok
    return tok


def to_fs_val(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return {"nullValue": None}
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [to_fs_val(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {str(k): to_fs_val(val) for k, val in v.items()}}}
    return {"stringValue": str(v)}


def from_fs_val(v):
    if not isinstance(v, dict):
        return v
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "arrayValue" in v:
        return [from_fs_val(x) for x in (v["arrayValue"].get("values") or [])]
    if "mapValue" in v:
        fields = v["mapValue"].get("fields") or {}
        return {k: from_fs_val(val) for k, val in fields.items()}
    return None


def upload_doc(tok, col, doc_id, data):
    url = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/{DATABASE_ID}/documents/"
        f"{urllib.parse.quote(col)}/{urllib.parse.quote(doc_id)}"
    )
    body = json.dumps({"fields": {str(k): to_fs_val(v) for k, v in data.items()}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def _list_collection_docs(tok, col):
    docs = []
    page_token = ""
    while True:
        qs = "pageSize=100" + (f"&pageToken={urllib.parse.quote(page_token)}" if page_token else "")
        url = (
            f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/{DATABASE_ID}/documents/"
            f"{urllib.parse.quote(col)}?{qs}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        for item in payload.get("documents") or []:
            doc_id = item["name"].rsplit("/", 1)[-1]
            fields = item.get("fields") or {}
            docs.append((doc_id, {k: from_fs_val(v) for k, v in fields.items()}))
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break
    return docs


def pull_docs(cols=PULL_COLLECTIONS):
    """从 Cloud Firestore 拉取云端最新文档写回本地 db/，防止覆盖网页端最新编辑。"""
    _ensure_proxy()
    tok = get_firebase_token()
    refreshed = False
    ok, fail = 0, 0
    for col in cols:
        try:
            try:
                docs = _list_collection_docs(tok, col)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed:
                    print("  [INFO] Firebase access_token 已过期，正在自动续期...")
                    tok = refresh_firebase_token()
                    refreshed = True
                    docs = _list_collection_docs(tok, col)
                else:
                    raise
            cdir = os.path.join(DB_DIR, col)
            os.makedirs(cdir, exist_ok=True)
            for doc_id, data in docs:
                fpath = os.path.join(cdir, f"{doc_id}.json")
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                ok += 1
            if not IS_CI:
                print(f"  [PULL OK] {col}: {len(docs)} 份")
        except Exception as e:
            if not IS_CI:
                print(f"  [PULL ERR] {col}: {e}")
            fail += 1
    print(f"云端拉取完成：成功 {ok} 份文档，失败 {fail} 个集合。")
    return ok, fail


def sync_docs(targets=None):
    """同步指定文档列表（格式 col/docId）或全量同步 db/。返回 (ok_count, fail_count)。"""
    _ensure_proxy()
    tok = get_firebase_token()
    refreshed = False

    doc_pairs = []
    if targets:
        for item in targets:
            if "/" not in item:
                print(f"  [SKIP] 无效目标格式（应为 col/docId）: {item}")
                continue
            col, doc_id = item.split("/", 1)
            fpath = os.path.join(DB_DIR, col, f"{doc_id}.json")
            if not os.path.isfile(fpath):
                print(f"  [SKIP] 文件不存在: {fpath}")
                continue
            doc_pairs.append((col, doc_id, fpath))
    else:
        for col in COLLECTIONS:
            cdir = os.path.join(DB_DIR, col)
            if not os.path.isdir(cdir):
                continue
            for fn in sorted(f for f in os.listdir(cdir) if f.endswith(".json")):
                doc_pairs.append((col, fn[:-5], os.path.join(cdir, fn)))

    ok, fail = 0, 0
    for col, doc_id, fpath in doc_pairs:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        try:
            status = upload_doc(tok, col, doc_id, data)
            if not IS_CI:
                print(f"  [OK {status}] {col}/{doc_id}")
            ok += 1
        except urllib.error.HTTPError as e:
            if e.code == 401 and not refreshed:
                try:
                    print("  [INFO] Firebase access_token 已过期，正在自动续期...")
                    tok = refresh_firebase_token()
                    refreshed = True
                    status = upload_doc(tok, col, doc_id, data)
                    if not IS_CI:
                        print(f"  [OK {status}] {col}/{doc_id}")
                    ok += 1
                    continue
                except Exception as re_err:
                    print(f"  [ERR 401] 续期后仍失败: {re_err}")
                    fail += 1
                    continue
            err_txt = e.read().decode("utf-8", errors="replace")[:300]
            print(f"  [ERR {e.code}] {'' if IS_CI else f'{col}/{doc_id}: '}{err_txt}")
            fail += 1
        except Exception as e:
            print(f"  [ERR] {'' if IS_CI else f'{col}/{doc_id}: '}{e}")
            fail += 1

    print(f"\n同步完成：成功 {ok} 份文档，失败 {fail} 份。")
    return ok, fail


def main():
    args = sys.argv[1:]
    if "--pull" in args:
        pull_docs()
        return
    targets = None
    if "--only" in args:
        idx = args.index("--only")
        targets = [a for a in args[idx + 1:] if not a.startswith("-")]
    sync_docs(targets=targets)


if __name__ == "__main__":
    main()
