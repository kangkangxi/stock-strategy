#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股尾盘短线策略引擎：14:30 强势股筛选 → 尾盘买入 → 次日冲高卖出
=================================================================
独立于「股票知识库」盯盘系统的第二套交易逻辑。纯标准库，无第三方依赖。

设计原则（对应策略文档 §13 Agent 执行原则）
  - 只用「当时可获得」的数据：10 分钟 K 线只取已完成的 bar，绝不使用未收盘 bar 或未来 K 线；
    日 K 的最后一根（当日进行中）明确标记为「进行中」。
  - 报告严格区分：已确认事实数据 / 指标计算结果 / 主观判断（预案）。
  - 数据缺失或校验失败时明确标记，不编造；核心校验失败时不出交易名单。
  - 没有符合条件的股票时输出「今日无交易信号」，不强行推荐。
  - 默认只输出分析与交易计划，不自动下单。

模式
  --mode scan    14:31 全市场涨幅榜初筛 → 日K位置 → 10分钟MA5启动 → 尾盘强度 → 评分 → 候选报告+推送
  --mode pm      14:50 持仓退出处理 + 尾盘确认（按最新行情复核当日候选）→ 买入清单+推送
  --mode exit    次日 9:35 / 14:50 持仓情景判定（A-E）→ 卖出信号+推送
  辅助：--status / --list / --hold CODE PRICE [SHARES] / --close CODE [PRICE]

