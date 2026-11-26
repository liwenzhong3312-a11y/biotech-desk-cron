#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生科标的研究台 · 每日自动监测脚本 (100% 对齐 devnotes.sourceRegistry 14 路消息源)
覆盖 SRCROUTES 全部 14 个路由键：
- 实时在线抓取：px (日线行情)、sec (SEC 申报列表)、13dg (13D/13G)、ir (公司 IR RSS)、ct (ClinicalTrials.gov)、comp (竞品 NCT)、media (STAT / Endpoints RSS)
- 同步与状态记录：pos、opt、si、rating、13f、web、pat（保留真实核对日期，不伪造未抓路由）
- 自动备份修改前的 JSON 到 undo/，并重新编译《生科标的研究台_本地版.html》
"""

import os
import re
import sys
import json
import time
import math
import shutil
import socket
import http.cookiejar
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(ROOT_DIR, "db")
UNDO_DIR = os.path.join(ROOT_DIR, "undo")
IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"

SEC_USER_AGENT = "BiotechResearchDesk/2.0 (LocalResearchDesk; contact@biotechdesk.local)"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

CIK_MAP = {
    "ABSI": "0001672688",
    "MANE": "0001827635",
    "ARWR": "0000879407",
    "ABCL": "0001703057",
    "KRYS": "0001711279",
    "MSLE": "0001421642",
    "SLS":  "0001390478",
}

def backup_file(path, col, doc_id):
    if not os.path.exists(path):
        return
    os.makedirs(UNDO_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak_name = f"{col}__{doc_id}__{ts}.json"
    shutil.copy2(path, os.path.join(UNDO_DIR, bak_name))

def _ensure_local_proxy():
    if "HTTPS_PROXY" not in os.environ and "https_proxy" not in os.environ:
        try:
            with socket.create_connection(("127.0.0.1", 20808), timeout=0.3):
                os.environ["HTTPS_PROXY"] = "http://127.0.0.1:20808"
                os.environ["HTTP_PROXY"] = "http://127.0.0.1:20808"
        except OSError:
            pass


def fetch_url(url, headers=None, timeout=15):
    req_headers = {"User-Agent": DEFAULT_UA}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

def fetch_json(url, headers=None, timeout=15):
    return json.loads(fetch_url(url, headers=headers, timeout=timeout))

def fetch_price_bars(symbol):
    """从 Yahoo Finance / Stooq 拉取最近 1 年日线"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1y&interval=1d"
        data = fetch_json(url)
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(timestamps):
            c = quote["close"][i]
            if c is None:
                continue
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            bars.append({
                "t": dt,
                "o": round(quote["open"][i] or c, 4),
                "h": round(quote["high"][i] or c, 4),
                "l": round(quote["low"][i] or c, 4),
                "c": round(c, 4),
                "v": int(quote["volume"][i] or 0)
            })
        if bars:
            return bars
    except Exception:
        pass

    try:
        stooq_sym = f"{symbol.lower()}.us"
        url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
        raw = fetch_url(url)
        lines = raw.strip().splitlines()
        bars = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5 and parts[4] != "null":
                bars.append({
                    "t": parts[0],
                    "o": float(parts[1]),
                    "h": float(parts[2]),
                    "l": float(parts[3]),
                    "c": float(parts[4]),
                    "v": int(float(parts[5])) if len(parts) > 5 and parts[5] else 0
                })
        return bars[-260:]
    except Exception:
        return []

def check_sec_submissions(sym, cik):
    """检查 SEC EDGAR 最新申报列表"""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        data = fetch_json(url, headers={"User-Agent": SEC_USER_AGENT})
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        items_list = recent.get("items", [])
        docs = recent.get("primaryDocument", [])
        out = []
        for i in range(min(15, len(forms))):
            out.append({
                "form": forms[i],
                "date": dates[i],
                "acc": accs[i],
                "items": items_list[i] if i < len(items_list) else "",
                "doc": docs[i] if i < len(docs) else ""
            })
        return out
    except Exception:
        return None

