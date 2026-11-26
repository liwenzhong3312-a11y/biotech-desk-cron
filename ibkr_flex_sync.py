#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生科标的研究台 · IBKR 官方只读 Flex Web Service V3 同步模块
功能：
1. 从环境变量 / 项目 .env / ~/.env 安全读取 IBKR_FLEX_TOKEN 与 IBKR_FLEX_QUERY_ID（绝不打印密钥）。
2. 通过 IBKR Flex Web Service V3 两步接口拉取 XML，或通过 --xml-file 解析本地导出的 Activity Flex XML。
3. 严格对齐研究台数据结构：
   - 更新 db/tickers/*.json 的 position（保留用户手填的 target/action/plan/holdPct/runupPlan）、monitor.fetched.pos 与持股变动 alerts。
   - 只增不删归档成交至 db/macro/fillsLog.json（双重去重 + 符号规范化），近 7 天写入 db/macro/fills.json。
4. 自动备份修改前的 JSON 至 undo/，按需重建独立版 HTML 与增量同步至 Cloud Firestore。
"""

import os
import re
import sys
import time
import json
import shutil
import socket
import argparse
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(ROOT_DIR, "db")
UNDO_DIR = os.path.join(ROOT_DIR, "undo")
FALLBACK_PROXY = "http://127.0.0.1:20808"

FLEX_ENDPOINTS = [
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest",
    "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest",
]


def _parse_env_file(path):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                    v = v[1:-1]
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


def load_ibkr_credentials():
    """按优先级从环境变量 -> 项目 .env -> ~/.env 读取凭证，不打印值。"""
    sources = [
        _parse_env_file(os.path.expanduser("~/.env")),
        _parse_env_file(os.path.join(ROOT_DIR, ".env")),
        os.environ,
    ]
    merged = {}
    for src in sources:
        for key in ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID"):
            val = (src.get(key) or "").strip()
            if val:
                merged[key] = val
    return merged.get("IBKR_FLEX_TOKEN", ""), merged.get("IBKR_FLEX_QUERY_ID", "")


def has_ibkr_credentials():
    tok, qid = load_ibkr_credentials()
    return bool(tok and qid)


def _http_get(url, proxy_url=None, timeout=25):
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "BiotechResearchDesk/2.0"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _local_proxy_open(host="127.0.0.1", port=20808):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _http_get_with_fallback(url, explicit_proxy=None, timeout=25):
    if explicit_proxy:
        return _http_get(url, proxy_url=explicit_proxy, timeout=timeout)
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if env_proxy:
        return _http_get(url, proxy_url=env_proxy, timeout=timeout)
    sys_proxies = urllib.request.getproxies()
    sys_p = sys_proxies.get("https") or sys_proxies.get("http")
    try:
        return _http_get(url, proxy_url=sys_p, timeout=timeout)
    except Exception:
        if sys_p != FALLBACK_PROXY and _local_proxy_open():
            return _http_get(url, proxy_url=FALLBACK_PROXY, timeout=timeout)
        raise


def fetch_flex_xml(token, query_id, proxy=None):
    """调用 IBKR Flex Web Service V3 两步接口拉取完整 XML 文本。"""
    ref_code = None
    stmt_url = None
    last_err = None

    for base in FLEX_ENDPOINTS:
        send_url = f"{base}?t={urllib.parse.quote(token)}&q={urllib.parse.quote(query_id)}&v=3"
        try:
            raw = _http_get_with_fallback(send_url, explicit_proxy=proxy, timeout=25)
            root = ET.fromstring(raw)
            status = (root.findtext("Status") or "").strip()
            if status == "Success":
                ref_code = (root.findtext("ReferenceCode") or "").strip()
                stmt_url = (root.findtext("Url") or "").strip()
                if ref_code and stmt_url:
                    break
            err_code = (root.findtext("ErrorCode") or "").strip()
            err_msg = (root.findtext("ErrorMessage") or "").strip()
            last_err = f"SendRequest 失败 (ErrorCode={err_code}): {err_msg}"
        except Exception as e:
            last_err = f"SendRequest 网络异常 ({base}): {type(e).__name__}"

    if not ref_code or not stmt_url:
        raise RuntimeError(last_err or "IBKR Flex SendRequest 未返回有效 ReferenceCode")

    # Step 2: GetStatement（处理 1019 生成中 / 1018 限频，最多重试 8 次）
    delays = [2, 2, 3, 4, 5, 5, 5, 5]
    for attempt in range(len(delays) + 1):
        sep = "&" if "?" in stmt_url else "?"
        get_url = f"{stmt_url}{sep}t={urllib.parse.quote(token)}&q={urllib.parse.quote(ref_code)}&v=3"
        raw = _http_get_with_fallback(get_url, explicit_proxy=proxy, timeout=40)
        stripped = raw.lstrip()
        if stripped.startswith("<FlexQueryResponse") or stripped.startswith("<?xml"):
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as pe:
                raise RuntimeError(f"IBKR 返回 XML 解析失败: {pe}")
            if root.tag == "FlexStatementResponse":
                status = (root.findtext("Status") or "").strip()
                err_code = (root.findtext("ErrorCode") or "").strip()
                err_msg = (root.findtext("ErrorMessage") or "").strip()
                if status != "Success":
                    if err_code in ("1018", "1019") and attempt < len(delays):
                        time.sleep(delays[attempt])
                        continue
                    raise RuntimeError(f"GetStatement 失败 (ErrorCode={err_code}): {err_msg}")
            return raw
        if attempt < len(delays):
            time.sleep(delays[attempt])
            continue
        raise RuntimeError("GetStatement 返回非 XML 响应")
    raise RuntimeError("GetStatement 重试次数耗尽")


def _num(val, default=None):
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _clean_qty(val):
    n = round(float(val), 4)
    if abs(n - round(n)) < 1e-6:
        return int(round(n))
    return n


def _nth_weekday_of_month(year, month, weekday, n):
    """weekday: Monday=0 .. Sunday=6"""
    first = datetime(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    return datetime(year, month, day)


def _et_offset_hours(dt_naive):
    """纯标准库美东夏令时换算：3 月第 2 个周日 02:00 ~ 11 月第 1 个周日 02:00 为 UTC-4，其余 UTC-5。"""
    y = dt_naive.year
    dst_start = _nth_weekday_of_month(y, 3, 6, 2).replace(hour=2, minute=0, second=0)
    dst_end = _nth_weekday_of_month(y, 11, 6, 1).replace(hour=2, minute=0, second=0)
    return -4 if dst_start <= dt_naive < dst_end else -5


def parse_ibkr_datetime_to_utc(dt_str):
    """将 IBKR Flex 的日期时间字符串统一转为 UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'。"""
    s = (dt_str or "").strip()
    if not s:
        return ""
    if s.endswith("Z") and "T" in s:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    # 统一分隔符
    norm = s.replace(";", " ").replace(",", " ").replace("T", " ").strip()
    norm = re.sub(r"\s+", " ", norm)

    # 提取末尾时区标记（若有）
    tz_hint = None
    m_tz = re.search(r"\s+(EST|EDT|UTC|GMT|[+-]\d{2}:?\d{2})$", norm, re.I)
    if m_tz:
        tz_hint = m_tz.group(1).upper()
        norm = norm[:m_tz.start()].strip()

    dt_naive = None
    for fmt in ("%Y%m%d %H%M%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H%M%S", "%Y%m%d", "%Y-%m-%d"):
        try:
            dt_naive = datetime.strptime(norm, fmt)
            break
        except ValueError:
            continue
    if dt_naive is None:
        return s

    if tz_hint in ("UTC", "GMT"):
        dt_utc = dt_naive
    elif tz_hint == "EST":
        dt_utc = dt_naive + timedelta(hours=5)
    elif tz_hint == "EDT":
        dt_utc = dt_naive + timedelta(hours=4)
    elif tz_hint and (tz_hint.startswith("+") or tz_hint.startswith("-")):
        sign = 1 if tz_hint[0] == "+" else -1
        digits = tz_hint[1:].replace(":", "")
        oh = int(digits[:2])
        om = int(digits[2:4]) if len(digits) >= 4 else 0
        dt_utc = dt_naive - timedelta(minutes=sign * (oh * 60 + om))
    else:
        # IBKR Flex 默认时间为美东时间（America/New_York）
        off = _et_offset_hours(dt_naive)
        dt_utc = dt_naive - timedelta(hours=off)

    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_flex_xml(xml_text):
    """
    解析 IBKR Activity Flex XML。
    返回:
      nav_total: float | None
      has_open_pos_section: bool
      positions: {sym: {"shares": ..., "avgCost": ..., "marketPrice": ..., "weightPct": ...}}
      trades: [{"id", "sym", "side", "qty", "px", "comm", "time"}, ...]
      statement_to: str
    """
    root = ET.fromstring(xml_text)

    # 0. statement 时间戳
    statement_to = ""
    for stmt in root.iter("FlexStatement"):
        if stmt.attrib.get("toDate"):
            statement_to = stmt.attrib["toDate"]
            break

    # 1. 提取账户总净值 NAV
    nav_total = None
    eq_rows = list(root.iter("EquitySummaryByReportDateInBase"))
    if eq_rows:
        eq_rows.sort(key=lambda e: e.attrib.get("reportDate", ""))
        nav_total = _num(eq_rows[-1].attrib.get("total"))
    if nav_total is None:
        for chg in root.iter("ChangeInNAV"):
            nav_total = _num(chg.attrib.get("endingValue"))
            if nav_total is not None:
                break

    # 2. 解析持仓 OpenPosition（仅 STK，优先 SUMMARY 防止 LOT 重复）
    has_open_pos_section = any(True for _ in root.iter("OpenPositions")) or any(True for _ in root.iter("OpenPosition"))
    raw_pos_by_sym = {}
    for el in root.iter("OpenPosition"):
        a = el.attrib
        if (a.get("assetCategory") or "STK").upper() != "STK":
            continue
        sym = (a.get("symbol") or "").strip().upper()
        if not sym:
            continue
        lod = (a.get("levelOfDetail") or "").strip().upper()
        raw_pos_by_sym.setdefault(sym, []).append((lod, a))

    positions = {}
    for sym, rows in raw_pos_by_sym.items():
        summary_rows = [a for lod, a in rows if lod in ("SUMMARY", "")]
        if summary_rows:
            a = summary_rows[0]
            qty_val = _num(a.get("position"), 0.0)
            avg_cost = _num(a.get("costBasisPrice"))
            mkt_px = _num(a.get("markPrice"))
            pct_nav = _num(a.get("percentOfNAV"))
            pos_val = _num(a.get("positionValue"))
            if nav_total is None and pos_val is not None and pct_nav and abs(pct_nav) > 1e-6:
                nav_total = pos_val / (pct_nav / 100.0)
        else:
            # 只有 LOT 行：按股数汇总与加权成本
            lot_rows = [a for _, a in rows]
            qty_val = sum(_num(a.get("position"), 0.0) for a in lot_rows)
            tot_cost_money = sum((_num(a.get("position"), 0.0) * _num(a.get("costBasisPrice"), 0.0)) for a in lot_rows)
            avg_cost = (tot_cost_money / qty_val) if abs(qty_val) > 1e-9 else None
            mkt_px = _num(lot_rows[0].get("markPrice"))
            pct_nav_sum = sum((_num(a.get("percentOfNAV"), 0.0) for a in lot_rows))
            pct_nav = pct_nav_sum if any(a.get("percentOfNAV") for a in lot_rows) else None
            pos_val = sum((_num(a.get("positionValue"), 0.0) for a in lot_rows)) if any(a.get("positionValue") for a in lot_rows) else None

        if pct_nav is None and nav_total and nav_total > 0 and pos_val is not None:
            pct_nav = pos_val / nav_total * 100.0

        positions[sym] = {
            "shares": _clean_qty(qty_val),
            "avgCost": round(avg_cost, 8) if avg_cost is not None else None,
            "marketPrice": round(mkt_px, 8) if mkt_px is not None else None,
            "weightPct": round(pct_nav, 3) if pct_nav is not None else None,
        }

    # 3. 解析成交 Trade / TradeConfirm（仅 STK，防 ORDER/EXECUTION 重复）
    trade_elems = list(root.iter("Trade")) + list(root.iter("TradeConfirm"))
    has_exec_lod = any((el.attrib.get("levelOfDetail") or "").strip().upper() == "EXECUTION" for el in trade_elems)

    trades = []
    seen_local = set()
    for el in trade_elems:
        a = el.attrib
        if (a.get("assetCategory") or "STK").upper() != "STK":
            continue
        lod = (a.get("levelOfDetail") or "").strip().upper()
        if lod in ("ORDER", "SYMBOL_SUMMARY", "SUBTOTAL", "TOTAL"):
            continue
        if has_exec_lod and lod and lod != "EXECUTION":
            continue

        sym = (a.get("symbol") or "").strip().upper()
        if not sym:
            continue
        bs_raw = (a.get("buySell") or "").strip().upper()
        if bs_raw in ("BUY", "BOT"):
            side = "BUY"
        elif bs_raw in ("SELL", "SLD"):
            side = "SELL"
        else:
            continue

        qty_raw = _num(a.get("quantity"))
        px_raw = _num(a.get("tradePrice"))
        if qty_raw is None or px_raw is None or abs(qty_raw) < 1e-9:
            continue

        qty = _clean_qty(abs(qty_raw))
        px = round(px_raw, 6)
        comm_ibkr = _num(a.get("ibCommission"), 0.0)
        # IBKR Flex 支付佣金为负、返佣为正；研究台 fillsLog 约定支付为正、返佣为负
        comm = round(-comm_ibkr, 6)

        dt_utc = parse_ibkr_datetime_to_utc(a.get("dateTime") or a.get("tradeDate") or "")
        raw_exec_id = (a.get("ibExecID") or a.get("tradeID") or a.get("transactionID") or "").strip()
        exec_id = raw_exec_id if raw_exec_id else f"{sym}.{side}.{qty}.{px}.{dt_utc}"

        if exec_id in seen_local:
            continue
        seen_local.add(exec_id)

        trades.append({
            "id": exec_id,
            "sym": sym,
            "side": side,
            "qty": qty,
            "px": px,
            "comm": comm,
            "time": dt_utc,
            "has_exec_id": bool(raw_exec_id),
        })

    trades.sort(key=lambda x: x["time"])
    return nav_total, has_open_pos_section, positions, trades, statement_to


def _backup_file(path, col, doc_id):
    if not os.path.exists(path):
        return
    os.makedirs(UNDO_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    shutil.copy2(path, os.path.join(UNDO_DIR, f"{col}__{doc_id}__{ts}.json"))


def _append_pos_alert(t_doc, date_str, sym, old_sh, new_sh):
    title = f"{sym} 持股由 {old_sh} 股变为 {new_sh} 股"
    alerts = t_doc.setdefault("alerts", [])
    for a in alerts:
        if a.get("date") == date_str and a.get("title") == title:
            return
    alerts.insert(0, {
        "date": date_str,
        "kind": "持仓",
        "level": "低",
        "read": "no",
        "title": title,
        "detail": f"IBKR Flex 只读同步检测到持仓股数变化（{old_sh} → {new_sh} 股），已自动刷新持仓与占组合比例。",
        "url": "",
    })


def compute_ib_avg_costs(fills):
    """按 IBKR 持仓页口径计算每个标的的「移动加权平均成本（含佣金）」：买入加权并摊入佣金，卖出不改变均价，清仓后归零。"""
    out = {}
    for sym in sorted({x.get("sym") for x in fills if x.get("sym")}):
        sh, avg = 0.0, 0.0
        for x in sorted((f for f in fills if f.get("sym") == sym), key=lambda f: str(f.get("time", ""))):
            q = float(x.get("qty") or 0)
            px = float(x.get("px") or 0)
            cm = float(x.get("comm") or 0)
            if x.get("side") == "BUY":
                avg = (sh * avg + q * px + cm) / (sh + q) if (sh + q) > 0 else 0.0
                sh += q
            else:
                sh -= q
                if abs(sh) < 1e-6:
                    sh, avg = 0.0, 0.0
        out[sym] = {"shares": sh, "avg": avg}
    return out


def run_ibkr_sync(xml_file=None, dry_run=False, rebuild_html=True, sync_cloud=False, proxy=None):
    """执行 IBKR Flex 同步主流程，返回结果摘要字典。"""
    if xml_file:
        with open(xml_file, "r", encoding="utf-8", errors="replace") as f:
            xml_text = f.read()
        src_label = f"IBKR Flex XML（{os.path.basename(xml_file)}）"
    else:
        token, query_id = load_ibkr_credentials()
        if not token or not query_id:
            raise RuntimeError("未配置 IBKR_FLEX_TOKEN 或 IBKR_FLEX_QUERY_ID，请先在 ~/.env 中设置。")
        xml_text = fetch_flex_xml(token, query_id, proxy=proxy)
        src_label = "IBKR Flex Web Service V3"

    nav_total, has_open_pos, pos_map, new_trades, stmt_to = parse_flex_xml(xml_text)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    now_hm = datetime.now().strftime("%Y-%m-%d %H:%M")

    changed_docs = []
    pos_changes = []

    # 0. 预先合并成交，按 IBKR 持仓页口径（含佣金的移动加权均价）计算持仓成本，保证与 IBKR 软件一致
    _fl_path = os.path.join(DB_DIR, "macro", "fillsLog.json")
    _pre_fills = []
    if os.path.isfile(_fl_path):
        with open(_fl_path, "r", encoding="utf-8") as _f:
            _pre_fills = list(json.load(_f).get("fills") or [])
    _pre_ids = {x.get("id") for x in _pre_fills if x.get("id")}
    _pre_fills += [t for t in new_trades if t["id"] not in _pre_ids]
    ib_avg = compute_ib_avg_costs(_pre_fills)

    # 1. 更新 db/tickers/*.json 的 position
    tickers_dir = os.path.join(DB_DIR, "tickers")
    if has_open_pos and os.path.isdir(tickers_dir):
        for fn in sorted(f for f in os.listdir(tickers_dir) if f.endswith(".json")):
            sym = fn[:-5]
            t_path = os.path.join(tickers_dir, fn)
            with open(t_path, "r", encoding="utf-8") as f:
                t_doc = json.load(f)

            old_pos = dict(t_doc.get("position") or {})
            old_sh = old_pos.get("shares")

            if sym in pos_map:
                p_new = pos_map[sym]
                merged_pos = dict(old_pos)
                merged_pos["shares"] = p_new["shares"]
                _ib = ib_avg.get(sym)
                if _ib and abs(_ib["shares"] - p_new["shares"]) < 1e-6 and _ib["avg"] > 0:
                    merged_pos["avgCost"] = round(_ib["avg"], 8)
                    merged_pos["avgCostSrc"] = "IBKR 持仓均价（移动加权，含佣金，与 IBKR 软件一致）"
                    merged_pos["avgCostFifo"] = p_new["avgCost"]
                else:
                    merged_pos["avgCost"] = p_new["avgCost"]
                    merged_pos["avgCostSrc"] = "IBKR Flex 批次均价（成交档案未对平，沿用 Flex）"
                merged_pos["marketPrice"] = p_new["marketPrice"]
                if p_new["weightPct"] is not None:
                    merged_pos["weightPct"] = p_new["weightPct"]
                merged_pos["source"] = "IBKR"
                merged_pos["syncedAt"] = now_utc
                new_sh = p_new["shares"]
            else:
                merged_pos = dict(old_pos)
                merged_pos["shares"] = 0
                merged_pos["avgCost"] = None
                merged_pos["marketPrice"] = None
                merged_pos["weightPct"] = 0
                merged_pos["source"] = "IBKR"
                merged_pos["syncedAt"] = now_utc
                new_sh = 0

            t_doc["position"] = merged_pos
            mon = t_doc.setdefault("monitor", {})
            fetched = mon.setdefault("fetched", {})
            fetched["pos"] = today_iso

            if old_sh is not None and old_sh != new_sh:
                _append_pos_alert(t_doc, today_iso, sym, old_sh, new_sh)
                pos_changes.append((sym, old_sh, new_sh))

            if not dry_run:
                _backup_file(t_path, "tickers", sym)
                with open(t_path, "w", encoding="utf-8") as f:
                    json.dump(t_doc, f, ensure_ascii=False, indent=2)
                changed_docs.append(f"tickers/{sym}")

    # 2. 合并归档成交至 macro/fillsLog.json（只增不删）与近 7 天 macro/fills.json
    fills_log_path = os.path.join(DB_DIR, "macro", "fillsLog.json")
    fills_7d_path = os.path.join(DB_DIR, "macro", "fills.json")

    if os.path.isfile(fills_log_path):
        with open(fills_log_path, "r", encoding="utf-8") as f:
            fl_doc = json.load(f)
    else:
        fl_doc = {"fills": []}

    existing_fills = list(fl_doc.get("fills") or [])
    seen_ids = {x.get("id") for x in existing_fills if x.get("id")}
    seen_fp = {
        (x.get("sym"), x.get("side"), float(x.get("qty", 0)), round(float(x.get("px", 0)), 4), str(x.get("time", ""))[:16])
        for x in existing_fills
    }

    added_trades = []
    for tr in new_trades:
        has_eid = tr.pop("has_exec_id", True)
        fp = (tr["sym"], tr["side"], float(tr["qty"]), round(float(tr["px"]), 4), tr["time"][:16])
        if tr["id"] in seen_ids:
            continue
        if not has_eid and fp in seen_fp:
            continue
        seen_ids.add(tr["id"])
        seen_fp.add(fp)
        added_trades.append(tr)

    all_fills = sorted(existing_fills + added_trades, key=lambda x: str(x.get("time", "")))
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_7d = [x for x in all_fills if str(x.get("time", "")) >= cutoff_7d]

    if not dry_run and (added_trades or not os.path.isfile(fills_7d_path)):
        _backup_file(fills_log_path, "macro", "fillsLog")
        fl_doc["fills"] = all_fills
        fl_doc["syncedTo"] = now_hm
        fl_doc["updatedAt"] = now_hm
        fl_doc["src"] = src_label
        with open(fills_log_path, "w", encoding="utf-8") as f:
            json.dump(fl_doc, f, ensure_ascii=False, indent=2)
        changed_docs.append("macro/fillsLog")

        _backup_file(fills_7d_path, "macro", "fills")
        with open(fills_7d_path, "w", encoding="utf-8") as f:
            json.dump({
                "fills": recent_7d,
                "src": f"{src_label}（近 7 天）",
                "updatedAt": now_hm,
            }, f, ensure_ascii=False, indent=2)
        changed_docs.append("macro/fills")

    if not dry_run and rebuild_html:
        import server
        server.build_standalone_html()

    if not dry_run and sync_cloud and changed_docs:
        import sync_to_firestore
        sync_to_firestore.sync_docs(targets=changed_docs)

    return {
        "nav_total": nav_total,
        "has_open_pos": has_open_pos,
        "positions": pos_map,
        "pos_changes": pos_changes,
        "parsed_trades": len(new_trades),
        "added_trades": len(added_trades),
        "total_fills_log": len(all_fills),
        "recent_7d": len(recent_7d),
        "changed_docs": changed_docs,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(description="IBKR 官方只读 Flex Web Service 持仓与成交同步")
    parser.add_argument("--xml-file", help="从本地导出的 IBKR Activity Flex XML 文件同步（可用于补录历史成交）")
    parser.add_argument("--dry-run", action="store_true", help="仅解析与核对，不写入 db/ 文件")
    parser.add_argument("--no-html", action="store_true", help="不重新编译独立版 HTML")
    parser.add_argument("--sync-cloud", action="store_true", help="同步变更的文档至 Cloud Firestore")
    parser.add_argument("--proxy", help="显式指定代理 URL（如 http://127.0.0.1:20808）")
    args = parser.parse_args()

    res = run_ibkr_sync(
        xml_file=args.xml_file,
        dry_run=args.dry_run,
        rebuild_html=not args.no_html,
        sync_cloud=args.sync_cloud,
        proxy=args.proxy,
    )
    print("==================================================")
    print(f"  IBKR Flex 只读同步完成 {'[DRY-RUN]' if res['dry_run'] else ''}")
    print("==================================================")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"  已刷新持仓标的数: {len(res['positions'])} 个 | 本次新增成交: {res['added_trades']} 笔")
    else:
        print(f"  账户净值 (NAV): {res['nav_total']}")
        print(f"  识别到持仓标的: {len(res['positions'])} 个 -> { {k: v['shares'] for k, v in res['positions'].items()} }")
        if res["pos_changes"]:
            print(f"  持股变动: {res['pos_changes']}")
        print(f"  XML 股票成交: {res['parsed_trades']} 笔 | 本次新增入库: {res['added_trades']} 笔 | 档案总计: {res['total_fills_log']} 笔")
        if res["changed_docs"]:
            print(f"  已更新文档: {', '.join(res['changed_docs'])}")


if __name__ == "__main__":
    main()
