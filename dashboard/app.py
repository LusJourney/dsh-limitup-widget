#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股涨停直播看板 —— 单文件 Flask 后端（akshare 免费数据源，无需任何 key）。

运行：``python app.py``            -> http://127.0.0.1:8000
     ``PORT=8001 python app.py``   -> 端口覆盖
     ``LIMITUP_DISABLE_BG=1 ...``  -> 不启动后台刷新/概念线程（QA 单测用）
     ``LIMITUP_FAKE_FAIL=1 ...``   -> 每次 akshare 拉取都抛异常（QA 验证 stale）

设计要点（详见 CONTRACT.md）：
* 所有 akshare 调用统一走 :func:`ak_call`：全局 >=1s 节流（跨线程）、超时保护、异常包装。
* 列名按中文关键词防御式映射（见 ``_LIMIT_UP_KEYS`` 等），未知列每函数每进程只 warning 一次。
* 交易日历用 ``ak.tool_trade_date_hist_sina()``，进程内 + ``data/trade_calendar.json`` 文件缓存。
* 盘前 / 非交易日自动回退到"最近一个已开盘交易日"，``trade_date != requested_date`` 时
  ``is_fallback=true``（实测东方财富接口在盘前传当日日期会静默返回上一交易日数据，
  因此必须自己先把"有效交易日"算出来再取数，否则 trade_date 会标错 —— 见 resolve_dates）。
* 概念映射（慢）在后台线程逐板块抓成分，每天一份 JSON 缓存，页面先用"所属行业"兜底。
* CSV 每次成功刷新追加写入 ``data/YYYYMMDD.csv``（trade_date，utf-8-sig，csv 模块转义）。
* 失败保留上一份好数据并置 ``stale.is_stale=true``，绝不编造数据。
"""

from __future__ import annotations

import copy
import csv
import hmac
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径 / 时区 / 配置
# ---------------------------------------------------------------------------

try:  # 北京时间（系统 tzdata 缺失时退化为固定 +08:00）
    from zoneinfo import ZoneInfo

    BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 兜底
    BJ_TZ = timezone(timedelta(hours=8))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PAGE_FILE = BASE_DIR / "page.html"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


DISABLE_BG = _env_flag("LIMITUP_DISABLE_BG")   # QA 单测：不启动后台刷新/概念线程
FAKE_FAIL = _env_flag("LIMITUP_FAKE_FAIL")     # QA 验证 stale：每次拉取都抛异常


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


PORT = _env_int("PORT", 8000)
HOST = os.environ.get("HOST", "") or "127.0.0.1"

# 公网加固（全部可选，默认全关，不影响本地使用）
# 完全公开只读部署时，建议至少设置 LIMITUP_RATE_LIMIT 防止被刷。
ACCESS_CODE = os.environ.get("LIMITUP_ACCESS_CODE", "").strip()   # 非空则要求访问码
RATE_LIMIT_PER_MIN = _env_int("LIMITUP_RATE_LIMIT", 0)            # 每 IP 每分钟请求上限，0=不限
TRUST_PROXY = _env_flag("LIMITUP_TRUST_PROXY")                    # 反代后从 X-Forwarded-For 取真实 IP
REFRESH_COOLDOWN_SEC = _env_int("LIMITUP_REFRESH_COOLDOWN", 10)   # /api/refresh 每 IP 冷却秒数

# 全局常量
MIN_AK_INTERVAL = 1.02          # 两次 akshare 调用之间的最小间隔（秒，跨线程）
REFRESH_INTERVAL_SEC = 60       # 盘中刷新间隔
STALE_AFTER_SEC = 180           # 交易时段内距上次成功 >180s 也算 stale
INITIAL_WAIT_SEC = 20.0         # 首个 /api/snapshot 最多等待初始加载完成的时间
AK_TIMEOUT_POOL = 30.0          # 涨停/炸板/跌停/昨日涨停池单次调用超时
AK_TIMEOUT_CALENDAR = 25.0      # 交易日历
AK_TIMEOUT_CONCEPT_LIST = 180.0  # 概念列表（东财分页 + 逐页 sleep，实测约 40~60s）
AK_TIMEOUT_CONCEPT_CONS = 30.0  # 单个概念成分
AK_TIMEOUT_SPOT = 420.0         # 全市场实时行情（东财分页 59 页、每页 4~7s，实测约 5~6 分钟）
SPOT_MIN_INTERVAL = 600         # 全市场行情缓存的最小刷新间隔（秒）
AK_TIMEOUT_WATCH_QUOTE = 15.0   # 单只自选股快照（stock_bid_ask_em，单次请求）
WATCH_QUOTE_TTL = 45            # 单只自选快照缓存有效期（秒）
CSV_HEADER = [
    "timestamp", "code", "name", "status", "streak",
    "first_seal", "seal_amount", "break_times", "concepts",
]

LOG = logging.getLogger("limitup")


def _setup_logging() -> None:
    """INFO 级，stderr 带时间；重复调用只生效一次（方便 QA 自行配置 logging）。"""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_setup_logging()

START_TS = time.time()

# ---------------------------------------------------------------------------
# 时间基础工具
# ---------------------------------------------------------------------------


def now_beijing() -> datetime:
    """当前北京时间（带 tzinfo）。"""
    return datetime.now(BJ_TZ)


def _as_bj(value=None) -> datetime:
    """把 date / naive datetime / aware datetime 归一化成带北京时区的 datetime。"""
    if value is None:
        return now_beijing()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=BJ_TZ)
        return value.astimezone(BJ_TZ)
    if isinstance(value, date):
        return datetime.combine(value, dtime(0, 0), tzinfo=BJ_TZ)
    return now_beijing()


def fmt_iso(value) -> str | None:
    """datetime -> '2026-11-05T00:30:12+08:00'（契约里的时间格式）。"""
    if value is None:
        return None
    try:
        return _as_bj(value).isoformat(timespec="seconds")
    except Exception:
        return None


def fmt_ymd(value) -> str | None:
    """date/datetime -> 'YYYYMMDD'。"""
    if value is None:
        return None
    d = _to_date(value)
    return d.strftime("%Y%m%d") if d else None


def _to_date(value) -> date | None:
    """宽松转 date：支持 date / datetime / 'YYYY-MM-DD' / 'YYYYMMDD' / pandas Timestamp。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = safe_str(value)
    if not s:
        return None
    s = s.strip()
    for fmt, ln in (("%Y-%m-%d", 10), ("%Y%m%d", 8), ("%Y/%m/%d", 10)):
        try:
            return datetime.strptime(s[:ln], fmt).date()
        except (TypeError, ValueError):
            continue
    try:  # pandas.Timestamp / numpy.datetime64
        return value.date()
    except Exception:
        return None


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _infer_is_trading_day(day: date, day_set: set) -> bool:
    """判断某天是否交易日；若交易日历未覆盖到该天（日历滞后），用"工作日"兜底推断。"""
    if day in day_set:
        return True
    if day_set:
        try:
            latest = max(day_set)
        except (TypeError, ValueError):
            latest = None
        if latest is not None and day > latest:
            return _is_weekday(day)
    return False


def _weekday_fallback_dates(today: date, back_days: int = 400, forward_days: int = 10):
    """交易日历不可用时的退化："工作日=交易日"。"""
    out = []
    start = today - timedelta(days=back_days)
    d = start
    end = today + timedelta(days=forward_days)
    while d <= end:
        if _is_weekday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 数值 / 字符串安全转换（JSON 里绝不允许 NaN / "-" / "" 混入数值字段）
# ---------------------------------------------------------------------------

_NA_TEXT = {"", "-", "--", "---", "nan", "none", "null", "nat", "n/a", "na", "inf", "+inf", "-inf"}

# 兼容中文列里常见的空值写法
_NA_TEXT |= {"无", "—", "－"}


def safe_float(value, default=None):
    """转 float；NaN/Inf/NaT/'-'/'' -> default。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return f if math.isfinite(f) else default
    # numpy 标量 / pandas NA 等
    try:
        item = value.item()  # type: ignore[attr-defined]
    except Exception:
        item = None
    if item is not None and item is not value and not isinstance(item, (str, bytes)):
        return safe_float(item, default)
    s = str(value).strip()
    if s.lower() in _NA_TEXT:
        return default
    s = s.replace(",", "").replace("%", "").replace("￥", "").replace("¥", "").strip()
    if s.lower() in _NA_TEXT:
        return default
    try:
        f = float(s)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default


def safe_int(value, default=None):
    """转 int（四舍五入），失败 -> default。"""
    f = safe_float(value)
    if f is None:
        return default
    try:
        return int(round(f))
    except (TypeError, ValueError, OverflowError):
        return default


def safe_str(value, default=None):
    """转非空字符串；NaN/NaT/'-'/'' -> default。"""
    if value is None:
        return default
    tname = type(value).__name__
    if tname in ("NAType", "NaTType"):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        if hasattr(value, "item") and not isinstance(value, (str, bytes)):
            item = value.item()
            if not isinstance(item, (str, bytes)) and item is not value:
                return safe_str(item, default)
    except Exception:
        pass
    s = str(value).strip()
    if s.lower() in _NA_TEXT:
        return default
    return s or default


def to_yi(value, digits: int = 2):
    """元 -> 亿元（保留 digits 位）；无效 -> None。"""
    f = safe_float(value)
    if f is None:
        return None
    try:
        return round(f / 1e8, digits)
    except (TypeError, ValueError, OverflowError):
        return None


_CODE_RE = re.compile(r"\d{6}")


def normalize_code(value):
    """任意代码写法 -> 6 位数字字符串（'000001'）；无法识别 -> None。"""
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            value = int(value)
    s = str(value).strip().upper()
    if not s or s.lower() in _NA_TEXT:
        return None
    s = s.replace("SH", "").replace("SZ", "").replace("BJ", "")
    m = _CODE_RE.search(s)
    if m:
        return m.group(0)
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return None


_TIME_COLON_RE = re.compile(r"^(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2}))?$")


def format_hms(value):
    """各种"首次封板时间"写法 -> 'HH:MM:SS'：

    ``92500`` / ``"92500"`` / ``"092500"`` / ``"09:25:00"`` / ``datetime.time`` 均可。
    """
    if value is None:
        return None
    tname = type(value).__name__
    if tname in ("NAType", "NaTType"):
        return None
    if isinstance(value, dtime):
        return value.strftime("%H:%M:%S")
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    # 数字形态：HHMMSS 整数
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = safe_float(value)
        if f is None:
            return None
        return _hms_from_int(int(round(f)))
    s = safe_str(value)
    if not s:
        return None
    s = s.strip().replace("：", ":")
    m = _TIME_COLON_RE.match(s)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if hh <= 23 and mm <= 59 and ss <= 59:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return None
    if s.isdigit() and len(s) in (5, 6):
        return _hms_from_int(int(s))
    return None


def _hms_from_int(value: int):
    """HHMMSS 整数 -> 'HH:MM:SS'（0 视为未提供）。"""
    if value is None or value <= 0:
        return None
    if value > 235959:
        return None
    s = f"{value:06d}"
    hh, mm, ss = int(s[0:2]), int(s[2:4]), int(s[4:6])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def sanitize_json(obj):
    """递归把对象清洗成可 ``json.dumps(..., allow_nan=False)`` 的纯 Python 结构。

    - NaN / Inf / NaT / pandas NA -> None
    - numpy 标量 -> Python 标量
    - datetime/date/time -> ISO 字符串
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_json(v) for v in obj]
    if isinstance(obj, datetime):
        return fmt_iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dtime):
        return obj.strftime("%H:%M:%S")
    tname = type(obj).__name__
    if tname in ("NAType", "NaTType"):
        return None
    try:
        item = obj.item()  # numpy / pandas 标量
    except Exception:
        item = None
    if item is not None and item is not obj:
        return sanitize_json(item)
    text = str(obj)
    return None if text.strip().lower() in _NA_TEXT else text


def _short_err(exc, limit: int = 300) -> str:
    """异常 -> 单行短描述（stale.reason / warnings 用）。"""
    try:
        text = str(exc).strip()
    except Exception:
        text = ""
    name = type(exc).__name__
    out = f"{name}: {text}" if text else name
    out = " ".join(out.split())
    return out[:limit]