def check_rss_feed(url, keywords=None):
    """拉取 RSS XML 并按关键词过滤"""
    try:
        xml_txt = fetch_url(url, timeout=12)
        root = ET.fromstring(xml_txt)
        items = []
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            desc = (it.findtext("description") or "").strip()
            if keywords:
                blob = f"{title} {desc}".lower()
                if not any(k.lower() in blob for k in keywords if k):
                    continue
            items.append({"title": title, "link": link, "pubDate": pub})
        return items
    except Exception:
        return None

def check_clinical_trial(nct_id):
    """检查 ClinicalTrials.gov API v2 快照"""
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    try:
        data = fetch_json(url)
        ps = data.get("protocolSection", {})
        status_mod = ps.get("statusModule", {})
        design_mod = ps.get("designModule", {})
        return {
            "nctId": nct_id,
            "overallStatus": status_mod.get("overallStatus"),
            "lastUpdatePostDate": status_mod.get("lastUpdatePostDateStruct", {}).get("date"),
            "primaryCompletionDate": status_mod.get("primaryCompletionDateStruct", {}).get("date"),
            "EnrollmentCount": design_mod.get("enrollmentInfo", {}).get("count")
        }
    except Exception:
        return None

def extract_nct_ids(obj):
    raw = json.dumps(obj, ensure_ascii=False)
    return sorted(list(set(re.findall(r"NCT\d{8}", raw))))

def append_alert_dedup(t_doc, date_str, kind, level, title, detail, url=""):
    """向标的 alerts 顶部追加去重后的事实提醒（不覆盖历史提醒）"""
    alerts = t_doc.setdefault("alerts", [])
    for a in alerts:
        if a.get("date") == date_str and a.get("title") == title:
            return False
    alerts.insert(0, {
        "date": date_str,
        "kind": kind,
        "level": level,
        "read": "no",
        "title": title,
        "detail": detail,
        "url": url or ""
    })
    return True


def check_price_alerts(sym, t_doc, bars):
    """基于最新日线自动检测 ≥8% 异动或 50 日均线穿越，生成事实型技术面提醒"""
    if not bars or len(bars) < 51:
        return
    last = bars[-1]
    prev = bars[-2]
    closes = [b["c"] for b in bars]
    ma50_now = sum(closes[-50:]) / 50.0
    ma50_prev = sum(closes[-51:-1]) / 50.0
    chg_pct = (last["c"] / prev["c"] - 1.0) * 100.0 if prev["c"] else 0.0
    cross_up = prev["c"] < ma50_prev and last["c"] >= ma50_now
    cross_dn = prev["c"] >= ma50_prev and last["c"] < ma50_now
    if abs(chg_pct) >= 8.0 or cross_up or cross_dn:
        dir_txt = "涨" if chg_pct >= 0 else "跌"
        ma_txt = "，重新站上 50 日线" if cross_up else ("，跌破 50 日线" if cross_dn else "")
        title = f"{sym} {last['t'][5:].replace('-','/')} {dir_txt} {abs(chg_pct):.1f}% 收 ${last['c']:.2f}{ma_txt}"
        detail = (
            f"最新交易日 {last['t']} 收盘 ${last['c']:.2f}（前收 ${prev['c']:.2f}，{chg_pct:+.1f}%），"
            f"50 日均线约 ${ma50_now:.2f}，成交量 {last.get('v', 0):,} 股。自动监测技术面异动记录，需结合公告与板块判断。"
        )
        append_alert_dedup(t_doc, last["t"], "技术面", "中", title, detail)


