"""Contract tests for the A-share limit-up tracker backend (`app.py`).

Written against `CONTRACT.md` §3 (snapshot JSON), §4 (backend behaviour) and §6
(acceptance items 7 & 8).  The backend is implemented by a different agent; the
contract fixes the *names* of the pure functions (§4.6) but not their internal
argument conventions.  These tests therefore import `app` defensively and probe a
small set of plausible calling conventions per function.  When `app.py` is missing
or not importable yet (deps still installing) every test skips instead of failing,
so the suite stays collectable while the backend is being built.

Run from the project root:

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
"""

from __future__ import annotations

import csv
import importlib
import inspect
import json
import math
import os
import re
import signal
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
PAGE_PATH = PROJECT_ROOT / "page.html"
DATA_DIR = PROJECT_ROOT / "data"

# CONTRACT §4.5 — exact CSV header.
CSV_HEADER = (
    "timestamp,code,name,status,streak,first_seal,seal_amount,break_times,concepts"
)

# CONTRACT §4.6 — pure, unit-testable functions.
PURE_FUNCTIONS = [
    "pick_col",
    "map_limit_up_row",
    "resolve_dates",
    "build_stats",
    "group_themes",
    "build_ladder",
    "compute_phase",
    "should_refresh",
    "format_hms",
    "to_yi",
]

SIX_DIGIT = re.compile(r"^\d{6}$")
PHASES = {"pre_open", "auction", "trading", "lunch", "closed", "non_trading_day"}

try:  # Asia/Shanghai zone for deterministic "Beijing time" fixtures
    from zoneinfo import ZoneInfo

    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    CN_TZ = None