时间基准全部取本机时间（CST）。测试/复算可用 --force-now "YYYY-MM-DD HH:MM" 覆盖时钟，
但引擎自身不会去取未来数据：所有 bar 都会按「时间 <= 时钟」过滤。
"""

import argparse
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.realpath(__file__))
BASE = os.environ.get("SHORT_TERM_BASE") or os.path.dirname(HERE)
REPORT_DIR = os.path.join(BASE, "报告")
LOG_DIR = os.path.join(BASE, "日志")
POS_FILE = os.path.join(BASE, "positions.json")
CFG_FILE = os.path.join(BASE, "config.json")
WECOM_PUSH = os.environ.get("WECOM_PUSH") or os.path.join(BASE, "scripts", "wecom_push.py")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
EM_REF = "http://quote.eastmoney.com/"
SINA_REF = "https://finance.sina.com.cn"

# 全A股（沪深主板/创业/科创/北交所）筛选范围；北交所默认在初筛里排除
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS_CLIST = "f2,f3,f5,f6,f8,f10,f12,f13,f14,f18,f20,f21,f22,f26,f62,f100,f124"

DEFAULT_CFG = {
    # 一、涨幅初筛（待回测校准的初始参数，不是市场规律）
    "pct_low": 3.0,
    "pct_high": 8.0,
    "near_limit_pct": 1.5,          # 距涨停价 < 该百分比 视为接近涨停 → 风险标记
    "min_amount_yi": 2.0,           # 最小成交额（亿元）
    "min_turnover": 2.0,            # 最小换手率 %
    "max_turnover": 30.0,           # 最大换手率 %（流动性/异动异常）
    "min_volume_ratio": 0.8,        # 最小量比
    "max_candidates_analyze": 90,   # 第二阶段（K线分析）最多取多少只
    "fetch_workers": 4,             # K线并发抓取线程数（过高易被东财限流）
    "exclude_boards": ["bj"],       # 排除板块：bj=北交所
    "exclude_new_days": 25,         # 上市不足 N 个自然日视为次新 → 排除
    # 二、技术条件
    "ma10m_period": 5,              # 10分钟均线周期（MA5）
    "max_dev_from_ma": 3.0,         # 现价高于 10分钟MA5 的最大乖离 %
    "min_launch_gain": 0.0,         # 启动后累计涨幅下限（可要求已启动）
    "max_launch_gain": 9.0,         # 启动以来累计涨幅上限（超过视为已追高）
    "max_5d_gain": 15.0,            # 过去5日累计涨幅上限（排除连续暴涨）
    "min_upside_pct": 3.0,          # 到最近压力位的最小空间 %
    "min_rr": 2.0,                  # 最小预期盈亏比
    "max_stop_pct": 5.5,            # 止损距离上限 %
    "min_stop_pct": 1.2,            # 止损距离下限 %
    # 三、账户与风险（账户总资金为 0 时只给止损价与盈亏比，不给股数）
    "account_equity": 0.0,
    "risk_per_trade_pct": 1.0,
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax_rate": 0.0005,       # 卖出
    "transfer_fee_rate": 0.00001,
    "slippage_pct": 0.10,           # 单边滑点 %
    # 四、执行
    "force_exit_time": "1450",      # 次日最迟退出时间
    "max_hold_days": 1,             # 初始策略：次日卖出
    "paper_track": True,            # 14:50 买入清单自动记为模拟持仓（用于次日卖出提醒）
    "max_buy_list": 5,             # 14:50 最终买入清单上限（避免同板块过度集中）
    "push": True,
    "push_max_stocks": 6,
    "push_top_n": 6,                # 评分表里推送前 N 名
    "summary_push_full": True,      # 周/月总结推全量（漏斗明细+全部分档+逐笔+诊断），False=只推摘要
    "summary_push_trades": 40,      # 全量推送里逐笔明细最多列多少笔
    "shadow_track": True,           # 影子跟踪：初筛池全量记「假设 14:51 买入、次日 14:51 卖出」
    "shadow_max_pool": 400,         # 影子池每日最多记多少只（初筛池通常 ~166）
    "exit_brief_from": "1000",      # 开盘后（该时刻起）的持仓巡检用简报格式，9:35 仍发完整情景
    "kline_snapshots": True,        # 每笔建仓（交易+影子池）记 T+1/T+2/T+3 三个交易日的 10 分钟K线
    "kline_snapshot_min_gap_days": 4,   # 建仓后满 N 个自然日（三天都已收完）才抓，一次取三天
    "kline_snapshot_max_age_days": 12,  # 超过 N 天（5分钟K线窗口外）不再补，标记无法补记
    "kline_snapshot_max_attempts": 3,   # 抓取尝试上限，到上限按现有数据定稿
    "score_weights": {
        "日K启动形态": 25,
        "10分钟MA5启动结构": 25,
        "尾盘强度": 20,
        "成交量配合": 15,
        "市场及板块环境": 10,
        "上方空间与风险收益比": 5,
    },
    "index_secids": ["1.000001", "0.399001", "0.399006", "1.000688"],
}


# ────────────────────────────── 基础工具 ──────────────────────────────
def now_clock(force=None):
    return force if force else datetime.now()


def log(msg):
    print(msg, flush=True)


def in_trading_hours(clock):
    t = hhmm(clock)
    return ("0925" <= t <= "1135") or ("1255" <= t <= "1505")


def market_data_fresh(env, clock):
    """非交易日/数据未更新时返回 False（用于避免节假日推送陈旧数据）。"""
    ts = env.get("ts")
    if not ts:
        return True                     # 拿不到时间戳时不拦截，交由行情校验提示
    try:
        d = datetime.fromtimestamp(float(ts))
    except (ValueError, OSError, OverflowError):
        return True
    return d.strftime("%Y-%m-%d") == clock.strftime("%Y-%m-%d")


def num(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def hhmm(dt):
    return dt.strftime("%H%M")


def hm(dt):
    return dt.strftime("%H:%M")


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 读取 config.json 失败，使用默认参数：{e}")
    else:
        save_json(CFG_FILE, cfg)
    return cfg


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


AUDIT_ERRORS = []


def audit_event(kind, data):
    return None


def audit_emit(kind, data):
    try:
        audit_event(kind, data)
    except Exception as error:
        name = type(error).__name__
        if name not in AUDIT_ERRORS:
            AUDIT_ERRORS.append(name)


def http_get(url, ref=None, enc="utf-8", timeout=15, retries=3):
    last = None
    for i in range(retries):
        started = datetime.now().astimezone().isoformat()
        try:
            h = dict(UA)
            if ref:
                h["Referer"] = ref
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            audit_emit("http", {"url": url, "encoding": enc, "attempt": i + 1,
                                 "started_at": started, "received_at": datetime.now().astimezone().isoformat(),
                                 "raw": raw})
            return raw.decode(enc, "ignore")
        except Exception as e:  # noqa: BLE001
            audit_emit("http", {"url": url, "encoding": enc, "attempt": i + 1,
                                 "started_at": started, "received_at": datetime.now().astimezone().isoformat(),
                                 "error_type": type(e).__name__})
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"请求失败 {url}：{last}")


def http_json(url, ref=None, timeout=15, retries=3):
    txt = http_get(url, ref=ref, timeout=timeout, retries=retries)
    txt = txt.strip()
    return json.loads(repair_json(txt))


def repair_json(txt):
    """新浪部分接口键名无引号 → 补引号。"""
    if txt.startswith("[") or txt.startswith("{"):
        return txt
    return txt


# ────────────────── 数据源可用性与熔断（避免被限流后继续猛打） ──────────────────
EM_BREAK = {"fails": 0, "until": 0.0}
EM_BREAK_AFTER = 6          # 东财连续失败次数阈值
EM_BREAK_SECONDS = 120      # 触发后暂停东财 K 线请求的时长（秒）


def em_ok():
    return time.time() >= EM_BREAK["until"]


def _em_fail():
    EM_BREAK["fails"] += 1
    if EM_BREAK["fails"] >= EM_BREAK_AFTER and EM_BREAK["until"] <= time.time():
        EM_BREAK["until"] = time.time() + EM_BREAK_SECONDS
        EM_BREAK["fails"] = 0
        log(f"[warn] 东财接口连续失败 {EM_BREAK_AFTER} 次 → 熔断 {EM_BREAK_SECONDS}s，期间改用备用数据源")


def _em_success():
    EM_BREAK["fails"] = 0
    EM_BREAK["logged"] = False


def brief_err(e):
    """异常文本压缩成一句可推送的话（去掉整条 URL，避免推送里堆参数）。"""
    s = str(e)
    for a, b in (("Remote end closed connection without response", "东财主动断开连接"),
                 ("RemoteDisconnected", "连接被断开"),
                 ("Connection reset by peer", "连接被重置"),
                 ("timed out", "超时"),
                 ("Cannot assign requested address", "网络地址不可用")):
        s = s.replace(a, b)
    s = re.sub(r"https?://[^\s：，。；）】\]]*", "", s)
    s = re.sub(r"\s*[：:]\s*", "：", s)
    s = re.sub(r"\s+", " ", s).strip(" ：:、;；（）")
    return (s or type(e).__name__)[:80]


# ────────────────────────────── 板块/代码工具 ──────────────────────────────
def board_of(code):
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "30", "20")):
        return "sz"
    if code.startswith(("8", "4", "92")):
        return "bj"
    return "sh" if code.startswith(("9", "5")) else "sz"


def secid_of(code):
    return ("1." if board_of(code) == "sh" else "0.") + code


def tx_of(code):
    return board_of(code) + code


def limit_pct(code, name):
    """涨跌停幅度（%）。ST 5%，主板 10%，创业/科创 20%，北交所 30%。"""
    if name and ("ST" in name.upper() or "退" in name):
        return 5.0
    b = board_of(code)
    if b == "bj":
        return 30.0
    if code.startswith(("30", "68")):
        return 20.0
    return 10.0


def is_st(name):
    if not name:
        return False
    up = name.upper()
    return "ST" in up or "退" in name


def is_new_listing(name):
    if not name:
        return False
    n = name.strip()
    return n.startswith(("N", "C")) or "N" == n[:1]


# ────────────────────────────── 数据源 ──────────────────────────────
def em_clist(fid="f3", pz=100, max_pages=60, stop_pct=3.0, fs=FS_ALL_A, fields=FIELDS_CLIST):
    """东财全市场列表（涨幅倒序翻页），返回 (rows, 全市场只数)。"""
    out, pn, total = [], 1, None
    while pn <= max_pages:
        url = (f"http://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1"
               f"&fltt=2&invt=2&fid={fid}&fs={fs}&fields={fields}")
        j = http_json(url, ref=EM_REF)
        total = (j.get("data") or {}).get("total") or total
        diff = ((j.get("data") or {}).get("diff")) or []
        if not diff:
            break
        for x in diff:
            out.append({
                "code": str(x.get("f12") or ""),
                "name": str(x.get("f14") or ""),
                "price": num(x.get("f2")),
                "pct": num(x.get("f3")),
                "vol": num(x.get("f5")),
                "amount": num(x.get("f6")),
                "turnover": num(x.get("f8")),
                "vol_ratio": num(x.get("f10")),
                "prev_close": num(x.get("f18")),
                "mktcap": num(x.get("f21")),
                "speed": num(x.get("f22")),
                "main_net": num(x.get("f62")),
                "industry": str(x.get("f100") or "").strip(),
                "list_date": str(x.get("f26") or ""),
            })
        last_pct = num(diff[-1].get("f3"))
        if last_pct is None or last_pct < stop_pct:
            break
        pn += 1
    return out, total


def sina_clist(stop_pct=3.0, max_pages=8, page_size=100):
    """新浪全市场涨幅榜（东财 clist 被拒时的备用源），返回 (rows, 全市场只数=None)。

    新浪该接口不提供 量比/主力净额/涨速/上市日期 → 这些字段为 None，
    stage1 对 None 是放行（不会误杀），但 prescore 会偏低，报告里会标注数据源。
    """
    out = []
    for page in range(1, max_pages + 1):
        url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num={page_size}&sort=changepercent&asc=0"
               "&node=hs_a&symbol=&_s_r_a=page")
        try:
            txt = http_get(url, ref=SINA_REF).strip()
            txt = re.sub(r'([{,])\s*"?([A-Za-z_]\w*)"?\s*:', r'\1"\2":', txt)
            arr = json.loads(txt)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 新浪涨幅榜第 {page} 页失败：{brief_err(e)}")
            break
        if not isinstance(arr, list) or not arr:
            break
        for x in arr:
            out.append({
                "code": str(x.get("code") or ""),
                "name": str(x.get("name") or ""),
                "price": num(x.get("trade")),
                "pct": num(x.get("changepercent")),
                "vol": num(x.get("volume")),
                "amount": num(x.get("amount")),
                "turnover": num(x.get("turnoverratio")),
                "vol_ratio": None,
                "prev_close": num(x.get("settlement")),
                "mktcap": num(x.get("nmc")),
                "speed": None, "main_net": None,
                "industry": "", "list_date": "",
            })
        last = num(arr[-1].get("changepercent"))
        if len(arr) < page_size or last is None or last < stop_pct:
            break
    return out, None


def em_kline(code, klt=5, lmt=420, timeout=15):
    """东财 K 线。klt=5 → 5分钟；klt=101 → 日线。fqt=0 不复权。

    东财 API 在被限流时会直接拒连（Empty reply / RemoteDisconnected），
    这里带熔断：连续失败若干次后本进程内不再请求东财，交给调用方走备用源。
    """
    if not em_ok():
        raise RuntimeError(f"东财接口熔断中（{int(EM_BREAK['until'] - time.time())}s 内不重试）")
    url = (f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid_of(code)}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt={klt}&fqt=0&end=20500101&lmt={lmt}")
    try:
        j = http_json(url, ref=EM_REF, timeout=timeout)
    except Exception:
        _em_fail()
        raise
    _em_success()
    kl = ((j.get("data") or {}).get("klines")) or []
    bars = []
    for line in kl:
        p = line.split(",")
        if len(p) < 7:
            continue
        bars.append({
            "t": p[0].strip(),
            "o": float(p[1]), "c": float(p[2]), "h": float(p[3]), "l": float(p[4]),
            "v": float(p[5]), "amt": float(p[6]),
        })
    return bars


def sina_kline(code, scale=5, datalen=320):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={tx_of(code)}&scale={scale}&ma=no&datalen={datalen}")
    txt = http_get(url, ref=SINA_REF).strip()
    txt = re.sub(r'([{,])\s*"?([A-Za-z_]\w*)"?\s*:', r'\1"\2":', txt)
    data = json.loads(txt)
    if not isinstance(data, list):
        return []
    bars = []
    for b in data:
        vol = float(b.get("volume", 0) or 0)          # 新浪为「股」
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        amt = float(b.get("amount", 0) or 0)
        est = not amt
        if est:
            # 新浪该接口不给成交额 → 用 (H+L+C)/3 估算，保证 VWAP 仍可计算（标记为估算值）
            amt = vol * (h + l + c) / 3.0
        bars.append({
            # 新浪时间戳带秒（"2026-10-14 15:00:00"），必须规范化成东财的 "YYYY-MM-DD HH:MM"，
            # 否则 build_10m 里 strptime("%Y-%m-%d %H:%M") 会失败、把全部 10 分钟桶当成非法对丢掉
            "t": _norm_ts(str(b["day"])),
            "o": float(b["open"]), "c": c, "h": h, "l": l,
            "v": vol / 100.0,                          # 股 → 手
            "amt": amt, "amt_est": est,
        })
    return bars


def _norm_ts(day):
    """把新浪的 "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD" 统一成东财风格 "YYYY-MM-DD HH:MM"。"""
    s = str(day).strip().replace("T", " ")
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:16]


def fetch_5m(code, clock, today, lmt=420):
    """5 分钟 K 线：东财优先，失败或数据不是当日时自动回退新浪。

    返回 (bars, src, status, errs, notes)：
      bars 为空 → 两个源都不可用（调用方必须把这种情况报成「数据缺失」，不能当作信号）；
      src 为实际数据源（东财 / 新浪 / ""）；status 为 validate_bars 的 ✅/⚠️/❌。
    """
    errs, notes, bars, src = [], [], [], ""
    if em_ok():
        time.sleep(random.uniform(0, 0.15))     # 打散并发请求，降低被限流概率
        try:
            bars = em_kline(code, klt=5, lmt=lmt)
            src = "东财"
        except Exception as e:  # noqa: BLE001
            errs.append(f"东财5分钟K线不可用（{brief_err(e)}）")
    elif not EM_BREAK.get("logged"):
        EM_BREAK["logged"] = True
        log("[i] 东财接口熔断中，本轮跳过东财、改用备用数据源")
    if not bars or bars[-1]["t"][:10] != today:
        try:
            b2 = sina_kline(code, scale=5, datalen=320)
            if b2 and (not bars or b2[-1]["t"] > bars[-1]["t"]):
                bars, src = b2, "新浪"
                if any(b.get("amt_est") for b in bars):
                    notes.append("5分钟K线取自新浪备用源，当日均价(VWAP)按 (H+L+C)/3 估算")
        except Exception as e:  # noqa: BLE001
            errs.append(f"新浪5分钟K线不可用（{brief_err(e)}）")
    if not bars:
        return [], src, "❌", errs or ["东财与新浪均无5分钟K线"], notes
    status, verrs = validate_bars(bars, f"5分钟K线({src})", clock, today)
    return bars, src, status, errs + verrs, notes


def tencent_quotes(codes):
    """腾讯实时行情（批量）。返回 code -> dict。字段含量比、涨跌停价。"""
    out = {}
    if not codes:
        return out
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        q = ",".join(tx_of(c) for c in chunk)
        try:
            txt = http_get("http://qt.gtimg.cn/q=" + q, enc="gbk", timeout=12)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 腾讯行情失败：{e}")
            continue
        for line in txt.strip().split(";"):
            if "=" not in line or '"' not in line:
                continue
            code_raw = line.split("=")[0].strip().replace("v_", "")
            f = line.split('"')[1].split("~")
            if len(f) < 50:
                continue
            code = code_raw[2:]
            g = lambda idx: num(f[idx]) if len(f) > idx else None  # noqa: E731
            out[code] = {
                "code": code, "name": f[1], "price": g(3), "prev_close": g(4), "open": g(5),
                "vol": g(6), "time": f[30] if len(f) > 30 else "", "change": g(31), "pct": g(32),
                "high": g(33), "low": g(34), "amount": (g(37) or 0) * 1e4, "turnover": g(38),
                "amplitude": g(43), "float_cap": g(44), "total_cap": g(45),
                "limit_up": g(47), "limit_down": g(48), "vol_ratio": g(49),
            }
    return out


def em_trends_1m(code):
    """东财当日分时（1分钟，含均价）。"""
    url = (f"http://push2.eastmoney.com/api/qt/stock/trends2/get?secid={secid_of(code)}"
           f"&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays=1&iscr=0&iscca=0")
    j = http_json(url, ref=EM_REF)
    out = []
    for line in ((j.get("data") or {}).get("trends") or []):
        p = line.split(",")
        if len(p) < 8:
            continue
        out.append({"t": p[0].strip(), "o": float(p[1]), "c": float(p[2]), "h": float(p[3]),
                    "l": float(p[4]), "v": float(p[5]), "amt": float(p[6]), "avg": float(p[7])})
    return out


def em_market_env():
    """指数 + 涨跌家数 + 行业板块。"""
    env = {"indices": [], "up": None, "down": None, "sectors_top": [], "sectors_bottom": []}
    try:
        url = ("http://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
               "&fields=f1,f2,f3,f4,f12,f13,f14,f104,f105,f106,f124"
               "&secids=1.000001,0.399001,0.399006,1.000688")
        j = http_json(url, ref=EM_REF)
        up = down = 0
        ts = 0
        for x in ((j.get("data") or {}).get("diff") or []):
            env["indices"].append({"name": x.get("f14"), "code": x.get("f12"),
                                   "price": num(x.get("f2")), "pct": num(x.get("f3")),
                                   "change": num(x.get("f4"))})
            ts = max(ts, num(x.get("f124")) or 0)
            if str(x.get("f12")) in ("000001", "399001"):
                up += num(x.get("f104")) or 0
                down += num(x.get("f105")) or 0
        env["up"], env["down"] = up, down
        env["ts"] = ts
    except Exception as e:  # noqa: BLE001
        env["error_indices"] = str(e)
    try:
        for key, po in (("sectors_top", 1), ("sectors_bottom", 0)):
            url = (f"http://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=10&po={po}&np=1&fltt=2"
                   f"&invt=2&fid=f3&fs=m:90+t:2&fields=f2,f3,f12,f14,f104,f105,f128,f140")
            j = http_json(url, ref=EM_REF)
            for x in ((j.get("data") or {}).get("diff") or []):
                env[key].append({"name": x.get("f14"), "code": x.get("f12"),
                                 "pct": num(x.get("f3")), "up": num(x.get("f104")),
                                 "down": num(x.get("f105")), "leader": x.get("f128")})
    except Exception as e:  # noqa: BLE001
        env["error_sectors"] = str(e)
    return env


# ────────────────────────────── 指标计算 ──────────────────────────────
def build_10m(bars5, clock):
    """5分钟（结束时间标注）按日内顺序两两合并成 10 分钟，只保留已完成的 bar。

    只使用时间 <= clock 的 5 分钟 bar；每桶 2 根 5 分钟 bar 齐全才算完成。
    A股每半日 5 分钟 bar 数为偶数（上午 24、下午 24），故按日内位置配对不会跨午休。
    """
    out = []
    cap = clock.strftime("%Y-%m-%d %H:%M")
    cur_day, buf = None, []

    def flush(day, buf_):
        for i in range(0, len(buf_) - 1, 2):
            a, b = buf_[i], buf_[i + 1]
            try:
                t1 = datetime.strptime(a["t"], "%Y-%m-%d %H:%M")
                t2 = datetime.strptime(b["t"], "%Y-%m-%d %H:%M")
                valid_pair = (t2 - t1).total_seconds() == 300
            except ValueError:
                valid_pair = False
            out.append({"t": b["t"], "o": a["o"], "c": b["c"],
                        "h": max(a["h"], b["h"]), "l": min(a["l"], b["l"]),
                        "v": a["v"] + b["v"], "amt": a.get("amt", 0) + b.get("amt", 0),
                        "_valid": valid_pair})

    for b in bars5:
        if b["t"] > cap:
            break
        d = b["t"][:10]
        if d != cur_day:
            if buf:
                flush(cur_day, buf)
            cur_day, buf = d, []
        buf.append(b)
    if buf:
        flush(cur_day, buf)
    # 只保留「两根5分钟bar严格相邻5分钟」的10分钟桶，剔除残缺/跨时段拼接的桶
    out = [x for x in out if x.pop("_valid", True)]
    return out


def ma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ma_series(values, n):
    return [None] * (n - 1) + [sum(values[i - n + 1:i + 1]) / n for i in range(n - 1, len(values))]


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100.0


# ────────────────────────────── 10分钟结构分析 ──────────────────────────────
def analyze_10m(code, bars10, bars5, quote, clock, cfg):
    """核心：识别当日 10分钟 MA5 向上拐头的启动结构，并评估其当前有效性。"""
    today = clock.strftime("%Y-%m-%d")
    res = {"ok": False, "reason": "", "turn_time": None, "bars_used": 0,
           "ma5_last": None, "ma5_dir": None, "dev": None, "launch_gain": None,
           "pullback": None, "form": None, "platform_low": None, "breakout_level": None,
           "vwap": None, "vol_after_before": None, "daily_high": None, "dist_high": None,
           "turn_confirmed_bars": 0, "errors": []}
    period = int(cfg["ma10m_period"])
    closes = [b["c"] for b in bars10]
    if len(bars10) < period + 3:
        res["reason"] = f"10分钟K线不足（{len(bars10)} 根）"
        res["errors"].append(res["reason"])
        return res
    mas = ma_series(closes, period)
    today_idx = [i for i, b in enumerate(bars10) if b["t"][:10] == today]
    if not today_idx:
        res["reason"] = "当日无已完成的10分钟K线"
        res["errors"].append(res["reason"])
        return res
    res["bars_used"] = len(today_idx)
    res["bars_last_time"] = bars10[-1]["t"]
    last = len(bars10) - 1
    price = quote["price"] or closes[-1]

    # —— 当日 MA5 向上拐头检测：之前走平或向下 → 现在向上，且价格站上 MA5
    turns = []
    for k in today_idx:
        if k < max(period, 3) or mas[k] is None or mas[k - 1] is None or mas[k - 2] is None:
            continue
        slope_now = mas[k] - mas[k - 1]
        slope_prev = mas[k - 1] - mas[k - 2]
        flat_prev = abs(slope_prev) <= mas[k - 1] * 0.0006      # 走平
        if slope_now > mas[k] * 0.0002 and (slope_prev <= 0 or flat_prev) and closes[k] > mas[k]:
            # 拐头后至少延续 1 根、且结构未被破坏
            persist = 0
            for m in range(k + 1, len(bars10)):
                if mas[m] is not None and mas[m] >= mas[m - 1] - mas[m - 1] * 0.0006:
                    persist += 1
                else:
                    break
            turns.append({"idx": k, "t": bars10[k]["t"], "persist": persist,
                          "ma5": mas[k], "close": closes[k], "vol": bars10[k]["v"]})
    # 结构是否有效：优先取当日最早一次「未被破坏」的拐头 —— 它才是启动起点；
    # 若所有拐头都被破坏过，则退回最后一次拐头并标记结构已破坏。
    valid = []
    for t in turns:
        k = t["idx"]
        broken = any(mas[m] is not None and mas[m] < mas[k] * 0.9975 for m in range(k + 1, len(bars10)))
        if not broken:
            valid.append(t)
    chosen = valid[0] if valid else (turns[-1] if turns else None)
    if chosen is None:
        res["reason"] = "当日未出现10分钟MA5向上拐头启动信号"
        res["errors"].append(res["reason"])
        res["ma5_last"], res["ma5_dir"] = mas[last], _dir_of(mas, last)
        return res

    k = chosen["idx"]
    res["turn_time"] = chosen["t"]
    res["turn_confirmed_bars"] = chosen["persist"]
    res["structure_broken"] = not valid

    # —— MA5 当前状态
    res["ma5_last"] = mas[last]
    res["ma5_dir"] = _dir_of(mas, last)
    res["dev"] = pct(price, mas[last])
    # —— 启动以来累计涨幅（以拐头前一根 10 分钟收盘为基准，避免用未来信息）
    base = closes[k - 1]
    res["launch_base"] = base
    res["launch_gain"] = pct(price, base)
    # —— 启动后最高 / 回调幅度（含当前价）
    after = bars10[k:]
    hi = max([b["h"] for b in after] + [price])
    lo = min([b["l"] for b in after[1:]] + [price]) if len(after) > 1 else price
    res["high_after_turn"] = hi
    res["pullback"] = pct(price, hi)
    # —— 启动平台（拐头前一根 + 启动后回调低点构成的支撑）
    res["platform_low"] = min([bars10[k - 1]["l"]] + [b["l"] for b in after[1:]]) if len(after) > 1 else bars10[k - 1]["l"]
    res["breakout_level"] = max([b["h"] for b in bars10[max(0, k - 6):k]] or [bars10[k - 1]["h"]])
    # —— 当日量能结构与均价
    today_bars = [bars10[i] for i in today_idx]
    amt = sum(b.get("amt", 0) for b in today_bars)
    vol = sum(b["v"] for b in today_bars)
    res["vwap"] = (amt / (vol * 100)) if amt and vol else None
    v_before = sum(b["v"] for b in bars10[k - 4:k]) or 0      # 拐头前4根（可能跨到前一交易日，作为基线）
    v_after = sum(b["v"] for b in bars10[k:]) or 0            # 拐头后至最新已完成bar（均为当日）
    n_after = max(1, len(bars10[k:]))
    n_before = max(1, len(bars10[k - 4:k]))
    res["vol_after_before"] = (v_after / n_after) / ((v_before / n_before) or 1) if v_before else None
    # —— 当日最高（用 5 分钟 bar + 实时价，比 10 分钟粒度细）
    highs = [b["h"] for b in bars5 if b["t"][:10] == today and b["t"] <= clock.strftime("%Y-%m-%d %H:%M")]
    res["daily_high"] = max(highs + [price]) if highs else price
    res["dist_high"] = pct(price, res["daily_high"])
    res["daily_low"] = min([b["l"] for b in bars5 if b["t"][:10] == today] + [price]) if highs else price

    # —— 形态分类 A/B/C（§六）
    above_ma = price > (mas[last] or 0)
    above_platform = price > res["platform_low"]
    if res["pullback"] is not None and res["pullback"] >= -1.5 and above_ma:
        res["form"] = "A"      # 启动后高位横盘，回调较浅
    elif (res["pullback"] is not None and res["pullback"] > -4.5 and above_ma
          and res["dev"] is not None and res["dev"] >= -0.5 and above_platform):
        res["form"] = "B"      # 小幅回落但仍在关键均线与平台上方
    else:
        res["form"] = "C"      # 冲高回落 / 跌破MA5 / 跌回平台下方 → 淘汰
    res["ok"] = res["form"] in ("A", "B")
    if not res["ok"]:
        res["reason"] = "10分钟结构已走坏（C类：冲高回落或跌破MA5/启动平台）"
    return res


def _dir_of(mas, last):
    if mas[last] is None or mas[last - 1] is None:
        return "未知"
    d = mas[last] - mas[last - 1]
    if d > mas[last] * 0.0003:
        return "向上"
    if d < -mas[last] * 0.0003:
        return "向下"
    return "走平"


# ────────────────────────────── 日K分析 ──────────────────────────────
def analyze_daily(code, daily, quote, cfg):
    """日K位置与「首次明显转强」判定。最后一根（当日进行中）用实时价替代。"""
    today = quote.get("date") or datetime.now().strftime("%Y-%m-%d")
    res = {"trend": None, "position": None, "first_strong": False, "first_strong_reasons": [],
           "break_platform": False, "pressure": None, "pressure_gap": None, "support": None,
           "ma5": None, "ma10": None, "ma20": None, "ma60": None, "score": 0, "notes": [],
           "five_day_gain": None, "vol_ratio_5d": None}
    done = [b for b in daily if b["t"][:10] < today]
    if len(done) < 30:
        res["notes"].append(f"历史日K不足（{len(done)} 根）")
        return res
    price = quote["price"]
    closes = [b["c"] for b in done] + [price]
    vols = [b["v"] for b in done]
    res["ma5"], res["ma10"] = ma(closes, 5), ma(closes, 10)
    res["ma20"], res["ma60"] = ma(closes, 20), ma(closes, 60)
    ma20_prev = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else None
    r20 = done[-20:]
    r60 = done[-60:]
    hi20, lo20 = max(b["h"] for b in r20), min(b["l"] for b in r20)
    hi60, lo60 = max(b["h"] for b in r60), min(b["l"] for b in r60)
    hi120 = max(b["h"] for b in done[-120:]) if len(done) >= 120 else hi60
    chg20 = pct(price, r20[0]["c"])
    res["five_day_gain"] = pct(price, done[-5]["c"])
    vol_ma5 = ma(vols, 5)
    tx_vol = (quote.get("vol") or 0)
    res["vol_ratio_5d"] = (tx_vol / vol_ma5) if vol_ma5 else None

    # 近期趋势
    ma20_slope = pct(res["ma20"], ma20_prev) if (res["ma20"] and ma20_prev) else 0
    if res["ma20"] and price < res["ma20"] and ma20_slope is not None and ma20_slope < -0.5 and (chg20 or 0) < -3:
        res["trend"] = "下跌"
    elif res["ma20"] and price > res["ma20"] and (ma20_slope or 0) > 1.5 and (chg20 or 0) > 8:
        res["trend"] = "上升"
    else:
        res["trend"] = "横盘/筑底"
    # 区间位置（近60日）
    rng = max(hi60 - lo60, 1e-9)
    res["position"] = (price - lo60) / rng
    # 突破/站上
    res["break_platform"] = price > hi20
    above_ma20 = bool(res["ma20"] and price > res["ma20"])
    above_ma60 = bool(res["ma60"] and price > res["ma60"])
    # 量能配合
    vol_expand = bool(tx_vol and vol_ma5 and tx_vol >= vol_ma5 * 1.2)
    # 首次明显转强（§四）：前期调整/横盘 + 当日放量转强 + 不是连续暴涨延续
    reasons = []
    cond_adjust = res["position"] is not None and res["position"] < 0.75
    cond_not_extend = res["five_day_gain"] is not None and res["five_day_gain"] <= cfg["max_5d_gain"]
    cond_vol = vol_expand
    cond_break = res["break_platform"] or above_ma20 or above_ma60
    if cond_adjust:
        pos_txt = f"{round((res['position'] or 0) * 100)}%"
        reasons.append(f"前期处于{res['trend']}状态（60日区间位置 {pos_txt}）")
    if cond_vol:
        reasons.append(f"当日成交量放大至5日均量的 {res['vol_ratio_5d']:.2f} 倍")
    if res["five_day_gain"] is not None and res["five_day_gain"] <= 0:
        reasons.append(f"近5日仍为回调（{res['five_day_gain']:+.1f}%），非连续暴涨后的加速")
    if res["break_platform"]:
        reasons.append("突破近20日平台高点")
    elif above_ma20:
        reasons.append("重新站上 MA20")
    elif above_ma60:
        reasons.append("重新站上 MA60")
    if not cond_not_extend:
        res["notes"].append(f"近5日已累计上涨 {res['five_day_gain']:.1f}%，属延续性加速而非首次转强")
    res["first_strong"] = bool(cond_adjust and cond_vol and cond_break and cond_not_extend)
    res["first_strong_reasons"] = reasons

    # 上方压力位 / 支撑位
    cands = []
    for label, val in (("近20日平台高点", hi20), ("近60日高点", hi60), ("近120日高点", hi120),
                       ("MA20", res["ma20"]), ("MA60", res["ma60"])):
        if val and val > price * 1.002:
            cands.append((label, val))
    # 整数关口
    step = 1.0 if price < 20 else (5.0 if price < 100 else 10.0)
    integer_level = math.ceil(price * 1.005 / step) * step
    if integer_level > price:
        cands.append((f"整数关口 {integer_level:g}", integer_level))
    cands.sort(key=lambda x: x[1])
    if not cands:
        # 已在近期新高之上：用上一档整数关口或 +8% 作为参考目标
        res["pressure"], res["pressure_label"] = round(price * 1.08, 2), "近期无压力（新高区，参考+8%）"
    else:
        res["pressure_label"], res["pressure"] = cands[0][0], cands[0][1]
    res["pressure_gap"] = pct(res["pressure"], price)
    ma_support = max([v for v in (res["ma5"], res["ma20"]) if v and v < price] or [0])
    res["support"] = max(ma_support, lo20) if lo20 < price else ma_support

    # 日K形态评分（满分 = 权重表中的 25 分，见 score_daily）
    res["score"] = score_daily(res, quote, cfg)
    return res


def score_daily(d, quote, cfg):
    w = cfg["score_weights"]["日K启动形态"]
    s = 0.0
    if d["position"] is not None:
        if d["position"] < 0.45:
            s += w * 0.24
        elif d["position"] < 0.65:
            s += w * 0.16
        elif d["position"] < 0.8:
            s += w * 0.08
    if d["break_platform"]:
        s += w * 0.20
    elif d["ma20"] and quote["price"] > d["ma20"]:
        s += w * 0.12
    p = quote.get("pct") or 0
    if 4.0 <= p <= 8.0:
        s += w * 0.16
    elif 3.0 <= p < 4.0:
        s += w * 0.08
    if d["vol_ratio_5d"] and d["vol_ratio_5d"] >= 1.3:
        s += w * 0.16
    elif d["vol_ratio_5d"] and d["vol_ratio_5d"] >= 1.0:
        s += w * 0.08
    if d["five_day_gain"] is not None and d["five_day_gain"] <= 10:
        s += w * 0.12
    elif d["five_day_gain"] is not None and d["five_day_gain"] <= cfg["max_5d_gain"]:
        s += w * 0.05
    if d["pressure_gap"] is not None and d["pressure_gap"] >= 5:
        s += w * 0.12
    elif d["pressure_gap"] is not None and d["pressure_gap"] >= 3:
        s += w * 0.06
    return round(min(s, w), 1)


# ────────────────────────────── 数据校验 ──────────────────────────────
def validate_quote(q, clock):
    """行情内部自洽性 + 新鲜度。返回 (状态, 问题列表)。"""
    errs = []
    if not q or not q.get("price"):
        return "❌", ["行情缺失或无成交价（可能停牌）"]
    p, o, h, l, pc = q["price"], q.get("open"), q.get("high"), q.get("low"), q.get("prev_close")
    if None not in (o, h, l) and not (l - 1e-6 <= min(o, p) and max(o, p) <= h + 1e-6):
        errs.append(f"OHLC 不自洽（现价{p} 开{o} 高{h} 低{l}）")
    if pc and abs((q.get("pct") or 0) - (p / pc - 1) * 100) > 0.35:
        errs.append(f"涨跌幅与昨收不符（显示{q.get('pct')}%）")
    ts = q.get("time") or ""
    try:
        t = datetime.strptime(ts, "%Y%m%d%H%M%S")
        if clock.strftime("%Y-%m-%d") == t.strftime("%Y-%m-%d"):
            lag = (clock - t).total_seconds() / 60.0
            # 仅在交易时段内检查新鲜度（盘后复算/非交易时段不报警）
            if in_trading_hours(clock) and lag > 12:
                errs.append(f"行情延迟 {lag:.0f} 分钟（{ts}）")
        elif in_trading_hours(clock):
            errs.append(f"行情日期非当日（{ts}）")
    except ValueError:
        errs.append(f"行情时间字段异常（{ts}）")
    status = "⚠️" if errs else "✅"
    return status, errs


def cross_check(row, q):
    """东财列表 × 腾讯行情 双源交叉。"""
    errs = []
    if not q or row.get("pct") is None or q.get("pct") is None:
        return "⚠️", ["缺少双源数据，未做交叉校验"]
    if abs(row["price"] - q["price"]) > max(0.02, q["price"] * 0.005):
        errs.append(f"现价差异（东财{row['price']} / 腾讯{q['price']}）")
    if abs(row["pct"] - q["pct"]) > 0.35:
        errs.append(f"涨幅差异（东财{row['pct']}% / 腾讯{q['pct']}%）")
    if row.get("amount") and q.get("amount") and abs(row["amount"] - q["amount"]) / max(row["amount"], 1) > 0.08:
        errs.append("成交额差异 >8%")
    return ("⚠️" if errs else "✅"), errs


def validate_bars(bars, name, clock, today):
    errs = []
    if not bars:
        return "❌", [f"{name} 为空"]
    ts = [b["t"] for b in bars]
    if ts != sorted(ts):
        errs.append(f"{name} 时间未排序")
    if len(set(ts)) != len(ts):
        errs.append(f"{name} 存在重复时间戳")
    for b in bars[-3:]:
        if b["t"][:10] > today:
            errs.append(f"{name} 出现未来日期 {b['t']}")
        if not (b["l"] - 1e-6 <= min(b["o"], b["c"]) and max(b["o"], b["c"]) <= b["h"] + 1e-6):
            errs.append(f"{name} OHLC 越界（{b['t']}）")
    cap = clock.strftime("%Y-%m-%d %H:%M")
    if bars[-1]["t"] > cap:
        # 结束时间在时钟之后、但开始时间已过 → 正在进行中的 bar：build_10m 会按时间过滤掉，不算异常
        try:
            t_start = datetime.strptime(bars[-1]["t"], "%Y-%m-%d %H:%M") - timedelta(minutes=5)
            in_progress = t_start.strftime("%Y-%m-%d %H:%M") <= cap
        except ValueError:
            in_progress = False
        if not in_progress:
            errs.append(f"{name} 使用了未来K线（{bars[-1]['t']} > {cap}）")
    return ("⚠️" if errs else "✅"), errs


def validate_history(daily, today, clock):
    errs = []
    days = [b["t"][:10] for b in daily]
    if days != sorted(days):
        errs.append("日K未按时间排序")
    if len(set(days)) != len(days):
        errs.append("日K存在重复日期")
    if any(d > today for d in days):
        errs.append("日K出现未来日期")
    for b in daily[-5:]:
        if not (b["l"] - 1e-6 <= min(b["o"], b["c"]) and max(b["o"], b["c"]) <= b["h"] + 1e-6):
            errs.append(f"日K OHLC 越界（{b['t']}）")
    if daily and days[-1] != today:
        errs.append(f"日K最新一根为 {days[-1]}，非当日（今日进行中K线缺失）")
    return ("⚠️" if errs else "✅"), errs


# ────────────────────────────── 单只候选分析 ──────────────────────────────
def analyze_one(row, env, cfg, clock, quotes=None):
    """对一只初筛候选做完整分析：日K + 10分钟结构 + 尾盘强度 + 评分 + 硬性条件。"""
    code, name = row["code"], row["name"]
    today = clock.strftime("%Y-%m-%d")
    out = {"code": code, "name": name, "industry": row.get("industry", ""),
           "errors": [], "hard_fail": [], "warn": []}
    # 行情：优先腾讯（含量比/涨跌停），回退东财列表值
    q = (quotes or {}).get(code)
    if q and q.get("price"):
        price, prev_close = q["price"], q["prev_close"]
        quote = {"price": price, "open": q["open"], "high": q["high"], "low": q["low"],
                 "prev_close": prev_close, "pct": (pct(price, prev_close) if prev_close else row.get("pct")),
                 "amount": q["amount"], "vol": q["vol"],
                 "turnover": q.get("turnover"), "vol_ratio": q.get("vol_ratio"),
                 "limit_up": q.get("limit_up"), "limit_down": q.get("limit_down"),
                 "time": q.get("time"), "date": today, "source": "腾讯"}
    else:
        quote = {"price": row.get("price"), "pct": row.get("pct"), "amount": row.get("amount"),
                 "vol": row.get("vol"), "turnover": row.get("turnover"),
                 "vol_ratio": row.get("vol_ratio"), "open": None, "high": None, "low": None,
                 "prev_close": row.get("prev_close"), "time": "", "date": today, "source": "东财列表"}
        out["warn"].append("腾讯行情缺失，使用东财列表数据（缺开高低/量比字段）")
    if not quote["price"]:
        out["errors"].append("无有效现价，跳过")
        out["skip"] = True
        return out

    # 数据校验
    if quote["source"] == "腾讯":
        qstatus, qerrs = validate_quote(quote, clock)
    else:
        qstatus, qerrs = "⚠️", ["腾讯行情缺失，未做行情自洽性校验"]
    out["quote_status"], out["quote_errs"] = qstatus, qerrs
    cstatus, cerrs = cross_check(row, q)
    out["cross_status"], out["cross_errs"] = cstatus, cerrs

    # 日K
    try:
        daily = em_kline(code, klt=101, lmt=320)
        out["daily_src"] = "东财"
    except Exception as e:  # noqa: BLE001
        daily = []
        out["errors"].append(f"日K东财不可用（{brief_err(e)}）")
    if len([b for b in daily if b["t"][:10] < today]) < 30:
        try:
            daily = sina_kline(code, scale=240, datalen=320)
            out["daily_src"] = "新浪"
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"日K新浪回退失败（{brief_err(e)}）")
    if daily:
        hstatus, herrs = validate_history(daily, today, clock)
        out["daily_status"], out["daily_errs"] = hstatus, herrs
    else:
        out["daily_status"], out["daily_errs"] = "❌", ["日K全源缺失"]
    d = analyze_daily(code, daily, quote, cfg)
    out["daily"] = d

    # 10分钟（5分钟两两合并）：东财优先，失败/过期自动回退新浪
    bars5, bars5_src, bstatus, berrs, bnotes = fetch_5m(code, clock, today, lmt=420)
    out["errors"].extend(berrs)
    for n in bnotes:
        out["warn"].append(n)
    if not bars5:
        out["errors"].append("5分钟K线全源缺失（东财+新浪）")
    out["bars5_src"], out["bars_status"], out["bars_errs"] = bars5_src or "—", bstatus, berrs

    bars10 = build_10m(bars5, clock)
    st = analyze_10m(code, bars10, bars5, quote, clock, cfg)
    out["m10"] = st

    # —— 尾盘强度（14:30/14:50 时点）——
    strong = {}
    strong["above_ma5"] = bool(st.get("ma5_last") and quote["price"] > st["ma5_last"])
    strong["ma5_dir"] = st.get("ma5_dir")
    strong["above_vwap"] = bool(st.get("vwap") and quote["price"] >= st["vwap"])
    strong["dist_high"] = st.get("dist_high")
    strong["daily_high"] = st.get("daily_high")
    strong["daily_low"] = st.get("daily_low")
    strong["form"] = st.get("form")
    strong["pullback"] = st.get("pullback")
    out["strong"] = strong

    # 板块与市场环境（§七）
    ind = (row.get("industry") or "").strip()
    sec_top = env.get("sectors_top", [])
    sec_bot = env.get("sectors_bottom", [])
    out["sector"] = {
        "industry": ind or "—",
        "pct": next((x["pct"] for x in sec_top if x["name"] == ind), None),
        "rank": next((f"第{i + 1}" for i, x in enumerate(sec_top) if x["name"] == ind), None),
        "weak": any(x["name"] == ind for x in sec_bot),
        "strong_count": env.get("industry_strong", {}).get(ind, 0),
        "sector_leaders": [x["name"] for x in sec_top[:3]],
    }

    # —— 评分 ——
    sc = score_all(row, d, st, strong, env, quote, cfg)
    out["scores"] = sc
    out["total"] = sc["total"]

    # —— 交易计划（止损/目标/盈亏比/仓位/买入区间）——
    plan = build_plan(code, market_row=row, quote=quote, daily=d, st=st, cfg=cfg)
    out["plan"] = plan

    # —— 硬性条件（§八）——
    fails = []
    p = quote.get("pct") or 0
    if not (cfg["pct_low"] <= p <= cfg["pct_high"]):
        fails.append(f"涨幅 {p:.2f}% 不在 {cfg['pct_low']}%~{cfg['pct_high']}% 初筛区间")
    lim = quote.get("limit_up")
    if lim and lim > 0 and (lim / quote["price"] - 1) * 100 < cfg["near_limit_pct"]:
        fails.append(f"距涨停仅 {(lim / quote['price'] - 1) * 100:.2f}%，接近涨停不可追")
    if (quote.get("amount") or 0) < cfg["min_amount_yi"] * 1e8:
        fails.append(f"成交额 {(quote.get('amount') or 0) / 1e8:.2f}亿 < {cfg['min_amount_yi']}亿")
    to = quote.get("turnover")
    if to is not None and not (cfg["min_turnover"] <= to <= cfg["max_turnover"]):
        fails.append(f"换手率 {to:.1f}% 超出自设区间 {cfg['min_turnover']}~{cfg['max_turnover']}%")
    vr = quote.get("vol_ratio")
    if vr is not None and vr > 0 and vr < cfg["min_volume_ratio"]:
        fails.append(f"量比 {vr:.2f} < {cfg['min_volume_ratio']}")
    if is_st(name):
        fails.append("ST/风险警示股，默认排除")
    if is_new_listing(name):
        fails.append("次新股（N/C 开头），默认排除")
    if d.get("trend") is None:
        fails.append("日K数据不足，无法判定位置")
    elif not d.get("first_strong"):
        fails.append("日K未满足「近期调整后首次明显转强/突破」")
    if d.get("five_day_gain") is not None and d["five_day_gain"] > cfg["max_5d_gain"]:
        fails.append(f"近5日累计涨幅 {d['five_day_gain']:.1f}% > {cfg['max_5d_gain']}%，属延续性加速")
    if not st.get("ok"):
        fails.append(st.get("reason") or "10分钟MA5启动结构不成立")
    if not strong["above_ma5"]:
        fails.append("现价已跌破 10分钟 MA5")
    if st.get("dev") is not None and st["dev"] > cfg["max_dev_from_ma"]:
        fails.append(f"现价高于 MA5 {st['dev']:.2f}%（乖离过大）")
    if st.get("launch_gain") is not None and st["launch_gain"] > cfg["max_launch_gain"]:
        fails.append(f"启动以来已累计上涨 {st['launch_gain']:.1f}%，涨幅已大")
    if d.get("pressure_gap") is not None and d["pressure_gap"] < cfg["min_upside_pct"]:
        fails.append(f"距上方压力位仅 {d['pressure_gap']:.1f}% < {cfg['min_upside_pct']}%")
    if plan.get("rr") is None:
        fails.append("无法计算盈亏比（止损/压力位数据不足）")
    elif plan["rr"] < cfg["min_rr"]:
        fails.append(f"预期盈亏比 {plan['rr']:.2f} < {cfg['min_rr']}")
    out["hard_fail"] = fails
    out["tradable"] = len(fails) == 0
    return out


def score_all(row, d, st, strong, env, quote, cfg):
    w = cfg["score_weights"]
    sc = {}
    sc["日K启动形态"] = round(d.get("score") or 0, 1)
    # 10分钟 MA5 启动结构（25）
    s = 0.0
    ww = w["10分钟MA5启动结构"]
    if st.get("turn_time"):
        s += ww * 0.34
    if st.get("ok"):
        s += ww * 0.16
    if st.get("turn_confirmed_bars", 0) >= 2:
        s += ww * 0.12
    if strong.get("above_ma5") and strong.get("ma5_dir") == "向上":
        s += ww * 0.20
    dev = st.get("dev")
    if dev is not None:
        if 0 <= dev <= 1.5:
            s += ww * 0.18
        elif 1.5 < dev <= 3.0:
            s += ww * 0.10
        elif dev > 3.0:
            s += 0
        else:
            s += ww * 0.04
    sc["10分钟MA5启动结构"] = round(min(s, ww), 1)
    # 尾盘强度（20）
    s = 0.0
    ww = w["尾盘强度"]
    form = strong.get("form")
    if form == "A":
        s += ww * 0.40
    elif form == "B":
        s += ww * 0.25
    pb = strong.get("pullback")
    if pb is not None:
        if pb >= -1.5:
            s += ww * 0.20
        elif pb > -3.0:
            s += ww * 0.10
    if strong.get("above_vwap"):
        s += ww * 0.20
    dh = strong.get("dist_high")
    if dh is not None:
        if dh >= -1.5:
            s += ww * 0.20
        elif dh >= -3.0:
            s += ww * 0.10
    sc["尾盘强度"] = round(min(s, ww), 1)
    # 成交量配合（15）
    s = 0.0
    ww = w["成交量配合"]
    vr5 = d.get("vol_ratio_5d")
    if vr5:
        if vr5 >= 1.5:
            s += ww * 0.40
        elif vr5 >= 1.0:
            s += ww * 0.27
        else:
            s += ww * 0.07
    vab = st.get("vol_after_before")
    if vab is not None and vab >= 1.0:
        s += ww * 0.33
    elif vab is not None and vab >= 0.7:
        s += ww * 0.13
    vrn = quote.get("vol_ratio")
    if vrn:
        if 1.0 <= vrn <= 5.0:
            s += ww * 0.27
        elif 0.8 <= vrn < 1.0:
            s += ww * 0.13
        elif vrn > 5.0:
            s += ww * 0.07
    sc["成交量配合"] = round(min(s, ww), 1)
    # 市场及板块环境（10）
    sc["市场及板块环境"] = round(score_env(env, row, cfg), 1)
    # 上方空间与风险收益比（5）
    ww = w["上方空间与风险收益比"]
    s = 0.0
    gap = d.get("pressure_gap")
    if gap is not None:
        if gap >= 6:
            s += ww * 0.6
        elif gap >= 4:
            s += ww * 0.4
        elif gap >= cfg["min_upside_pct"]:
            s += ww * 0.2
    sc["上方空间与风险收益比"] = round(min(s, ww), 1)
    sc["total"] = round(sum(v for k, v in sc.items() if k != "total"), 1)
    return sc


def score_env(env, row, cfg):
    """市场与板块环境 0~10。"""
    w = cfg["score_weights"]["市场及板块环境"]
    s = 0.0
    idx = {i["code"]: i for i in env.get("indices", [])}
    sh = idx.get("000001", {}).get("pct")
    cyb = idx.get("399006", {}).get("pct")
    if sh is not None:
        if sh >= 0.5:
            s += w * 0.30
        elif sh >= -0.3:
            s += w * 0.18
        else:
            s += w * 0.05
    if cyb is not None and cyb >= 0:
        s += w * 0.10
    up, down = env.get("up"), env.get("down")
    if up and down:
        ratio = up / max(up + down, 1)
        if ratio >= 0.6:
            s += w * 0.20
        elif ratio >= 0.45:
            s += w * 0.12
        else:
            s += w * 0.03
    # 板块：候选所属行业排名靠前 + 板块内多只强势股
    ind = (row.get("industry") or "").strip()
    tops = [x["name"] for x in env.get("sectors_top", [])]
    if ind and ind in tops[:5]:
        s += w * 0.25
    elif ind and ind in tops[:10]:
        s += w * 0.15
    cnt = env.get("industry_strong", {}).get(ind, 0)
    if cnt >= 3:
        s += w * 0.15
    elif cnt >= 2:
        s += w * 0.08
    return min(s, w)


def build_plan(code, market_row, quote, daily, st, cfg):
    """交易计划：止损、目标、盈亏比、买入区间、仓位、失效条件。"""
    price = quote["price"]
    plan = {"entry_zone": None, "stop": None, "stop_pct": None, "target": None, "rr": None,
            "qty": 0, "invalidate": [], "notes": []}
    ma5 = st.get("ma5_last")
    plat = st.get("platform_low")
    amp = quote.get("amplitude")
    if not amp and quote.get("high") and quote.get("low") and quote.get("prev_close"):
        amp = (quote["high"] - quote["low"]) / quote["prev_close"] * 100
    floor = cfg["min_stop_pct"]
    if amp:
        floor = min(max(floor, 0.30 * amp), cfg["max_stop_pct"])
    # 止损＝最近的有效支撑（启动平台 / 10分钟MA5 下方 2%），并按当日振幅设置最小距离
    cands = [v for v in (plat, (ma5 * 0.98) if ma5 else None) if v and v < price]
    if not cands:
        plan["notes"].append("无可用支撑位，止损无法设定")
        return plan
    stop_raw = max(cands)
    stop = max(stop_raw, price * (1 - cfg["max_stop_pct"] / 100))   # 过远 → 上移到最大容忍距离
    stop_pct = (price - stop) / price * 100
    if stop_pct < floor:
        stop = price * (1 - floor / 100)                            # 过近 → 按波动率放宽
        stop_pct = floor
        plan["notes"].append(f"支撑过近（{round(stop_raw, 2)}），按当日波动率放宽到 {floor:.1f}%")
    target = daily.get("pressure") or price * (1 + 4 * stop_pct / 100)
    plan["stop"], plan["stop_pct"] = round(stop, 2), round(stop_pct, 2)
    plan["target"] = round(target, 2)
    plan["target_label"] = daily.get("pressure_label") or "参考目标"
    plan["rr"] = round((target - price) / max(price - stop, 1e-9), 2)
    low = max(ma5 or price * 0.995, price * 0.995)
    plan["entry_zone"] = [round(low, 2), round(price * 1.005, 2)]
    eq, risk = cfg["account_equity"], cfg["risk_per_trade_pct"]
    if eq > 0:
        qty = int((eq * risk / 100.0) / max(price - stop, 1e-9) // 100) * 100
        plan["qty"] = qty
        plan["position_pct"] = round(qty * price / eq * 100, 1)
        plan["risk_amount"] = round(qty * (price - stop), 0)
    # 失效条件
    plan["invalidate"] = [
        f"现价跌破 {round(stop, 2)}（止损）",
        f"10分钟 MA5 转为向下且价格跌破 {round(plat or 0, 2)}（启动平台）",
        f"尾盘拉升但成交额未同步放大（放量滞涨）",
        f"大盘或所属板块由强转弱",
    ]
    # 成本核算（买入+卖出费用与滑点后的最小可行空间）
    cost = (cfg["commission_rate"] * 2 + cfg["stamp_tax_rate"] + cfg["transfer_fee_rate"] * 2
            + cfg["slippage_pct"] / 100 * 2) * 100
    plan["cost_pct"] = round(cost, 3)
    plan["net_rr"] = round(((target - price) / price * 100 - cost) / max(stop_pct + cost, 1e-9), 2) \
        if target else None
    return plan


def scenarios_text(plan, st, quote):
    """次日情景预案（主观判断，明确标注）。"""
    e = quote["price"]
    stop = plan.get("stop")
    plat = st.get("platform_low")
    prev_high = st.get("daily_high")
    out = []
    out.append({"情景": "A 高开并继续冲高", "条件": "开盘高于昨收 0.5% 以上且盘中站上开盘价",
                "动作": f"持有观察；冲高后回落跌破分时均价或自当日最高回落 3%（≈{round((prev_high or e) * 0.97, 2)}）→ 分批/全部卖出"})
    out.append({"情景": "B 高开但迅速走弱", "条件": "高开后跌破开盘价或前一交易日收盘价",
                "动作": f"按计划减仓或清仓，不等二次冲高（失效位 {round(stop, 2)} 直接全清）" if stop else "减仓或清仓"})
    out.append({"情景": "C 平开或小幅高开", "条件": "开盘在昨收 ±0.5% 内",
                "动作": f"10:00 前若不能突破早盘高点且量能萎缩 → 减半；跌破 {round(plat, 2)} → 全部卖出" if plat else "观察量能与早盘高点"})
    out.append({"情景": "D 低开", "条件": "开盘低于昨收 0.5% 以上且持续走弱",
                "action": "", "动作": f"低开 -2% 内且快速收复昨收 → 继续观察；否则跌破 {round(stop, 2)} 全部卖出，不期待反弹" if stop else "控制风险优先"})
    out.append({"情景": "E 低开后快速反弹", "条件": "低开后重新站回昨收与 10分钟 MA5 上方",
                "动作": "可继续观察至 10:30，仍遵守上述失效条件与最迟退出时间"})
    return out


# ────────────────────────────── 报告与推送 ──────────────────────────────
def fmt_pct(v, digits=2):
    return "—" if v is None else f"{v:+.{digits}f}%"


# ────────────────────── 已购买清单（持仓列表）渲染 ──────────────────────
def action_color(act):
    """企业微信 markdown 只有三种颜色：info=绿、comment=灰、warning=橙红。
    继续持有观察 → info（绿）；全部卖出/卖出/减仓 → warning（最接近红色）；
    T+1 说明与停牌提示 → comment（灰）。"""
    act = act or ""
    if act.startswith("T+1") or act.startswith("停牌"):
        return "comment"
    if "卖出" in act or "减" in act or "清仓" in act or "跌停" in act:
        return "warning"
    return "info"


def action_mark(act):
    """「全部卖出」前加 🔴，让红色警告在群里第一眼可辨。"""
    act = act or ""
    return f"🔴 {act}" if "全部卖出" in act else act


def render_position_line(p, q=None):
    q = q or {}
    px = q.get("price")
    act = p.get("last_advice") or "待次日评估"
    color = action_color(act)
    seg = f"{p['code']} {p['name']}　买入 {p['entry']}（{str(p.get('entry_date') or '')[5:]}）"
    if px:
        seg += f"　现价 {px}（{fmt_pct(pct(px, p.get('entry')))}）"
    if p.get("shares"):
        seg += f"　{p['shares']:.0f} 股"
    return f"<font color=\"{color}\">　{action_mark(seg + '　→ ' + act)}</font>"


def build_position_list(pos, quotes=None):
    hs = [p for p in pos.get("positions", []) if p.get("status") == "holding"]
    if not hs:
        return ["**📋 已购买清单：空仓**"]
    L = [f"**📋 已购买清单（{len(hs)} 只）**"]
    for p in hs:
        L.append(render_position_line(p, (quotes or {}).get(p["code"])))
    L.append("<font color=\"comment\">买入自动入单，全部卖出自动移出；减仓不改变清单。</font>")
    return L


def prune_closed(pos):
    """已平仓记录归档到 history，保证「已购买清单」只剩当前持仓。"""
    closed = [p for p in pos.get("positions", []) if p.get("status") == "closed"]
    if closed:
        pos["positions"] = [p for p in pos.get("positions", []) if p.get("status") != "closed"]
        pos.setdefault("history", []).extend(closed)
    return len(closed)


def write_position_list(pos, clock, quotes=None):
    """把当前已购买清单落到 报告/已购买清单.md（纯文本 + 🟢/🟠/🔴，方便直接看）。"""
    hs = [p for p in pos.get("positions", []) if p.get("status") == "holding"]
    L = [f"# 📋 已购买清单 · 更新于 {clock.strftime('%Y-%m-%d %H:%M')}", ""]
    if not hs:
        L.append("当前空仓。")
    for p in hs:
        q = (quotes or {}).get(p["code"]) or {}
        px = q.get("price")
        act = p.get("last_advice") or "待次日评估"
        icon = "🔴" if "全部卖出" in act else ("🟠" if action_color(act) == "warning" else "🟢")
        seg = (f"{icon} {p['code']} {p['name']}　买入 {p['entry']}"
               f"（{str(p.get('entry_date') or '')[5:]}）")
        if px:
            seg += f"　现价 {px}（{fmt_pct(pct(px, p.get('entry')))}）"
        if p.get("shares"):
            seg += f"　{p['shares']:.0f} 股"
        L.append(seg + f"　→ {act}")
    L.append("")
    L.append("规则：14:51 尾盘确认买入的标的自动加入；引擎建议「全部卖出」并执行后自动移出"
             "（归档到 positions.json → history）；减仓/减半不改变清单。")
    with open(os.path.join(REPORT_DIR, "已购买清单.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))


def build_md_report(clock, cfg, env, candidates, stage1_count, validations, mode_label, notes=None):
    today = clock.strftime("%Y-%m-%d")
    tradable = [c for c in candidates if c.get("tradable")]
    watch = [c for c in candidates if not c.get("tradable")]
    tradable.sort(key=lambda x: -x["total"])
    watch.sort(key=lambda x: -x["total"])
    L = []
    L.append(f"# 🎯 A股尾盘短线（14:30筛选→次日冲高卖出）· {mode_label}")
    L.append("")
    L.append(f"- 生成时间：{clock.strftime('%Y-%m-%d %H:%M')}（CST）　阶段：{mode_label}")
    L.append(f"- 初筛命中（涨幅 {cfg['pct_low']}%~{cfg['pct_high']}%）：{stage1_count} 只；送K线深度分析：{len(candidates)} 只")
    L.append(f"- 交易名单：**{len(tradable)}** 只；观察名单：{len(watch)} 只")
    L.append("")
    # 大盘环境
    L.append("## 🧭 市场环境")
    L.append("")
    for i in env.get("indices", []):
        L.append(f"- {i['name']}：{i['price']}　{fmt_pct(i['pct'])}")
    if env.get("up") is not None:
        L.append(f"- 沪深涨跌家数：上涨 {int(env['up'])} / 下跌 {int(env['down'])}")
    if env.get("sectors_top"):
        tops = "、".join(f"{x['name']}({fmt_pct(x['pct'])})" for x in env["sectors_top"][:5])
        L.append(f"- 行业板块前五：{tops}")
    if env.get("sectors_bottom"):
        bots = "、".join(f"{x['name']}({fmt_pct(x['pct'])})" for x in env["sectors_bottom"][:3])
        L.append(f"- 行业板块末三：{bots}")
    L.append("")
    # 数据校验
    L.append("## 🔎 数据校验（强制）")
    L.append("")
    for v in validations:
        L.append(f"- {v}")
    L.append("")
    if notes:
        for n in notes:
            L.append(f"> {n}")
        L.append("")
    if not candidates:
        L.append("## 今日无交易信号")
        L.append("")
        L.append("初筛区间内没有任何股票通过后续技术条件筛选，或数据校验未通过。**不强行推荐。**")
        return "\n".join(L)

    L.append("## 📋 交易名单（硬性条件全部通过）")
    L.append("")
    if not tradable:
        L.append("**无。** 今日所有候选均未同时满足硬性条件，不产生买入信号。")
        L.append("")
    for c in tradable:
        L.extend(render_stock(c, rank=True))
    L.append("")
    L.append(f"## 👀 观察名单（未通过硬性条件，按总分排序，最多 {cfg['push_top_n']} 只）")
    L.append("")
    for c in watch[:cfg["push_top_n"]]:
        L.extend(render_stock(c, rank=False))
    L.append("")
    L.append("## 评分明细表（满分100，仅用于排序，不能替代硬性条件）")
    L.append("")
    for c in sorted(candidates, key=lambda x: -x["total"])[:12]:
        sc = c["scores"]
        flag = "✅交易" if c.get("tradable") else "观察"
        L.append(f"- {flag} {c['code']} {c['name']}　总分 {c['total']}　"
                 f"日K {sc['日K启动形态']}／10min {sc['10分钟MA5启动结构']}／尾盘 {sc['尾盘强度']}／"
                 f"量能 {sc['成交量配合']}／环境 {sc['市场及板块环境']}／空间RR {sc['上方空间与风险收益比']}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("本报告为策略规则的机械执行结果，区分【事实数据】【计算结果】【主观判断】三部分。"
             "短线动量策略属于**待验证假设**，未完成历史回测与模拟交易验证前不应视为正期望；"
             "技术分析不构成投资建议。默认只输出分析与交易计划，不自动下单。")
    return "\n".join(L)


def render_stock(c, rank=True):
    d, st, strong, plan, sc = c["daily"], c["m10"], c["strong"], c["plan"], c["scores"]
    L = []
    L.append(f"### {'★ ' if rank else ''}{c['code']} {c['name']}　总分 {c['total']}")
    L.append("")
    L.append("**【事实数据】**")
    L.append(f"- 现价 {c.get('price')}　涨幅 {fmt_pct(c.get('pct'))}　"
             f"成交额 {((c.get('amount') or 0) / 1e8):.2f}亿　换手 {c.get('turnover')}%　量比 {c.get('vol_ratio')}")
    L.append(f"- 当日最高 {strong.get('daily_high')} / 最低 {st.get('daily_low')}　"
             f"距当日最高 {fmt_pct(strong.get('dist_high'))}　均价(VWAP) {round(st['vwap'], 2) if st.get('vwap') else '—'}")
    L.append(f"- 日K数据源 {c.get('daily_src')}　10分钟数据源 {c.get('bars5_src')}　"
             f"行情源 {c.get('quote_src', '腾讯/东财')}")
    L.append("")
    L.append("**【计算结果】**")
    L.append(f"- 日K：趋势 {d.get('trend')}　60日区间位置 {round((d.get('position') or 0) * 100)}%　"
             f"MA5 {round(d['ma5'], 2) if d.get('ma5') else '—'} / MA20 {round(d['ma20'], 2) if d.get('ma20') else '—'} / "
             f"MA60 {round(d['ma60'], 2) if d.get('ma60') else '—'}　近5日累计 {fmt_pct(d.get('five_day_gain'))}")
    L.append(f"- 首次明显转强：{'✅ 是' if d.get('first_strong') else '❌ 否'}"
             + ("（" + "；".join(d.get('first_strong_reasons') or []) + "）" if d.get("first_strong_reasons") else ""))
    L.append(f"- 突破近期平台：{'✅' if d.get('break_platform') else '❌'}　"
             f"最近压力位 {d.get('pressure')}（{d.get('pressure_label')}，空间 {fmt_pct(d.get('pressure_gap'))}）　"
             f"支撑 {round(d['support'], 2) if d.get('support') else '—'}")
    L.append(f"- 10分钟MA5：拐头时间 {st.get('turn_time') or '未出现'}　当前 MA5 {round(st['ma5_last'], 2) if st.get('ma5_last') else '—'}　"
             f"方向 {st.get('ma5_dir')}　偏离 {fmt_pct(st.get('dev'))}　已完成bar数 {st.get('bars_used')}")
    L.append(f"- 启动以来累计涨幅 {fmt_pct(st.get('launch_gain'))}　启动后回调 {fmt_pct(strong.get('pullback'))}　"
             f"形态 {strong.get('form')} 类　启动平台 {round(st['platform_low'], 2) if st.get('platform_low') else '—'}")
    L.append(f"- 量能：当日量/5日均量 {round(d['vol_ratio_5d'], 2) if d.get('vol_ratio_5d') else '—'}　"
             f"启动后/启动前均量比 {round(st['vol_after_before'], 2) if st.get('vol_after_before') else '—'}")
    sc2 = c.get("sector") or {}
    sec_txt = f"{sc2.get('industry', '—')}"
    if sc2.get("pct") is not None:
        sec_txt += f"（行业 {fmt_pct(sc2['pct'])}"
        sec_txt += f"，{sc2['rank']}）" if sc2.get("rank") else "）"
    if sc2.get("strong_count"):
        sec_txt += f"　板块内涨幅≥3% 的股票 {sc2['strong_count']} 只"
    if sc2.get("weak"):
        sec_txt += "　⚠️ 该行业在跌幅榜前列"
    L.append(f"- 板块环境：{sec_txt}")
    L.append(f"- 评分：日K {sc['日K启动形态']}／10min {sc['10分钟MA5启动结构']}／尾盘 {sc['尾盘强度']}／"
             f"量能 {sc['成交量配合']}／环境 {sc['市场及板块环境']}／空间RR {sc['上方空间与风险收益比']} = **{c['total']}**")
    L.append("")
    L.append("**【交易计划】**")
    if plan.get("stop"):
        L.append(f"- 计划买入区间 {plan['entry_zone'][0]}–{plan['entry_zone'][1]}　止损 {plan['stop']}（-{plan['stop_pct']}%）　"
                 f"目标 {plan['target']}（{plan['target_label']}）　盈亏比 {plan['rr']}（扣费净值比 {plan.get('net_rr')}）")
        if plan.get("qty"):
            L.append(f"- 建议仓位：{plan['qty']} 股（约 {plan.get('position_pct')}% 仓位，单笔风险 {plan.get('risk_amount')} 元）")
        else:
            L.append("- 仓位：未配置账户总资金（config.account_equity=0），仅给止损价与盈亏比，不给股数")
        L.append("- 失效条件：" + "；".join(plan["invalidate"]))
        if plan.get("notes"):
            L.append("- 说明：" + "；".join(plan["notes"]))
    else:
        L.append("- 无法生成交易计划：" + "；".join(plan.get("notes") or ["止损位不足"]))
    L.append("")
    L.append("**【主观判断·次日情景预案】**")
    for s in c.get("scenarios", []):
        L.append(f"- {s['情景']}：{s['条件']} → {s['动作']}")
    L.append("")
    L.append("**【风险因素】**")
    for r in c.get("risks", []):
        L.append(f"- {r}")
    L.append("")
    if not c.get("tradable"):
        L.append("**【未满足条件的具体原因】**")
        for r in c.get("hard_fail", []):
            L.append(f"- ❌ {r}")
        L.append("")
    warn = (c.get("quote_errs") or []) + (c.get("cross_errs") or []) + (c.get("daily_errs") or []) + (c.get("bars_errs") or [])
    if warn:
        L.append("**【数据告警】**")
        for r in warn:
            L.append(f"- ⚠️ {r}")
        L.append("")
    return L


def build_risks(c, cfg, env):
    risks = []
    d, st, quote = c["daily"], c["m10"], c
    if is_st(c["name"]):
        risks.append("ST/风险警示股")
    lim = c.get("limit_up")
    if lim and lim > 0 and (lim / (c.get("price") or 1) - 1) * 100 < 3:
        risks.append(f"接近涨停（{(lim / c['price'] - 1) * 100:.1f}%），次日高开空间与承接风险大")
    if (c.get("vol_ratio") or 0) > 5:
        risks.append("量比过高，存在获利盘集中出逃风险")
    if (c.get("pct") or 0) >= 7:
        risks.append("当日涨幅已接近筛选上限，追高风险")
    if d.get("position") is not None and d["position"] > 0.8:
        risks.append("股价处于60日区间高位，上方套牢盘少但获利盘多")
    if d.get("five_day_gain") is not None and d["five_day_gain"] > 10:
        risks.append(f"近5日已累计上涨 {d['five_day_gain']:.1f}%，注意延续性加速后的回落")
    if st.get("dev") is not None and st["dev"] > 2:
        risks.append(f"短线乖离 {st['dev']:.2f}%，回抽 MA5 概率上升")
    if st.get("vol_after_before") is not None and st["vol_after_before"] < 0.8:
        risks.append("启动后量能未放大，量价配合不足")
    gap = d.get("pressure_gap")
    if gap is not None and gap < 4:
        risks.append(f"距上方压力位仅 {gap:.1f}%，上行空间受限")
    sh = next((i["pct"] for i in env.get("indices", []) if i["code"] == "000001"), None)
    if sh is not None and sh < -0.5:
        risks.append(f"大盘偏弱（上证 {sh:+.2f}%），尾盘强势股次日溢价下降")
    if not risks:
        risks.append("未发现显著异常，但仍存在隔夜跳空与情绪反转风险")
    risks.append("隔夜跳空风险：次日开盘可能直接跳过止损位，实际亏损可能大于计划")
    return risks


def build_push(clock, cfg, env, tradable, watch, validations, mode_label, extra_lines=None):
    today = clock.strftime("%Y-%m-%d")
    L = []
    L.append(f"**🎯 短线策略 · {mode_label} · {clock.strftime('%m-%d %H:%M')}**")
    sh = next((i for i in env.get("indices", []) if i["code"] == "000001"), None)
    cyb = next((i for i in env.get("indices", []) if i["code"] == "399006"), None)
    parts = []
    if sh:
        parts.append(f"上证 {sh['price']}（{fmt_pct(sh['pct'])}）")
    if cyb:
        parts.append(f"创业板 {fmt_pct(cyb['pct'])}")
    if env.get("up") is not None:
        parts.append(f"涨/跌 {int(env['up'])}/{int(env['down'])}")
    L.append(" ".join(parts))
    if env.get("sectors_top"):
        L.append(f"强势板块：{'、'.join(x['name'] for x in env['sectors_top'][:3])}")
    L.append(f"<font color=\"comment\">校验 {' | '.join(v.split(' ')[0] for v in validations)}</font>")
    L.append("")
    if not tradable:
        L.append("<font color=\"comment\">**今日无交易信号** — 没有候选股同时满足全部硬性条件。不强行推荐。</font>")
    else:
        L.append(f"**交易名单 {len(tradable)} 只**（硬性条件全通过）")
        for c in tradable[:cfg["push_max_stocks"]]:
            st, d, plan = c["m10"], c["daily"], c["plan"]
            L.append(f"**{c['code']} {c['name']}**　{c['price']}　{fmt_pct(c.get('pct'))}　评分 {c['total']}"
                     + (f"　{(c.get('sector') or {}).get('industry', '')}" if (c.get("sector") or {}).get("industry") else ""))
            L.append(f"　日K：{d.get('trend')}｜首次转强 {'✅' if d.get('first_strong') else '❌'}｜"
                     f"压力 {d.get('pressure')}（空间 {fmt_pct(d.get('pressure_gap'))}）")
            L.append(f"　10min：MA5拐头 {st.get('turn_time') or '—'}｜形态 {c['strong'].get('form')}类｜"
                     f"偏离 {fmt_pct(st.get('dev'))}｜启动后+{st.get('launch_gain') and round(st['launch_gain'], 1)}%")
            L.append(f"　计划：买 {plan.get('entry_zone', ['—'])[0]}–{plan.get('entry_zone', ['—', '—'])[1]}｜"
                     f"止损 {plan.get('stop')}（-{plan.get('stop_pct')}%）｜目标 {plan.get('target')}｜盈亏比 {plan.get('rr')}")
            L.append(f"　量能：量比 {c.get('vol_ratio')}｜当日量/5日均 {round(d['vol_ratio_5d'], 2) if d.get('vol_ratio_5d') else '—'}｜"
                     f"成交额 {((c.get('amount') or 0) / 1e8):.2f}亿")
            L.append(f"<font color=\"warning\">　风险：{'；'.join(c.get('risks', [])[:2])}</font>")
            L.append("")
    if watch:
        L.append(f"**观察名单 {len(watch)} 只（未通过硬性条件）**")
        for c in watch[:4]:
            L.append(f"- {c['code']} {c['name']} {fmt_pct(c.get('pct'))} 分 {c['total']}｜{c.get('primary_fail') or '条件不足'}")
        L.append("")
    if extra_lines:
        L.extend(extra_lines)
    L.append("<font color=\"comment\">技术分析不构成投资建议；策略为待验证假设，未回测前不具备正期望保证。</font>")
    return "\n".join(L)


def push_report(text, title="短线策略", tag=""):
    if not text.strip():
        return 0
    if tag:
        text = f"**【联调测试】{tag}**\n\n" + text
    os.makedirs(REPORT_DIR, exist_ok=True)
    p = os.path.join(REPORT_DIR, "最新推送.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    if not os.path.exists(WECOM_PUSH):
        log(f"[warn] 未找到推送器 {WECOM_PUSH}")
        return 1
    r = subprocess.run([sys.executable, WECOM_PUSH, p], capture_output=True, text=True)
    log((r.stdout or "").strip() or (r.stderr or "").strip())
    return r.returncode


# ────────────────────────────── 运行记录（供周/月总结复盘） ──────────────────────────────
RUN_LOG = os.path.join(LOG_DIR, "运行记录.jsonl")


def record_run(clock, mode, ok=True, **stats):
    rec = {"date": clock.strftime("%Y-%m-%d"), "ts": clock.strftime("%Y-%m-%d %H:%M:%S"),
           "mode": mode, "ok": bool(ok)}
    rec.update(stats)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"[warn] 写运行记录失败：{e}")


def reason_cat(reason):
    """把卖出原因归到统计分析用的类别。"""
    r = reason or ""
    for key, cat in (("止损", "止损"), ("最迟退出", "最迟退出"), ("跌停", "跌停未成交"),
                     ("减半", "冲高回落减仓"), ("跌破前收", "高开走弱"), ("高开", "高开走弱"),
                     ("低开", "低开走弱"), ("MA5", "跌破MA5"), ("手动", "手动平仓")):
        if key in r:
            return cat
    return "其他"


def advice_cat(a):
    """由卖出建议的触发原因归纳类别。"""
    txt = "；".join(a.get("reasons") or [])
    if "止损" in txt:
        return "止损"
    if "最迟退出" in txt:
        return "最迟退出"
    if "高开后跌破前收" in txt or "情景B" in txt:
        return "高开走弱"
    if "情景D" in txt:
        return "低开走弱"
    if "MA5" in txt:
        return "跌破MA5"
    if "冲高回落" in txt:
        return "冲高回落减仓"
    return "其他"


# ────────────────────────────── 持仓与卖出 ──────────────────────────────

def load_positions():
    return read_json(POS_FILE, {"version": 1, "positions": [], "paper": {"trades": []}})


def save_positions(pos):
    pos["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(POS_FILE, pos)


def fee_of(amount, side, cfg):
    fee = max(cfg["commission_min"], amount * cfg["commission_rate"]) + amount * cfg["transfer_fee_rate"]
    if side == "sell":
        fee += amount * cfg["stamp_tax_rate"]
    return fee


def net_pnl(entry, exit_p, shares, cfg):
    """含双边费用与滑点的净盈亏。"""
    slip = cfg["slippage_pct"] / 100
    buy_p = entry * (1 + slip)
    sell_p = exit_p * (1 - slip)
    amt_b = buy_p * shares
    amt_s = sell_p * shares
    cost = fee_of(amt_b, "buy", cfg) + fee_of(amt_s, "sell", cfg)
    pnl = amt_s - amt_b - cost
    pctg = pnl / amt_b * 100 if amt_b else 0
    return {"pnl": round(pnl, 2), "pct": round(pctg, 3), "cost": round(cost, 2)}


def exit_advice(pos, q, m10, clock, cfg):
    """次日卖出情景判定与动作建议。"""
    out = {"code": pos["code"], "name": pos["name"], "entry": pos["entry"],
           "scenario": "", "action": "", "reasons": [], "lines": []}
    if not q or not q.get("price"):
        out["scenario"] = "停牌/无行情"
        out["action"] = "无法卖出（停牌或无成交），按计划顺延至可交易时点"
        out["reasons"].append("行情缺失，无法判定情景")
        return out
    P = q["price"]
    C0 = pos.get("prev_close_at_entry") or q.get("prev_close")
    O = q.get("open")
    H, L = q.get("high"), q.get("low")
    vwap = m10.get("vwap")
    ma5 = m10.get("ma5_last")
    plan = pos.get("plan", {})
    stop = plan.get("stop")
    gap = pct(O, q.get("prev_close")) if O and q.get("prev_close") else 0
    out["gap"] = gap
    out["price"] = P
    out["pct_since_entry"] = pct(P, pos["entry"])
    # 情景判定
    if gap is not None and gap >= 0.5:
        if P >= (O or P) and H and P >= H * 0.99:
            out["scenario"] = "A 高开并继续冲高"
        else:
            out["scenario"] = "B 高开但走弱"
    elif gap is not None and gap <= -0.5:
        if q.get("prev_close") and P >= q["prev_close"]:
            out["scenario"] = "E 低开后快速反弹"
        else:
            out["scenario"] = "D 低开"
    else:
        out["scenario"] = "C 平开或小幅高开"
    today_hm = hhmm(clock)
    force = cfg["force_exit_time"]
    actions, reasons = [], []
    if m10.get("data_missing"):
        reasons.append(f"10分钟K线数据缺失（{m10.get('err') or '东财+新浪均不可用'}）："
                       "MA5/均价判定不可用，本次只按价格与纪律判定")
    if stop and P <= stop:
        actions.append("全部卖出")
        reasons.append(f"跌破计划止损 {stop}")
    if q.get("limit_down") and q["limit_down"] > 0 and abs(P - q["limit_down"]) < 1e-6:
        actions.append("跌停无法卖出")
        reasons.append("封在跌停价，实际可能无法成交，次日优先处理")
    if out["scenario"].startswith("B") and q.get("prev_close") and P < q["prev_close"]:
        actions.append("全部卖出/至少减半")
        reasons.append("高开后跌破前收，情景B按计划处理")
    if out["scenario"].startswith("D") and O and P < O:
        actions.append("卖出")
        reasons.append("低开后持续走弱，情景D优先控制风险")
    if ma5 and P < ma5 * 0.995 and vwap and P < vwap:
        actions.append("减仓/卖出")
        reasons.append(f"跌破10分钟MA5（{round(ma5, 2)}）且低于当日均价（{round(vwap, 2)}）")
    if H and P <= H * 0.97 and (out["pct_since_entry"] or 0) > 0:
        actions.append("减半（冲高回落）")
        reasons.append(f"自当日最高 {H} 回落 {(P / H - 1) * 100:.1f}%")
    if today_hm >= force:
        actions.append("全部卖出（最迟退出）")
        reasons.append(f"到达预设最迟退出时间 {force[:2]}:{force[2:]}")
    # 14:00-14:45 尾盘窗口：给出「现在减仓」还是「持有到最迟退出」的决断
    if "1400" <= today_hm < "1445":
        if not ma5 and not vwap:
            out["tail_note"] = ("尾盘窗口：10分钟数据缺失，无法判定强弱 —— 不凭感觉持有，"
                                f"按最迟退出 {force[:2]}:{force[2:]} 的纪律执行")
        else:
            strong_tail = (P >= (q.get("prev_close") or 0)) and (not ma5 or P >= ma5)
            if strong_tail:
                out["tail_note"] = "尾盘窗口：维持强势，按计划持有到 14:51 前执行最迟退出"
            else:
                out["tail_note"] = "尾盘窗口：已转弱（低于昨收或 10分钟MA5），建议 14:30–14:51 主动减仓，不等最迟退出"
                actions.append("减仓/卖出")
                reasons.append("尾盘窗口已转弱")
    if pos.get("entry_date") == clock.strftime("%Y-%m-%d"):
        actions = ["T+1：当日买入不可卖出，仅记录"]
        reasons = ["A股 T+1 限制"]
    if not actions:
        out["action"] = "继续持有观察"
        reasons.append("未触发任何卖出条件")
        if out["scenario"].startswith("A"):
            reasons.append("高开且维持强势，按情景A观察分时均线与成交量")
        elif out["scenario"].startswith("E"):
            reasons.append("低开已收复关键位置，可观察到 10:30，仍遵守失效条件")
    else:
        # 取最强动作
        for pref in ("全部卖出（最迟退出）", "跌停无法卖出", "全部卖出", "全部卖出/至少减半", "减仓/卖出", "减半（冲高回落）", "卖出"):
            if pref in actions:
                out["action"] = pref
                break
        else:
            out["action"] = actions[0]
    out["reasons"] = reasons
    return out


def mode_exit(clock, cfg, args, with_list=True):
    pos = load_positions()
    holdings = [p for p in pos.get("positions", []) if p.get("status") == "holding"]
    if not holdings:
        log("[exit] 无持仓，跳过（不推送）")
        record_run(clock, "exit", ok=True, holdings=0, pushed=False, note="无持仓")
        return 0, None
    try:
        env_chk = em_market_env()
        if not market_data_fresh(env_chk, clock):
            log("[exit] 行情日期非当日（非交易日）→ 不推送")
            return 0, None
    except Exception:  # noqa: BLE001
        pass
    codes = [p["code"] for p in holdings]
    quotes = tencent_quotes(codes)
    advices = []
    exit_notes = []
    for p in holdings:
        q = quotes.get(p["code"]) or {}
        m10 = {}
        today = clock.strftime("%Y-%m-%d")
        bars5, src5, st5, err5, note5 = fetch_5m(p["code"], clock, today, lmt=200)
        for n in note5:
            if n not in exit_notes:
                exit_notes.append(n)
        if not bars5:
            m10 = {"data_missing": True, "src": src5 or None, "err": "；".join(err5[:2])}
            log(f"[warn] {p['code']} 10分钟数据缺失：{m10['err']}")
        else:
            bars10 = build_10m(bars5, clock)
            closes = [b["c"] for b in bars10]
            period = int(cfg["ma10m_period"])
            today_bars = [b for b in bars10 if b["t"][:10] == today]
            amt = sum(b.get("amt", 0) for b in today_bars)
            vol = sum(b["v"] for b in today_bars)
            m10 = {"ma5_last": ma(closes, period), "vwap": (amt / (vol * 100)) if amt and vol else None,
                   "bars": len(bars10), "src": src5, "note": (note5[0] if note5 else None)}
            if not m10["ma5_last"] and not m10["vwap"]:
                m10["data_missing"] = True
        advices.append(exit_advice(p, q, m10, clock, cfg))
        p["last_advice"] = advices[-1]["action"]
        p["last_checked"] = clock.strftime("%Y-%m-%d %H:%M")

    L = [f"**📤 短线持仓监控 · {clock.strftime('%m-%d %H:%M')}**", ""]
    brief = (hhmm(clock) >= str(cfg.get("exit_brief_from", "1000"))
             and getattr(args, "mode", "exit") == "exit")   # 开盘后半小时巡检只发简报
    if "1400" <= hhmm(clock) < "1445":
        brief = True                                    # 尾盘窗口：只发简报，避免当天第 N 条长消息
    if brief:
        L[0] = (f"**⏰ 尾盘窗口简报 · {clock.strftime('%m-%d %H:%M')}**（14:51 前最后决断）"
                if "1400" <= hhmm(clock) < "1445"
                else f"**📤 持仓巡检 · {clock.strftime('%m-%d %H:%M')}**（每半小时一次）")
    sh = None
    try:
        env = em_market_env()
        sh = next((i for i in env.get("indices", []) if i["code"] == "000001"), None)
    except Exception:  # noqa: BLE001
        env = {}
    if sh:
        L.append(f"上证 {sh['price']}（{fmt_pct(sh['pct'])}）")
        L.append("")
    list_at = len(L)
    for a in advices:
        p = next(x for x in holdings if x["code"] == a["code"])
        plan = p.get("plan", {})
        if brief:
            act = a["action"]
            L.append(f"<font color=\"{action_color(act)}\">**{a['code']} {a['name']}**　{a.get('price')}　"
                     f"→ {action_mark(act)}</font>")
            L.append(f"　{a.get('tail_note') or a['scenario']}｜止损 {plan.get('stop')}｜"
                     f"最迟退出 {cfg['force_exit_time'][:2]}:{cfg['force_exit_time'][2:]}")
            continue
        L.append(f"**{a['code']} {a['name']}**　现价 {a.get('price')}　次日 {fmt_pct(a.get('pct_since_entry'))}（相对买入价）")
        L.append(f"　情景：{a['scenario']}（开盘缺口 {fmt_pct(a.get('gap'))}）")
        L.append(f"　买入 {a['entry']}｜止损 {plan.get('stop')}｜目标 {plan.get('target')}｜最迟退出 {cfg['force_exit_time'][:2]}:{cfg['force_exit_time'][2:]}")
        act = a["action"]
        L.append(f"<font color=\"{action_color(act)}\">　动作：{action_mark(act)}</font>")
        for r in a["reasons"]:
            L.append(f"　· {r}")
        if a.get("tail_note"):
            L.append(f"　尾盘：{a['tail_note']}")
        L.append("")
    if exit_notes and not brief:
        for n in exit_notes:
            L.append(f"<font color=\"comment\">数据源：{n}</font>")
        L.append("")
    # 已购买清单：引擎建议「全部卖出」即视为执行，按当时价格结算并移出清单
    settled = []
    if hhmm(clock) < cfg["force_exit_time"]:
        for a in advices:
            p = next(x for x in holdings if x["code"] == a["code"])
            if "全部卖出" not in a["action"] or "跌停" in a["action"]:
                continue
            q = quotes.get(p["code"]) or {}
            res = _close_paper(pos, p, q.get("price") or p["entry"],
                               f"引擎建议 {a['action']}（{clock.strftime('%H:%M')} {a['scenario']}）",
                               cfg, clock, cat=advice_cat(a))
            settled.append((p, res))
    if settled:
        L.append("**🧾 卖出结算（已按建议全部卖出并移出清单）**")
        for p, res in settled:
            L.append(f"　{p['code']} {p['name']}：{p['entry']} → {p['close']['price']}　"
                     f"净盈亏 {res['pct']:+.2f}%（{res['pnl']:+.0f} 元，含费用滑点）")
        L.append("")
    # 14:50：最迟退出时间结清全部持仓（同样视为已执行卖出）
    closed = []
    if hhmm(clock) >= cfg["force_exit_time"]:
        for p in holdings:
            if p.get("entry_date") == clock.strftime("%Y-%m-%d"):
                p["last_advice"] = "T+1 当日买入不可卖出，次日再评估"
                continue
            q = quotes.get(p["code"]) or {}
            px = q.get("price") or p["entry"]
            res = _close_paper(pos, p, px, f"T+1 最迟退出（{cfg['force_exit_time'][:2]}:{cfg['force_exit_time'][2:]} 结算）", cfg, clock, cat="最迟退出")
            closed.append((p, res))
    if closed:
        L.append(f"**🧾 卖出结算（{cfg['force_exit_time'][:2]}:{cfg['force_exit_time'][2:]} 最迟退出，已移出清单）**")
        for p, res in closed:
            L.append(f"　{p['code']} {p['name']}：{p['entry']} → {p['close']['price']}　"
                     f"净盈亏 {res['pct']:+.2f}%（{res['pnl']:+.0f} 元，含费用滑点）")
        L.append("")
    prune_closed(pos)          # 已卖出归档到 history，清单只剩在持仓
    stats = paper_stats(pos)
    if not brief and with_list:
        L[list_at:list_at] = build_position_list(pos, quotes) + [""]
    if stats and hk_count(pos) > 0:
        L.append(f"<font color=\"comment\">策略统计：{stats}</font>")
    # 变化门控：非关键时点（--only-if-changed）只在建议发生实质变化时推送
    state = pos.setdefault("advice_state", {})
    changed = []
    for a in advices:
        sig = f"{a['scenario']}|{a['action']}|{(a.get('reasons') or [''])[0]}"
        if (state.get(a["code"]) or {}).get("sig") != sig:
            changed.append(a["code"])
        state[a["code"]] = {"sig": sig, "at": clock.strftime("%Y-%m-%d %H:%M"),
                            "action": a["action"], "scenario": a["scenario"]}
    save_positions(pos)
    write_position_list(pos, clock, quotes)
    # 落报告
    md = os.path.join(REPORT_DIR, f"持仓监控_{clock.strftime('%Y-%m-%d_%H%M')}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# 📤 短线持仓监控 {clock.strftime('%Y-%m-%d %H:%M')}\n\n" + "\n".join(L))
    with open(os.path.join(REPORT_DIR, "最新持仓监控.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"[exit] 报告 {md}")
    mandatory = not getattr(args, "only_if_changed", False)
    if not mandatory and not changed:
        log(f"[exit] 非关键时点且 {len(advices)} 只持仓建议均无变化 → 不推送")
        record_run(clock, "exit", ok=True, holdings=len(holdings), pushed=False,
                   actions=[a["action"] for a in advices], note="建议无变化，静默")
        return 0, None
    if changed:
        log(f"[exit] 建议变化：{'、'.join(changed)}")
    record_run(clock, "exit", ok=True, holdings=len(holdings), pushed=True,
               changed=changed, actions=[a["action"] for a in advices],
               settlements=len(settled) + len(closed))
    return 0, "\n".join(L)


def hk_count(pos):
    return len(pos.get("paper", {}).get("trades", []))


def _close_paper(pos, p, price, reason, cfg, clock, cat=None):
    """把一条模拟持仓按 price 结算并写入 paper.trades（含入场时的分析属性，供复盘分档统计）。"""
    res = net_pnl(p["entry"], price, p.get("shares") or 100, cfg)
    p["status"] = "closed"
    p["close"] = {"date": clock.strftime("%Y-%m-%d"), "time": clock.strftime("%H:%M"),
                  "price": price, **res, "reason": reason}
    pos.setdefault("paper", {}).setdefault("trades", []).append({
        "code": p["code"], "name": p["name"], "entry": p["entry"], "exit": price,
        "entry_date": p.get("entry_date"), "exit_date": clock.strftime("%Y-%m-%d"),
        "exit_time": clock.strftime("%H:%M"), "reason": reason,
        "reason_cat": cat or reason_cat(reason),
        "attrs": p.get("attrs") or {}, **res})
    return res


def paper_stats(pos):
    trades = pos.get("paper", {}).get("trades", [])
    if not trades:
        return ""
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    avg = sum(t["pct"] for t in trades) / n
    aw = sum(t["pct"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in trades if t["pnl"] <= 0]
    al = sum(t["pct"] for t in losses) / len(losses) if losses else 0
    return (f"样本 {n} 笔｜胜率 {len(wins) / n * 100:.0f}%｜平均 {avg:+.2f}%｜"
            f"平均盈利 {aw:+.2f}%／平均亏损 {al:+.2f}%｜盈亏比 {abs(aw / al):.2f}" if al else
            f"样本 {n} 笔｜胜率 {len(wins) / n * 100:.0f}%｜平均 {avg:+.2f}%")


# ────────────────────────────── 周/月总结与复盘 ──────────────────────────────
def period_range(clock, kind):
    """week = 本周一~今天；month = 上一个自然月。"""
    if kind == "week":
        start = clock.date() - timedelta(days=clock.weekday())
        end = clock.date()
    else:
        first = clock.date().replace(day=1)
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    return start, end


def _within(datestr, start, end):
    return bool(datestr) and start.isoformat() <= str(datestr) <= end.isoformat()


def iter_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def collect_period(start, end):
    """汇总区间内的平仓记录、每日候选/买入清单、运行记录。"""
    d = {"trades": [], "cand": {}, "buys": {}, "runs": [], "shadow": {}, "ksnap": {}}
    pos = load_positions()
    for t in pos.get("paper", {}).get("trades", []):
        if _within(t.get("exit_date"), start, end):
            d["trades"].append(t)
    if os.path.isdir(REPORT_DIR):
        for fn in sorted(os.listdir(REPORT_DIR)):
            m = re.match(r"候选_(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m and _within(m.group(1), start, end):
                d["cand"][m.group(1)] = read_json(os.path.join(REPORT_DIR, fn)) or {}
            m2 = re.match(r"买入清单_(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m2 and _within(m2.group(1), start, end):
                d["buys"][m2.group(1)] = read_json(os.path.join(REPORT_DIR, fn)) or {}
            m3 = re.match(r"影子_(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m3 and _within(m3.group(1), start, end):
                d["shadow"][m3.group(1)] = read_json(os.path.join(REPORT_DIR, fn)) or {}
            m4 = re.match(r"K线快照_(\d{4}-\d{2}-\d{2})\.json$", fn)
            if m4 and _within(m4.group(1), start, end):
                d["ksnap"][m4.group(1)] = read_json(os.path.join(REPORT_DIR, fn)) or {}
    for r in iter_jsonl(RUN_LOG):
        if _within(r.get("date"), start, end):
            d["runs"].append(r)
    d["trades"].sort(key=lambda t: ((t.get("exit_date") or ""), (t.get("code") or "")))
    return d


def _agg(trades):
    """一组交易的统计：胜率/平均盈亏/盈亏比/盈利因子/资金曲线/最大回撤/连亏。"""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pcts = [t["pct"] for t in trades]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    aw = statistics.mean([t["pct"] for t in wins]) if wins else 0.0
    al = statistics.mean([t["pct"] for t in losses]) if losses else 0.0
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    curve = [1.0]
    for t in trades:
        curve.append(curve[-1] * (1 + t["pct"] / 100))
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    cur_w = cur_l = max_w = max_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        max_w, max_l = max(max_w, cur_w), max(max_l, cur_l)
    return {"n": n, "win": len(wins), "loss": len(losses), "winrate": len(wins) / n * 100,
            "avg": statistics.mean(pcts), "avg_w": aw, "avg_l": al,
            "pf": (abs(aw / al) if al else None), "profit_factor": (gp / gl if gl else None),
            "sum": sum(pcts), "curve_end": (curve[-1] - 1) * 100, "maxdd": dd * 100,
            "best": max(pcts), "worst": min(pcts), "gross_profit": gp, "gross_loss": gl,
            "cost_total": sum(t.get("cost", 0) for t in trades),
            "max_win_streak": max_w, "max_loss_streak": max_l}


def _group(trades, keyfn):
    buckets = {}
    for t in trades:
        k = keyfn(t)
        if k is None or k == "":
            k = "未记录"
        buckets.setdefault(k, []).append(t)
    return {k: _agg(v) for k, v in buckets.items()}


def _norm_reason(s):
    return re.sub(r"[-\d.]+%?", "#", str(s or "")).strip()


def shadow_summary(data, cfg=None):
    """影子池（初筛池全量）次日收益统计：回答「各关卡淘汰的票是否真的更差」。"""
    rows, unsettled = [], 0
    for sh in (data.get("shadow") or {}).values():
        rs = sh.get("rows") or []
        if sh.get("skip_note") or (rs and not sh.get("settled")):
            unsettled += 1
        rows.extend(rs)
    done = [r for r in rows if r.get("pct") is not None]
    out = {"n_all": len(rows), "n": len(done), "unsettled_days": unsettled}
    if not done:
        return out

    def bucket(keyfn):
        g = {}
        for r in done:
            k = keyfn(r)
            if not k:
                continue
            b = g.setdefault(k, {"n": 0, "sum": 0.0, "win": 0})
            b["n"] += 1
            b["sum"] += r["pct"]
            b["win"] += 1 if r["pct"] > 0 else 0
        for b in g.values():
            b["avg"] = b["sum"] / b["n"]
            b["winrate"] = b["win"] / b["n"] * 100
        return g

    out["by_stage"] = bucket(lambda r: r.get("stage"))

    def pct_band(r):
        p = r.get("entry_pct")
        if p is None or p < 0:
            return None
        lo = float((cfg or {}).get("pct_low", 3.0))
        hi = float((cfg or {}).get("pct_high", 8.0))
        mid = float(int((lo + hi) / 2))
        if p < lo:
            return f"<{lo:.0f}%"
        if p < mid:
            return f"{lo:.0f}~{mid:.0f}%"
        if p < hi:
            return f"{mid:.0f}~{hi:.0f}%"
        return f"≥{hi:.0f}%"
    out["by_pct"] = bucket(pct_band)

    def score_band(r):
        s = r.get("score")
        if s is None:
            return None
        return "高分(≥75)" if s >= 75 else ("中分(65~75)" if s >= 65 else "低分(<65)")
    out["by_score"] = bucket(score_band)
    out["by_fail_reason"] = bucket(lambda r: (f"{r.get('stage')}｜{r.get('detail')}"
                                              if r.get("stage") in ("硬性未通过", "14:51淘汰") and r.get("detail")
                                              else None))
    pcts = [r["pct"] for r in done]
    out.update({"avg": statistics.mean(pcts), "winrate": sum(1 for p in pcts if p > 0) / len(pcts) * 100,
                "best": max(pcts), "worst": min(pcts)})
    return out


def kline_summary(data):
    """K线快照衍生指标：T+1 盘中最高/最低/收盘、T+3 收盘 相对入场价（影子池与实际交易分开）。"""
    groups = {}
    for snap in (data.get("ksnap") or {}).values():
        tdays = snap.get("target_days") or []
        for r in (snap.get("rows") or {}).values():
            bars = (r or {}).get("bars") or {}
            entry = (r or {}).get("entry")
            if not entry or not bars:
                continue
            ds = tdays or sorted(bars)
            d1, d3 = ds[0], ds[-1]
            b1 = bars.get(d1) or []
            if not b1:
                continue
            b3 = bars.get(d3) or []
            groups.setdefault((r or {}).get("kind") or "shadow", []).append({
                "hi1": (max(x[2] for x in b1) / entry - 1) * 100,
                "lo1": (min(x[3] for x in b1) / entry - 1) * 100,
                "c1": (b1[-1][4] / entry - 1) * 100,
                "c3": ((b3[-1][4] / entry - 1) * 100 if b3 else None)})
    out = {}
    for kind, rows in groups.items():
        if not rows:
            continue
        c3 = [x["c3"] for x in rows if x["c3"] is not None]
        out[kind] = {"n": len(rows), "n3": len(c3),
                     "hi1": statistics.mean([x["hi1"] for x in rows]),
                     "lo1": statistics.mean([x["lo1"] for x in rows]),
                     "c1": statistics.mean([x["c1"] for x in rows]),
                     "c3": (statistics.mean(c3) if c3 else None)}
    return out


def summarize(data, cfg=None):
    st = _agg(data["trades"])
    trades = data["trades"]
    by_cat = _group(trades, lambda t: t.get("reason_cat") or reason_cat(t.get("reason")))
    by_form = _group(trades, lambda t: (t.get("attrs") or {}).get("form"))
    by_slot = _group(trades, lambda t: (t.get("attrs") or {}).get("turn_slot"))

    def score_band(t):
        s = (t.get("attrs") or {}).get("score")
        if s is None:
            return None
        return "高分(≥75)" if s >= 75 else ("中分(65~75)" if s >= 65 else "低分(<65)")
    by_score = _group(trades, score_band)

    def gain_band(t):
        g = (t.get("attrs") or {}).get("launch_gain")
        if g is None:
            return None
        return "启动后<3%" if g < 3 else ("启动后3~6%" if g <= 6 else "启动后>6%")
    by_gain = _group(trades, gain_band)

    def pct_band(t):
        p = (t.get("attrs") or {}).get("entry_pct")
        if p is None:
            return None
        return "涨幅3~5%" if p < 5 else "涨幅5~8%"
    by_pct = _group(trades, pct_band)
    by_ind = _group(trades, lambda t: (t.get("attrs") or {}).get("industry"))

    # 选股漏斗
    scan_days = sorted(data["cand"].keys())
    stage1 = [c.get("stage1") or 0 for c in data["cand"].values()]
    analyzed = [len(c.get("candidates") or []) for c in data["cand"].values()]
    tradable = [len([x for x in (c.get("candidates") or []) if x.get("tradable")]) for c in data["cand"].values()]
    buy_days = sorted(data["buys"].keys())
    confirmed = sum(len(b.get("confirmed") or []) for b in data["buys"].values())
    rejected = sum(len(b.get("rejected") or []) for b in data["buys"].values())
    overflow = sum(len(b.get("overflow") or []) for b in data["buys"].values())
    from collections import Counter
    fail_cnt, rej_cnt = Counter(), Counter()
    for c in data["cand"].values():
        for x in c.get("candidates") or []:
            fs = x.get("hard_fail") or []
            if fs:
                fail_cnt[_norm_reason(fs[0])] += 1
    for b in data["buys"].values():
        for r in b.get("rejected") or []:
            rs = r.get("reasons") or []
            if rs:
                rej_cnt[_norm_reason(rs[0])] += 1
    data_fail = sorted({r.get("date") for r in data["runs"]
                        if (r.get("mode") == "scan" and (r.get("ok") is False or r.get("critical")))})
    funnel = {"scan_days": len(scan_days), "buy_days": len(buy_days),
              "stage1_avg": (statistics.mean(stage1) if stage1 else 0),
              "analyzed_avg": (statistics.mean(analyzed) if analyzed else 0),
              "tradable_avg": (statistics.mean(tradable) if tradable else 0),
              "tradable_total": sum(tradable), "confirmed": confirmed, "rejected": rejected,
              "overflow": overflow, "fail_top": fail_cnt.most_common(6),
              "rej_top": rej_cnt.most_common(5), "data_fail_days": data_fail,
              "scan_missing": [d for d in data["buys"] if d not in data["cand"]]}
    return {"stats": st, "by_cat": by_cat, "by_form": by_form, "by_slot": by_slot,
            "by_score": by_score, "by_gain": by_gain, "by_pct": by_pct, "by_ind": by_ind,
            "funnel": funnel, "shadow": shadow_summary(data, cfg), "kline": kline_summary(data)}


def diagnose(summary, cfg):
    """把统计结果转成可执行的改进建议（规则驱动、带数据证据，不做主观猜测）。"""
    st, f = summary["stats"], summary["funnel"]
    out = []

    def add(level, title, evidence, action):
        out.append({"level": level, "title": title, "evidence": evidence, "action": action})

    n = st.get("n", 0)
    if n == 0:
        add("info", "本期无平仓样本", "没有足够交易样本（0 笔）",
            "继续累积前向样本；若连续多期无样本，按下方「通过率」条目检查参数是否过严")
    elif n < 8:
        add("info", "样本量偏小", f"仅 {n} 笔，统计结论不稳健",
            "单期结论只做方向性参考，至少累积 20 笔再按结论调参")
    if n:
        cats = summary["by_cat"]
        zs = cats.get("止损")
        if zs and zs["n"] >= 2 and zs["avg"] <= -1.2:
            add("warning", "止损单平均亏损偏大",
                f"止损出场 {zs['n']} 笔，平均 {zs['avg']:+.2f}%，最差 {zs['worst']:+.2f}%",
                f"把 min_stop_pct 从 {cfg['min_stop_pct']}% 提高，或按 max(min_stop_pct, 0.4×当日振幅%) 放宽止损距离")
        last = cats.get("最迟退出")
        if last and last["n"] / max(n, 1) >= 0.5 and last["avg"] <= 0:
            add("warning", "过半个股靠最迟退出结清，且为负贡献",
                f"最迟退出 {last['n']}/{n} 笔，平均 {last['avg']:+.2f}%",
                "说明次日冲高预期多数未兑现：考虑 9:35–10:30 主动止盈，并提高入场质量"
                f"（min_rr {cfg['min_rr']}→2.5、要求量比≥1.2、min_upside_pct {cfg['min_upside_pct']}→4）")
        weak = cats.get("跌停未成交")
        if weak:
            add("warning", "出现跌停无法卖出", f"{weak['n']} 笔按跌停处理（实际可能无法成交）",
                "隔夜风险按最坏情况估算仓位；连续出现时缩小单笔风险占比")
        sc = summary["by_score"]
        hi, mid, lo = sc.get("高分(≥75)"), sc.get("中分(65~75)"), sc.get("低分(<65)")
        if hi and mid and hi["n"] >= 2 and mid["n"] >= 2 and hi["avg"] < mid["avg"] - 0.5:
            add("warning", "高分档未优于中分档：评分与结果不相关",
                f"高分 {hi['n']} 笔 {hi['avg']:+.2f}%　中分 {mid['n']} 笔 {mid['avg']:+.2f}%",
                "评分权重需要回测校准（当前权重是初始值）；建议先按「日K启动形态+10分钟结构」两项排序做对照")
        for key, label, act in (("by_form", "形态", None), ("by_slot", "拐头时段", None),
                                ("by_gain", "启动后涨幅", None), ("by_pct", "当日涨幅区间", None)):
            g = summary[key]
            items = [(k, v) for k, v in g.items() if v.get("n", 0) >= 2 and k != "未记录"]
            if len(items) >= 2:
                items.sort(key=lambda x: -x[1]["avg"])
                good, bad = items[0], items[-1]
                if good[1]["avg"] - bad[1]["avg"] >= 1.5 and bad[1]["avg"] <= 0:
                    tips = {
                        "by_form": f"收紧到只做 {good[0]} 类形态（{bad[0]} 类样本为负）",
                        "by_slot": f"加时段过滤：只做 {good[0]}启动（{bad[0]}启动样本为负）",
                        "by_gain": f"下调 max_launch_gain（当前 {cfg['max_launch_gain']}%），"
                                   f"排除 {bad[0]} 的样本",
                        "by_pct": f"收窄初筛涨幅区间（当前 {cfg['pct_low']}~{cfg['pct_high']}%）",
                    }
                    add("warning", f"{label}分档差异明显",
                        f"{good[0]} {good[1]['n']} 笔 {good[1]['avg']:+.2f}% ／ "
                        f"{bad[0]} {bad[1]['n']} 笔 {bad[1]['avg']:+.2f}%", tips[key])
        ind = [(k, v) for k, v in summary["by_ind"].items() if v.get("n", 0) >= 3 and k != "未记录"]
        for k, v in ind:
            if v["sum"] < 0:
                add("warning", f"板块集中且整体亏损：{k}",
                    f"{v['n']} 笔全部在该行业，合计 {v['sum']:+.2f}%",
                    "同板块同时最多 1–2 只（在 14:51 确认环节加同行业去重），并检查该板块当日是否已过热")
        if st.get("gross_profit") and st.get("cost_total"):
            ratio = st["cost_total"] / (st["gross_profit"] + st["cost_total"])
            if ratio > 0.25:
                add("warning", "交易成本占毛利比例偏高",
                    f"累计成本 {st['cost_total']:.0f} 元，占毛利 {ratio * 100:.0f}%",
                    f"减少小空间交易：min_upside_pct {cfg['min_upside_pct']}%→4%，或只做距压力位≥6% 的标的")
        if st.get("maxdd", 0) <= -3 and st.get("curve_end", 0) > 0:
            add("info", "资金曲线回撤偏深",
                f"累计 {st['curve_end']:+.2f}%，最大回撤 {st['maxdd']:.2f}%",
                f"单笔风险占比从 {cfg['risk_per_trade_pct']}% 下调，或减少同时持仓只数（max_buy_list）")
        if st.get("max_loss_streak", 0) >= 3:
            add("info", "出现连续亏损", f"最长连亏 {st['max_loss_streak']} 笔",
                "设退役条件：连亏 3 笔后暂停一日或只做评分前 1 名，避免连续在同一市场环境里重复出错")
    # 漏斗类
    if f["scan_days"]:
        if f["tradable_avg"] < 1:
            add("warning", "硬性条件过严：日均通过不足 1 只",
                f"{f['scan_days']} 个交易日平均通过 {f['tradable_avg']:.2f} 只"
                f"（日均初筛 {f['stage1_avg']:.0f} 只，送分析 {f['analyzed_avg']:.0f} 只）",
                f"逐项放宽一个门槛做对照：min_rr {cfg['min_rr']}→1.5、min_upside_pct "
                f"{cfg['min_upside_pct']}%→2%，或 max_dev_from_ma {cfg['max_dev_from_ma']}%→4%")
        if f["tradable_total"] and f["rejected"] / max(f["tradable_total"], 1) >= 0.5:
            top = f["rej_top"][0][0] if f["rej_top"] else "—"
            add("warning", "14:31 通过但 14:51 被淘汰的比例偏高",
                f"14:31 通过 {f['tradable_total']} 只，14:51 淘汰 {f['rejected']} 只，主因：{top}",
                "把尾盘强势要求前移到 14:31（要求 MA5 方向向上、距当日高点 <2%），"
                "或把确认时点从 14:51 提前到 14:40 以留出执行窗口")
        if f["scan_missing"]:
            add("warning", "存在有买入清单但缺当日候选文件的天数",
                "、".join(f["scan_missing"][:5]), "检查 14:31 任务是否被跳过或数据源失败")
    if f["data_fail_days"]:
        add("warning", "存在数据校验失败或关键项异常的交易日",
            "、".join(f["data_fail_days"][:6]), "排查对应日期的数据源抓取日志，确认这些日子未出交易建议")
    # 影子跟踪：初筛池随机买入基准 + 各关卡是否误杀好票（样本量大，结论才可靠）
    sh = summary.get("shadow") or {}
    n_sh, base = sh.get("n", 0), sh.get("avg")
    stages = sh.get("by_stage") or {}
    pass_b = stages.get("买入执行") or {}
    if n_sh >= 30 and base is not None:
        add("info", "影子基准：初筛池「随机买入」次日收益",
            f"已结算 {n_sh} 条（记录 {sh.get('n_all')} 条），平均 {base:+.2f}%，胜率 {sh.get('winrate'):.0f}%",
            "这是策略文档第十二节要求的随机买入基准：实际交易平均收益必须高于它，否则选股流程没有产生增量")
        if st.get("n") and pass_b.get("n", 0) >= 5:
            gap = st["avg"] - pass_b["avg"]
            if gap <= 0.3:
                add("warning", "执行环节未跑赢自己的候选池",
                    f"实际 {st['n']} 笔 {st['avg']:+.2f}%（含费用） vs 影子「买入执行」{pass_b['n']} 条 "
                    f"{pass_b['avg']:+.2f}%（毛收益），差 {gap:+.2f}pct",
                    "问题在卖出而非选股：核对「最迟退出 14:50 + 四种情景卖出」是否比「次日 14:50 无条件卖出」更差，"
                    "先改卖出规则再谈选股权重")
        for stage in ("硬性未通过", "14:51淘汰", "未深度分析", "通过超上限"):
            b = stages.get(stage)
            if not b or b.get("n", 0) < 30:
                continue
            ref = pass_b["avg"] if pass_b.get("n", 0) >= 5 else base
            if b["avg"] > ref + 0.3:
                add("warning", f"「{stage}」这一关可能在筛掉好票",
                    f"被淘汰 {b['n']} 条平均次日 {b['avg']:+.2f}%（胜率 {b['winrate']:.0f}%），"
                    f"高于{'执行批' if pass_b.get('n', 0) >= 5 else '初筛池'}的 {ref:+.2f}%",
                    "先放宽该关一档做对照（影子池次日收益会立刻反映效果），确认收益提升再改 config，别直接删关卡")
        fr = [(k, v) for k, v in (sh.get("by_fail_reason") or {}).items() if v.get("n", 0) >= 30]
        if fr:
            fr.sort(key=lambda x: -x[1]["avg"])
            k, v = fr[0]
            if v["avg"] > (base or 0) + 0.3 and v["avg"] > 0:
                add("warning", f"淘汰原因中最赚钱的一类：{k}",
                    f"{v['n']} 条平均次日 {v['avg']:+.2f}%（胜率 {v['winrate']:.0f}%），高于初筛池基准 {base:+.2f}%",
                    "该项可能是反指标：先判断它是「日K/结构」类硬性条件还是数据缺失误判，再决定放宽门槛还是改判据")
        sc = sh.get("by_score") or {}
        hi, lo = sc.get("高分(≥75)"), sc.get("低分(<65)")
        if hi and lo and hi.get("n", 0) >= 30 and lo.get("n", 0) >= 30 and hi["avg"] <= lo["avg"]:
            add("warning", "评分与次日收益不相关（影子样本）",
                f"高分 {hi['n']} 条 {hi['avg']:+.2f}% vs 低分 {lo['n']} 条 {lo['avg']:+.2f}%",
                "评分权重需重新校准：先用影子样本做单因子检验（形态/涨幅/量比各自单独分档），再改 score_weights")
        bp = [(k, v) for k, v in (sh.get("by_pct") or {}).items() if v.get("n", 0) >= 30]
        if len(bp) >= 2:
            bp.sort(key=lambda x: x[1]["avg"])
            if bp[-1][1]["avg"] - bp[0][1]["avg"] >= 0.8:
                add("warning", "当日涨幅档在影子样本上差异明显",
                    f"{bp[-1][0]} {bp[-1][1]['n']} 条 {bp[-1][1]['avg']:+.2f}% ／ "
                    f"{bp[0][0]} {bp[0][1]['n']} 条 {bp[0][1]['avg']:+.2f}%",
                    f"按影子样本收窄初筛涨幅区间（当前 {cfg['pct_low']}~{cfg['pct_high']}%），每次只改一端，"
                    "改完用下一周影子数据验证效果")
    elif n_sh:
        add("info", "影子样本还不足",
            f"已结算 {n_sh} 条（≥30 条才下结论），记录 {sh.get('n_all')} 条",
            "每日 14:51 自动记录、次日结算，无需人工干预")
    kl = (summary.get("kline") or {}).get("shadow") or {}
    if kl.get("n", 0) >= 30 and kl.get("c3") is not None:
        gap = kl["c3"] - kl["c1"]
        if gap >= 1.0:
            add("info", "影子池上「多持到 T+3」优于 T+1 卖出",
                f"T+1 收盘 {kl['c1']:+.2f}% → T+3 收盘 {kl['c3']:+.2f}%（{kl.get('n3')} 条）",
                "先做 T+1/T+3 两组对照（含最大回撤），再考虑把 max_hold_days 从 1 改成 3；改前确认能接受回撤加深")
        elif gap <= -1.0:
            add("info", "影子池上 T+1 卖出优于多持两天",
                f"T+1 收盘 {kl['c1']:+.2f}% → T+3 收盘 {kl['c3']:+.2f}%（{kl.get('n3')} 条）",
                "维持 max_hold_days=1；T+1 盘中最高价是卖点理论上界，可据此微调卖出时点（如 14:00→14:30）")
    if not out:
        add("info", "未发现明确偏离", "各项指标没有触发预设的改进阈值",
            "保持参数不变继续累积样本；本期结论仅作记录")
    order = {"warning": 0, "info": 1}
    out.sort(key=lambda x: order.get(x["level"], 2))
    return out


def fmt_stat_line(a, label=""):
    if not a or not a.get("n"):
        return f"{label}：无样本"
    pf = f"{a['pf']:.2f}" if a.get("pf") else "—"
    return (f"{label}：{a['n']} 笔　胜率 {a['winrate']:.0f}%　平均 {a['avg']:+.2f}%　盈亏比 {pf}")


def build_summary_report(clock, cfg, kind, start, end, summ, diags):
    st, f = summ["stats"], summ["funnel"]
    label = "周总结" if kind == "week" else "月总结"
    L = [f"# 📈 短线策略{label} · {start} ~ {end}", ""]
    L.append(f"- 生成时间：{clock.strftime('%Y-%m-%d %H:%M')}（CST）　统计区间：{start} ~ {end}"
             f"　统计口径：**区间内平仓的模拟交易**（等额单笔、含佣金/印花税/过户费/滑点）")
    L.append(f"- 选股覆盖：{f['scan_days']} 个交易日有候选文件、{f['buy_days']} 个交易日有买入清单")
    L.append("")
    if not st.get("n"):
        L.append("## 交易结果：本期无平仓样本")
        L.append("")
    else:
        L.append("## 交易结果")
        L.append("")
        L.append(f"- 交易笔数：{st['n']}（盈利 {st['win']} / 亏损 {st['loss']}）")
        L.append(f"- 胜率：**{st['winrate']:.1f}%**")
        L.append(f"- 平均每笔：**{st['avg']:+.2f}%**（平均盈利 {st['avg_w']:+.2f}% / 平均亏损 {st['avg_l']:+.2f}%）")
        L.append(f"- 盈亏比（均盈/均亏）：**{st['pf']:.2f}**" if st.get("pf") else "- 盈亏比：无亏损样本")
        L.append(f"- 盈利因子：**{st['profit_factor']:.2f}**" if st.get("profit_factor") else "- 盈利因子：无亏损样本")
        L.append(f"- 累计（等额复利模拟资金曲线）：**{st['curve_end']:+.2f}%**　最大回撤：**{st['maxdd']:.2f}%**")
        L.append(f"- 最佳 {st['best']:+.2f}% / 最差 {st['worst']:+.2f}%　最长连盈 {st['max_win_streak']} 笔 / 连亏 {st['max_loss_streak']} 笔")
        L.append(f"- 交易成本合计：{st['cost_total']:.0f} 元（费率见 config.json）")
        L.append("")
    L.append("## 选股漏斗")
    L.append("")
    L.append(f"- 日均初筛命中：{f['stage1_avg']:.0f} 只　送深度分析：{f['analyzed_avg']:.0f} 只　通过硬性条件：{f['tradable_avg']:.2f} 只/日（合计 {f['tradable_total']}）")
    L.append(f"- 14:51 确认买入：{f['confirmed']} 笔　当轮淘汰：{f['rejected']} 笔　超上限放弃：{f['overflow']} 笔")
    if f["fail_top"]:
        L.append("- 未通过硬性条件的主因（归一化后计数）：")
        for k, v in f["fail_top"]:
            L.append(f"  - {v} 次：{k}")
    if f["rej_top"]:
        L.append("- 14:51 淘汰原因：")
        for k, v in f["rej_top"]:
            L.append(f"  - {v} 次：{k}")
    L.append("")
    if st.get("n"):
        L.append("## 分档统计（用于识别哪一类信号更有效）")
        L.append("")
        for title, key in (("卖出原因", "by_cat"), ("启动形态", "by_form"), ("拐头时段", "by_slot"),
                           ("评分档", "by_score"), ("启动后涨幅", "by_gain"), ("当日涨幅区间", "by_pct")):
            items = sorted(summ[key].items(), key=lambda x: -x[1].get("avg", 0))
            txt = "｜".join(f"{k} {v['n']}笔 {v['avg']:+.2f}%" for k, v in items if v.get("n"))
            L.append(f"- {title}：{txt or '—'}")
        L.append("")
    sh = summ.get("shadow") or {}
    if sh.get("n"):
        L.append("## 影子跟踪（初筛池次日收益基准）")
        L.append("")
        L.append(f"- 已结算 **{sh['n']}** 条／记录 {sh['n_all']} 条（未结算 {sh['unsettled_days']} 个交易日）　"
                 f"平均次日收益 **{sh['avg']:+.2f}%**　胜率 {sh['winrate']:.0f}%　"
                 f"最佳 {sh['best']:+.2f}% / 最差 {sh['worst']:+.2f}%")
        L.append("- 口径：每日初筛池**全部**标的，假设 14:51 买入、次日 14:51（≈14:50）卖出，毛收益不计费用；"
                 "真实时点前向数据、无未来函数 → 即第十二节的「随机买入基准」")
        L.append("- 按关卡（在哪一步通过／被淘汰）：")
        for k, v in sorted(sh["by_stage"].items(), key=lambda x: -x[1]["n"]):
            L.append(f"  - {k}：{v['n']} 条　平均 {v['avg']:+.2f}%　胜率 {v['winrate']:.0f}%")
        pb = (sh["by_stage"].get("买入执行") or {})
        if st.get("n") and pb.get("n"):
            L.append(f"- 执行对照：实际 {st['n']} 笔 {st['avg']:+.2f}%（含费用） vs 影子同批 {pb['n']} 条 "
                     f"{pb['avg']:+.2f}%（毛收益）")
        for title, key in (("当日涨幅档", "by_pct"), ("评分档", "by_score")):
            items = sorted((sh.get(key) or {}).items(), key=lambda x: -x[1]["avg"])
            if items:
                L.append(f"- {title}：" + "｜".join(f"{k} {v['n']}条 {v['avg']:+.2f}%" for k, v in items))
        fr = sorted((sh.get("by_fail_reason") or {}).items(), key=lambda x: -x[1]["avg"])[:8]
        if fr:
            L.append("- 淘汰原因分档（按次日收益排序）：")
            for k, v in fr:
                L.append(f"  - {k}：{v['n']} 条　{v['avg']:+.2f}%　胜率 {v['winrate']:.0f}%")
        L.append("")
    kl = summ.get("kline") or {}
    if kl:
        L.append("## K线快照（T+1 / T+2 / T+3 的 10 分钟K线）")
        L.append("")
        L.append("- 口径：每笔建仓（实际交易 + 影子池）都记录其后三个交易日的 10 分钟K线；下面是相对入场价的平均表现 —— "
                 "**T+1 最高**＝当日卖点理论上限，**T+1 收盘**＝持到当日收盘，**T+3 收盘**＝多持两天的结果。")
        for kind, label in (("shadow", "影子池"), ("trade", "实际交易")):
            v = kl.get(kind)
            if not v:
                continue
            seg = (f"- {label} {v['n']} 条：T+1 最高 {v['hi1']:+.2f}%／最低 {v['lo1']:+.2f}%／"
                   f"收盘 {v['c1']:+.2f}%")
            if v.get("c3") is not None:
                seg += f"｜T+3 收盘 {v['c3']:+.2f}%（{v['n3']} 条）"
            L.append(seg)
        sh = kl.get("shadow") or {}
        if sh.get("c1") is not None:
            extra = (f"；T+3 收盘 − T+1 收盘 = 多持两天 {sh['c3'] - sh['c1']:+.2f}pct"
                     if sh.get("c3") is not None else "；T+3 数据待补齐")
            L.append(f"- 读法：T+1 最高 − T+1 收盘 = 当日卖点还剩 {sh['hi1'] - sh['c1']:+.2f}pct（影子池）" + extra)
        L.append("- 原始数据：`报告/K线快照_YYYY-MM-DD.json`（bar = [结束时间, 开, 高, 低, 收, 量(手)]，"
                 "只含已完成 bar、不含未来K线）")
        L.append("")
    L.append("## 复盘：需要改进的地方")
    L.append("")
    for d in diags:
        icon = "⚠️" if d["level"] == "warning" else "ℹ️"
        L.append(f"### {icon} {d['title']}")
        L.append(f"- 现象/数据：{d['evidence']}")
        L.append(f"- 建议动作：{d['action']}")
        L.append("")
    L.append("---")
    L.append("")
    L.append("统计基于**已购买清单**（14:51 确认价建仓、按引擎卖出建议或最迟退出平仓，含费用与滑点）；"
             "你按建议执行全部买入与全部卖出，减仓未计入，故与实盘基本一致。"
             "本策略仍属待验证假设，样本未达 20 笔前不建议据此调整资金规模。技术分析不构成投资建议。")
    return "\n".join(L)


def _bucket_line(summ, title, key):
    items = sorted(summ[key].items(), key=lambda x: -x[1].get("avg", 0))
    txt = "｜".join(f"{k} {v['n']}笔 {v['avg']:+.2f}%" for k, v in items if v.get("n"))
    return f"- {title}：{txt or '—'}"


def build_summary_push(clock, cfg, kind, start, end, summ, diags, trades=None, full=None):
    """推送正文。full=True 推全量（漏斗明细+全部分档+逐笔+全部诊断），False 只推摘要。"""
    st, f = summ["stats"], summ["funnel"]
    label = "周总结" if kind == "week" else "月总结"
    s5, e5 = str(start)[5:], str(end)[5:]
    if full is None:
        full = bool(cfg.get("summary_push_full", True))
    L = [f"**📈 短线策略{label} · {s5} ~ {e5}**", ""]
    if st.get("n"):
        pf = f"{st['pf']:.2f}" if st.get("pf") else "—"
        pfact = f"{st['profit_factor']:.2f}" if st.get("profit_factor") else "—"
        L.append(f"样本 **{st['n']} 笔**｜胜率 **{st['winrate']:.0f}%**（{st['win']}胜{st['loss']}负）｜"
                 f"平均 **{st['avg']:+.2f}%**｜盈亏比 {pf}｜盈利因子 {pfact}")
        L.append(f"平均盈利 {st['avg_w']:+.2f}%／平均亏损 {st['avg_l']:+.2f}%｜"
                 f"资金曲线 {st['curve_end']:+.2f}%｜最大回撤 {st['maxdd']:.2f}%")
        L.append(f"最佳 {st['best']:+.2f}%／最差 {st['worst']:+.2f}%｜"
                 f"连盈 {st['max_win_streak']} 笔／连亏 {st['max_loss_streak']} 笔｜成本 {st['cost_total']:.0f} 元")
    else:
        L.append("<font color=\"comment\">本期无平仓样本（没有产生可统计的交易）</font>")
    L.append("")
    L.append(f"选股：{f['scan_days']} 个交易日｜日均初筛 {f['stage1_avg']:.0f} 只 → 日均通过 {f['tradable_avg']:.2f} 只｜"
             f"14:51 买入 {f['confirmed']} 笔／淘汰 {f['rejected']} 笔")
    if not full:
        if st.get("n"):
            cats = "｜".join(f"{k} {v['n']}笔 {v['avg']:+.2f}%" for k, v in
                             sorted(summ["by_cat"].items(), key=lambda x: -x[1]["n"]) if v.get("n"))
            L.append(f"卖出构成：{cats}")
        L.append("")
        L.append("**复盘：需要改进的地方**")
        for d in diags[:5]:
            icon = "⚠️" if d["level"] == "warning" else "·"
            L.append(f"{icon} **{d['title']}**")
            L.append(f"　{d['evidence']}")
            L.append(f"　→ {d['action']}")
        if len(diags) > 5:
            L.append(f"（另有 {len(diags) - 5} 条见完整报告）")
    else:
        # ── 全量：选股漏斗明细 ──
        L.append("")
        L.append("**选股漏斗**")
        L.append(f"{f['scan_days']} 天有候选文件／{f['buy_days']} 天有买入清单｜"
                 f"日均送分析 {f['analyzed_avg']:.0f} 只｜通过硬性条件合计 {f['tradable_total']} 只")
        if f["fail_top"]:
            L.append("未通过主因：")
            for k, v in f["fail_top"]:
                L.append(f"· {v} 次｜{k}")
        if f["rej_top"]:
            L.append("14:51 淘汰原因：")
            for k, v in f["rej_top"]:
                L.append(f"· {v} 次｜{k}")
        if f.get("data_fail_days"):
            L.append(f"<font color=\"comment\">数据异常日：{'、'.join(f['data_fail_days'])}</font>")
        # ── 全量：分档统计 ──
        if st.get("n"):
            L.append("")
            L.append("**分档统计**")
            for title, key in (("卖出原因", "by_cat"), ("启动形态", "by_form"), ("拐头时段", "by_slot"),
                               ("评分档", "by_score"), ("启动后涨幅", "by_gain"),
                               ("当日涨幅", "by_pct"), ("行业", "by_ind")):
                L.append(_bucket_line(summ, title, key))
            # ── 全量：逐笔明细 ──
            if trades:
                cap = int(cfg.get("summary_push_trades", 40))
                L.append("")
                L.append(f"**逐笔明细**（{len(trades)} 笔）")
                for t in trades[:cap]:
                    a = t.get("attrs") or {}
                    tags = []
                    if a.get("form"):
                        tags.append(f"{a['form']}类")
                    if a.get("score") is not None:
                        tags.append(f"{a['score']:.0f}分")
                    if t.get("reason_cat") or t.get("reason"):
                        tags.append(t.get("reason_cat") or t.get("reason"))
                    D = (t.get("exit_date") or "")[5:]
                    sign = "+" if t.get("pct", 0) >= 0 else ""
                    L.append(f"{D} {t.get('code')} {t.get('name')} {t.get('entry')}→{t.get('exit')} "
                             f"{sign}{t.get('pct', 0):.2f}%｜{'／'.join(tags)}")
                if len(trades) > cap:
                    L.append(f"（另有 {len(trades) - cap} 笔见完整报告）")
        sh = summ.get("shadow") or {}
        if sh.get("n"):
            L.append("")
            L.append("**影子跟踪（初筛池基准）**")
            L.append(f"已结算 {sh['n']} 条／记录 {sh['n_all']} 条｜平均次日 {sh['avg']:+.2f}%｜"
                     f"胜率 {sh['winrate']:.0f}%" + (f"｜实际 {st['n']} 笔 {st['avg']:+.2f}%" if st.get("n") else ""))
            for k, v in sorted(sh["by_stage"].items(), key=lambda x: -x[1]["n"]):
                L.append(f"· {k} {v['n']}条 {v['avg']:+.2f}%（胜率 {v['winrate']:.0f}%）")
            sc = sorted((sh.get("by_score") or {}).items(), key=lambda x: -x[1]["avg"])
            if sc:
                L.append("评分档：" + "｜".join(f"{k} {v['n']}条 {v['avg']:+.2f}%" for k, v in sc))
            bp = sorted((sh.get("by_pct") or {}).items(), key=lambda x: -x[1]["avg"])
            if bp:
                L.append("涨幅档：" + "｜".join(f"{k} {v['n']}条 {v['avg']:+.2f}%" for k, v in bp))
        kl = summ.get("kline") or {}
        if kl:
            L.append("")
            L.append("**K线快照（T+1/T+3 10分钟K线）**")
            for kind, label in (("shadow", "影子池"), ("trade", "实际交易")):
                v = kl.get(kind)
                if not v:
                    continue
                seg = f"{label} {v['n']} 条：T+1 最高 {v['hi1']:+.2f}%／收盘 {v['c1']:+.2f}%"
                if v.get("c3") is not None:
                    seg += f"｜T+3 收盘 {v['c3']:+.2f}%"
                L.append(seg)
        L.append("")
        L.append(f"**复盘：需要改进的地方**（{len(diags)} 条）")
        for d in diags:
            icon = "⚠️" if d["level"] == "warning" else "·"
            L.append(f"{icon} **{d['title']}**")
            L.append(f"　{d['evidence']}")
            L.append(f"　→ {d['action']}")
    L.append("")
    L.append("<font color=\"comment\">口径：区间内按建议卖出平仓的持仓（全部买入/全部卖出均按建议执行，减仓不计），"
             "等额单笔、含费用滑点；样本未达 20 笔前不建议据此调整资金规模。技术分析不构成投资建议。</font>")
    return "\n".join(L)


def mode_summary(clock, cfg, args, kind):
    start, end = period_range(clock, kind)
    log(f"[{kind}] 统计区间 {start} ~ {end}")
    data = collect_period(start, end)
    summ = summarize(data, cfg)
    diags = diagnose(summ, cfg)
    label = "周总结" if kind == "week" else "月总结"
    md = build_summary_report(clock, cfg, kind, start, end, summ, diags)
    fn = (f"周总结_{end}.md" if kind == "week" else f"月总结_{start.strftime('%Y-%m')}.md")
    p = os.path.join(REPORT_DIR, fn)
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(REPORT_DIR, f"最新{label}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    log(f"[{kind}] 报告 {p}（平仓样本 {summ['stats'].get('n', 0)} 笔）")
    # 空期保护：既无交易、又整天没有候选文件 → 不推送（避免节假日噪声）
    if not summ["stats"].get("n") and summ["funnel"]["scan_days"] == 0:
        log(f"[{kind}] 区间内既无交易也无选股记录 → 不推送")
        record_run(clock, kind, ok=True, pushed=False, trades=0, scan_days=0)
        return 0
    push = build_summary_push(clock, cfg, kind, start, end, summ, diags,
                              trades=data["trades"], full=(False if args.brief_summary else None))
    if cfg["push"] and not args.no_push:
        push_report(push, tag=args.test_tag)
    else:
        log(push)
    record_run(clock, kind, ok=True, pushed=bool(cfg["push"] and not args.no_push),
               trades=summ["stats"].get("n", 0), scan_days=summ["funnel"]["scan_days"],
               winrate=round(summ["stats"].get("winrate", 0), 1),
               warnings=len([d for d in diags if d["level"] == "warning"]))
    return 0


# ────────────────────────────── 主流程 ──────────────────────────────

def stage1_filter(rows, cfg, clock):
    """第一步：涨幅榜初筛（§三）。"""
    keep, dropped = [], {"停牌无价": 0, "ST/风险警示": 0, "次新": 0, "板块排除": 0,
                         "涨幅区间": 0, "成交额": 0, "换手率": 0, "量比": 0}
    today = clock.date()
    for r in rows:
        if not r["code"] or r["price"] is None or not r["pct"]:
            dropped["停牌无价"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "停牌无价"})
            continue
        if is_st(r["name"]):
            dropped["ST/风险警示"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "ST/风险警示"})
            continue
        if board_of(r["code"]) in cfg["exclude_boards"]:
            dropped["板块排除"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "板块排除"})
            continue
        ld = r.get("list_date") or ""
        if len(ld) == 8:
            try:
                d0 = datetime.strptime(ld, "%Y%m%d").date()
                if (today - d0).days < cfg["exclude_new_days"]:
                    dropped["次新"] += 1
                    audit_emit("stage1", {"row": r, "passed": False, "reason": "次新"})
                    continue
            except ValueError:
                pass
        if not (cfg["pct_low"] <= r["pct"] <= cfg["pct_high"]):
            dropped["涨幅区间"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "涨幅区间"})
            continue
        if (r.get("amount") or 0) < cfg["min_amount_yi"] * 1e8:
            dropped["成交额"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "成交额"})
            continue
        to = r.get("turnover")
        if to is not None and not (cfg["min_turnover"] <= to <= cfg["max_turnover"]):
            dropped["换手率"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "换手率"})
            continue
        vr = r.get("vol_ratio")
        if vr is not None and vr > 0 and vr < cfg["min_volume_ratio"]:
            dropped["量比"] += 1
            audit_emit("stage1", {"row": r, "passed": False, "reason": "量比"})
            continue
        audit_emit("stage1", {"row": r, "passed": True, "reason": None})
        keep.append(r)
    return keep, dropped


def prescore(r, cfg):
    """第二阶段前的粗排序：偏好放量、成交额大、涨幅在区间中段、主力净流入。"""
    s = 0.0
    s += min((r.get("amount") or 0) / 1e9, 3) * 2
    s += min((r.get("vol_ratio") or 0), 4) * 1.2
    p = r.get("pct") or 0
    s += 2.5 - abs(p - (cfg["pct_low"] + cfg["pct_high"]) / 2)
    if (r.get("main_net") or 0) > 0:
        s += 2
    if r.get("speed") and r["speed"] > 0:
        s += 0.5
    return s


def mode_scan(clock, cfg, args):
    today = clock.strftime("%Y-%m-%d")
    validations = []
    log(f"[scan] 开始 {clock.strftime('%Y-%m-%d %H:%M')} 全市场初筛")
    try:
        env = em_market_env()
    except Exception as e:  # noqa: BLE001
        env = {}
        validations.append(f"❌ 市场环境数据获取失败：{e}")
    if not market_data_fresh(env, clock):
        log("[scan] 行情日期非当日（非交易日或数据未更新）→ 不推送，直接退出")
        return 0
    clist_src = "东财"
    try:
        rows, market_total = em_clist(stop_pct=cfg["pct_low"], max_pages=60)
    except Exception as e:  # noqa: BLE001
        log(f"[err] 东财涨幅榜失败：{brief_err(e)} → 尝试新浪备用源")
        try:
            rows, market_total = sina_clist(stop_pct=cfg["pct_low"])
            clist_src = "新浪备用"
        except Exception as e2:  # noqa: BLE001
            log(f"[err] 新浪涨幅榜也失败：{brief_err(e2)}")
            msg = (f"**⚠️ 短线策略 {today} 14:31 初筛失败**\n\n"
                   f"涨幅榜两个源都取不到（东财：{brief_err(e)}；新浪：{brief_err(e2)}），"
                   "本轮不产生任何候选。数据缺失时不编造结论。")
            if cfg["push"] and not args.no_push:
                push_report(msg)
            return 1
    log(f"[scan] 涨幅榜（{clist_src}）返回 {len(rows)} 行（全市场 {market_total} 只）")
    audit_emit("scan_market", {"rows": rows, "market_total": market_total, "source": clist_src,
                                "environment": env, "coverage": "fetched_rank_pages_only"})
    keep, dropped = stage1_filter(rows, cfg, clock)
    if clist_src == "东财":
        if market_total and 3000 <= market_total <= 8000:
            validations.append(f"✅ 涨幅榜样本 {len(rows)} 行（全市场 {market_total} 只，其中涨幅≥{cfg['pct_low']}% 的 {len(rows)} 只）")
        else:
            validations.append(f"⚠️ 全市场只数异常（{market_total}），涨幅榜可能不完整")
    else:
        validations.append(f"⚠️ 涨幅榜取自{clist_src}源（东财 clist 被拒）：样本 {len(rows)} 行，"
                           f"缺量比/主力净额字段，粗排序会偏低；建议收盘后复核")
    validations.append(f"✅ 初筛过滤：{'、'.join(f'{k} {v}' for k, v in dropped.items() if v)}")
    keep.sort(key=lambda r: -prescore(r, cfg))
    stage1_count = len(keep)
    picked = keep[:cfg["max_candidates_analyze"]]
    log(f"[scan] 初筛命中 {stage1_count} 只，深度分析 {len(picked)} 只")

    codes = [r["code"] for r in picked]
    quotes = tencent_quotes(codes)
    audit_emit("scan_rank", {"picked": picked, "not_analyzed": keep[len(picked):],
                              "reason": "max_candidates_analyze", "quotes": quotes})
    validations.append(f"{'✅' if len(quotes) >= len(codes) * 0.9 else '⚠️'} 腾讯行情覆盖 {len(quotes)}/{len(codes)}")

    env["industry_strong"] = {}
    for r in keep:
        ind = (r.get("industry") or "").strip()
        env["industry_strong"][ind] = env["industry_strong"].get(ind, 0) + 1

    results = []
    with ThreadPoolExecutor(max_workers=int(cfg.get("fetch_workers", 4))) as ex:
        futs = {ex.submit(analyze_one, r, env, cfg, clock, quotes): r for r in picked}
        for fut in futs:
            r = futs[fut]
            try:
                c = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"[warn] {r['code']} 分析异常：{e}")
                audit_emit("scan_analysis", {"code": r["code"], "outcome": "exception",
                                              "error_type": type(e).__name__})
                continue
            if c.get("skip"):
                log(f"[warn] {r['code']} {r['name']} 跳过：{'；'.join(c.get('errors', []))}")
                audit_emit("scan_analysis", {"code": r["code"], "outcome": "skipped", "result": c})
                continue
            q = quotes.get(c["code"]) or {}
            c["price"] = q.get("price") or r["price"]
            c["pct"] = (q["price"] / q["prev_close"] - 1) * 100 if q.get("price") and q.get("prev_close") else r["pct"]
            c["amount"] = q.get("amount") or r.get("amount")
            c["turnover"] = q.get("turnover") if q.get("turnover") is not None else r.get("turnover")
            c["vol_ratio"] = q.get("vol_ratio") if q.get("vol_ratio") is not None else r.get("vol_ratio")
            c["limit_up"] = q.get("limit_up")
            c["prev_close"] = q.get("prev_close") or r.get("prev_close")
            c["quote_src"] = "腾讯" if q.get("price") else "东财列表"
            c["risks"] = build_risks(c, cfg, env)
            c["scenarios"] = scenarios_text(c["plan"], c["m10"], {"price": c["price"]})
            if not c.get("tradable"):
                c["primary_fail"] = (c.get("hard_fail") or ["条件不足"])[0]
            audit_emit("scan_analysis", {"code": r["code"], "outcome": "analyzed", "result": c})
            results.append(c)

    results.sort(key=lambda x: -x["total"])
    tradable = [c for c in results if c.get("tradable")]
    watch = [c for c in results if not c.get("tradable")]

    # 校验汇总
    qs = [c.get("quote_status") for c in results]
    cs = [c.get("cross_status") for c in results]
    bs = [c.get("bars_status") for c in results]
    ds = [c.get("daily_status") for c in results]
    validations.append(f"{'✅' if '❌' not in qs else '❌'} 行情自洽性：✅{qs.count('✅')} ⚠️{qs.count('⚠️')} ❌{qs.count('❌')}")
    validations.append(f"{'✅' if '❌' not in cs else '⚠️'} 双源交叉（东财×腾讯）：✅{cs.count('✅')} ⚠️{cs.count('⚠️')}")
    validations.append(f"{'✅' if '❌' not in bs else '⚠️'} 5分钟K线完整性：✅{bs.count('✅')} ⚠️{bs.count('⚠️')} ❌{bs.count('❌')}")
    sets = {}
    for c in results:
        k = c.get("bars5_src") or "缺失"
        sets[k] = sets.get(k, 0) + 1
    if sets:
        tag = "✅" if sets.get("东财", 0) and "缺失" not in sets else "⚠️"
        validations.append(f"{tag} 5分钟K线数据源：" + "、".join(f"{k} {v} 只" for k, v in sets.items())
                           + ("" if sets.get("东财", 0) else "（东财不可用，已回退新浪备用源）"))
    validations.append(f"{'✅' if '❌' not in ds else '❌'} 日K历史：✅{ds.count('✅')} ⚠️{ds.count('⚠️')} ❌{ds.count('❌')}")
    critical = ("❌" in ds) or (len([x for x in qs if x == "❌"]) > len(qs) * 0.2)
    notes = []
    if hhmm(clock) > "1500":
        notes.append("⚠️ 本报告为**盘后复算**：报告中的「14:30/14:50 状态」实为收盘定格数据，"
                     "仅用于复核规则与数据链路，不能作为实盘尾盘决策依据。")
    if critical:
        notes.append("❌ **数据校验未通过（关键项失败）**：本轮不出交易名单，请勿采信排序。")
        tradable = []

    audit_emit("scan_final", {"tradable": [c["code"] for c in tradable], "critical": bool(critical),
                               "results": results, "validations": validations, "environment": env})
    mode_label = "14:30 尾盘强势股筛选"
    md = build_md_report(clock, cfg, env, results, stage1_count, validations, mode_label, notes)
    os.makedirs(REPORT_DIR, exist_ok=True)
    p = os.path.join(REPORT_DIR, f"尾盘选股_{today}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    log(f"[scan] 报告 {p}")

    # 候选落地（供 14:50 确认使用）
    cand_json = os.path.join(REPORT_DIR, f"候选_{today}.json")
    save_json(cand_json, {
        "date": today, "generated": clock.strftime("%Y-%m-%d %H:%M"),
        "mode": "scan", "stage1": stage1_count, "validations": validations,
        # 影子跟踪用：初筛池全量（14:51 用现价记一份「假设买入」，次日收盘后补卖出价）
        "stage1_pool": [{
            "code": r["code"], "name": r["name"], "price": r["price"], "pct": r["pct"],
            "industry": r.get("industry"), "vol_ratio": r.get("vol_ratio"),
            "turnover": r.get("turnover"), "amount": r.get("amount"),
            "prev_close": r.get("prev_close"),
        } for r in keep],
        "candidates": [{
            "code": c["code"], "name": c["name"], "industry": c.get("industry"),
            "price": c["price"], "pct": c["pct"], "total": c["total"],
            "amount": c.get("amount"), "turnover": c.get("turnover"),
            "vol_ratio": c.get("vol_ratio"), "quote_src": c.get("quote_src"),
            "limit_up": c.get("limit_up"), "prev_close": c.get("prev_close"),
            "tradable": bool(c.get("tradable")), "hard_fail": c.get("hard_fail"),
            "plan": c.get("plan"), "scenarios": c.get("scenarios"),
            "m10": {k: c["m10"].get(k) for k in ("turn_time", "ma5_last", "ma5_dir", "dev",
                                                 "launch_gain", "pullback", "form", "platform_low",
                                                 "vwap", "daily_high")},
            "daily": {k: c["daily"].get(k) for k in ("trend", "first_strong", "pressure",
                                                     "pressure_gap", "support", "five_day_gain",
                                                     "vol_ratio_5d", "break_platform")},
            "scores": c.get("scores"), "risks": c.get("risks"),
        } for c in results],
    })
    push_text = build_push(clock, cfg, env, tradable, watch, validations, mode_label)
    if notes:
        push_text = "\n".join(notes[:1]) + "\n" + push_text
    if cfg["push"] and not args.no_push:
        push_report(push_text)
    log(f"[scan] 完成：交易名单 {len(tradable)} 只，观察 {len(watch)} 只")
    record_run(clock, "scan", ok=not critical, stage1=stage1_count, analyzed=len(results),
               tradable=len(tradable), watch=len(watch), critical=bool(critical),
               validations=validations[:6])
    return 0


def shadow_file(day):
    return os.path.join(REPORT_DIR, f"影子_{day}.json")


def shadow_days():
    if not os.path.isdir(REPORT_DIR):
        return []
    out = []
    for fn in os.listdir(REPORT_DIR):
        m = re.match(r"^影子_(\d{4}-\d{2}-\d{2})\.json$", fn)
        if m:
            out.append(m.group(1))
    return sorted(out)


def settle_shadow(clock, cfg):
    """次日 14:51：给最近一个交易日的影子池补卖出价（≈14:50 卖出，毛收益不计费用）。"""
    today = clock.strftime("%Y-%m-%d")
    days = [d for d in shadow_days() if d < today]
    if not days:
        return None
    for d in days[:-1]:                     # 更早的缺口无法还原次日 14:50 价，标注不计入统计
        old = read_json(shadow_file(d)) or {}
        if old.get("rows") and not old.get("settled") and not old.get("skip_note"):
            old["skip_note"] = "缺次日行情快照，未结算（不计入统计）"
            save_json(shadow_file(d), old)
    day = days[-1]
    sh = read_json(shadow_file(day)) or {}
    if sh.get("settled"):
        return None
    rows = [r for r in (sh.get("rows") or []) if r.get("entry")]
    if not rows:
        return None
    quotes = tencent_quotes([r["code"] for r in rows])
    ok = 0
    for r in rows:
        px = (quotes.get(r["code"]) or {}).get("price")
        if not px:
            continue
        r["exit"], r["exit_date"] = px, today
        r["pct"] = (px / r["entry"] - 1) * 100
        ok += 1
    if ok < max(1, int(len(rows) * 0.8)):
        log(f"[shadow] {day} 影子结算行情覆盖不足（{ok}/{len(rows)}）→ 下轮重试")
        return None
    sh["settled"], sh["settled_at"], sh["exit_day"], sh["n_settled"] = True, clock.strftime("%Y-%m-%d %H:%M"), today, ok
    save_json(shadow_file(day), sh)
    log(f"[shadow] {day} 影子池结算 {ok}/{len(rows)} 条（卖出价取今日 {clock.strftime('%H:%M')}）")
    return ok


def build_shadow(clock, cfg, data, quotes, confirmed, overflow, rejected, nodata):
    """14:51 给当日初筛池每只记一份「假设 14:51 买入」，并标出它在哪一关被淘汰。"""
    today = clock.strftime("%Y-%m-%d")
    pool = data.get("stage1_pool") or []
    fallback = not pool
    if fallback:                            # 老候选文件没有初筛池 → 用深度分析过的候选兜底
        pool = [{"code": c["code"], "name": c.get("name"), "price": c.get("price"),
                 "pct": c.get("pct"), "industry": c.get("industry")}
                for c in (data.get("candidates") or [])]
    pool = pool[:int(cfg.get("shadow_max_pool", 400))]
    cands = {c["code"]: c for c in (data.get("candidates") or [])}
    conf = {r["code"] for r in confirmed}
    over = {r["code"] for r in overflow}
    rej = {r["code"]: r for r in rejected}
    nod = {r["code"] for r in nodata}
    rows = []
    for p in pool:
        code = p.get("code")
        if not code:
            continue
        c = cands.get(code) or {}
        q = quotes.get(code) or {}
        entry = q.get("price") or p.get("price")
        prev = q.get("prev_close") or p.get("prev_close") or c.get("prev_close")
        if code in conf:
            stage, detail = "买入执行", ""
        elif code in over:
            stage, detail = "通过超上限", ""
        elif code in rej:
            stage, detail = "14:51淘汰", _norm_reason(((rej[code].get("reasons") or [""])[0]) or "")
        elif code in nod:
            stage, detail = "14:51数据缺失", ""
        elif c and not c.get("tradable"):
            stage, detail = "硬性未通过", _norm_reason(((c.get("hard_fail") or [""])[0]) or "")
        elif c:
            stage, detail = "14:51淘汰", "未复核"
        else:
            stage, detail = "未深度分析", ""
        rows.append({
            "code": code, "name": p.get("name"), "entry": entry,
            "entry_pct": ((entry / prev - 1) * 100 if entry and prev else p.get("pct")),
            "stage": stage, "detail": detail,
            "score": c.get("total"), "form": (c.get("m10") or {}).get("form"),
            "industry": p.get("industry") or c.get("industry"),
            "vol_ratio": p.get("vol_ratio"),
            "exit": None, "pct": None, "exit_date": None,
        })
    sh = {"date": today, "generated": clock.strftime("%Y-%m-%d %H:%M"),
          "note": "影子池：假设 14:51 买入、次日 14:51（≈14:50）卖出，毛收益不计费用；"
                  "仅作前向对照基准，不参与交易与持仓",
          "source": "候选兜底" if fallback else "初筛池",
          "entry_time": clock.strftime("%H:%M"), "settled": False, "rows": rows}
    save_json(shadow_file(today), sh)
    from collections import Counter
    cnt = Counter(r["stage"] for r in rows)
    log(f"[shadow] {today} 影子池 {len(rows)} 条（{'、'.join(f'{k} {v}' for k, v in cnt.items())}）")
    return len(rows)


KSNAP_RE = re.compile(r"^K线快照_(\d{4}-\d{2}-\d{2})\.json$")
KSNAP_DAYS = 3          # 建仓后 T+1 / T+2 / T+3 三个交易日的 10 分钟K线


def ksnap_file(day):
    return os.path.join(REPORT_DIR, f"K线快照_{day}.json")


def _kline_entries(day):
    """某建仓日的全部标的：实际交易（含在持）+ 影子池，去重，交易优先。"""
    out = {}
    pos = load_positions()
    for t in (pos.get("paper", {}).get("trades") or []) + (pos.get("positions") or []):
        if t.get("entry_date") == day and t.get("code"):
            out[t["code"]] = {"name": t.get("name"), "entry": t.get("entry"), "kind": "trade"}
    sh = read_json(shadow_file(day)) or {}
    for r in sh.get("rows") or []:
        code = r.get("code")
        if code and r.get("entry") and code not in out:
            out[code] = {"name": r.get("name"), "entry": r.get("entry"), "kind": "shadow"}
    return out


def collect_kline_snapshots(clock, cfg):
    """给每个建仓日 D 记录其后 T+1/T+2/T+3 三个交易日的 10 分钟K线（交易 + 影子池都记）。

    一次性抓取：5 分钟K线单次可取约 8 个交易日，等三个交易日都收完（默认 D 后 4 个自然日）
    再抓一次，切出三天，避免每天重复请求。
    """
    if not cfg.get("kline_snapshots", True):
        return 0
    today = clock.strftime("%Y-%m-%d")
    days = set()
    pos = load_positions()
    for t in (pos.get("paper", {}).get("trades") or []) + (pos.get("positions") or []):
        if t.get("entry_date"):
            days.add(t["entry_date"])
    days.update(shadow_days())
    min_gap = int(cfg.get("kline_snapshot_min_gap_days", 4))
    max_age = int(cfg.get("kline_snapshot_max_age_days", 12))
    max_try = int(cfg.get("kline_snapshot_max_attempts", 3))
    n_done = 0
    for day in sorted(days):
        if day >= today:
            continue
        try:
            gap = (clock.date() - datetime.strptime(day, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        p = ksnap_file(day)
        snap = read_json(p) or {}
        if snap.get("complete"):
            continue
        if gap > max_age:                     # 5分钟K线窗口覆盖不到这么久，无法补
            snap.update({"date": day, "complete": True, "target_days": [],
                         "note": "建仓日距今超过 5 分钟K线可取窗口（约 8 个交易日），无法补记",
                         "rows": snap.get("rows") or {}})
            save_json(p, snap)
            continue
        if gap < min_gap:
            continue
        entries = _kline_entries(day)
        if not entries:
            continue
        rows = snap.get("rows") or {}
        codes = [c for c in entries if not (rows.get(c) or {}).get("bars")]
        if not codes:
            snap["complete"] = True
            save_json(p, snap)
            continue
        log(f"[ksnap] {day} 抓取 {len(codes)} 只的 10 分钟K线（T+{KSNAP_DAYS}）")

        def job(code):
            bars5, src, _st, errs, _notes = fetch_5m(code, clock, today, lmt=400)
            return code, (build_10m(bars5, clock) if bars5 else []), (src or ""), errs

        results = {}
        try:
            with ThreadPoolExecutor(max_workers=int(cfg.get("fetch_workers", 4))) as ex:
                for code, bars10, src, errs in ex.map(job, codes):
                    results[code] = (bars10, src, errs)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] K线快照抓取异常：{brief_err(e)}")
            continue
        alldates = sorted({b["t"][:10] for bars10, _s, _e in results.values()
                           for b in bars10 if b["t"][:10] > day})
        targets = alldates[:KSNAP_DAYS]
        for code, (bars10, src, errs) in results.items():
            per = {}
            for b in bars10:
                d = b["t"][:10]
                if d <= day:
                    continue
                per.setdefault(d, []).append([b["t"][11:], round(b["o"], 3), round(b["h"], 3),
                                              round(b["l"], 3), round(b["c"], 3), round(b.get("v", 0))])
            keep = {d: per[d] for d in targets if d in per}
            rows[code] = {"name": entries[code]["name"], "entry": entries[code]["entry"],
                          "kind": entries[code]["kind"], "src": src or "缺失",
                          "bars": keep, "missing": [d for d in targets if d not in keep],
                          "err": (errs[0] if errs and not keep else None)}
        attempts = int(snap.get("attempts", 0)) + 1
        ok_rows = [r for r in rows.values() if (r or {}).get("src") != "缺失"]
        complete = bool(len(targets) >= KSNAP_DAYS and ok_rows and
                        all(len((r or {}).get("bars") or {}) >= KSNAP_DAYS for r in ok_rows))
        snap = {"date": day, "generated": clock.strftime("%Y-%m-%d %H:%M"),
                "target_days": targets, "attempts": attempts, "complete": complete,
                "rows": rows,
                "note": "每笔建仓（实际交易 + 影子池）记录 T+1/T+2/T+3 三个交易日的 10 分钟K线："
                        "bar = [结束时间, 开, 高, 低, 收, 量(手)]，只含已完成 bar，用于复盘卖出时点"}
        if not complete and attempts >= max_try:
            snap["complete"] = True
            snap["note"] += "（已达重试上限，按现有数据定稿）"
        save_json(p, snap, compact=True)
        n_done += 1
        log(f"[ksnap] {day} 完成：{len(rows)} 只，目标日 {targets}，"
            f"覆盖 {len(ok_rows)}/{len(rows)}（attempts {attempts}）")
    return n_done


def mode_pm(clock, cfg, args):
    """14:50：影子结算 + 持仓退出处理 + 尾盘确认（复核 14:30 候选）→ 买入清单。"""
    today = clock.strftime("%Y-%m-%d")
    log(f"[pm] 开始 {clock.strftime('%Y-%m-%d %H:%M')}")
    if cfg.get("shadow_track", True):
        try:
            settle_shadow(clock, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 影子结算失败：{e}")
    cand_json = os.path.join(REPORT_DIR, f"候选_{today}.json")
    data = read_json(cand_json)
    # 1) 持仓退出（若有）；有候选文件时清单由本函数末尾统一渲染，避免重复
    _, exit_text = (mode_exit(clock, cfg, args, with_list=not data)
                    if load_positions().get("positions") else (0, None))
    # 2) 尾盘确认
    if not data:
        log("[pm] 当日无 14:30 候选文件，跳过买入确认")
        if exit_text and cfg["push"] and not args.no_push:
            push_report(exit_text)
        return 0
    cands = [c for c in data.get("candidates", []) if c.get("tradable")]
    if not cands:
        log("[pm] 14:30 阶段无通过硬性条件的候选 → 今日无交易信号")
    codes = [c["code"] for c in cands]
    quotes = tencent_quotes(codes)
    try:
        env = em_market_env()
    except Exception:  # noqa: BLE001
        env = {}
    audit_emit("pm_input", {"candidates": data, "quotes": quotes, "environment": env})
    confirmed, rejected, nodata = [], [], []
    src_used, src_notes = {}, []
    for c in cands:
        q = quotes.get(c["code"]) or {}
        price = q.get("price") or c["price"]
        prev = q.get("prev_close") or c.get("prev_close")
        pctg = (price / prev - 1) * 100 if price and prev else c.get("pct")
        reasons = []
        m10 = {}
        data_ok = True
        # 10分钟数据：东财优先 → 新浪回退；两源都没有时明确记为「数据缺失」，绝不当作「转弱」淘汰
        bars5, src5, st5, err5, note5 = fetch_5m(c["code"], clock, today, lmt=420)
        src_used[src5 or "缺失"] = src_used.get(src5 or "缺失", 0) + 1
        for n in note5:
            if n not in src_notes:
                src_notes.append(n)
        if not bars5:
            data_ok = False
            reasons.append(f"10分钟K线数据缺失（{'；'.join(err5[:2])}）→ 尾盘强度无法判定")
        else:
            bars10 = build_10m(bars5, clock)
            period = int(cfg["ma10m_period"])
            closes = [b["c"] for b in bars10]
            ma5 = ma(closes, period)
            today_idx = [i for i, b in enumerate(bars10) if b["t"][:10] == today]
            last = len(bars10) - 1
            today_bars = [bars10[i] for i in today_idx]
            amt = sum(b.get("amt", 0) for b in today_bars)
            vol = sum(b["v"] for b in today_bars)
            vwap = (amt / (vol * 100)) if amt and vol else None
            dev = pct(price, ma5) if ma5 else None
            highs = [b["h"] for b in bars5 if b["t"][:10] == today and b["t"] <= clock.strftime("%Y-%m-%d %H:%M")]
            hi = max(highs + [price]) if highs else price
            m10 = {"ma5_last": ma5, "vwap": vwap, "dev": dev, "daily_high": hi,
                   "dist_high": pct(price, hi), "src": src5,
                   "ma5_dir": _dir_of(ma_series(closes, period), last) if ma5 else None}
            if ma5 is None and vwap is None:
                data_ok = False
                reasons.append(f"10分钟K线不足（{len(bars10)} 根）→ 尾盘强度无法判定")
        if data_ok:
            if not (cfg["pct_low"] <= (pctg or 0) <= cfg["pct_high"] + 1.0):
                reasons.append(f"尾盘涨幅 {pctg:.2f}% 已偏离初筛区间")
            if m10.get("ma5_last") and price < m10["ma5_last"]:
                reasons.append(f"尾盘跌破 10分钟 MA5（{round(m10['ma5_last'], 2)}）")
            if m10.get("dev") is not None and m10["dev"] > cfg["max_dev_from_ma"]:
                reasons.append(f"尾盘乖离 MA5 {m10['dev']:.2f}%（追高风险）")
            if m10.get("vwap") and price < m10["vwap"]:
                reasons.append(f"尾盘低于当日均价（{round(m10['vwap'], 2)}）")
        plan = c.get("plan") or {}
        stop = plan.get("stop")
        if stop and price <= stop:
            reasons.append(f"已跌破计划止损 {stop}")
        rec = {"code": c["code"], "name": c["name"], "scan_price": c["price"], "price": price,
               "pct": pctg, "m10": m10, "plan": plan, "reasons": reasons, "data_ok": data_ok,
               "score": c.get("total"), "scenarios": c.get("scenarios"),
               "industry": c.get("industry"),
               "form": (c.get("m10") or {}).get("form"),
               "turn_time": (c.get("m10") or {}).get("turn_time"),
               "launch_gain": (c.get("m10") or {}).get("launch_gain"),
               "scan_pct": c.get("pct"), "scan_vol_ratio": c.get("vol_ratio"),
               "daily": c.get("daily")}
        audit_emit("pm_evaluation", {"code": c["code"], "result": rec,
                                      "outcome": "passed" if data_ok and not reasons else "rejected" if data_ok else "missing_data"})
        (confirmed if (data_ok and not reasons) else (rejected if data_ok else nodata)).append(rec)
    confirmed.sort(key=lambda x: -(x.get("score") or 0))
    limit = int(cfg.get("max_buy_list", 5))
    overflow = confirmed[limit:]
    confirmed = confirmed[:limit]
    audit_emit("pm_final", {"confirmed": confirmed, "rejected": rejected, "nodata": nodata,
                             "overflow": overflow, "overflow_reason": "max_buy_list"})
    if overflow:
        log(f"[pm] 通过条件超过上限 {limit} 只，按评分截断 {len(overflow)} 只")

    # 影子跟踪：初筛池全量记一份「假设 14:51 买入」，次日 pm 补卖出价（扩大前向验证样本）
    n_shadow = 0
    if cfg.get("shadow_track", True):
        try:
            pool_codes = [p["code"] for p in (data.get("stage1_pool") or [])]
            sq = dict(quotes)
            if pool_codes:
                pq = tencent_quotes(pool_codes)
                sq.update({k: v for k, v in pq.items() if v})
            n_shadow = build_shadow(clock, cfg, data, sq, confirmed, overflow, rejected, nodata)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 影子池记录失败：{e}")

    L = [f"**🎯 短线策略 · 14:50 尾盘确认 · {clock.strftime('%m-%d %H:%M')}**", ""]
    if env.get("indices"):
        sh = next((i for i in env["indices"] if i["code"] == "000001"), None)
        cyb = next((i for i in env["indices"] if i["code"] == "399006"), None)
        seg = []
        if sh:
            seg.append(f"上证 {sh['price']}（{fmt_pct(sh['pct'])}）")
        if cyb:
            seg.append(f"创业板 {fmt_pct(cyb['pct'])}")
        L.append("　".join(seg))
        L.append("")
    if confirmed:
        L.append(f"**✅ 尾盘仍满足条件，可进入买入执行（{len(confirmed)} 只）**")
        L.append("")
        for r in confirmed:
            p = r["plan"]
            L.append(f"**{r['code']} {r['name']}**　现价 {r['price']}（{fmt_pct(r['pct'])}）　评分 {r['score']}")
            L.append(f"　买入区间 {p.get('entry_zone', ['—'])[0]}–{p.get('entry_zone', ['—', '—'])[1]}｜"
                     f"止损 {p.get('stop')}（-{p.get('stop_pct')}%）｜目标 {p.get('target')}｜盈亏比 {p.get('rr')}")
            L.append(f"　10min MA5 {round(r['m10']['ma5_last'], 2) if r['m10'].get('ma5_last') else '—'}"
                     f"（方向 {r['m10'].get('ma5_dir')}，偏离 {fmt_pct(r['m10'].get('dev'))}）｜"
                     f"距当日高点 {fmt_pct(r['m10'].get('dist_high'))}")
            L.append("")
        L.append("<font color=\"comment\">执行要求：买入价不高于区间上限；尾盘急拉或量能掉队则放弃；"
                 "单笔风险按计划止损计算，不因排名靠前直接买入。</font>")
    else:
        if nodata:
            L.append(f"**今日无交易信号**（{len(nodata)} 只候选因 5 分钟K线数据缺失**未能复核**，"
                     "不能视为已转弱；数据恢复前不追买）")
        else:
            L.append("**今日无交易信号**（14:50 复核后没有候选仍满足尾盘强势条件）")
        L.append("")
    if nodata:
        L.append(f"**⚠️ 数据缺失，无法判定（{len(nodata)} 只）**")
        for r in nodata[:8]:
            L.append(f"- {r['code']} {r['name']}：{'；'.join(r['reasons'])}")
        L.append("")
    if rejected:
        L.append(f"**❌ 已在尾盘转弱/淘汰（{len(rejected)} 只）**")
        for r in rejected[:8]:
            L.append(f"- {r['code']} {r['name']}：{'；'.join(r['reasons'])}")
        L.append("")
    if "新浪" in src_used:
        for n in (src_notes or ["5分钟K线取自新浪备用源"]):
            L.append(f"<font color=\"comment\">数据源：{n}</font>")
        L.append("")
    if overflow:
        L.append(f"<font color=\"comment\">另有 {len(overflow)} 只同时满足条件但超出单次买入上限 "
                 f"（{limit} 只），已放弃以避免同板块风险过度集中。</font>")
        L.append("")
    if exit_text:
        L.append("──── 持仓处理 ────")
        L.append(exit_text)
    # 3) 加入已购买清单（14:51 确认价记为买入；你已确认买入与全部卖出都会执行）
    pos = load_positions()
    prune_closed(pos)
    if any(p["code"] not in quotes for p in pos.get("positions", [])):
        quotes.update(tencent_quotes([p["code"] for p in pos["positions"]]))
    added = []
    if confirmed and cfg["paper_track"] and not args.no_hold:
        eq = cfg["account_equity"]
        for r in confirmed:
            if any(p.get("code") == r["code"] and p.get("status") == "holding" for p in pos["positions"]):
                continue
            plan = r["plan"] or {}
            shares = plan.get("qty") or 0
            tt = (r.get("turn_time") or "")
            slot = "上午" if tt[-5:-3] and int(tt[-5:-3]) < 12 else ("午后" if tt else None)
            p = {"code": r["code"], "name": r["name"], "entry": r["price"],
                 "entry_date": today, "entry_time": clock.strftime("%H:%M"),
                 "shares": shares, "source": "执行",
                 "plan": plan, "scenarios": r.get("scenarios"), "status": "holding",
                 "industry": r.get("industry"),
                 "attrs": {"score": r.get("score"), "form": r.get("form"),
                           "turn_time": r.get("turn_time"), "turn_slot": slot,
                           "launch_gain": r.get("launch_gain"),
                           "entry_pct": r.get("pct"), "vol_ratio": r.get("scan_vol_ratio"),
                           "rr": plan.get("rr"), "stop_pct": plan.get("stop_pct"),
                           "industry": r.get("industry"),
                           "pressure_gap": (r.get("daily") or {}).get("pressure_gap"),
                           "vol_ratio_5d": (r.get("daily") or {}).get("vol_ratio_5d"),
                           "trend": (r.get("daily") or {}).get("trend")},
                 "prev_close_at_entry": (quotes.get(r["code"]) or {}).get("prev_close"),
                 "max_exit": f"次日 {cfg['force_exit_time'][:2]}:{cfg['force_exit_time'][2:]}",
                 "note": "14:51 尾盘确认后按现价加入已购买清单（视为已执行买入）"}
            pos["positions"].append(p)
            added.append(f"{r['code']} {r['name']}")
        save_positions(pos)
    if added:
        L.append(f"<font color=\"comment\">已加入已购买清单：{'、'.join(added)}。"
                 f"次日 9:35 / 14:51 推送卖出建议；建议「全部卖出」执行后自动移出清单，"
                 f"减仓/减半不改变清单。</font>")
    L.append("")
    L.extend(build_position_list(pos, quotes))
    write_position_list(pos, clock, quotes)
    if n_shadow:
        L.append("")
        L.append(f"<font color=\"comment\">影子跟踪：今日初筛池 {n_shadow} 条已记录（假设 14:51 买入、次日 14:51 结算），"
                 "只用于周/月复盘对照，不参与交易与持仓。</font>")
    md = "\n".join(L)
    with open(os.path.join(REPORT_DIR, f"买入清单_{today}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    save_json(os.path.join(REPORT_DIR, f"买入清单_{today}.json"), {
        "date": today, "generated": clock.strftime("%Y-%m-%d %H:%M"),
        "scan_tradable": len(cands), "confirmed": confirmed, "rejected": rejected,
        "nodata": [{"code": x["code"], "name": x["name"], "reasons": x["reasons"]} for x in nodata],
        "kline_src": src_used,
        "overflow": [{"code": x["code"], "name": x["name"], "score": x.get("score")} for x in overflow],
    })
    record_run(clock, "pm", ok=True, confirmed=len(confirmed), rejected=len(rejected),
               nodata=len(nodata), kline_src=src_used,
               overflow=len(overflow), exit_section=bool(exit_text))
    if cfg["push"] and not args.no_push:
        push_report(md)
    log(f"[pm] 完成：确认 {len(confirmed)} 只，淘汰 {len(rejected)} 只，数据缺失无法判定 {len(nodata)} 只")
    return 0


def mode_status(clock, cfg):
    pos = load_positions()
    print(f"已购买清单：{len([p for p in pos.get('positions', []) if p.get('status') == 'holding'])} 只在持")
    for p in pos.get("positions", []):
        print(f"- {p['code']} {p['name']}　买入 {p['entry']}（{p.get('entry_date')} {p.get('entry_time')}）"
              f"　状态 {p.get('status')}　最近建议 {p.get('last_advice') or '—'}")
    hist = pos.get("history", [])
    if hist:
        print(f"（已卖出归档 {len(hist)} 条）")
    print(f"\n策略统计：{paper_stats(pos) or '暂无平仓样本'}")
    trades = pos.get("paper", {}).get("trades", [])
    for t in trades[-10:]:
        print(f"  · {t['code']} {t['name']} {t['entry']} → {t['exit']}　{t['pct']:+.2f}%")
    return 0


def main():
    ap = argparse.ArgumentParser(description="A股尾盘短线策略引擎")
    ap.add_argument("--mode", choices=["scan", "pm", "exit", "status", "auto", "week", "month", "ksnap"],
                    default="auto")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-hold", action="store_true", help="不登记模拟持仓")
    ap.add_argument("--only-if-changed", action="store_true",
                    help="持仓监控的非关键时点：仅当卖出建议发生实质变化时才推送")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    ap.add_argument("--force-now", default=None, help='覆盖时钟，如 "2026-10-13 14:31"')
    ap.add_argument("--test-tag", default="", help="在推送内容前加【联调测试】标记（用于验证推送链路）")
    ap.add_argument("--brief-summary", action="store_true", help="周/月总结只推摘要版（覆盖 config 的 summary_push_full）")
    ap.add_argument("--hold", nargs="+", help="手工登记持仓：CODE PRICE [SHARES]")
    ap.add_argument("--close", nargs="+", help="平掉持仓：CODE [PRICE]")
    args = ap.parse_args()

    if args.dry_run:
        args.no_push = True
    clock = now_clock(args.force_now and datetime.strptime(args.force_now, "%Y-%m-%d %H:%M")
                      or None)
    cfg = load_cfg()
    if args.no_push:
        cfg["push"] = False
    audit_emit("config", {"effective": cfg, "clock": clock.isoformat(), "mode": args.mode})
    os.makedirs(REPORT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    if args.hold:
        code = args.hold[0]
        price = float(args.hold[1])
        shares = float(args.hold[2]) if len(args.hold) > 2 else 0
        pos = load_positions()
        cand = read_json(os.path.join(REPORT_DIR, f"候选_{clock.strftime('%Y-%m-%d')}.json")) or {}
        plan = next((c.get("plan") for c in cand.get("candidates", []) if c["code"] == code), {})
        nm = (tencent_quotes([code]).get(code) or {}).get("name") or code
        pos["positions"].append({"code": code, "name": nm, "entry": price,
                                 "entry_date": clock.strftime("%Y-%m-%d"), "entry_time": clock.strftime("%H:%M"),
                                 "shares": shares, "source": "手动", "plan": plan or {},
                                 "status": "holding", "note": "手工登记"})
        save_positions(pos)
        write_position_list(pos, clock)
        print(f"已加入已购买清单：{code} {nm} @ {price}")
        return 0
    if args.close:
        code = args.close[0]
        price = float(args.close[1]) if len(args.close) > 1 else None
        pos = load_positions()
        n = 0
        for p in pos["positions"]:
            if p["code"] == code and p.get("status") == "holding":
                p["status"] = "closed"
                if price:
                    res = net_pnl(p["entry"], price, p.get("shares") or 100, cfg)
                    p["close"] = {"price": price, "date": clock.strftime("%Y-%m-%d"), **res, "reason": "手动平仓"}
                    pos.setdefault("paper", {}).setdefault("trades", []).append(
                        {"code": p["code"], "name": p["name"], "entry": p["entry"], "exit": price,
                         "entry_date": p.get("entry_date"), "exit_date": clock.strftime("%Y-%m-%d"),
                         "exit_time": clock.strftime("%H:%M"), "reason": "手动平仓",
                         "reason_cat": "手动平仓", "attrs": p.get("attrs") or {}, **res})
                n += 1
        prune_closed(pos)
        save_positions(pos)
        write_position_list(pos, clock)
        print(f"已平仓并移出清单 {n} 条：{code}")
        return 0
    if args.mode == "status":
        return mode_status(clock, cfg)
    if args.mode == "ksnap":
        n = collect_kline_snapshots(clock, cfg)
        log(f"[ksnap] 本次完成 {n} 个建仓日的 10 分钟K线快照")
        record_run(clock, "ksnap", ok=True, snapshots=n, pushed=False)
        return 0
    if args.mode == "auto":
        t = hhmm(clock)
        if t <= "1440":
            args.mode = "exit"
        elif t >= "1445":
            args.mode = "pm"
        else:
            args.mode = "scan"
    print(f"[i] 模式 {args.mode}　时钟 {clock.strftime('%Y-%m-%d %H:%M')}　数据目录 {BASE}")
    if args.mode == "scan":
        return mode_scan(clock, cfg, args)
    if args.mode == "pm":
        return mode_pm(clock, cfg, args)
    if args.mode in ("week", "month"):
        return mode_summary(clock, cfg, args, args.mode)
    if args.mode == "exit":
        rc, text = mode_exit(clock, cfg, args)
        if text:
            if cfg["push"]:
                push_report(text, tag=args.test_tag)
            else:
                log(text)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