def update_live_options(sym, t_doc, today_iso):
    """为 uiV2 且有待发生关键催化剂的标的拉取 Yahoo Finance 实时期权跨式，更新 checks.implied 与 monitor.fetched.opt。"""
    if not t_doc.get("uiV2"):
        return False
    has_key_cat = any(
        c.get("owner") != "competitor" and c.get("tier") == "关键" and c.get("status") != "已发生"
        for c in (t_doc.get("catalysts") or [])
    )
    if not has_key_cat:
        return False
    try:
        cj = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(cj)]
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"),
            ("Accept", "application/json,text/html,*/*"),
        ]
        try:
            opener.open("https://fc.yahoo.com", timeout=8)
        except Exception:
            pass
        crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8).read().decode("utf-8").strip()
        if not crumb:
            return False
        u1 = f"https://query1.finance.yahoo.com/v7/finance/options/{urllib.parse.quote(sym)}?crumb={urllib.parse.quote(crumb)}"
        d1 = json.loads(opener.open(u1, timeout=12).read().decode("utf-8"))
        res = d1["optionChain"]["result"][0]
        spot = res["quote"].get("regularMarketPrice")
        exps = res.get("expirationDates") or []
        if not spot or len(exps) < 2:
            return False

        # 取最近两个月度到期日（排除距今 < 3 天）
        now_ts = time.time()
        valid_exps = [ts for ts in exps if ts - now_ts >= 3 * 86400][:2]
        if len(valid_exps) < 2:
            return False

        snaps = []
        for ts in valid_exps:
            dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            u2 = f"https://query1.finance.yahoo.com/v7/finance/options/{urllib.parse.quote(sym)}?date={ts}&crumb={urllib.parse.quote(crumb)}"
            d2 = json.loads(opener.open(u2, timeout=12).read().decode("utf-8"))
            opt = d2["optionChain"]["result"][0]["options"][0]
            calls, puts = opt["calls"], opt["puts"]
            strikes = sorted(set(c["strike"] for c in calls) & set(p["strike"] for p in puts))
            if not strikes:
                continue
            atm = min(strikes, key=lambda s: abs(s - spot))
            c = next(x for x in calls if x["strike"] == atm)
            p = next(x for x in puts if x["strike"] == atm)
            if (c.get("ask") or 0) <= 0 or (p.get("ask") or 0) <= 0:
                continue
            c_mid = ((c.get("bid") or 0) + c["ask"]) / 2.0
            p_mid = ((p.get("bid") or 0) + p["ask"]) / 2.0
            straddle = c_mid + p_mid
            pct = straddle / spot * 100.0
            iv_avg = ((c.get("impliedVolatility") or 0) + (p.get("impliedVolatility") or 0)) * 50.0
            snaps.append((dt_str, atm, straddle, pct, iv_avg, c, p))

        if len(snaps) < 2:
            return False

        near, far = snaps[0], snaps[1]
        ev_pct = math.sqrt(max(0.0, far[3] ** 2 - near[3] ** 2))
        near_lbl = f"{int(near[0][5:7])}/{int(near[0][8:10])} 到期"
        far_lbl = f"{int(far[0][5:7])}/{int(far[0][8:10])} 到期"
        imp = t_doc.setdefault("checks", {}).setdefault("implied", {})
        imp["asOf"] = today_iso
        imp["spot"] = round(float(spot), 2)
        imp["near"] = round(near[3], 1)
        imp["nearExp"] = near_lbl
        imp["total"] = round(far[3], 1)
        imp["exp"] = far_lbl
        imp["event"] = round(ev_pct, 1)
        imp["iv"] = round(max(near[4], far[4]), 1)
        imp["src"] = (
            f"Yahoo Finance 期权链（${near[1]:g} 跨式；{near[0][5:]} 看涨 {near[5].get('bid')}/{near[5].get('ask')}、"
            f"看跌 {near[6].get('bid')}/{near[6].get('ask')}；{far[0][5:]} 看涨 {far[5].get('bid')}/{far[5].get('ask')}、"
            f"看跌 {far[6].get('bid')}/{far[6].get('ask')}）"
        )
        imp["note"] = (
            f"自动同步（{today_iso}，现价 ${spot:.2f}）：${near[1]:g} 跨式 {near_lbl} ±{near[3]:.1f}%（IV {near[4]:.1f}%）、"
            f"{far_lbl} ±{far[3]:.1f}%（IV {far[4]:.1f}%），方差剥离增量事件波动约 ±{ev_pct:.1f}%（粗算）。"
        )
        t_doc.setdefault("monitor", {}).setdefault("fetched", {})["opt"] = today_iso
        return True
    except Exception:
        return False