def _cst(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A datetime in Beijing time (naive if tz db unavailable)."""
    naive = datetime(year, month, day, hour, minute)
    return naive.replace(tzinfo=CN_TZ) if CN_TZ else naive


# --------------------------------------------------------------------------- #
# app import fixture
# --------------------------------------------------------------------------- #


class _ImportTimeout(Exception):
    pass


def _on_alarm(signum, frame):  # pragma: no cover - safety net only
    raise _ImportTimeout("timed out importing app.py (>30s)")


@pytest.fixture(scope="module")
def app():
    """Import app.py with LIMITUP_DISABLE_BG=1 already set (see conftest.py)."""
    if not APP_PATH.exists():
        pytest.skip(f"app.py not found at {APP_PATH} — backend has not landed yet")
    if "app" in sys.modules and getattr(sys.modules["app"], "__file__", "") == str(
        APP_PATH
    ):
        return sys.modules["app"]
    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(30)
    except (ValueError, AttributeError):  # not in main thread — skip the guard
        old_handler = None
    try:
        module = importlib.import_module("app")
    except _ImportTimeout:
        pytest.skip("importing app.py exceeded 30s — refusing to hang the suite")
    except Exception as exc:  # deps missing / half-written file
        pytest.skip(f"app.py present but not importable yet ({exc!r})")
    finally:
        if old_handler is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    return module


# --------------------------------------------------------------------------- #
# generic helpers
# --------------------------------------------------------------------------- #


def _param_names(fn) -> list[str]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover
        return []
    return [
        p.name
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]


def _kw_candidates(fn, mapping):
    """Map fn parameter names to values via substring aliases; return kwargs dict."""
    kwargs = {}
    for name in _param_names(fn):
        low = name.lower()
        for aliases, value in mapping:
            if any(alias in low for alias in aliases):
                kwargs[name] = value
                break
    return kwargs


def _call(fn, candidates):
    """Call `fn` trying (args_tuple, kwargs_dict) candidates in order.

    Returns the first successful result.  A candidate raising anything is recorded
    and the next convention is attempted; if all fail an informative AssertionError
    is raised (this is an adaptive harness, see module docstring).
    """
    errors = []
    for args, kwargs in candidates:
        try:
            return fn(*args, **kwargs)
        except ImportError as exc:  # dependency not installed yet
            pytest.skip(f"{fn.__name__} needs a missing dependency: {exc}")
        except Exception as exc:  # wrong convention or real bug — try next shape
            errors.append(f"  {fn.__name__}(*{args!r}, **{kwargs!r}) -> {exc!r}")
    sig = ""
    try:
        sig = str(inspect.signature(fn))
    except (TypeError, ValueError):  # pragma: no cover
        pass
    raise AssertionError(
        f"could not call {fn.__name__}{sig} with any known convention:\n"
        + "\n".join(errors)
    )


def _require(app, *names):
    for name in names:
        fn = getattr(app, name, None)
        if callable(fn):
            return fn
    pytest.fail(
        f"app.py exists but none of {names} is callable — CONTRACT §4.6 requires these "
        f"pure functions"
    )


def _optional(app, *names):
    for name in names:
        fn = getattr(app, name, None)
        if callable(fn):
            return fn
    return None


def _input_variants(values):
    """Return the given pools as-is plus pandas.DataFrame versions when possible."""
    variants = [tuple(values)]
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return variants
    frames = []
    for value in values:
        if isinstance(value, (list, tuple)):
            frames.append(pd.DataFrame(list(value)))
        else:
            frames.append(value)
    variants.append(tuple(frames))
    return variants


# ---- result normalisation -------------------------------------------------- #


def _as_yyyymmdd(value):
    keys = ("requested_date", "requested", "date", "trade_date", "value")
    if value is None:
        return None
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                got = _as_yyyymmdd(value[key])
                if got:
                    return got
        return None
    if isinstance(value, (tuple, list)):
        for item in value:
            got = _as_yyyymmdd(item)
            if got and len(got) == 8 and got.isdigit():
                return got
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    if isinstance(value, int):
        return f"{value:08d}" if value < 10**8 else str(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("-", "").replace("/", "")
        return cleaned if len(cleaned) == 8 and cleaned.isdigit() else None
    return None


def _as_list(value, keys=("themes", "groups", "ladder", "data", "rows")):
    if isinstance(value, dict):
        for key in keys:
            if key in value and isinstance(value[key], (list, tuple)):
                return list(value[key])
    if isinstance(value, (list, tuple)):
        return list(value)
    raise AssertionError(f"expected a list or dict with one of {keys}, got {value!r}")


def _as_stats(value):
    if isinstance(value, dict):
        if isinstance(value.get("stats"), dict):
            return value["stats"]
        return value
    raise AssertionError(f"build_stats returned {value!r}, expected a dict")


def _as_phase(value):
    if isinstance(value, dict):
        for key in ("market_phase", "phase", "value"):
            if key in value:
                return _as_phase(value[key])
        raise AssertionError(f"compute_phase returned a dict without a phase key: {value!r}")
    return str(value).strip().lower()


def _assert_six_digit_codes(obj):
    """Walk a JSON-ish structure; every value under key 'code' must be 6 digits."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "code":
                assert isinstance(value, str) and SIX_DIGIT.match(value), (
                    f"code must be a 6-digit string, got {value!r}"
                )
            else:
                _assert_six_digit_codes(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _assert_six_digit_codes(item)


# --------------------------------------------------------------------------- #
# synthetic pools
# --------------------------------------------------------------------------- #


def _stock(code, name, streak, first_seal, concepts, industry="通用设备", **extra):
    stock = {
        "code": code,
        "name": name,
        "streak": streak,
        "first_seal": first_seal,
        "last_seal": None,
        "seal_amount_yuan": 123456789.0,
        "seal_amount_yi": 1.23,
        "break_times": 0,
        "turnover": 12.3,
        "float_cap_yuan": 12345678901.0,
        "float_cap_yi": 123.46,
        "industry": industry,
        "concepts": list(concepts),
        "is_watch": False,
    }
    stock.update(extra)
    return stock


def _limit_up_rows():
    # 8 rows, max streak = 5, one theme tie resolved by earliest first_seal.
    return [
        _stock("600001", "甲股", 3, "09:25:00", ["机器人"]),
        _stock("600002", "乙股", 5, "09:31:00", ["机器人"]),
        _stock("600003", "丙股", 2, "09:40:00", ["固态电池"]),
        _stock("600004", "丁股", 1, "09:30:00", ["固态电池"]),
        _stock("600005", "戊股", 4, "09:35:00", ["算力"]),
        _stock("600006", "己股", 1, "10:02:00", ["机器人"]),
        _stock("600007", "庚股", 2, "09:26:00", ["算力"]),
        _stock("600008", "辛股", 2, "14:59:00", ["机器人"]),
    ]


def _broken_rows():
    return [
        {
            "code": "600101",
            "name": "炸一",
            "concepts": ["机器人"],
            "industry": "通用设备",
            "high_pct": 10.01,
            "current_pct": 3.21,
            "break_times": 2,
            "first_seal": "09:31:00",
            "break_time": "10:23:45",
        },
        {
            "code": "600102",
            "name": "炸二",
            "concepts": [],
            "industry": "软件开发",
            "high_pct": 9.98,
            "current_pct": 1.5,
            "break_times": 1,
            "first_seal": None,
            "break_time": None,
        },
    ]


def _limit_down_rows():
    return [
        {
            "code": "600201",
            "name": "跌一",
            "pct": -9.98,
            "concepts": [],
            "industry": "煤炭",
        }
    ]


def _previous_rows():
    # 昨日涨停今日平均涨跌幅 = (2.0 - 1.0 + 4.0) / 3 = 1.6667%
    return [
        {"code": "600301", "name": "昨一", "pct": 2.0},
        {"code": "600302", "name": "昨二", "pct": -1.0},
        {"code": "600303", "name": "昨三", "pct": 4.0},
    ]


# --------------------------------------------------------------------------- #
# 0. module-level contract: import mode + required pure functions
# --------------------------------------------------------------------------- #


def test_app_imports_with_bg_disabled_and_has_pure_functions(app):
    assert os.environ.get("LIMITUP_DISABLE_BG") == "1"
    assert Path(app.__file__).resolve() == APP_PATH.resolve()
    missing = [name for name in PURE_FUNCTIONS if not callable(getattr(app, name, None))]
    assert not missing, f"missing contract pure functions (§4.6): {missing}"


# --------------------------------------------------------------------------- #
# 1. pick_col — defensive keyword column mapping (§4.2, §6.8)
# --------------------------------------------------------------------------- #


def _pick_col(app, columns, keywords):
    fn = _require(app, "pick_col")
    columns = list(columns)
    keywords = list(keywords)
    candidates = [
        ((columns, keywords), {}),
        ((tuple(columns), tuple(keywords)), {}),
        ((columns, keywords[0]), {}),
    ]
    kwargs = _kw_candidates(
        fn,
        [
            (("column", "col", "cols", "field", "header"), columns),
            (("keyword", "key", "name", "target", "want", "pattern"), keywords),
        ],
    )
    if kwargs:
        candidates.append(((), kwargs))
    # DataFrame-first signature support
    try:
        import pandas as pd

        frame = pd.DataFrame({column: [None] for column in columns})
        candidates.append(((frame, keywords), {}))
    except ImportError:  # pragma: no cover
        pass
    errors = []
    for args, kwargs in candidates:
        try:
            got = fn(*args, **kwargs)
        except Exception as exc:
            errors.append(f"  pick_col(*{args!r}, **{kwargs!r}) -> {exc!r}")
            continue
        if got in columns:
            return got
        errors.append(f"  pick_col(...) returned {got!r}, not one of the columns")
    raise AssertionError("pick_col mapping failed:\n" + "\n".join(errors))


def test_pick_col_keyword_mapping_renamed_columns(app):
    columns = [
        "股票代码",
        "证券名称",
        "连板数",
        "首次封板时间",
        "最后封板时间",
        "封板资金",
        "炸板次数",
        "所属行业",
        "换手率",
        "流通市值",
    ]
    assert _pick_col(app, columns, ["代码"]) == "股票代码"
    assert _pick_col(app, columns, ["名称"]) == "证券名称"
    assert _pick_col(app, columns, ["连板"]) == "连板数"
    assert _pick_col(app, columns, ["首次封板时间"]) == "首次封板时间"
    # English/renamed source columns matched by keyword, case-insensitively (§4.2)
    assert _pick_col(app, ["Code", "Name", "Streak"], ["代码", "code"]) == "Code"


def test_pick_col_case_space_and_dash_insensitive(app):
    # §4.2: 大小写 / 空格 / '-' 不敏感
    assert _pick_col(app, [" CODE "], ["code"]) == " CODE "
    assert _pick_col(app, ["股票 名称 "], ["名称"]) == "股票 名称 "
    assert _pick_col(app, ["首次封板-时间"], ["封板时间"]) == "首次封板-时间"


def test_pick_col_unknown_returns_falsey_without_crashing(app):
    fn = _require(app, "pick_col")
    candidates = [
        ((["神秘列"], ["代码"]), {}),
        ((["神秘列"], ["代码"], False), {}),
    ]
    kwargs = _kw_candidates(
        fn,
        [
            (("column", "col", "cols", "field", "header"), ["神秘列"]),
            (("keyword", "key", "name", "target", "want", "pattern"), ["代码"]),
        ],
    )
    if kwargs:
        candidates.append(((), kwargs))
    fatal = []
    for args, kwargs in candidates:
        try:
            got = fn(*args, **kwargs)
        except Exception as exc:
            fatal.append(repr(exc))
            continue
        assert not got, f"unknown column must not resolve, got {got!r} (§4.2)"
        return
    pytest.fail(f"pick_col crashed on unknown columns instead of returning nothing: {fatal}")


# --------------------------------------------------------------------------- #
# 2. map_limit_up_row — renamed Chinese columns still map (§4.2, §6.8)
# --------------------------------------------------------------------------- #


RAW_LIMIT_UP_ROW = {
    "代码": "600519",
    "名称": "贵州茅台",
    "连板数": 3,
    "首次封板时间": "09:25:00",
    "最后封板时间": "10:11:00",
    "封板资金": 123456789.0,
    "炸板次数": 1,
    "所属行业": "白酒",
    "换手率": 12.3,
    "流通市值": 12345678901.0,
}

RENAMED_LIMIT_UP_ROW = {
    "股票代码": "000001",
    "证券名称": "平安银行",
    "连板": 2,
    "首次封板时间": "09:31:00",
    "最后封板时间": "13:05:00",
    "封板资金": 987654321.0,
    "炸板次数": 0,
    "行业": "银行",
    "换手率": 3.21,
    "流通市值": 234567890123.0,
}


def _map_one_row(app, row):
    fn = _require(app, "map_limit_up_row")
    candidates = [((row,), {}), ((row, list(row.keys())), {}), ((row, {}), {})]
    try:
        import pandas as pd

        candidates.insert(0, ((pd.Series(row),), {}))
        candidates.insert(0, ((pd.DataFrame([row]),), {}))
    except ImportError:  # pragma: no cover
        pass
    errors = []
    for args, kwargs in candidates:
        try:
            got = fn(*args, **kwargs)
        except Exception as exc:
            errors.append(f"  map_limit_up_row(*{type(args[0]).__name__}) -> {exc!r}")
            continue
        if isinstance(got, dict):
            return got
        errors.append(f"  map_limit_up_row returned {type(got).__name__}, expected dict")
    raise AssertionError("map_limit_up_row failed:\n" + "\n".join(errors))


def test_map_limit_up_row_contract_fields(app):
    mapped = _map_one_row(app, dict(RAW_LIMIT_UP_ROW))
    assert mapped["code"] == "600519"
    assert SIX_DIGIT.match(mapped["code"])
    assert mapped["name"] == "贵州茅台"
    assert mapped["streak"] == 3
    assert mapped["first_seal"] == "09:25:00"
    assert mapped["last_seal"] == "10:11:00"
    assert mapped["seal_amount_yuan"] == pytest.approx(123456789.0)
    assert mapped["seal_amount_yi"] == pytest.approx(1.23)
    assert mapped["break_times"] == 1
    assert mapped["turnover"] == pytest.approx(12.3)
    assert mapped["float_cap_yuan"] == pytest.approx(12345678901.0)
    assert mapped["float_cap_yi"] == pytest.approx(123.46)
    assert mapped["industry"] == "白酒"
    assert isinstance(mapped["concepts"], list)


def test_map_limit_up_row_renamed_columns(app):
    mapped = _map_one_row(app, dict(RENAMED_LIMIT_UP_ROW))
    assert mapped["code"] == "000001"
    assert SIX_DIGIT.match(mapped["code"])
    assert mapped["name"] == "平安银行"
    assert mapped["streak"] == 2
    assert mapped["industry"] == "银行"
    # No invented data: numeric fields must be numbers or None, never "nan"/"-"/"".
    for value in mapped.values():
        assert not (isinstance(value, float) and math.isnan(value))


# --------------------------------------------------------------------------- #
# 3. resolve_dates — trading-date fallback (§4.1, §6.2)
# --------------------------------------------------------------------------- #

CAL_STRINGS = ["20261102", "20261103", "20261104", "20261105", "20261106"]
CAL_DATES = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]


def _resolve(app, now, cal_strings=CAL_STRINGS):
    fn = _require(app, "resolve_dates")
    cal_dates = [datetime.strptime(day, "%Y%m%d").date() for day in cal_strings]
    candidates = []
    for cal in (cal_strings, cal_dates):
        for moment in (now, now.replace(tzinfo=None) if now.tzinfo else now):
            candidates.append(((moment, cal), {}))
            candidates.append(((cal, moment), {}))
    candidates.append(((now,), {}))
    for cal in (cal_strings, cal_dates):
        for moment in (now, now.replace(tzinfo=None) if now.tzinfo else now):
            kwargs = _kw_candidates(
                fn,
                [
                    (("now", "current", "dt", "time", "today"), moment),
                    (("calendar", "trade_date", "trading_day", "dates", "days"), cal),
                ],
            )
            if kwargs:
                candidates.append(((), kwargs))
    result = _call(fn, candidates)
    requested = _as_yyyymmdd(result)
    if requested is None:
        raise AssertionError(f"resolve_dates returned no YYYYMMDD date: {result!r}")
    return requested


def test_resolve_dates_pre_open_uses_previous_trade_date(app):
    # Thursday 2026-11-04 08:00 Beijing (before 09:25) -> previous opened day 11-03.
    assert _resolve(app, _cst(2026, 11, 4, 8, 0)) == "20261103"


def test_resolve_dates_non_trading_day_falls_back(app):
    # Saturday 2026-11-07 -> Friday 2026-11-06.
    assert _resolve(app, _cst(2026, 11, 7, 12, 0)) == "20261106"


def test_resolve_dates_intraday_uses_today(app):
    # Thursday 2026-11-04 10:00 (after 09:25, trading day) -> same day.
    assert _resolve(app, _cst(2026, 11, 4, 10, 0)) == "20261104"


def test_resolve_dates_monday_pre_open_falls_back_to_friday(app):
    cal = CAL_STRINGS + ["20261109"]
    # Monday 2026-11-09 08:30 -> Friday 2026-11-06.
    assert _resolve(app, _cst(2026, 11, 9, 8, 30), cal_strings=cal) == "20261106"


# --------------------------------------------------------------------------- #
# 4. build_stats — 炸板率 / max streak / premium / null denominators (§3)
# --------------------------------------------------------------------------- #


def _build_stats(app, limit_up, broken, limit_down, previous):
    fn = _require(app, "build_stats")
    pool_variants = [limit_up, broken, limit_down, previous]
    candidates = []
    for conv in _input_variants(pool_variants):
        candidates.append((tuple(conv), {}))
    for conv in _input_variants(pool_variants):
        kwargs = _kw_candidates(
            fn,
            [
                (("limit_up", "zt", "up"), conv[0]),
                (("broken", "zb", "break"), conv[1]),
                (("limit_down", "dt", "down"), conv[2]),
                (("previous", "prev", "yesterday"), conv[3]),
            ],
        )
        if len(kwargs) >= 2:
            candidates.append(((), kwargs))
    candidates.append(((limit_up, broken, limit_down), {}))
    candidates.append(((limit_up, broken), {}))
    candidates.append(
        (
            (
                {
                    "limit_up": limit_up,
                    "broken": broken,
                    "limit_down": limit_down,
                    "previous": previous,
                },
            ),
            {},
        )
    )
    return _as_stats(_call(fn, candidates))


def test_build_stats_metrics(app):
    stats = _build_stats(app, _limit_up_rows(), _broken_rows(), _limit_down_rows(), _previous_rows())
    assert stats["limit_up_count"] == 8
    assert stats["broken_count"] == 2
    assert stats["limit_down_count"] == 1
    assert stats["broken_rate"] == pytest.approx(2 / (8 + 2), abs=1e-9)
    assert stats["max_streak"] == 5
    assert stats["prev_limit_up_premium"] == pytest.approx(5 / 3, abs=1e-6)
    assert stats["prev_limit_up_count"] == 3


def test_build_stats_zero_denominator_is_null(app):
    stats = _build_stats(app, [], [], [], [])
    assert stats["limit_up_count"] == 0
    assert stats["broken_count"] == 0
    assert stats["broken_rate"] is None  # §3: 分母 0 时为 null
    assert stats["max_streak"] == 0
    assert stats["prev_limit_up_premium"] is None  # 不可用=null
    assert stats["prev_limit_up_count"] == 0


# --------------------------------------------------------------------------- #
# 5. group_themes — ordering / hottest concepts / industry fallback (§4.3)
# --------------------------------------------------------------------------- #


def _group_themes(app, rows, concept_map=None, concept_list=None):
    fn = _require(app, "group_themes")
    concept_map = concept_map or {}
    concept_list = concept_list or []
    concept_rank_dict = {}
    for item in concept_list:
        if isinstance(item, dict):
            concept_rank_dict[item.get("name")] = item.get("pct")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            concept_rank_dict[item[0]] = item[1]
    candidates = [
        ((rows, concept_map, concept_list), {}),
        ((rows, concept_map, concept_rank_dict), {}),
        ((rows, concept_map), {}),
        ((rows,), {}),
    ]
    for conv in _input_variants((rows,)):
        candidates.append(((conv[0], concept_map, concept_list), {}))
    kwargs = _kw_candidates(
        fn,
        [
            (("rows", "stocks", "limit_up", "items"), rows),
            (("concept_map", "mapping", "stock_concept", "map"), concept_map),
            (("concept_list", "concept_rank", "rank", "concepts", "boards"), concept_list),
        ],
    )
    if kwargs:
        candidates.append(((), kwargs))
    return _as_list(_call(fn, candidates))


def _all_theme_stocks(themes):
    stocks = {}
    for theme in themes:
        for stock in theme.get("stocks") or []:
            stocks[stock.get("code")] = stock
    return stocks


def test_group_themes_ordering(app):
    rows = [
        _stock("600001", "甲股", 2, "09:25:00", ["机器人"]),
        _stock("600002", "乙股", 1, "09:40:00", ["机器人"]),
        _stock("600003", "丙股", 2, "09:30:00", ["固态电池"]),
        _stock("600004", "丁股", 1, "09:35:00", ["固态电池"]),
        _stock("600005", "戊股", 1, "09:25:00", ["算力"]),
    ]
    concept_map = {
        "600001": ["机器人"],
        "600002": ["机器人"],
        "600003": ["固态电池"],
        "600004": ["固态电池"],
        "600005": ["算力"],
    }
    concept_list = [
        {"name": "机器人", "pct": 5.0},
        {"name": "固态电池", "pct": 3.0},
        {"name": "算力", "pct": 2.0},
    ]
    themes = _group_themes(app, rows, concept_map, concept_list)
    names = [theme.get("name") for theme in themes]
    counts = [theme.get("count") for theme in themes]
    # count desc; tie on count -> earliest first_seal asc (机器人 09:25 vs 固态电池 09:30)
    assert names == ["机器人", "固态电池", "算力"], names
    assert counts == [2, 2, 1], counts
    for theme in themes:
        seals = [stock.get("first_seal") for stock in theme.get("stocks") or []]
        assert seals == sorted(seals), f"{theme.get('name')} stocks not first_seal asc: {seals}"
    assert sum(counts) == len(rows)


def test_group_themes_industry_fallback(app):
    rows = [
        _stock("600010", "甲股", 1, "09:25:00", [], industry="通用设备"),
        _stock("600011", "乙股", 1, "09:26:00", [], industry="通用设备"),
        _stock("600012", "丙股", 2, "09:30:00", [], industry="软件开发"),
    ]
    themes = _group_themes(app, rows, {}, [])
    names = {theme.get("name") for theme in themes}
    assert names == {"通用设备", "软件开发"}, names
    assert all(theme.get("is_industry_fallback") for theme in themes)
    assert sum(theme.get("count") or 0 for theme in themes) == 3


def test_group_themes_at_most_two_hottest_concepts(app):
    rows = [
        _stock("600021", "甲股", 1, "09:25:00", ["机器人", "减速器", "固态电池"]),
        _stock("600022", "乙股", 1, "09:26:00", ["机器人", "减速器"]),
        _stock("600023", "丙股", 1, "09:27:00", ["机器人"]),
    ]
    concept_map = {
        "600021": ["机器人", "减速器", "固态电池"],
        "600022": ["机器人", "减速器"],
        "600023": ["机器人"],
    }
    # 机器人 appears in 3 limit-ups, 减速器 in 2, 固态电池 in 1 -> hottest two keep order.
    concept_list = [
        {"name": "机器人", "pct": 5.0},
        {"name": "减速器", "pct": 3.0},
        {"name": "固态电池", "pct": 2.0},
    ]
    themes = _group_themes(app, rows, concept_map, concept_list)
    stocks = _all_theme_stocks(themes)
    assert "600021" in stocks, f"stock 600021 missing from themes: {[t.get('name') for t in themes]}"
    for code, stock in stocks.items():
        concepts = stock.get("concepts") or []
        assert len(concepts) <= 2, f"{code} shows {len(concepts)} concepts, contract max is 2"
    hottest = stocks["600021"]["concepts"]
    assert "机器人" in hottest
    assert "固态电池" not in hottest, f"coldest concept should be dropped, got {hottest}"


# --------------------------------------------------------------------------- #
# 6. build_ladder — streak desc, stocks first_seal asc (§3)
# --------------------------------------------------------------------------- #


def test_build_ladder_ordering(app):
    fn = _require(app, "build_ladder")
    rows = [
        _stock("600001", "甲股", 1, "09:25:00", []),
        _stock("600002", "乙股", 3, "10:00:00", []),
        _stock("600003", "丙股", 2, "09:40:00", []),
        _stock("600004", "丁股", 3, "09:31:00", []),
        _stock("600005", "戊股", 5, "14:00:00", []),
    ]
    candidates = [((rows,), {}), ((rows, None), {})]
    kwargs = _kw_candidates(
        fn, [(("rows", "stocks", "limit_up", "items"), rows)]
    )
    if kwargs:
        candidates.append(((), kwargs))
    ladder = _as_list(_call(fn, candidates))
    streaks = [group.get("streak") for group in ladder]
    assert streaks == [5, 3, 2, 1], streaks
    three = next(group for group in ladder if group.get("streak") == 3)
    codes = [stock.get("code") for stock in three.get("stocks") or []]
    assert codes == ["600004", "600002"], codes  # 09:31 before 10:00
    for group in ladder:
        seals = [stock.get("first_seal") for stock in group.get("stocks") or []]
        assert seals == sorted(seals)


# --------------------------------------------------------------------------- #
# 7. compute_phase / should_refresh / format_hms / to_yi (§4.4, §4.6)
# --------------------------------------------------------------------------- #


def _compute_phase(app, now, cal=CAL_STRINGS):
    fn = _require(app, "compute_phase")
    candidates = [((now,), {}), ((now, cal), {}), ((now, list(CAL_DATES)), {})]
    kwargs = _kw_candidates(fn, [(("now", "current", "dt", "time"), now)])
    if kwargs:
        candidates.append(((), kwargs))
    return _as_phase(_call(fn, candidates))


def test_compute_phase_mapping(app):
    assert _compute_phase(app, _cst(2026, 11, 4, 8, 0)) == "pre_open"
    # 09:20 is inside the 09:15-09:25 opening auction; pre_open is also defensible
    # because §4.1 treats "<09:25" as pre-open for date resolution.
    assert _compute_phase(app, _cst(2026, 11, 4, 9, 20)) in {"auction", "pre_open"}
    assert _compute_phase(app, _cst(2026, 11, 4, 10, 0)) == "trading"
    assert _compute_phase(app, _cst(2026, 11, 4, 12, 0)) == "lunch"
    assert _compute_phase(app, _cst(2026, 11, 4, 14, 0)) == "trading"
    assert _compute_phase(app, _cst(2026, 11, 4, 15, 30)) == "closed"
    assert _compute_phase(app, _cst(2026, 11, 7, 12, 0)) == "non_trading_day"
    assert _compute_phase(app, _cst(2026, 11, 7, 12, 0)) in PHASES


def test_should_refresh_windows(app):
    fn = _require(app, "should_refresh")

    def call(now):
        candidates = [
            ((now,), {}),
            ((now, None), {}),
            ((now, now - timedelta(seconds=120)), {}),
        ]
        kwargs = _kw_candidates(
            fn,
            [
                (("now", "current", "dt", "time"), now),
                (("last", "since", "previous", "last_success"), now - timedelta(seconds=120)),
            ],
        )
        if kwargs:
            candidates.append(((), kwargs))
        result = _call(fn, candidates)
        if isinstance(result, dict):
            for key in ("should_refresh", "refresh", "value"):
                if key in result:
                    return bool(result[key])
        return bool(result)

    assert call(_cst(2026, 11, 4, 10, 0)) is True
    assert call(_cst(2026, 11, 4, 14, 0)) is True
    assert call(_cst(2026, 11, 4, 12, 0)) is False
    assert call(_cst(2026, 11, 4, 15, 30)) is False
    assert call(_cst(2026, 11, 7, 10, 0)) is False


def test_format_hms(app):
    fn = _require(app, "format_hms")
    results = []
    for value in ("092500", "09:25:00", 92500):
        candidates = [((value,), {}), ((value, "09:25:00"), {})]
        try:
            results.append(fn(*candidates[0][0]))
        except Exception:
            continue
    assert results, "format_hms could not be exercised"
    for result in results:
        assert result == "09:25:00", f"format_hms returned {result!r}"
    # Missing value must stay null (§3: 没有则 null), not become a fake "00:00:00".
    for value in (None, ""):
        try:
            assert fn(value) in (None, "")
        except TypeError:
            continue


def test_to_yi(app):
    fn = _require(app, "to_yi")
    assert fn(123456789.0) == pytest.approx(1.23)
    assert fn(0) == pytest.approx(0.0)
    for missing in (None, ""):
        try:
            assert fn(missing) is None
        except TypeError:
            continue


# --------------------------------------------------------------------------- #
# 8. CSV persistence (§4.5, §6.3)
# --------------------------------------------------------------------------- #


def test_csv_header_contract_static():
    if not APP_PATH.exists():
        pytest.skip("app.py not found — backend has not landed yet")
    source = APP_PATH.read_text(encoding="utf-8")
    assert "utf-8-sig" in source, "CSV must be written with utf-8-sig (CONTRACT §4.5)"
    assert "csv" in source.lower(), "CSV must be written with the csv module (§4.5)"
    # The nine header names must appear in the source in contract order.
    cursor = 0
    for field in CSV_HEADER.split(","):
        position = source.find(field, cursor)
        assert position >= 0, f"CSV header field {field!r} missing/in wrong order in app.py"
        cursor = position + len(field)


def test_csv_file_if_present():
    if not DATA_DIR.exists():
        pytest.skip("data/ does not exist yet (no successful refresh so far)")
    files = sorted(DATA_DIR.glob("*.csv"))
    if not files:
        pytest.skip("no data/*.csv yet — needs one successful live refresh")
    for path in files:
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{path.name} is not utf-8-sig"
        text = raw.decode("utf-8-sig")
        reader = csv.reader(text.splitlines())
        rows = list(reader)
        assert rows, f"{path.name} is empty"
        assert ",".join(rows[0]) == CSV_HEADER, f"{path.name} header={rows[0]}"
        statuses = set()
        for row in rows[1:]:
            assert len(row) == 9, f"{path.name} bad row width: {row}"
            timestamp, code, name, status, streak, first_seal, seal_amount, break_times, concepts = row
            assert SIX_DIGIT.match(code), f"{path.name} non-6-digit code: {code!r}"
            assert status in {"limit_up", "broken", "limit_down"}, status
            statuses.add(status)
            int(streak)  # must be numeric
            int(break_times)
            float(seal_amount)
        assert statuses, f"{path.name} has no data rows"


# --------------------------------------------------------------------------- #
# 9. snapshot JSON serialisation — allow_nan=False + 6-digit codes (§3)
# --------------------------------------------------------------------------- #


def _maybe_snapshot_builder(app):
    return _optional(
        app,
        "build_snapshot",
        "make_snapshot",
        "_build_snapshot",
        "compose_snapshot",
        "snapshot_payload",
    )


def test_snapshot_json_serializable_allow_nan(app):
    if not APP_PATH.read_text(encoding="utf-8").find("allow_nan=False") >= 0:
        pytest.fail("app.py never uses json.dumps(..., allow_nan=False) — CONTRACT §3")
    builder = _maybe_snapshot_builder(app)
    if builder is not None:
        rows = _limit_up_rows()
        concepts = {"ready": True, "loaded": 1, "total": 1, "updated_at": None, "note": None}
        candidates = [
            ((rows, _broken_rows(), _limit_down_rows(), _previous_rows(), concepts, []), {}),
            ((rows, _broken_rows(), _limit_down_rows(), _previous_rows(), concepts), {}),
            ((rows, _broken_rows(), _limit_down_rows(), _previous_rows()), {}),
            ((), _kw_candidates(
                builder,
                [
                    (("limit_up", "zt", "up"), rows),
                    (("broken", "zb"), _broken_rows()),
                    (("limit_down", "dt", "down"), _limit_down_rows()),
                    (("previous", "prev", "yesterday"), _previous_rows()),
                    (("concept",), concepts),
                    (("watch",), []),
                ],
            )),
        ]
        payload = _call(builder, candidates)
        assert isinstance(payload, dict), f"snapshot builder returned {type(payload)}"
        dumped = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        parsed = json.loads(dumped)
        assert parsed.get("ok") is True
        _assert_six_digit_codes(parsed)
        return
    # Fallback: no full builder — still prove the stock row is JSON-clean with
    # allow_nan=False and 6-digit string codes (the NaN/code hazards of §3).
    mapped = _map_one_row(app, dict(RAW_LIMIT_UP_ROW))
    dumped = json.dumps({"stocks": [mapped], "stats": {"broken_rate": None}}, ensure_ascii=False, allow_nan=False)
    parsed = json.loads(dumped)
    _assert_six_digit_codes(parsed)
    for value in mapped.values():
        assert not (isinstance(value, float) and math.isnan(value))


# --------------------------------------------------------------------------- #
# 10. front-end static contract checks (page.html — owned by frontend-dev)
# --------------------------------------------------------------------------- #


def test_page_html_static_contract():
    if not PAGE_PATH.exists():
        pytest.skip("page.html not found — frontend has not landed yet")
    html = PAGE_PATH.read_text(encoding="utf-8")
    # §5: single self-contained page, Chart.js via CDN, dark theme, Chinese labels.
    assert "chart.js" in html.lower(), "Chart.js CDN missing (CONTRACT §5.5)"
    assert "cdn.jsdelivr.net" in html, "Chart.js CDN URL must be cdn.jsdelivr.net (§5.5)"
    assert "limitup.watchlist" in html, "localStorage key limitup.watchlist missing (§5.6)"
    assert "数据已过期" in html, "stale red banner text 数据已过期 missing (§5.1)"
    assert "暂无数据" in html, "empty-state placeholder 暂无数据 missing (§5.8)"
    assert "/api/snapshot" in html, "page must poll /api/snapshot (§2, §5.8)"
    for label in ("涨停", "炸板", "题材", "连板梯队", "时间线", "自选"):
        assert label in html, f"required Chinese section label {label!r} missing (§5)"
    for phase in ("pre_open", "auction", "trading", "lunch", "closed", "non_trading_day"):
        assert phase in html, f"market_phase mapping for {phase!r} missing (§5.1)"


# --------------------------------------------------------------------------- #
# 11. stale logic — §4.4 / §6.6 (unit-level, no network)
# --------------------------------------------------------------------------- #


def test_compute_stale_semantics(app):
    """stale.is_stale / since / reason semantics without touching the network."""
    compute_stale = getattr(app, "_compute_stale", None)
    if not callable(compute_stale):
        pytest.skip("app._compute_stale not available")
    now = _cst(2026, 11, 4, 14, 0)  # trading phase
    # No error yet: not stale.
    state = compute_stale(now, None, None, "trading")
    assert state.get("is_stale") is False
    # An error after a good refresh: stale, since = last success, reason kept.
    last_success = _cst(2026, 11, 4, 13, 58)
    state = compute_stale(now, last_success, "akshare 拉取失败: boom", "trading")
    assert state.get("is_stale") is True
    assert state.get("since") == app.fmt_iso(last_success)
    assert "boom" in (state.get("reason") or "")
    # Inside trading hours, a good refresh older than STALE_AFTER_SEC is stale too.
    old_success = _cst(2026, 11, 4, 13, 54)  # 6 minutes ago > 180s
    state = compute_stale(now, old_success, None, "trading")
    assert state.get("is_stale") is True
    assert state.get("since") == app.fmt_iso(old_success)
    # Fresh success: not stale.
    state = compute_stale(now, _cst(2026, 11, 4, 13, 59).replace(second=30), None, "trading")
    assert state.get("is_stale") is False