# ---------------------------------------------------------------------------
# 列名关键词映射（防御式：中文字段名可能变，按"包含"匹配）
# ---------------------------------------------------------------------------

_UNKNOWN_COL_LOGGED: set[str] = set()
_UNKNOWN_COL_LOCK = threading.Lock()


def _norm_key(value) -> str:
    """列名归一化：去空白 / 连接符 / 括号标点，转小写。"""
    return re.sub(r"[\s\-_·（）()\[\]【】:：/、,，]+", "", str(value)).lower()


def pick_col(columns, keywords, required: bool = False, exclude=()):
    """按关键词"包含"匹配返回第一个命中的列名；找不到返回 None / 抛 KeyError。

    :param columns: 可迭代的列名
    :param keywords: 关键词列表（或单个字符串），按优先级排列
    :param required: True 时找不到就抛 :class:`KeyError`（契约：必需列缺失 => 数据源整体失败）
    :param exclude: 排除关键词（命中则跳过该列）
    """
    cols = [c for c in (columns or [])]
    if isinstance(keywords, str):
        keywords = [keywords]
    norm_cols = [(c, _norm_key(c)) for c in cols]
    norm_excl = [_norm_key(x) for x in (exclude or ()) if _norm_key(x)]
    for kw in keywords or ():
        nkw = _norm_key(kw)
        if not nkw:
            continue
        for col, ncol in norm_cols:
            if nkw in ncol and not any(ne in ncol for ne in norm_excl):
                return col
    if required:
        raise KeyError(f"缺少必需列 {list(keywords)}，现有列={cols}")
    return None


def _matched_columns(columns, keymap) -> set:
    """某个映射表能匹配到的全部列名（用于"未识别列"告警）。"""
    matched = set()
    for keywords in keymap.values():
        col = pick_col(columns, keywords)
        if col is not None:
            matched.add(col)
    return matched


def _log_unknown_columns(func_name: str, columns, mapped_columns) -> None:
    """未知列告警：每个函数名每进程只打一次。格式 ``[columns] xxx 未识别列: [...]``。"""
    mapped = set(mapped_columns or ())
    unknown = [str(c) for c in (columns or []) if c not in mapped]
    if not unknown:
        return
    with _UNKNOWN_COL_LOCK:
        if func_name in _UNKNOWN_COL_LOGGED:
            return
        _UNKNOWN_COL_LOGGED.add(func_name)
    LOG.warning("[columns] %s 未识别列: %s", func_name, unknown)


# 涨停池 / 通用股票字段关键词
_LIMIT_UP_KEYS = {
    "code": ["代码", "股票代码", "证券代码", "code", "symbol"],
    "name": ["名称", "股票名称", "证券名称", "简称", "name"],
    "streak": ["连板数", "连板", "streak", "boards"],
    "first_seal": ["首次封板时间", "首次封板", "first_seal", "firstseal"],
    "last_seal": ["最后封板时间", "最后封板", "last_seal", "lastseal"],
    "seal_amount": ["封板资金", "封单资金", "封板金额", "seal_amount"],
    "break_times": ["炸板次数", "开板次数", "break_times"],
    "industry": ["所属行业", "行业", "industry"],
    "turnover": ["换手率", "turnover"],
    "float_cap": ["流通市值", "流通市值元", "float_cap"],
    "pct": ["涨跌幅", "pct", "change_pct"],
    "total_cap": ["总市值", "total_cap"],
    "amount": ["成交额", "amount"],
}

# 炸板池关键词（免费接口**没有** 炸板时间 / 最高涨跌幅 列，按契约 high_pct 可由 涨停价/最新价/涨跌幅 推导）
_BROKEN_KEYS = {
    "code": ["代码", "股票代码", "code"],
    "name": ["名称", "股票名称", "name"],
    "pct": ["涨跌幅", "pct", "change_pct"],
    "high_pct": ["最高涨跌幅", "最高涨幅", "high_pct"],
    "latest": ["最新价", "latest", "price"],
    "limit_up_price": ["涨停价", "limit_up_price"],
    "break_times": ["炸板次数", "break_times"],
    "first_seal": ["首次封板时间", "首次封板", "first_seal"],
    "break_time": ["炸板时间", "炸板时刻", "break_time"],
    "industry": ["所属行业", "行业", "industry"],
    "streak": ["连板数", "连板", "streak"],
    "turnover": ["换手率", "turnover"],
    "amplitude": ["振幅", "amplitude"],
}

# 跌停池关键词
_LIMIT_DOWN_KEYS = {
    "code": ["代码", "股票代码", "code"],
    "name": ["名称", "股票名称", "name"],
    "pct": ["涨跌幅", "pct", "change_pct"],
    "industry": ["所属行业", "行业", "industry"],
    "streak": ["连续跌停", "连板数", "streak"],
}


def _df_columns(df) -> list:
    """DataFrame 列名列表（避免对 pandas Index 做真值判断）。"""
    cols = getattr(df, "columns", None)
    if cols is None:
        return []
    try:
        return [c for c in cols]
    except Exception:
        return []


def _row_columns(row) -> list:
    """从 pandas.Series / dict / 其它 mapping 里取列名列表。"""
    idx = getattr(row, "index", None)
    if idx is not None:
        try:
            return list(idx)
        except Exception:
            pass
    if hasattr(row, "keys"):
        try:
            return list(row.keys())
        except Exception:
            pass
    if isinstance(row, dict):
        return list(row.keys())
    return []


def _cell(row, col):
    if col is None:
        return None
    try:
        return row[col]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 纯函数 #1: 涨停池单行 -> Stock 对象
# ---------------------------------------------------------------------------


def map_limit_up_row(row) -> dict:
    """涨停股池的一行 -> 契约里的 Stock 对象（概念字段留空，后续再由概念映射填充）。

    :param row: pandas.Series 或 dict（键=中文列名）
    :return: dict，字段严格按 CONTRACT §3 的 Stock 对象；缺失值一律 None
    """
    cols = _row_columns(row)
    pick_col(cols, _LIMIT_UP_KEYS["code"], required=True)
    pick_col(cols, _LIMIT_UP_KEYS["name"], required=True)
    _log_unknown_columns("map_limit_up_row", cols, _matched_columns(cols, _LIMIT_UP_KEYS))

    def get(key):
        return _cell(row, pick_col(cols, _LIMIT_UP_KEYS[key]))

    seal_amount = safe_float(get("seal_amount"))
    float_cap = safe_float(get("float_cap"))
    turnover = safe_float(get("turnover"))
    return {
        "code": normalize_code(get("code")),
        "name": safe_str(get("name")),
        "streak": safe_int(get("streak")),
        "first_seal": format_hms(get("first_seal")),
        "last_seal": format_hms(get("last_seal")),
        "seal_amount_yuan": seal_amount,
        "seal_amount_yi": to_yi(seal_amount),
        "break_times": safe_int(get("break_times")),
        "turnover": round(turnover, 2) if turnover is not None else None,
        "float_cap_yuan": float_cap,
        "float_cap_yi": to_yi(float_cap),
        "industry": safe_str(get("industry")),
        "concepts": [],
        "is_watch": False,
    }


def derive_high_pct(current_pct, latest_price, limit_up_price):
    """炸板股"当日最高涨幅 %"的数据推导（免费接口没有 最高涨跌幅 列）。

    炸板股按定义盘中触及过涨停价，故：``prev_close = 最新价 / (1 + 涨跌幅/100)``，
    ``high_pct = (涨停价 / prev_close - 1) * 100``。任一输入缺失/非法则返回 None，
    不硬编码 10.0（主板 10% / 创业板科创板 20% / 北交所 30% / ST 5% 各不相同）。
    """
    pct = safe_float(current_pct)
    price = safe_float(latest_price)
    limit_price = safe_float(limit_up_price)
    if pct is None or price is None or limit_price is None:
        return None
    if pct <= -99.0 or price <= 0 or limit_price <= 0:
        return None
    prev_close = price / (1.0 + pct / 100.0)
    if prev_close <= 0:
        return None
    high_pct = (limit_price / prev_close - 1.0) * 100.0
    if not math.isfinite(high_pct):
        return None
    if high_pct < -1.0 or high_pct > 50.0:  # 明显异常的数据不做展示
        return None
    return round(high_pct, 2)


def map_broken_row(row) -> dict:
    """炸板股池的一行 -> 契约 §3 的 broken 对象。"""
    cols = _row_columns(row)
    pick_col(cols, _BROKEN_KEYS["code"], required=True)
    pick_col(cols, _BROKEN_KEYS["name"], required=True)
    _log_unknown_columns("stock_zt_pool_zbgc_em", cols, _matched_columns(cols, _BROKEN_KEYS))

    def get(key):
        return _cell(row, pick_col(cols, _BROKEN_KEYS[key]))

    high_raw = safe_float(get("high_pct"))
    high_pct = high_raw
    if high_pct is None:
        high_pct = derive_high_pct(get("pct"), get("latest"), get("limit_up_price"))
    pct = safe_float(get("pct"))
    return {
        "code": normalize_code(get("code")),
        "name": safe_str(get("name")),
        "concepts": [],
        "industry": safe_str(get("industry")),
        "high_pct": high_pct,
        "current_pct": round(pct, 2) if pct is not None else None,
        "break_times": safe_int(get("break_times")),
        "first_seal": format_hms(get("first_seal")),
        "break_time": format_hms(get("break_time")),
    }


def map_limit_down_row(row) -> dict:
    """跌停股池的一行 -> 契约 §3 的 limit_down 对象。"""
    cols = _row_columns(row)
    pick_col(cols, _LIMIT_DOWN_KEYS["code"], required=True)
    pick_col(cols, _LIMIT_DOWN_KEYS["name"], required=True)
    _log_unknown_columns("stock_zt_pool_dtgc_em", cols, _matched_columns(cols, _LIMIT_DOWN_KEYS))

    def get(key):
        return _cell(row, pick_col(cols, _LIMIT_DOWN_KEYS[key]))

    pct = safe_float(get("pct"))
    return {
        "code": normalize_code(get("code")),
        "name": safe_str(get("name")),
        "pct": round(pct, 2) if pct is not None else None,
        "concepts": [],
        "industry": safe_str(get("industry")),
    }


# ---------------------------------------------------------------------------
# 纯函数 #2: 交易日 / 市场阶段 / 刷新窗口
# ---------------------------------------------------------------------------


def compute_phase(now_bj=None, is_trading_day=None) -> str:
    """市场阶段：pre_open | auction | trading | lunch | closed | non_trading_day。

    ``is_trading_day=None`` 时按"工作日"判断（纯函数，不做网络/文件 IO）。
    """
    now = _as_bj(now_bj)
    if is_trading_day is None:
        is_trading_day = _is_weekday(now.date())
    if not is_trading_day:
        return "non_trading_day"
    t = now.time()
    if t < dtime(9, 15):
        return "pre_open"
    if t < dtime(9, 25):
        return "auction"
    if t <= dtime(11, 30):
        return "trading"
    if t < dtime(13, 0):
        return "lunch"
    if t <= dtime(15, 0):
        return "trading"
    return "closed"