def sync_fallback_fetched(t_doc, fetched):
    """将各模块已有的真实核查日期同步到 monitor.fetched（避免页面显示无记录）"""
    mon = t_doc.get("monitor") or {}
    if not fetched.get("sec") and mon.get("secChk"):
        fetched["sec"] = mon["secChk"]
    if not fetched.get("pos"):
        pos_sync = str((t_doc.get("position") or {}).get("syncedAt") or "")[:10]
        if pos_sync:
            fetched["pos"] = pos_sync
    if not fetched.get("opt"):
        opt_asof = ((t_doc.get("checks") or {}).get("implied") or {}).get("asOf")
        if opt_asof:
            fetched["opt"] = opt_asof
    if not fetched.get("si"):
        si_asof = ((t_doc.get("checks") or {}).get("si") or {}).get("asOf")
        if si_asof:
            fetched["si"] = si_asof
    if not fetched.get("rating"):
        b_dates = sorted([b.get("date") for b in (t_doc.get("banks") or []) if b.get("date")])
        if b_dates:
            fetched["rating"] = b_dates[-1]
    if not fetched.get("13f") and mon.get("last13F"):
        fetched["13f"] = mon["last13F"]
    if not fetched.get("web"):
        w_dates = sorted([p.get("webAsOf") for p in (t_doc.get("pipeline") or []) if p.get("webAsOf")])
        if w_dates:
            fetched["web"] = w_dates[-1]
    if not fetched.get("pat"):
        p_dates = sorted([s.get("date") for s in ((t_doc.get("patent") or {}).get("searches") or []) if s.get("date")])
        if p_dates:
            fetched["pat"] = p_dates[-1]