def should_refresh(now_bj=None, is_trading_day=None, trade_dates=None) -> bool:
    """是否处于主动刷新窗口（09:25–11:30、13:00–15:00，北京时间）。"""
    now = _as_bj(now_bj)
    if is_trading_day is None:
        days = _coerce_trade_dates(trade_dates)
        if days is not None:
            is_trading_day = _infer_is_trading_day(now.date(), set(days))
        else:
            is_trading_day = _is_weekday(now.date())
    if not is_trading_day:
        return False
    t = now.time()
    return (dtime(9, 25) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def _coerce_trade_dates(trade_dates):
    """把各种形态的交易日列表 -> 排序去重的 date 列表（失败返回 None）。"""
    if not trade_dates:
        return None
    out = []
    for v in trade_dates:
        d = _to_date(v)
        if d is not None:
            out.append(d)
    if not out:
        return None
    return sorted(set(out))


def resolve_dates(now_bj=None, trade_dates=None) -> dict:
    """计算 requested_date / 有效交易日 / 回退候选（纯函数，不联网）。

    规则（与 CONTRACT §4.1 + §3 示例一致）：

    * ``requested_date`` = **有效交易日** = 最近一个已经开盘的交易日：今天若是交易日且
      已过 09:25 -> 今天；今天盘前 / 非交易日 -> 之前最近的交易日（契约 §4.1 中
      "当前 <09:25 或非交易日 -> 最近一个已开盘交易日"）。
    * ``calendar_date``（= ``today``）= 当前北京时间的日历日。快照里对外展示的
      ``requested_date`` 用的是它（契约 §3 示例：盘前 requested_date=20261105、
      trade_date=20261104、is_fallback=true）。
    * 实测东财接口在盘前传当日日期会静默返回上一交易日数据，所以必须自己算出
      有效交易日再取数，否则 trade_date 会标错。
    * ``is_fallback`` = 有效交易日 != 日历日。
    * ``candidates`` = 从有效交易日起往回最多 10 个自然日内所有交易日（降序），
      供"取不到数据就继续往前回溯"使用。

    :return: dict 含 requested_date / calendar_date / trade_date / effective_trade_date /
             is_fallback / fallback_note / candidates / market_phase / today /
             calendar_source / warnings
    """
    now = _as_bj(now_bj)
    today = now.date()
    warnings: list[str] = []

    days = _coerce_trade_dates(trade_dates)
    source = "provided"
    if days is None:
        days = _coerce_trade_dates(_TRADE_DATES)
        source = _TRADE_CAL_SOURCE or "memory"
    if days is None:
        days = _weekday_fallback_dates(today)
        source = "weekday_fallback"
        warnings.append("交易日历不可用，已按“工作日=交易日”退化处理")

    day_set = set(days)
    is_trading_today = _infer_is_trading_day(today, day_set)
    now_t = now.time()
    if is_trading_today and now_t >= dtime(9, 25):
        effective = today
    else:
        earlier = [d for d in days if d < today]
        if earlier:
            effective = max(earlier)
        else:  # 日历异常：退化为前一天
            effective = today - timedelta(days=1)

    is_fallback = effective != today
    if is_fallback:
        if not is_trading_today:
            fallback_note = f"{today.strftime('%Y%m%d')} 非交易日，已回退到最近交易日 {effective.strftime('%Y%m%d')}"
        else:
            fallback_note = f"{today.strftime('%Y%m%d')} 盘前无数据，已回退到最近交易日 {effective.strftime('%Y%m%d')}"
    else:
        fallback_note = None

    # 回溯候选：有效交易日起、最近 10 个自然日内的交易日（降序）
    # 注意只取 <= effective 的日期：effective 之后的日期（如今日盘前）东财会静默返回
    # 上一交易日的数据，绝不能当成那个日期的数据来用。
    window_start = effective - timedelta(days=10)
    cand_dates = [d for d in days if window_start <= d <= effective]
    cand_dates = sorted(set(cand_dates), reverse=True)
    candidates = [effective] + [d for d in cand_dates if d != effective]
    candidates = candidates[:12]

    return {
        "today": today.strftime("%Y%m%d"),
        "calendar_date": today.strftime("%Y%m%d"),
        # 注意：requested_date 语义 = "实际要向东财请求的交易日"（有效交易日）；
        # 快照展示层的 requested_date 用 calendar_date（契约 §3 示例）。
        "requested_date": effective.strftime("%Y%m%d"),
        "trade_date": effective.strftime("%Y%m%d"),
        "effective_trade_date": effective.strftime("%Y%m%d"),
        "is_fallback": bool(is_fallback),
        "fallback_note": fallback_note,
        "candidates": [d.strftime("%Y%m%d") for d in candidates],
        "market_phase": compute_phase(now, is_trading_day=is_trading_today),
        "calendar_source": source,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 纯函数 #3: 统计 / 题材分组 / 连板梯队 / 时间线
# ---------------------------------------------------------------------------


def _first_seal_key(stock: dict) -> str:
    """排序键：首次封板时间升序，无时间排最后（构成合法 HH:MM:SS 的字典序即时间序）。"""
    value = (stock or {}).get("first_seal")
    if isinstance(value, str) and len(value) == 8 and value[2] == ":":
        return value
    return "99:99:99"


def build_stats(limit_up, broken, limit_down, prev_pool=None, prev_count=None) -> dict:
    """统计块。

    :param limit_up: list[Stock]（涨停股，需 streak 字段）
    :param broken: list[dict]（炸板股）
    :param limit_down: list[dict]（跌停股）
    :param prev_pool: list[dict]（昨日涨停股池，需 pct 字段）或 list[float]
    :param prev_count: 昨日涨停股数；None 时用 len(prev_pool)
    """
    limit_up = limit_up or []
    broken = broken or []
    limit_down = limit_down or []
    lu, br, ld = len(limit_up), len(broken), len(limit_down)
    denominator = lu + br
    broken_rate = round(br / denominator, 4) if denominator else None

    streaks = []
    for stock in limit_up:
        s = safe_int((stock or {}).get("streak"), 1)
        streaks.append(1 if not s or s < 1 else s)
    max_streak = max(streaks) if streaks else 0

    pcts = []
    for item in prev_pool or []:
        if isinstance(item, dict):
            p = safe_float(item.get("pct"))
        else:
            p = safe_float(item)
        if p is not None:
            pcts.append(p)
    # 溢价保留原始精度（展示层再做四舍五入）：QA 契约测试断言 5/3 精确值
    premium = (sum(pcts) / len(pcts)) if pcts else None
    if prev_count is not None:
        count = prev_count
    elif prev_pool is None:
        count = None
    else:
        count = len(prev_pool)

    return {
        "limit_up_count": lu,
        "broken_count": br,
        "limit_down_count": ld,
        "broken_rate": broken_rate,
        "max_streak": max_streak,
        "prev_limit_up_premium": premium,
        "prev_limit_up_count": count,
    }


def assign_concepts(stocks, concept_map=None, concept_pct=None):
    """给每只涨停股挑最多 2 个概念（原地修改 ``stock['concepts']``）。

    排序：今日涨停家数最多的概念优先，同数量按概念当日涨幅降序，再按名称。
    """
    concept_map = concept_map or {}
    concept_pct = concept_pct or {}
    stocks = stocks or []
    heat = {}
    for stock in stocks:
        for name in concept_map.get((stock or {}).get("code"), []) or []:
            heat[name] = heat.get(name, 0) + 1

    def sort_key(name):
        pct = safe_float(concept_pct.get(name), -999.0)
        return (-heat.get(name, 0), -(pct if pct is not None else -999.0), str(name))

    for stock in stocks:
        code = (stock or {}).get("code")
        cands = list(dict.fromkeys(concept_map.get(code) or []))
        cands.sort(key=sort_key)
        stock["concepts"] = cands[:2]
    return stocks


def _pick_concepts(code, concept_map, concept_pct, heat):
    """给炸板/跌停股挑最多 2 个概念（不含行业兜底）。"""
    if not code:
        return []
    cands = list(dict.fromkeys((concept_map or {}).get(code) or []))
    cands.sort(
        key=lambda name: (
            -(heat or {}).get(name, 0),
            -(safe_float((concept_pct or {}).get(name), -999.0) or -999.0),
            str(name),
        )
    )
    return cands[:2]


def group_themes(stocks, concept_map=None, concept_pct=None):
    """涨停股 -> 题材分组列表（契约 §3 themes）。

    * 每只股票归属**一个主题材**：其最热概念；没有任何概念时用``所属行业``兜底
      （该题材 ``is_industry_fallback=true``）。
    * 组内股票按 first_seal 升序；题材排序：count 降序 -> 最早 first_seal 升序 -> 名称。
    """
    concept_map = concept_map or {}
    concept_pct = concept_pct or {}
    work = []
    for stock in stocks or []:
        item = dict(stock or {})
        item["concepts"] = list(item.get("concepts") or [])
        work.append(item)
    assign_concepts(work, concept_map, concept_pct)

    groups: dict[str, dict] = {}
    for stock in work:
        concepts = stock.get("concepts") or []
        if concepts:
            name = concepts[0]
            from_concept = True
        else:
            name = safe_str(stock.get("industry")) or "其他"
            from_concept = False
        group = groups.get(name)
        if group is None:
            group = {"name": name, "stocks": [], "is_industry_fallback": True}
            groups[name] = group
        group["is_industry_fallback"] = group["is_industry_fallback"] and (not from_concept)
        group["stocks"].append(stock)

    themes = []
    for group in groups.values():
        members = sorted(group["stocks"], key=lambda s: (_first_seal_key(s), s.get("code") or ""))
        themes.append(
            {
                "name": group["name"],
                "count": len(members),
                "earliest_first_seal": members[0].get("first_seal") if members else None,
                "is_industry_fallback": bool(group["is_industry_fallback"]),
                "stocks": members,
            }
        )
    themes.sort(key=lambda t: (-t["count"], _first_seal_key(t["stocks"][0]) if t["stocks"] else "99:99:99", t["name"]))
    return themes


def build_ladder(stocks):
    """连板梯队：按 streak 降序分组，组内按 first_seal 升序；只应传涨停股。"""
    groups: dict[int, list] = {}
    for stock in stocks or []:
        streak = safe_int((stock or {}).get("streak"), 1)
        if not streak or streak < 1:
            streak = 1
        groups.setdefault(streak, []).append(stock)
    ladder = []
    for streak in sorted(groups.keys(), reverse=True):
        members = sorted(groups[streak], key=lambda s: (_first_seal_key(s), s.get("code") or ""))
        ladder.append({"streak": streak, "stocks": members})
    return ladder


def build_timeline(stocks, themes=None):
    """首次封板时间线（Chart.js 用），按 first_seal 升序。"""
    theme_of = {}
    for theme in themes or []:
        for stock in theme.get("stocks") or []:
            code = (stock or {}).get("code")
            if code and code not in theme_of:
                theme_of[code] = theme.get("name")
    items = []
    for stock in stocks or []:
        stock = stock or {}
        code = stock.get("code")
        items.append(
            {
                "code": code,
                "name": stock.get("name"),
                "first_seal": stock.get("first_seal"),
                "theme": theme_of.get(code),
                "streak": stock.get("streak"),
                "seal_amount_yi": stock.get("seal_amount_yi"),
            }
        )
    items.sort(key=lambda x: (_first_seal_key(x), x.get("code") or ""))
    return items


# ---------------------------------------------------------------------------
# akshare 调用闸门（全局节流 + 超时 + 异常包装）
# ---------------------------------------------------------------------------

_AK_LOCK = threading.RLock()
_AK_NEXT_ALLOWED = 0.0
_AK_MODULE = None


def _ak():
    """惰性导入 akshare（让纯函数单测不必加载 pandas/akshare）。"""
    global _AK_MODULE
    if _AK_MODULE is None:
        import akshare  # noqa: F401  (惰性导入)

        _AK_MODULE = akshare
    return _AK_MODULE


def _throttle_wait() -> None:
    """全局节流：任意两次 akshare 调用的**开始时刻**至少间隔 MIN_AK_INTERVAL 秒。"""
    global _AK_NEXT_ALLOWED
    with _AK_LOCK:
        now = time.monotonic()
        if now < _AK_NEXT_ALLOWED:
            time.sleep(_AK_NEXT_ALLOWED - now)
        _AK_NEXT_ALLOWED = time.monotonic() + MIN_AK_INTERVAL


def ak_call(fn, *args, timeout=None, **kwargs):
    """执行一次 akshare 调用：全局节流 + 可选超时 + 异常透传。

    ``LIMITUP_FAKE_FAIL=1`` 时直接抛 :class:`RuntimeError`（QA 验证 stale 用）。
    """
    if FAKE_FAIL:
        raise RuntimeError("[LIMITUP_FAKE_FAIL] 已按环境变量禁用 akshare 拉取")
    _throttle_wait()
    name = getattr(fn, "__name__", str(fn))
    if timeout is None:
        return fn(*args, **kwargs)
    box: dict = {}

    def _runner():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 需把异常带回调用线程
            box["error"] = exc

    thread = threading.Thread(target=_runner, name=f"ak-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"akshare 调用超时(>{timeout:.0f}s): {name}")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# 交易日历（进程内 + data/trade_calendar.json 文件缓存，一天一刷）
# ---------------------------------------------------------------------------

_TRADE_CAL_LOCK = threading.RLock()
_TRADE_DATES: list[date] | None = None
_TRADE_DATE_SET: set[date] = set()
_TRADE_CAL_SOURCE = "unknown"
_TRADE_CAL_DAY: date | None = None


def _calendar_path() -> Path:
    return DATA_DIR / "trade_calendar.json"


def _same_bj_day(iso_text, today: date) -> bool:
    try:
        dt = datetime.fromisoformat(str(iso_text))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ_TZ)
        return dt.astimezone(BJ_TZ).date() == today
    except Exception:
        return False


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON（先写 .tmp 再 os.replace，避免读到半个文件）。"""
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(sanitize_json(payload), ensure_ascii=False, allow_nan=False)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_trade_dates(force: bool = False, allow_network: bool = True):
    """加载交易日历。

    :return: (sorted list[date], source)；source ∈ sina / file / file-stale / weekday_fallback / memory
    """
    global _TRADE_DATES, _TRADE_DATE_SET, _TRADE_CAL_SOURCE, _TRADE_CAL_DAY
    with _TRADE_CAL_LOCK:
        today = now_beijing().date()
        if not force and _TRADE_DATES is not None and _TRADE_CAL_DAY == today:
            return list(_TRADE_DATES), _TRADE_CAL_SOURCE

        # 1) 文件缓存
        file_dates = None
        file_fresh = False
        path = _calendar_path()
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                dates = [_to_date(x) for x in (meta.get("dates") or [])]
                dates = sorted({d for d in dates if d is not None})
                if dates:
                    file_dates = dates
                    file_fresh = _same_bj_day(meta.get("fetched_at"), today)
            except Exception as exc:
                LOG.warning("[calendar] 读取交易日历缓存失败: %s", _short_err(exc))

        if file_dates is not None and (file_fresh or not allow_network):
            _TRADE_DATES = file_dates
            _TRADE_DATE_SET = set(file_dates)
            _TRADE_CAL_DAY = today
            _TRADE_CAL_SOURCE = "file" if file_fresh else "file-stale"
            if file_fresh:
                return list(_TRADE_DATES), _TRADE_CAL_SOURCE

        if not allow_network:
            if file_dates is not None:
                _TRADE_DATES = file_dates
                _TRADE_DATE_SET = set(file_dates)
                _TRADE_CAL_DAY = today
                _TRADE_CAL_SOURCE = "file-stale"
                return list(_TRADE_DATES), _TRADE_CAL_SOURCE
            fallback = _weekday_fallback_dates(today)
            _TRADE_DATES = fallback
            _TRADE_DATE_SET = set(fallback)
            _TRADE_CAL_DAY = today
            _TRADE_CAL_SOURCE = "weekday_fallback"
            LOG.warning("[calendar] 无交易日历缓存，按“工作日=交易日”退化（不联网模式）")
            return list(_TRADE_DATES), _TRADE_CAL_SOURCE

        # 2) 联网拉取
        try:
            df = ak_call(_ak().tool_trade_date_hist_sina, timeout=AK_TIMEOUT_CALENDAR)
            cols = _df_columns(df)
            col = pick_col(cols, ["trade_date", "日期", "date"], required=True)
            values = list(df[col])
            dates = sorted({d for d in (_to_date(v) for v in values) if d is not None})
            if not dates:
                raise ValueError("交易日历为空")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                path,
                {
                    "fetched_at": fmt_iso(now_beijing()),
                    "source": "akshare.tool_trade_date_hist_sina",
                    "count": len(dates),
                    "dates": [d.isoformat() for d in dates],
                },
            )
            _TRADE_DATES = dates
            _TRADE_DATE_SET = set(dates)
            _TRADE_CAL_DAY = today
            _TRADE_CAL_SOURCE = "sina"
            LOG.info("[calendar] 交易日历已更新: %d 个交易日（最新 %s）", len(dates), dates[-1].isoformat())
            return list(_TRADE_DATES), _TRADE_CAL_SOURCE
        except Exception as exc:
            LOG.warning("[calendar] 拉取交易日历失败: %s", _short_err(exc))
            if file_dates is not None:
                _TRADE_DATES = file_dates
                _TRADE_DATE_SET = set(file_dates)
                _TRADE_CAL_DAY = today
                _TRADE_CAL_SOURCE = "file-stale"
                LOG.warning("[calendar] 使用过期交易日历缓存（%d 条）", len(file_dates))
                return list(_TRADE_DATES), _TRADE_CAL_SOURCE
            fallback = _weekday_fallback_dates(today)
            _TRADE_DATES = fallback
            _TRADE_DATE_SET = set(fallback)
            _TRADE_CAL_DAY = today
            _TRADE_CAL_SOURCE = "weekday_fallback"
            return list(_TRADE_DATES), _TRADE_CAL_SOURCE


def _calendar_dates_for_request():
    """请求线程用：只读内存缓存，绝不联网/读盘（返回 None 时 resolve_dates 用工作日退化）。"""
    with _TRADE_CAL_LOCK:
        if _TRADE_DATES is None:
            return None
        return list(_TRADE_DATES)


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------


def _empty_snapshot_base() -> dict:
    """无数据时的占位快照（契约 §3 全字段，绝不编造数据）。"""
    return {
        "ok": True,
        "generated_at": None,
        "refreshed_at": None,
        "trade_date": None,
        "requested_date": None,
        "is_fallback": False,
        "fallback_note": None,
        "market_phase": "pre_open",
        "refreshing": False,
        "stale": {"is_stale": False, "since": None, "reason": None},
        "stats": {
            "limit_up_count": 0,
            "broken_count": 0,
            "limit_down_count": 0,
            "broken_rate": None,
            "max_streak": 0,
            "prev_limit_up_premium": None,
            "prev_limit_up_count": None,
        },
        "themes": [],
        "ladder": [],
        "broken": [],
        "limit_down": [],
        "timeline": [],
        "watchlist": [],
        "concepts": {
            "ready": False,
            "loaded": 0,
            "total": 0,
            "updated_at": None,
            "note": "概念映射等待启动",
        },
        "warnings": [],
        "log_file": None,
    }


_STATE_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
_CSV_LOCK = threading.Lock()

_STATE: dict = {
    "snapshot_base": _empty_snapshot_base(),
    "last_success_dt": None,
    "last_error": None,
    "refreshing": False,
    "trade_date": None,
    "requested_date": None,
    "raw_limit_up": [],
    "raw_broken": [],
    "raw_limit_down": [],
    "prev_pool": [],
    "prev_count": None,
    "prev_ok": False,
    "warnings": [],
    "log_file": None,
    "spot": {},
    "spot_updated_at": None,
    "spot_error": None,
    "watch_codes": [],
    "watch_quote_ts": {},
    "watch_quote_inflight": False,
    "names": {},
}

_INITIAL_DATA_READY = threading.Event()  # 首次"池子数据"就绪
_INITIAL_DONE = threading.Event()        # 首次加载（含自选行情，DISABLE_BG 下）结束
_THREADS_STARTED = False
_START_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 概念映射（后台线程 + 每日 JSON 缓存）
# ---------------------------------------------------------------------------

_CONCEPT_LOCK = threading.RLock()
_CONCEPT_MAP: dict[str, list[str]] = {}   # code -> [概念名, ...]（按板块涨幅降序加入）
_CONCEPT_LIST: list[dict] = []            # [{"name","code","pct"}] 按涨幅降序
_CONCEPT_PCT: dict[str, float | None] = {}
_CONCEPT_PROCESSED = 0
_CONCEPT_STATE: dict = {
    "ready": False,
    "loaded": 0,
    "total": 0,
    "updated_at": None,
    "note": "概念映射等待启动",
}


def _concept_paths(trade_date: str):
    return (
        DATA_DIR / f"concept_list_{trade_date}.json",
        DATA_DIR / f"concept_map_{trade_date}.json",
    )


def _update_concept_state(*, ready=None, loaded=None, total=None, note=None, touch=False) -> dict:
    """更新概念进度并同步进当前快照（页面轮询即可看到进度）。"""
    with _CONCEPT_LOCK:
        if ready is not None:
            _CONCEPT_STATE["ready"] = bool(ready)
        if loaded is not None:
            _CONCEPT_STATE["loaded"] = int(loaded)
        if total is not None:
            _CONCEPT_STATE["total"] = int(total)
        if note is not None:
            _CONCEPT_STATE["note"] = note
        if touch:
            _CONCEPT_STATE["updated_at"] = fmt_iso(now_beijing())
        state = copy.deepcopy(_CONCEPT_STATE)
    with _STATE_LOCK:
        _STATE["snapshot_base"]["concepts"] = state
    return state


def _concept_snapshot():
    """(map 拷贝, 板块涨幅拷贝, state 拷贝)。"""
    with _CONCEPT_LOCK:
        cmap = {code: list(names) for code, names in _CONCEPT_MAP.items()}
        pct = dict(_CONCEPT_PCT)
        state = copy.deepcopy(_CONCEPT_STATE)
    return cmap, pct, state


def _install_concept_cache(cache: dict, complete: bool) -> None:
    """把磁盘缓存装进内存。"""
    global _CONCEPT_PROCESSED  # 本函数会重置该模块级计数器（否则 Python 视为局部变量）
    with _CONCEPT_LOCK:
        _CONCEPT_MAP.clear()
        _CONCEPT_MAP.update({str(k): list(v) for k, v in (cache.get("map") or {}).items() if k})
        _CONCEPT_LIST[:] = [dict(c) for c in (cache.get("concepts") or [])]
        _CONCEPT_PCT.clear()
        for item in _CONCEPT_LIST:
            _CONCEPT_PCT[item["name"]] = item.get("pct")
        _CONCEPT_PROCESSED = min(int(cache.get("processed") or 0), len(_CONCEPT_LIST))
    _update_concept_state(
        ready=complete,
        loaded=_CONCEPT_PROCESSED,
        total=len(_CONCEPT_LIST),
        note=(
            f"概念映射就绪（{len(_CONCEPT_LIST)}/{len(_CONCEPT_LIST)}）"
            if complete
            else f"概念映射构建中 {_CONCEPT_PROCESSED}/{len(_CONCEPT_LIST)}，完成后自动刷新"
        ),
        touch=True,
    )


def _load_concept_cache(trade_date: str):
    """读取当日概念缓存；没有则返回 None。"""
    if not trade_date:
        return None
    list_path, map_path = _concept_paths(trade_date)
    if not list_path.exists():
        return None
    try:
        meta = json.loads(list_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.warning("[concept] 读取概念列表缓存失败: %s", _short_err(exc))
        return None
    if str(meta.get("trade_date")) != str(trade_date):
        return None
    concepts = [c for c in (meta.get("concepts") or []) if isinstance(c, dict) and c.get("name")]
    if not concepts:
        return None
    cmap = {}
    if map_path.exists():
        try:
            raw_map = json.loads(map_path.read_text(encoding="utf-8")) or {}
            cmap = {str(k): [str(x) for x in v] for k, v in raw_map.items() if k}
        except Exception as exc:
            LOG.warning("[concept] 读取概念成分缓存失败: %s", _short_err(exc))
            cmap = {}
    return {
        "trade_date": str(trade_date),
        "concepts": concepts,
        "processed": int(meta.get("processed") or 0),
        "complete": bool(meta.get("complete")),
        "updated_at": meta.get("updated_at"),
        "map": cmap,
    }


def _save_concept_cache(trade_date: str, complete: bool) -> None:
    """落盘概念列表 + 成分映射（每 20 个概念一次 + 完成时）。"""
    if not trade_date:
        return
    list_path, map_path = _concept_paths(trade_date)
    with _CONCEPT_LOCK:
        concepts = [dict(c) for c in _CONCEPT_LIST]
        processed = _CONCEPT_PROCESSED
        cmap = {code: list(names) for code, names in _CONCEPT_MAP.items()}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            list_path,
            {
                "trade_date": trade_date,
                "updated_at": fmt_iso(now_beijing()),
                "complete": bool(complete),
                "processed": int(processed),
                "total": len(concepts),
                "concepts": concepts,
            },
        )
        _atomic_write_json(map_path, cmap)
    except Exception as exc:
        LOG.warning("[concept] 概念缓存落盘失败: %s", _short_err(exc))


def _fetch_concept_list() -> list[dict]:
    """拉取概念板块列表并按当日涨幅降序（热点优先映射）。失败重试 3 次。"""
    last_exc = None
    for attempt in range(1, 4):
        try:
            df = ak_call(_ak().stock_board_concept_name_em, timeout=AK_TIMEOUT_CONCEPT_LIST)
            cols = _df_columns(df)
            name_col = pick_col(cols, ["板块名称", "概念名称", "名称", "name"], required=True)
            pct_col = pick_col(cols, ["涨跌幅", "pct"])
            code_col = pick_col(cols, ["板块代码", "概念代码", "code"])
            _log_unknown_columns(
                "stock_board_concept_name_em",
                cols,
                {c for c in (name_col, pct_col, code_col) if c is not None},
            )
            out = []
            for _, row in df.iterrows():
                name = safe_str(_cell(row, name_col))
                if not name:
                    continue
                out.append(
                    {
                        "name": name,
                        "code": safe_str(_cell(row, code_col)) if code_col else None,
                        "pct": safe_float(_cell(row, pct_col)) if pct_col else None,
                    }
                )
            if not out:
                raise ValueError("概念列表为空")
            out.sort(key=lambda c: (-(c["pct"] if c["pct"] is not None else -999.0), c["name"]))
            LOG.info("[concept] 概念列表获取成功: %d 个板块", len(out))
            return out
        except Exception as exc:
            last_exc = exc
            LOG.warning("[concept] 概念列表获取失败(第 %d/3 次): %s", attempt, _short_err(exc))
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(f"概念列表获取失败: {_short_err(last_exc) if last_exc else 'unknown'}")


def _concept_worker() -> None:
    """后台线程：逐概念抓成分股，建立 stock -> [概念] 映射。"""
    global _CONCEPT_PROCESSED  # 本函数会推进该模块级进度计数器
    try:
        if not _INITIAL_DATA_READY.wait(timeout=300):
            LOG.warning("[concept] 初始数据未就绪，概念映射稍后仍会尝试")
        with _STATE_LOCK:
            trade_date = _STATE["trade_date"]
        if not trade_date:
            trade_date = fmt_ymd(now_beijing())
        cache = _load_concept_cache(trade_date)
        if cache and cache.get("complete"):
            _install_concept_cache(cache, complete=True)
            LOG.info(
                "[concept] 命中当日完整缓存 %s（概念 %d / 映射股票 %d）",
                trade_date,
                len(cache.get("concepts") or []),
                len(cache.get("map") or {}),
            )
            _rebuild_derived_views()
            return

        if cache:
            _install_concept_cache(cache, complete=False)
            concepts = cache["concepts"]
            LOG.info("[concept] 续跑当日缓存 %s: %d/%d", trade_date, _CONCEPT_PROCESSED, len(concepts))
        else:
            concepts = _fetch_concept_list()
            with _CONCEPT_LOCK:
                _CONCEPT_LIST[:] = [dict(c) for c in concepts]
                _CONCEPT_PCT.clear()
                for item in _CONCEPT_LIST:
                    _CONCEPT_PCT[item["name"]] = item.get("pct")
                _CONCEPT_PROCESSED = 0
            _update_concept_state(
                ready=False,
                loaded=0,
                total=len(concepts),
                note=f"概念映射构建中 0/{len(concepts)}，完成后自动刷新",
                touch=True,
            )

        total = len(concepts)
        for index in range(_CONCEPT_PROCESSED, total):
            item = concepts[index]
            name = item.get("name")
            code = item.get("code") or name
            try:
                df = ak_call(_ak().stock_board_concept_cons_em, symbol=code, timeout=AK_TIMEOUT_CONCEPT_CONS)
                cols = _df_columns(df)
                code_col = pick_col(cols, ["代码", "股票代码", "code"], required=True)
                _log_unknown_columns("stock_board_concept_cons_em", cols, {code_col})
                with _CONCEPT_LOCK:
                    for value in df[code_col].tolist():
                        stock_code = normalize_code(value)
                        if not stock_code:
                            continue
                        names = _CONCEPT_MAP.setdefault(stock_code, [])
                        if name not in names:
                            names.append(name)
            except Exception as exc:
                LOG.warning("[concept] 概念“%s”成分拉取失败: %s", name, _short_err(exc))
            with _CONCEPT_LOCK:
                _CONCEPT_PROCESSED = index + 1
                processed_now = _CONCEPT_PROCESSED
            _update_concept_state(
                loaded=processed_now,
                total=total,
                note=f"概念映射构建中 {processed_now}/{total}，完成后自动刷新",
            )
            # 契约要求每 20 个落盘一次；第一个也落盘一次，便于尽早看到缓存文件
            if processed_now % 20 == 0 or processed_now == 1 or processed_now == total:
                _save_concept_cache(trade_date, complete=(processed_now >= total))
                _rebuild_derived_views()
                LOG.info("[concept] 概念映射进度 %d/%d（已落盘）", processed_now, total)

        _save_concept_cache(trade_date, complete=True)
        _update_concept_state(
            ready=True,
            loaded=total,
            total=total,
            note=f"概念映射就绪（{total}/{total}）",
            touch=True,
        )
        _rebuild_derived_views()
        LOG.info("[concept] 概念映射完成: %d 个概念 / %d 只股票", total, len(_CONCEPT_MAP))
    except Exception as exc:
        LOG.exception("[concept] 概念映射构建异常")
        _update_concept_state(note=f"概念映射构建失败: {_short_err(exc)}（使用所属行业兜底）", touch=True)


# ---------------------------------------------------------------------------
# 自选实时行情（stock_zh_a_spot_em 很慢：东财分页每页 sleep 0.5~1.5s，实测 60~90s）
# 因此单独线程低频刷新 + 缓存，绝不阻塞主刷新与 /api/snapshot
# ---------------------------------------------------------------------------

_SPOT_LOCK = threading.Lock()
_SPOT_IN_FLIGHT = False


def _map_spot_df(df) -> dict:
    cols = _df_columns(df)
    code_col = pick_col(cols, ["代码", "股票代码", "code"], required=True)
    name_col = pick_col(cols, ["名称", "股票名称", "name"], required=True)
    price_col = pick_col(cols, ["最新价", "price"])
    pct_col = pick_col(cols, ["涨跌幅", "pct"])
    _log_unknown_columns(
        "stock_zh_a_spot_em", cols, {c for c in (code_col, name_col, price_col, pct_col) if c is not None}
    )
    spot = {}
    for _, row in df.iterrows():
        code = normalize_code(_cell(row, code_col))
        if not code:
            continue
        spot[code] = {
            "code": code,
            "name": safe_str(_cell(row, name_col)),
            "price": safe_float(_cell(row, price_col)) if price_col else None,
            "pct": safe_float(_cell(row, pct_col)) if pct_col else None,
        }
    return spot


def _fetch_spot_once(timeout: float = AK_TIMEOUT_SPOT) -> bool:
    """拉一次全市场实时行情并更新缓存；同一时刻只允许一个线程执行。"""
    global _SPOT_IN_FLIGHT
    with _SPOT_LOCK:
        if _SPOT_IN_FLIGHT:
            return False
        _SPOT_IN_FLIGHT = True
    try:
        started = time.time()
        df = ak_call(_ak().stock_zh_a_spot_em, timeout=timeout)
        spot = _map_spot_df(df)
        with _STATE_LOCK:
            old_spot = dict(_STATE["spot"])
            names = dict(_STATE["names"])
        for code, entry in old_spot.items():
            if code not in spot:
                spot[code] = entry
                continue
            for key in ("limit_up_price", "limit_down_price"):
                if entry.get(key) is not None:
                    spot[code][key] = entry[key]
        for code, name in names.items():
            if code in spot and not spot[code].get("name"):
                spot[code]["name"] = name
        with _STATE_LOCK:
            _STATE["spot"] = spot
            _STATE["spot_updated_at"] = now_beijing()
            _STATE["spot_error"] = None
        LOG.info("[spot] 自选行情缓存刷新完成: %d 只（耗时 %.1fs）", len(spot), time.time() - started)
        return True
    except Exception as exc:
        with _STATE_LOCK:
            _STATE["spot_error"] = _short_err(exc)
        LOG.warning("[spot] 自选行情拉取失败: %s", _short_err(exc))
        return False
    finally:
        with _SPOT_LOCK:
            _SPOT_IN_FLIGHT = False


def _start_spot_fetch_async(timeout: float = AK_TIMEOUT_SPOT) -> bool:
    """需要时异步刷新自选行情（不阻塞调用方）。"""
    with _STATE_LOCK:
        updated = _STATE["spot_updated_at"]
        has_data = bool(_STATE["spot"])
    with _SPOT_LOCK:
        in_flight = _SPOT_IN_FLIGHT
    if in_flight:
        return False
    if has_data and updated is not None:
        if (now_beijing() - updated).total_seconds() < SPOT_MIN_INTERVAL:
            return False
    threading.Thread(target=_fetch_spot_once, kwargs={"timeout": timeout}, name="spot-fetch", daemon=True).start()
    return True


def _spot_loop() -> None:
    """后台线程：低频刷新自选行情缓存。"""
    while True:
        try:
            _start_spot_fetch_async()
        except Exception:  # pragma: no cover - 保底
            LOG.exception("[spot] 自选行情线程异常")
        time.sleep(10)


# ---------------------------------------------------------------------------
# 自选股快照快速路径（stock_bid_ask_em，单只一次请求）
# 说明：契约指定的 stock_zh_a_spot_em 是全市场分页接口，实测要 5~6 分钟才拉完；
# 为了自选股在页面轮询后几秒内就有价格，这里对**用户显式关注的代码**额外用
# 东财单只行情报价（免费接口）补一份，两个来源都写进同一份 spot 缓存。
# ---------------------------------------------------------------------------


def _record_watch_codes(codes) -> None:
    """记录最近一次 /api/snapshot?watch= 里的代码，供后台快照线程补齐行情。"""
    if not codes:
        return
    with _STATE_LOCK:
        current = list(_STATE["watch_codes"])
        for code in codes:
            if code not in current:
                current.append(code)
        _STATE["watch_codes"] = current[:50]


def _watch_quote_mapping(df):
    """stock_bid_ask_em 的 item/value 两列表 -> dict。"""
    cols = _df_columns(df)
    item_col = pick_col(cols, ["item", "项目"], required=True)
    value_col = pick_col(cols, ["value", "值"], required=True)
    result = {}
    for _, row in df.iterrows():
        key = safe_str(_cell(row, item_col))
        if key:
            result[key] = _cell(row, value_col)
    return result


def _fetch_stock_name(code: str):
    """用东财个股信息补股票简称（每只代码只查一次，结果常驻内存）。"""
    try:
        df = ak_call(_ak().stock_individual_info_em, symbol=code, timeout=20.0)
        cols = _df_columns(df)
        item_col = pick_col(cols, ["item", "项目"], required=True)
        value_col = pick_col(cols, ["value", "值"], required=True)
        for _, row in df.iterrows():
            if safe_str(_cell(row, item_col)) in ("股票简称", "名称", "简称"):
                return safe_str(_cell(row, value_col))
    except Exception as exc:
        LOG.warning("[watch] 股票 %s 名称查询失败: %s", code, _short_err(exc))
    return None


def _fetch_watch_quotes(codes) -> int:
    """逐只拉自选股快照（价格/涨跌幅/涨跌停价），成功多少只返回多少。"""
    ok_count = 0
    for code in codes:
        try:
            df = ak_call(_ak().stock_bid_ask_em, symbol=code, timeout=AK_TIMEOUT_WATCH_QUOTE)
            mapping = _watch_quote_mapping(df)
            price = safe_float(mapping.get("最新"))
            pct = safe_float(mapping.get("涨幅"))
            with _STATE_LOCK:
                entry = dict(_STATE["spot"].get(code) or {})
                known_name = entry.get("name") or _STATE["names"].get(code)
            if not known_name:
                known_name = _fetch_stock_name(code)
                if known_name:
                    with _STATE_LOCK:
                        names = dict(_STATE["names"])
                        names[code] = known_name
                        _STATE["names"] = names
            with _STATE_LOCK:
                entry.update(
                    {
                        "code": code,
                        "name": known_name or entry.get("name"),
                        "price": price,
                        "pct": pct,
                        "limit_up_price": safe_float(mapping.get("涨停")),
                        "limit_down_price": safe_float(mapping.get("跌停")),
                        "source": "bid_ask",
                        "ts": fmt_iso(now_beijing()),
                    }
                )
                spot = dict(_STATE["spot"])
                spot[code] = entry
                _STATE["spot"] = spot
                ts_map = dict(_STATE["watch_quote_ts"])
                ts_map[code] = fmt_iso(now_beijing())
                _STATE["watch_quote_ts"] = ts_map
            ok_count += 1
        except Exception as exc:
            LOG.warning("[watch] 自选股 %s 行情拉取失败: %s", code, _short_err(exc))
    if ok_count:
        LOG.info("[watch] 自选股快照更新 %d/%d 只", ok_count, len(codes))
    return ok_count


def _pending_watch_codes() -> list:
    """需要补快照的自选代码：还没有行情、或缓存超过 WATCH_QUOTE_TTL 秒。"""
    with _STATE_LOCK:
        codes = list(_STATE["watch_codes"])
        spot = dict(_STATE["spot"])
        ts_map = dict(_STATE["watch_quote_ts"])
    now_dt = now_beijing()
    pending = []
    for code in codes:
        entry = spot.get(code) or {}
        if entry.get("price") is None:
            pending.append(code)
            continue
        stamp = ts_map.get(code) or entry.get("ts")
        try:
            age = (now_dt - datetime.fromisoformat(str(stamp))).total_seconds() if stamp else 1e9
        except Exception:
            age = 1e9
        if age >= WATCH_QUOTE_TTL:
            pending.append(code)
    return pending


def _ensure_watch_quotes_async() -> None:
    """有需要时异步拉自选股快照（不阻塞请求线程）。"""
    with _STATE_LOCK:
        if _STATE["watch_quote_inflight"]:
            return
    pending = _pending_watch_codes()
    if not pending:
        return
    with _STATE_LOCK:
        if _STATE["watch_quote_inflight"]:
            return
        _STATE["watch_quote_inflight"] = True

    def _runner():
        try:
            _fetch_watch_quotes(pending)
        finally:
            with _STATE_LOCK:
                _STATE["watch_quote_inflight"] = False

    threading.Thread(target=_runner, name="watch-quote", daemon=True).start()


def _watch_quote_loop() -> None:
    """后台线程：有自选股时按 WATCH_QUOTE_TTL 刷新其快照。"""
    while True:
        try:
            _ensure_watch_quotes_async()
        except Exception:  # pragma: no cover - 保底
            LOG.exception("[watch] 自选股快照线程异常")
        time.sleep(5)


# ---------------------------------------------------------------------------
# CSV 落盘（契约 §4.5）
# ---------------------------------------------------------------------------


def _csv_int(value) -> str:
    """CSV 数值列：缺失写 0（保证每行 streak/break_times 都可被 int() 解析）。"""
    v = safe_int(value)
    return "0" if v is None else str(v)


def _csv_num(value) -> str:
    """CSV 金额列：缺失写 0.00（保证可被 float() 解析）。"""
    v = safe_float(value)
    if v is None:
        return "0.00"
    return f"{v:.2f}"


def append_csv(trade_date, refresh_dt, limit_up, broken, limit_down):
    """把本次刷新的三池数据追加进 ``data/YYYYMMDD.csv``，返回相对路径（失败返回 None）。"""
    if not trade_date:
        return None
    path = DATA_DIR / f"{trade_date}.csv"
    rel_path = f"data/{trade_date}.csv"
    timestamp = _as_bj(refresh_dt).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _CSV_LOCK:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            new_file = (not path.exists()) or path.stat().st_size == 0
            with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(CSV_HEADER)
                for stock in limit_up or []:
                    writer.writerow(
                        [
                            timestamp,
                            stock.get("code") or "",
                            stock.get("name") or "",
                            "limit_up",
                            _csv_int(stock.get("streak")),
                            stock.get("first_seal") or "",
                            _csv_num(stock.get("seal_amount_yuan")),
                            _csv_int(stock.get("break_times")),
                            "|".join(stock.get("concepts") or []),
                        ]
                    )
                for stock in broken or []:
                    writer.writerow(
                        [
                            timestamp,
                            stock.get("code") or "",
                            stock.get("name") or "",
                            "broken",
                            _csv_int(stock.get("streak")),
                            stock.get("first_seal") or "",
                            _csv_num(None),
                            _csv_int(stock.get("break_times")),
                            "|".join(stock.get("concepts") or []),
                        ]
                    )
                for stock in limit_down or []:
                    writer.writerow(
                        [
                            timestamp,
                            stock.get("code") or "",
                            stock.get("name") or "",
                            "limit_down",
                            _csv_int(stock.get("streak")),
                            "",
                            _csv_num(None),
                            _csv_int(stock.get("break_times")),
                            "|".join(stock.get("concepts") or []),
                        ]
                    )
        return rel_path
    except Exception as exc:
        LOG.warning("[csv] 写入 %s 失败: %s", rel_path, _short_err(exc))
        return None


# ---------------------------------------------------------------------------
# 数据源映射（DataFrame 级）
# ---------------------------------------------------------------------------


def _fetch_optional(source_name: str, caller, warnings: list):
    """拉取"可选"数据源：失败只记 warning，不影响主数据。"""
    try:
        return caller()
    except Exception as exc:
        message = f"{source_name} 拉取失败: {_short_err(exc)}"
        warnings.append(message)
        LOG.warning("[refresh] %s", message)
        return None


def _map_limit_up_df(df, warnings: list) -> list[dict]:
    """涨停股池 DataFrame -> list[Stock]。必需列（代码/名称）缺失 => 抛异常。"""
    if df is None or len(df) == 0:
        return []
    cols = _df_columns(df)
    pick_col(cols, _LIMIT_UP_KEYS["code"], required=True)
    pick_col(cols, _LIMIT_UP_KEYS["name"], required=True)
    _log_unknown_columns("stock_zt_pool_em", cols, _matched_columns(cols, _LIMIT_UP_KEYS))
    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        try:
            stock = map_limit_up_row(row)
        except Exception:
            skipped += 1
            continue
        if not stock.get("code"):
            skipped += 1
            continue
        rows.append(stock)
    if skipped:
        warnings.append(f"涨停池有 {skipped} 行缺少代码/名称，已跳过")
    rows.sort(key=lambda s: (_first_seal_key(s), s.get("code") or ""))
    return rows


def _map_broken_df(df, warnings: list) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    cols = _df_columns(df)
    pick_col(cols, _BROKEN_KEYS["code"], required=True)
    pick_col(cols, _BROKEN_KEYS["name"], required=True)
    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        try:
            item = map_broken_row(row)
        except Exception:
            skipped += 1
            continue
        if not item.get("code"):
            skipped += 1
            continue
        rows.append(item)
    if skipped:
        warnings.append(f"炸板池有 {skipped} 行缺少代码/名称，已跳过")
    rows.sort(key=lambda s: (_first_seal_key({"first_seal": s.get("first_seal")}), s.get("code") or ""))
    return rows


def _map_limit_down_df(df, warnings: list) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    cols = _df_columns(df)
    pick_col(cols, _LIMIT_DOWN_KEYS["code"], required=True)
    pick_col(cols, _LIMIT_DOWN_KEYS["name"], required=True)
    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        try:
            item = map_limit_down_row(row)
        except Exception:
            skipped += 1
            continue
        if not item.get("code"):
            skipped += 1
            continue
        rows.append(item)
    if skipped:
        warnings.append(f"跌停池有 {skipped} 行缺少代码/名称，已跳过")
    return rows


def _map_prev_df(df, warnings: list) -> list[dict]:
    """昨日涨停股池 -> [{code,name,pct}]（用于计算今日平均溢价）。"""
    if df is None or len(df) == 0:
        return []
    cols = _df_columns(df)
    pct_col = pick_col(cols, ["涨跌幅", "pct", "change_pct"], required=True)
    code_col = pick_col(cols, ["代码", "股票代码", "code"])
    name_col = pick_col(cols, ["名称", "股票名称", "name"])
    _log_unknown_columns(
        "stock_zt_pool_previous_em", cols, {c for c in (pct_col, code_col, name_col) if c is not None}
    )
    rows = []
    for _, row in df.iterrows():
        pct = safe_float(_cell(row, pct_col))
        if pct is None:
            continue
        rows.append(
            {
                "code": normalize_code(_cell(row, code_col)) if code_col else None,
                "name": safe_str(_cell(row, name_col)) if name_col else None,
                "pct": round(pct, 2),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 视图组装（题材 / 梯队 / 时间线 / 概念填充）
# ---------------------------------------------------------------------------


def _materialize(limit_up_raw, broken_raw, limit_down_raw):
    """把原始三池数据 + 当前概念映射 -> 展示用视图。"""
    concept_map, concept_pct, concept_state = _concept_snapshot()

    stocks = []
    for stock in limit_up_raw or []:
        item = dict(stock or {})
        item["concepts"] = []
        stocks.append(item)
    assign_concepts(stocks, concept_map, concept_pct)

    themes = group_themes(stocks, concept_map, concept_pct)
    ladder = build_ladder(stocks)
    timeline = build_timeline(stocks, themes)

    heat: dict[str, int] = {}
    for stock in stocks:
        for name in concept_map.get(stock.get("code"), []) or []:
            heat[name] = heat.get(name, 0) + 1

    broken = []
    for item in broken_raw or []:
        view = dict(item or {})
        view["concepts"] = _pick_concepts(view.get("code"), concept_map, concept_pct, heat)
        broken.append(view)
    limit_down = []
    for item in limit_down_raw or []:
        view = dict(item or {})
        view["concepts"] = _pick_concepts(view.get("code"), concept_map, concept_pct, heat)
        limit_down.append(view)

    return stocks, themes, ladder, timeline, broken, limit_down, concept_state


def _dedupe(messages) -> list[str]:
    seen = set()
    out = []
    for message in messages or []:
        text = str(message)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _compose_snapshot_base(
    *,
    trade_date,
    requested_date,
    is_fallback,
    fallback_note,
    market_phase,
    warnings,
    log_file,
    refreshed_dt,
) -> dict:
    """用当前 _STATE 里的原始数据组装一份完整快照（不含 watch 相关字段）。"""
    with _STATE_LOCK:
        limit_up_raw = [dict(s) for s in _STATE["raw_limit_up"]]
        broken_raw = [dict(s) for s in _STATE["raw_broken"]]
        limit_down_raw = [dict(s) for s in _STATE["raw_limit_down"]]
        prev_ok = bool(_STATE.get("prev_ok"))
        prev_pool = [dict(p) for p in _STATE["prev_pool"]] if prev_ok else None
        prev_count = _STATE["prev_count"] if prev_ok else None

    stocks, themes, ladder, timeline, broken, limit_down, concept_state = _materialize(
        limit_up_raw, broken_raw, limit_down_raw
    )
    stats = build_stats(stocks, broken, limit_down, prev_pool, prev_count)

    return {
        "ok": True,
        "generated_at": fmt_iso(now_beijing()),
        "refreshed_at": fmt_iso(refreshed_dt),
        "trade_date": trade_date,
        "requested_date": requested_date,
        "is_fallback": bool(is_fallback),
        "fallback_note": fallback_note,
        "market_phase": market_phase,
        "refreshing": False,
        "stale": {"is_stale": False, "since": None, "reason": None},
        "stats": stats,
        "themes": themes,
        "ladder": ladder,
        "broken": broken,
        "limit_down": limit_down,
        "timeline": timeline,
        "watchlist": [],
        "concepts": concept_state,
        "warnings": _dedupe(warnings),
        "log_file": log_file,
    }


_REBUILD_LOCK = threading.Lock()


def _rebuild_derived_views() -> None:
    """概念映射推进后，用上一份好数据重算题材/梯队/时间线（不发新的 akshare 请求）。"""
    with _REBUILD_LOCK:
        with _STATE_LOCK:
            if not _STATE["raw_limit_up"] and not _STATE["raw_broken"]:
                return
            base = _STATE["snapshot_base"]
            refreshed_at = base.get("refreshed_at")
        try:
            with _STATE_LOCK:
                limit_up_raw = [dict(s) for s in _STATE["raw_limit_up"]]
                broken_raw = [dict(s) for s in _STATE["raw_broken"]]
                limit_down_raw = [dict(s) for s in _STATE["raw_limit_down"]]
                prev_ok = bool(_STATE.get("prev_ok"))
                prev_pool = [dict(p) for p in _STATE["prev_pool"]] if prev_ok else None
                prev_count = _STATE["prev_count"] if prev_ok else None
            stocks, themes, ladder, timeline, broken, limit_down, concept_state = _materialize(
                limit_up_raw, broken_raw, limit_down_raw
            )
            stats = build_stats(stocks, broken, limit_down, prev_pool, prev_count)
            with _STATE_LOCK:
                current = _STATE["snapshot_base"]
                current["themes"] = themes
                current["ladder"] = ladder
                current["timeline"] = timeline
                current["broken"] = broken
                current["limit_down"] = limit_down
                current["stats"] = stats
                current["concepts"] = concept_state
                current["refreshed_at"] = refreshed_at
        except Exception:
            LOG.exception("[concept] 重算题材视图失败")


# ---------------------------------------------------------------------------
# 刷新主流程
# ---------------------------------------------------------------------------


def _do_refresh(trigger: str, started_at: float) -> None:
    """真正执行一次刷新；失败抛异常（由 perform_refresh 捕获并置 stale）。"""
    now_dt = now_beijing()
    warnings: list[str] = []

    trade_dates, calendar_source = load_trade_dates()
    if calendar_source in ("weekday_fallback", "file-stale"):
        warnings.append(f"交易日历来源降级: {calendar_source}")

    resolved = resolve_dates(now_dt, trade_dates)
    requested_date = resolved.get("calendar_date") or resolved["requested_date"]  # 展示用墙钟日
    warnings.extend(resolved.get("warnings") or [])

    # ---- 1) 涨停池：先取"有效交易日"，取不到再往前回溯（最多 10 个自然日） ----
    limit_up_df = None
    trade_date = None
    for candidate in resolved["candidates"]:
        try:
            df = ak_call(_ak().stock_zt_pool_em, date=candidate, timeout=AK_TIMEOUT_POOL)
        except Exception as exc:
            message = f"涨停池 {candidate} 拉取失败: {_short_err(exc)}"
            warnings.append(message)
            LOG.warning("[refresh] %s", message)
            continue
        if df is not None and len(df) > 0:
            trade_date = candidate
            limit_up_df = df
            break
        LOG.info("[refresh] 涨停池 %s 为空，继续回溯更早交易日", candidate)
        # 今日（已开盘）为空是合法状态：不再回溯，避免用别的交易日数据冒充今日
        if candidate == resolved["trade_date"] and not resolved["is_fallback"]:
            trade_date = candidate
            limit_up_df = df
            break

    if trade_date is None:
        raise RuntimeError("涨停池在最近 10 个自然日内均无数据")
    LOG.info("[refresh] %s: 使用交易日 %s（requested=%s, 回溯=%s）", trigger, trade_date, requested_date, trade_date != resolved["trade_date"])

    is_fallback = trade_date != requested_date
    if trade_date != resolved["trade_date"]:
        fallback_note = f"{resolved['trade_date']} 无数据，已回退到最近交易日 {trade_date}"
    else:
        fallback_note = resolved["fallback_note"] if is_fallback else None

    # ---- 2) 涨停股映射（必需列缺失 => 整体失败，走 stale） ----
    limit_up = _map_limit_up_df(limit_up_df, warnings) if (limit_up_df is not None) else []

    # ---- 3) 炸板池 / 跌停池 / 昨日涨停溢价（允许单独失败） ----
    broken: list[dict] = []
    broken_df = _fetch_optional(
        "stock_zt_pool_zbgc_em",
        lambda: ak_call(_ak().stock_zt_pool_zbgc_em, date=trade_date, timeout=AK_TIMEOUT_POOL),
        warnings,
    )
    if broken_df is not None:
        broken = _map_broken_df(broken_df, warnings)

    limit_down: list[dict] = []
    limit_down_df = _fetch_optional(
        "stock_zt_pool_dtgc_em",
        lambda: ak_call(_ak().stock_zt_pool_dtgc_em, date=trade_date, timeout=AK_TIMEOUT_POOL),
        warnings,
    )
    if limit_down_df is not None:
        limit_down = _map_limit_down_df(limit_down_df, warnings)

    prev_pool: list[dict] = []
    prev_df = _fetch_optional(
        "stock_zt_pool_previous_em",
        lambda: ak_call(_ak().stock_zt_pool_previous_em, date=trade_date, timeout=AK_TIMEOUT_POOL),
        warnings,
    )
    if prev_df is not None:
        prev_pool = _map_prev_df(prev_df, warnings)
    prev_count = len(prev_pool) if prev_df is not None else None
    prev_ok = prev_df is not None

    # ---- 4) 自选行情：只触发后台刷新，本线程绝不等待这个慢接口 ----
    _start_spot_fetch_async()

    # ---- 5) 发布：先更新原始数据，再组装快照（概念映射可能已在内存里） ----
    with _STATE_LOCK:
        _STATE["raw_limit_up"] = limit_up
        _STATE["raw_broken"] = broken
        _STATE["raw_limit_down"] = limit_down
        _STATE["prev_pool"] = prev_pool
        _STATE["prev_count"] = prev_count
        _STATE["prev_ok"] = prev_ok
        _STATE["trade_date"] = trade_date
        _STATE["requested_date"] = requested_date
        _STATE["warnings"] = _dedupe(warnings)

    base = _compose_snapshot_base(
        trade_date=trade_date,
        requested_date=requested_date,
        is_fallback=is_fallback,
        fallback_note=fallback_note,
        market_phase=resolved["market_phase"],
        warnings=warnings,
        log_file=None,
        refreshed_dt=now_dt,
    )

    # ---- 6) CSV（成功刷新才写；trade_date 为数据日；含概念，故用组装后的富集数据） ----
    log_file = append_csv(
        trade_date,
        now_dt,
        _flatten_limit_up(base),
        base["broken"],
        base["limit_down"],
    )
    base["log_file"] = log_file

    with _STATE_LOCK:
        _STATE["snapshot_base"] = base
        _STATE["last_success_dt"] = now_dt
        _STATE["last_error"] = None
        _STATE["log_file"] = log_file


def _flatten_limit_up(base: dict) -> list[dict]:
    """从快照的 ladder 里取回全部涨停股（带概念，顺序按首次封板）。"""
    stocks = []
    seen = set()
    for group in base.get("ladder") or []:
        for stock in group.get("stocks") or []:
            code = stock.get("code")
            if code and code in seen:
                continue
            seen.add(code)
            stocks.append(stock)
    stocks.sort(key=lambda s: (_first_seal_key(s), s.get("code") or ""))
    return stocks


def perform_refresh(trigger: str = "auto", wait_lock: float = 0.0) -> bool:
    """执行一次刷新（带互斥锁 + 失败保留上一份好数据）。返回是否成功。"""
    if wait_lock and wait_lock > 0:
        acquired = _REFRESH_LOCK.acquire(timeout=wait_lock)
    else:
        acquired = _REFRESH_LOCK.acquire(blocking=False)
    if not acquired:
        LOG.info("[refresh] %s: 已有刷新任务在执行，跳过本次", trigger)
        return False
    started_at = time.time()
    try:
        with _STATE_LOCK:
            _STATE["refreshing"] = True
        _do_refresh(trigger, started_at)
        with _STATE_LOCK:
            stats = dict(_STATE["snapshot_base"].get("stats") or {})
            trade_date = _STATE["trade_date"]
        LOG.info(
            "[refresh] %s 成功 trade_date=%s 涨停=%s 炸板=%s 跌停=%s 连板高度=%s 耗时=%.1fs",
            trigger,
            trade_date,
            stats.get("limit_up_count"),
            stats.get("broken_count"),
            stats.get("limit_down_count"),
            stats.get("max_streak"),
            time.time() - started_at,
        )
        return True
    except Exception as exc:
        error = _short_err(exc)
        with _STATE_LOCK:
            _STATE["last_error"] = error
        LOG.warning("[refresh] %s 失败 (%.1fs): %s（保留上一份好数据）", trigger, time.time() - started_at, error)
        return False
    finally:
        with _STATE_LOCK:
            _STATE["refreshing"] = False
        _REFRESH_LOCK.release()
        _INITIAL_DATA_READY.set()


# ---------------------------------------------------------------------------
# 快照读取（含 stale 判定 / 自选股 / is_watch 标记）
# ---------------------------------------------------------------------------


def _compute_stale(now_dt: datetime, last_success, last_error: str | None, phase: str) -> dict:
    if last_error:
        return {
            "is_stale": True,
            "since": fmt_iso(last_success) if last_success else None,
            "reason": f"akshare 拉取失败: {last_error}"[:400],
        }
    if last_success is not None and should_refresh(now_dt, is_trading_day=phase not in ("non_trading_day",)):
        age = (now_dt - _as_bj(last_success)).total_seconds()
        if age > STALE_AFTER_SEC:
            return {
                "is_stale": True,
                "since": fmt_iso(last_success),
                "reason": f"距上次成功刷新已超过 {STALE_AFTER_SEC} 秒",
            }
    return {"is_stale": False, "since": None, "reason": None}


def _limit_pct_by_board(code: str | None, name: str | None) -> float:
    """按板块/ST 推断涨跌停幅度（%），用于没有涨停池数据时的近似判定。"""
    text = (name or "").upper()
    if "ST" in text:
        return 5.0
    if code:
        if code.startswith(("300", "301", "688", "689")):
            return 20.0
        if code.startswith(("8", "4", "920")):
            return 30.0
    return 10.0


def _build_watchlist(codes, spot, limit_up_by_code, limit_down_by_code) -> list[dict]:
    """自选股实时行情列表（契约 §3 watchlist）。"""
    out = []
    for code in codes or []:
        up = limit_up_by_code.get(code) or {}
        down = limit_down_by_code.get(code) or {}
        row = spot.get(code)
        if row is None:
            out.append(
                {
                    "code": code,
                    "name": up.get("name") or down.get("name"),
                    "price": None,
                    "pct": None,
                    "is_limit_up": bool(up),
                    "is_limit_down": bool(down),
                    "in_limit_up_pool": bool(up),
                    "streak": up.get("streak"),
                    "ok": False,
                    "error": "行情数据尚未就绪（首次拉取中）" if not spot else "行情源未返回该代码（仅支持 A 股）",
                }
            )
            continue
        name = safe_str(row.get("name")) or up.get("name") or down.get("name")
        price = safe_float(row.get("price"))
        pct = safe_float(row.get("pct"))
        limit_pct = _limit_pct_by_board(code, name)
        limit_up_price = safe_float(row.get("limit_up_price"))
        limit_down_price = safe_float(row.get("limit_down_price"))
        is_limit_up = (
            bool(up)
            or (price is not None and limit_up_price is not None and price >= limit_up_price - 0.005)
            or (pct is not None and pct >= limit_pct - 0.15)
        )
        is_limit_down = (
            bool(down)
            or (price is not None and limit_down_price is not None and price <= limit_down_price + 0.005)
            or (pct is not None and pct <= -(limit_pct - 0.15))
        )
        out.append(
            {
                "code": code,
                "name": name,
                "price": price,
                "pct": round(pct, 2) if pct is not None else None,
                "is_limit_up": bool(is_limit_up),
                "is_limit_down": bool(is_limit_down),
                "in_limit_up_pool": bool(up),
                "streak": up.get("streak"),
                "ok": True,
                "error": None,
            }
        )
    return out


def _parse_watch(raw) -> list[str]:
    """解析 watch 参数：逗号/空格/换行分隔，支持 sh/sz/bj 前缀，归一化为 6 位代码。"""
    if not raw:
        return []
    codes: list[str] = []
    for part in re.split(r"[,\s;，；]+", str(raw)):
        code = normalize_code(part)
        if code and code not in codes:
            codes.append(code)
        if len(codes) >= 50:
            break
    return codes


def get_snapshot(watch_codes=None, wait_initial: bool = True) -> dict:
    """组装并返回一份可直接 JSON 化的快照（请求线程内只读缓存，不发起网络请求）。"""
    if wait_initial and not _INITIAL_DATA_READY.is_set():
        # 仅"首次加载中"时短暂等待，避免页面第一次拉取拿到空数组；超时后照常返回占位数据
        _INITIAL_DATA_READY.wait(timeout=INITIAL_WAIT_SEC)

    now_dt = now_beijing()
    with _STATE_LOCK:
        base = copy.deepcopy(_STATE["snapshot_base"])
        last_success = _STATE["last_success_dt"]
        last_error = _STATE["last_error"]
        refreshing = _STATE["refreshing"]
        spot = dict(_STATE["spot"])
        raw_limit_up = [dict(s) for s in _STATE["raw_limit_up"]]
        raw_limit_down = [dict(s) for s in _STATE["raw_limit_down"]]

    resolved = resolve_dates(now_dt, _calendar_dates_for_request())
    base["generated_at"] = fmt_iso(now_dt)
    base["requested_date"] = resolved.get("calendar_date") or resolved["requested_date"]
    base["market_phase"] = resolved["market_phase"]
    base["refreshing"] = bool(refreshing)
    base["stale"] = _compute_stale(now_dt, last_success, last_error, resolved["market_phase"])

    _, _, concept_state = _concept_snapshot()
    base["concepts"] = concept_state

    limit_up_by_code = {s.get("code"): s for s in raw_limit_up if s.get("code")}
    limit_down_by_code = {s.get("code"): s for s in raw_limit_down if s.get("code")}
    watch_codes = list(watch_codes or [])
    if watch_codes:
        # 记录用户关注代码；后台/一次性线程用单只行情接口补齐（不阻塞本请求）
        _record_watch_codes(watch_codes)
        try:
            _ensure_watch_quotes_async()
        except Exception:  # pragma: no cover - 保底
            LOG.exception("[watch] 触发自选股快照失败")
    base["watchlist"] = _build_watchlist(watch_codes, spot, limit_up_by_code, limit_down_by_code)

    if watch_codes:
        watch_set = set(watch_codes)
        for collection in (base.get("themes") or [], base.get("ladder") or []):
            for group in collection:
                for stock in group.get("stocks") or []:
                    stock["is_watch"] = stock.get("code") in watch_set

    return base


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------

try:
    from flask import Flask, Response, request
except Exception as _flask_exc:  # pragma: no cover - flask 未安装时给出清晰错误
    raise SystemExit(f"缺少依赖 flask，请先 pip install -r requirements.txt（{_flask_exc}）")

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False


def _json_response(payload, status: int = 200) -> Response:
    """安全 JSON 响应：保证可被 ``json.dumps(..., allow_nan=False)`` 序列化。"""
    try:
        text = json.dumps(sanitize_json(payload), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # 兜底：绝不让接口 500
        LOG.exception("[api] JSON 序列化失败")
        text = json.dumps(
            {
                "ok": False,
                "error": f"序列化失败: {_short_err(exc)}",
                "generated_at": fmt_iso(now_beijing()),
            },
            ensure_ascii=False,
        )
    response = Response(text, status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def _read_page_html():
    try:
        return PAGE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as exc:
        LOG.warning("[api] 读取 page.html 失败: %s", _short_err(exc))
        return None


# ---------------------------------------------------------------------------
# 公网访问保护：可选访问码 + 每 IP 限流 + 强制刷新冷却（默认全关）
# 本地使用不受影响；云端公开只读部署时通过环境变量开启。
# ---------------------------------------------------------------------------

_ACCESS_COOKIE = "limitup_code"
_GUARD_LOCK = threading.Lock()
_RATE_HITS: dict = {}        # ip -> list[float]，最近 60 秒的请求时间戳
_RATE_LAST_LOG: dict = {}    # ip -> 上次打印限流日志的时间
_REFRESH_LAST: dict = {}     # ip -> 上次手动刷新时间


def _client_ip() -> str:
    """真实客户端 IP；只有显式信任反代时才读 X-Forwarded-For。"""
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def _rate_limited(ip: str) -> bool:
    """滑动窗口限流；RATE_LIMIT_PER_MIN<=0 时不启用。"""
    if RATE_LIMIT_PER_MIN <= 0:
        return False
    now = time.time()
    with _GUARD_LOCK:
        hits = _RATE_HITS.setdefault(ip, [])
        while hits and now - hits[0] > 60.0:
            hits.pop(0)
        if len(hits) >= RATE_LIMIT_PER_MIN:
            last = _RATE_LAST_LOG.get(ip, 0.0)
            if now - last > 60.0:
                _RATE_LAST_LOG[ip] = now
                LOG.warning("[guard] 限流：%s 超过 %d 次/分钟", ip, RATE_LIMIT_PER_MIN)
            return True
        hits.append(now)
        if len(_RATE_HITS) > 5000:  # 防内存膨胀（异常流量兜底）
            _RATE_HITS.clear()
        return False


def _access_code_ok() -> bool:
    """未设置访问码时恒为真；否则参数 / 请求头 / Cookie 三者任一匹配即可。"""
    if not ACCESS_CODE:
        return True
    provided = (
        request.args.get("code")
        or request.headers.get("X-Limitup-Code")
        or request.cookies.get(_ACCESS_COOKIE)
        or ""
    ).strip()
    try:
        return hmac.compare_digest(provided.encode("utf-8"), ACCESS_CODE.encode("utf-8"))
    except Exception:  # pragma: no cover - 兜底
        return False


@app.before_request
def _guard_public_access():
    """访问码 / 限流 / 手动刷新冷却。默认全部关闭。"""
    path = request.path
    if path == "/api/health":  # 健康检查始终放行（负载均衡/监控用）
        return None
    ip = _client_ip()
    if not _access_code_ok():
        LOG.info("[guard] 拒绝无访问码请求：%s %s", ip, path)
        if path.startswith("/api/"):
            return _json_response(
                {"ok": False, "error": "需要访问码：请在链接后加 ?code=你的访问码"},
                401,
            )
        return Response(
            "需要访问码：请在网址后加 ?code=你的访问码 再打开（例如 http://你的域名/?code=xxx）",
            status=401,
            mimetype="text/plain",
        )
    if _rate_limited(ip):
        resp: Response
        if path.startswith("/api/"):
            resp = _json_response({"ok": False, "error": "请求过于频繁，请稍后再试"}, 429)
        else:
            resp = Response("请求过于频繁，请稍后再试", status=429, mimetype="text/plain")
        resp.headers["Retry-After"] = "60"
        return resp
    if path == "/api/refresh" and REFRESH_COOLDOWN_SEC > 0:
        now = time.time()
        with _GUARD_LOCK:
            last = _REFRESH_LAST.get(ip, 0.0)
            if now - last < REFRESH_COOLDOWN_SEC:
                wait = int(REFRESH_COOLDOWN_SEC - (now - last)) + 1
                LOG.info("[guard] 手动刷新冷却：%s 还需 %ds", ip, wait)
                return _json_response(
                    {"ok": False, "error": f"刷新太频繁，请 {wait} 秒后再试", "retry_after": wait},
                    429,
                )
            _REFRESH_LAST[ip] = now
    return None


@app.after_request
def _guard_response_headers(response: Response) -> Response:
    """安全响应头；访问码通过 URL 首次验证后写 HttpOnly Cookie，后续请求免带 code。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if ACCESS_CODE and request.args.get("code") and _access_code_ok():
        response.set_cookie(
            _ACCESS_COOKIE,
            ACCESS_CODE,
            httponly=True,
            samesite="Lax",
            max_age=60 * 60 * 24 * 30,
        )
    return response


@app.get("/")
def index():
    html = _read_page_html()
    if html is None:
        return Response(
            "page.html 未找到（前端页面尚未生成）",
            status=404,
            mimetype="text/plain",
        )
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/page.html")
def page_html():
    return index()


@app.get("/api/health")
def api_health():
    """健康检查：不触发任何 akshare 调用。"""
    return _json_response({"ok": True, "uptime_sec": round(time.time() - START_TS, 3)})


@app.get("/api/snapshot")
def api_snapshot():
    watch = _parse_watch(request.args.get("watch"))
    return _json_response(get_snapshot(watch, wait_initial=True))


@app.get("/api/refresh")
def api_refresh():
    """强制刷新一次（同步等待，最多约 30s），返回与 /api/snapshot 相同结构。"""
    watch = _parse_watch(request.args.get("watch"))
    try:
        worker = threading.Thread(
            target=perform_refresh,
            kwargs={"trigger": "manual", "wait_lock": 25.0},
            name="manual-refresh",
            daemon=True,
        )
        worker.start()
        worker.join(30.0)
        if worker.is_alive():
            LOG.warning("[refresh] 手动刷新超过 30s 未完成，先返回当前快照")
    except Exception:
        LOG.exception("[refresh] 手动刷新异常")
    return _json_response(get_snapshot(watch, wait_initial=True))


# ---------------------------------------------------------------------------
# 后台线程启动 / main
# ---------------------------------------------------------------------------


def _initial_load() -> None:
    """启动时的第一次拉取（在后台线程里跑，HTTP 服务不等它）。"""
    try:
        LOG.info("[startup] 初始数据拉取开始（akshare 免费源）")
        perform_refresh(trigger="startup", wait_lock=10.0)
        _INITIAL_DATA_READY.set()
        if DISABLE_BG:
            # 无后台周期线程时，仍拉一次自选行情（一次性，非周期刷新）
            _fetch_spot_once()
    except Exception:
        LOG.exception("[startup] 初始数据拉取异常")
        _INITIAL_DATA_READY.set()
    finally:
        _INITIAL_DONE.set()


def _refresh_loop() -> None:
    """定时刷新线程：仅 09:25–11:30 / 13:00–15:00 每 60s 一次。"""
    last_attempt = 0.0
    while True:
        try:
            now_dt = now_beijing()
            trade_dates, _ = load_trade_dates()
            day_set = set(trade_dates)
            is_trading_day = _infer_is_trading_day(now_dt.date(), day_set)
            if should_refresh(now_dt, is_trading_day=is_trading_day):
                if now_dt.timestamp() - last_attempt >= REFRESH_INTERVAL_SEC:
                    last_attempt = now_dt.timestamp()
                    perform_refresh(trigger="scheduled", wait_lock=0.0)
                    _start_spot_fetch_async()
        except Exception:
            LOG.exception("[refresh] 定时刷新线程异常")
        time.sleep(2)


def start_background_work() -> None:
    """启动后台线程（幂等）。``LIMITUP_DISABLE_BG=1`` 时只做一次初始化拉取。"""
    global _THREADS_STARTED
    with _START_LOCK:
        if _THREADS_STARTED:
            return
        _THREADS_STARTED = True

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DISABLE_BG:
        _update_concept_state(ready=False, loaded=0, total=0, note="概念映射未启用（LIMITUP_DISABLE_BG=1）")

    threading.Thread(target=_initial_load, name="initial-load", daemon=True).start()
    if not DISABLE_BG:
        threading.Thread(target=_refresh_loop, name="refresh-loop", daemon=True).start()
        threading.Thread(target=_spot_loop, name="spot-loop", daemon=True).start()
        threading.Thread(target=_watch_quote_loop, name="watch-quote-loop", daemon=True).start()
        threading.Thread(target=_concept_worker, name="concept-worker", daemon=True).start()


def main() -> None:
    global START_TS
    START_TS = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    LOG.info(
        "[startup] A股涨停直播看板启动 host=%s port=%s background=%s fake_fail=%s data_dir=%s python=%s",
        HOST,
        PORT,
        "off" if DISABLE_BG else "on",
        FAKE_FAIL,
        DATA_DIR,
        sys.version.split()[0],
    )
    start_background_work()
    LOG.info("[startup] HTTP 服务就绪: http://%s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