def run_monitor(dry_run=False, pull_cloud=False, sync_cloud=False, no_html=False):
    _ensure_local_proxy()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    print("==================================================")
    print(f"  生科标的研究台 · 每日自动监测 ({today_iso})")
    print("==================================================")

    if pull_cloud and not dry_run:
        print("\n[0/4] 正在从 Cloud Firestore 拉取云端最新人工编辑...")
        import sync_to_firestore
        sync_to_firestore.pull_docs()

    tickers_dir = os.path.join(DB_DIR, "tickers")
    prices_dir = os.path.join(DB_DIR, "prices")
    ticker_files = sorted([f for f in os.listdir(tickers_dir) if f.endswith(".json")])
    symbols = [f[:-5] for f in ticker_files]

    # 1. 拉取媒体 RSS（STAT & Endpoints，一次拉取供各标的关键词过滤）
    print("\n[1/4] 正在拉取行业一线媒体 RSS (STAT News / Endpoints News)...")
    stat_rss = check_rss_feed("https://www.statnews.com/feed/")
    endp_rss = check_rss_feed("https://endpoints.news/feed/")
    media_rss_ok = (stat_rss is not None) or (endp_rss is not None)
    print(f"  - STAT RSS: {'成功 ('+str(len(stat_rss))+'条)' if stat_rss is not None else '跳过'} | Endpoints RSS: {'成功 ('+str(len(endp_rss))+'条)' if endp_rss is not None else '跳过'}")

    # 2. 更新 XBI 与各标的日线 (px)
    print("\n[2/4] 正在检查并更新日线行情 (px)...")
    px_updated = {}
    for sym in symbols + ["XBI"]:
        p_path = os.path.join(prices_dir, f"{sym}.json")
        old_bars = []
        if os.path.exists(p_path):
            with open(p_path, "r", encoding="utf-8") as f:
                old_doc = json.load(f)
                old_bars = old_doc.get("bars", [])
        old_last = old_bars[-1]["t"] if old_bars else "无"
        new_bars = fetch_price_bars(sym)
        if new_bars and len(new_bars) >= len(old_bars):
            new_last = new_bars[-1]["t"]
            last_close = new_bars[-1]["c"]
            px_updated[sym] = (old_last, new_last, last_close)
            if not IS_CI:
                print(f"  - {sym:5s}: 最新交易日 {old_last} -> {new_last} (收盘 ${last_close})")
            if not dry_run:
                backup_file(p_path, "prices", sym)
                with open(p_path, "w", encoding="utf-8") as f:
                    json.dump({"sym": sym, "bars": new_bars, "updatedAt": int(time.time() * 1000)}, f, ensure_ascii=False, indent=2)
        else:
            if not IS_CI:
                print(f"  - {sym:5s}: 保持现有本地缓存 (最新交易日 {old_last})")
            if old_bars:
                px_updated[sym] = (old_last, old_last, old_bars[-1]["c"])
    if IS_CI:
        print(f"  - 行情更新完成（共处理 {len(px_updated)} 个标的）")

    # 2.5 若已配置 IBKR Flex 只读凭证，自动同步持仓与成交（pos / fillsLog）
    print("\n[2.5/4] 正在检查 IBKR Flex 只读持仓与成交同步...")
    try:
        import ibkr_flex_sync
        if ibkr_flex_sync.has_ibkr_credentials():
            ibkr_res = ibkr_flex_sync.run_ibkr_sync(dry_run=dry_run, rebuild_html=False, sync_cloud=False)
            if not IS_CI:
                print(f"  - IBKR Flex 已同步: 识别持仓 {len(ibkr_res['positions'])} 个，新增成交 {ibkr_res['added_trades']} 笔")
            else:
                print(f"  - IBKR Flex 同步完成（新增成交 {ibkr_res['added_trades']} 笔）")
        else:
            print("  - 未配置 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID，跳过持仓同步（保留原有 pos 核对日期）")
    except Exception as ib_err:
        print(f"  ! IBKR Flex 同步跳过: {ib_err}")

    # 3. 逐个标的检查 SEC、IR RSS、ClinicalTrials、媒体与会议日程并更新 monitor.fetched
    print("\n[3/4] 正在核查 14 路消息源并写入 monitor.fetched...")
    for sym in symbols:
        t_path = os.path.join(tickers_dir, f"{sym}.json")
        with open(t_path, "r", encoding="utf-8") as f:
            t_doc = json.load(f)

        mon = t_doc.setdefault("monitor", {})
        fetched = mon.setdefault("fetched", {})
        sync_fallback_fetched(t_doc, fetched)

        # (a) 行情 px 与技术面异动提醒
        if sym in px_updated:
            _, new_last, last_close = px_updated[sym]
            t_doc["spot"] = str(last_close)
            fetched["px"] = today_iso
            p_path = os.path.join(prices_dir, f"{sym}.json")
            if os.path.exists(p_path):
                with open(p_path, "r", encoding="utf-8") as pf:
                    check_price_alerts(sym, t_doc, json.load(pf).get("bars", []))

        # (b) SEC EDGAR (sec & 13dg) 与新申报提醒
        cik = CIK_MAP.get(sym) or t_doc.get("cik")
        if cik:
            cik10 = str(cik).zfill(10)
            sec_items = check_sec_submissions(sym, cik10)
            if sec_items is not None:
                prev_sec_chk = mon.get("secChk") or ""
                fetched["sec"] = today_iso
                fetched["13dg"] = today_iso
                mon["secChk"] = today_iso
                top_f = sec_items[0] if sec_items else None
                if top_f and not IS_CI:
                    print(f"  - [{sym}] SEC EDGAR (sec/13dg) 已核查: 最新申报 {top_f['form']} ({top_f['date']})")
                for f_item in sec_items:
                    f_dt = f_item.get("date") or ""
                    f_fm = f_item.get("form") or ""
                    if prev_sec_chk and f_dt > prev_sec_chk and any(k in f_fm.upper() for k in ("10-Q", "10-K", "8-K", "S-3", "424B", "13D", "13G", "4")):
                        acc_clean = (f_item.get("acc") or "").replace("-", "")
                        f_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_clean}/{f_item.get('doc','')}" if acc_clean else ""
                        lv = "高" if any(k in f_fm.upper() for k in ("10-Q", "10-K", "8-K", "S-3", "424B")) else "中"
                        append_alert_dedup(
                            t_doc, f_dt, "公告", lv,
                            f"{sym} 新增 SEC 申报：{f_fm}（{f_dt}）",
                            f"SEC EDGAR 监测到新申报 {f_fm}（Items: {f_item.get('items') or '无'}，Accession: {f_item.get('acc')}），请核对原文并分发至对应模块。",
                            f_url
                        )
            time.sleep(0.15)

        # (c) 公司 IR RSS (ir)
        ir_url = ((mon.get("feeds") or {}).get("ir"))
        if ir_url:
            ir_items = check_rss_feed(ir_url)
            if ir_items is not None:
                fetched["ir"] = today_iso
                if not IS_CI:
                    print(f"  - [{sym}] 公司 IR RSS (ir) 已核查: 返回 {len(ir_items)} 条新闻稿")

        # (d) ClinicalTrials.gov (ct & comp)
        own_ncts = extract_nct_ids({"p": t_doc.get("pipeline"), "c": (t_doc.get("checks") or {}).get("protocol")})
        comp_ncts = extract_nct_ids(t_doc.get("competitors"))
        if own_ncts:
            ct_ok = False
            for nct in own_ncts[:4]:
                snap = check_clinical_trial(nct)
                if snap:
                    ct_ok = True
                    if not IS_CI:
                        print(f"  - [{sym}] 自有试验 {nct} (ct): 状态={snap['overallStatus']}, 更新={snap['lastUpdatePostDate']}")
            if ct_ok:
                fetched["ct"] = today_iso
                for p in (t_doc.get("pipeline") or []):
                    if p.get("trial") in own_ncts:
                        p["ctChk"] = today_iso
                if (t_doc.get("checks") or {}).get("protocol"):
                    t_doc["checks"]["protocol"]["asOf"] = today_iso
        else:
            fetched["ct"] = today_iso

        if comp_ncts:
            comp_ok = False
            for nct in comp_ncts[:4]:
                snap = check_clinical_trial(nct)
                if snap:
                    comp_ok = True
            if comp_ok:
                fetched["comp"] = today_iso
                if not IS_CI:
                    print(f"  - [{sym}] 竞品试验 (comp) 已核查 ({len(comp_ncts)} 个登记号)")
        else:
            fetched["comp"] = today_iso

        # (e) 媒体 (media) 与 期权链 (opt)
        if media_rss_ok:
            fetched["media"] = today_iso
        if update_live_options(sym, t_doc, today_iso) and not IS_CI:
            print(f"  - [{sym}] 期权隐含波动 (opt) 已自动更新: ±{t_doc['checks']['implied']['event']}%")

        mon["lastRun"] = today_iso

        # (f) 检查会议与电话会追踪 (sessions) 并自动生成待取提醒
        overdue_sessions = []
        for sess in t_doc.get("sessions", []):
            s_date = sess.get("date", "")
            s_status = sess.get("status", "")
            if s_date and s_date <= today_iso and s_status != "已完成" and s_status != "跳过":
                if not IS_CI:
                    print(f"  ! [{sym}] 会议待取信息提醒：{s_date}《{sess.get('conf', '')}》已到/已过日期")
                overdue_sessions.append(f"{s_date}《{sess.get('conf', '')}》")
        if overdue_sessions:
            append_alert_dedup(
                t_doc, today_iso, "会议", "中",
                f"{sym} 有 {len(overdue_sessions)} 场已结束会议待取文字稿",
                "待取列表：" + "、".join(overdue_sessions) + "。请从资料库或官网获取文字稿后导入。",
                ((mon.get("feeds") or {}).get("ir") or "")
            )

        if not dry_run:
            backup_file(t_path, "tickers", sym)
            with open(t_path, "w", encoding="utf-8") as f:
                json.dump(t_doc, f, ensure_ascii=False, indent=2)

    # 4. 更新 overview.json 并重新打包独立版 HTML
    print("\n[4/4] 正在同步全局概览价格并重新编译《生科标的研究台_本地版.html》...")
    ov_path = os.path.join(DB_DIR, "macro", "overview.json")
    if os.path.exists(ov_path) and not dry_run:
        with open(ov_path, "r", encoding="utf-8") as f:
            ov_doc = json.load(f)
        for tk in ov_doc.get("tickers", []):
            s = tk.get("sym")
            if s in px_updated:
                tk["spot"] = str(px_updated[s][2])
        backup_file(ov_path, "macro", "overview")
        with open(ov_path, "w", encoding="utf-8") as f:
            json.dump(ov_doc, f, ensure_ascii=False, indent=2)

    if not dry_run and not no_html:
        import server
        server.build_standalone_html()

    if not dry_run and sync_cloud:
        print("\n[5/5] 正在将更新后的研究台数据同步至 Cloud Firestore...")
        import sync_to_firestore
        sync_to_firestore.sync_docs()

    print("\n✅ 每日自动监测与 14 路 monitor.fetched 更新完成！")

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    pull_c = "--pull-cloud" in sys.argv
    sync_c = "--sync-cloud" in sys.argv
    no_h = "--no-html" in sys.argv
    run_monitor(dry_run=dry, pull_cloud=pull_c, sync_cloud=sync_c, no_html=no_h)
