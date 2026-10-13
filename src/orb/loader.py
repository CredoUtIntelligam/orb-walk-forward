"""Load local Databento-style OHLCV-1m bars.

No network calls and no API keys. ``ts_event`` must be UTC (integer
nanoseconds or an explicit ``Z`` / ``+00:00`` offset). Values are converted
to America/New_York, so US daylight saving time follows the IANA rules.

Regular trading hours are the equity cash window ``[09:30, 16:00)`` ET,
intersected with the CME equity calendar (``CME_Equity``). Weekends and full
holidays are not sessions. Holiday short sessions, when futures still trade
a shortened day (July 4, Thanksgiving, MLK Day, and any other CME equity halt
before 16:00 ET), are early closes: the window ends at the halt, the session
is flagged ``early_close``, and :func:`signal_bars` always drops it. Those
sessions stay in the loaded frame and in the RTH gap report. That exclusion
is fixed.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

NY_TZ = ZoneInfo("America/New_York")

# Databento OHLCV bars are stamped at the start of the interval, so the last
# full RTH minute is the 15:59 bar. 16:00 is exclusive.
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
RTH_OPEN_MINUTES = 9 * 60 + 30
RTH_CLOSE_MINUTES = 16 * 60

# CME_Equity's regular electronic halt is 17:00 ET. Equity RTH stays capped
# at 16:00 ET; an earlier halt is an early close (or no RTH, if it falls at
# or before 09:30).
CME_EQUITY_CALENDAR = "CME_Equity"

REQUIRED_COLUMNS = ("ts_event", "open", "high", "low", "close", "volume", "symbol")
OHLC_COLUMNS = ("open", "high", "low", "close")
LEADING_COLUMNS = ("open", "high", "low", "close", "volume", "symbol")

FIXTURE_NAME = "SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv"

_INTEGER_NS = re.compile(r"\d+")
_DIGIT_LIKE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_EXPLICIT_OFFSET = re.compile(
    r"(?:T|\s)\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?P<off>Z|[+-]\d{2}:\d{2}|[+-]\d{4}|[+-]\d{2})$",
    re.IGNORECASE,
)

_UTC_REQUIRED = (
    "pass integer nanoseconds or explicit UTC (Z or +00:00)"
)
_GAP_COLUMNS = ("symbol", "session", "start", "end", "missing_minutes")


def sample_fixture_path() -> Path:
    """Return the bundled synthetic MES OHLCV-1m CSV.

    The file is fabricated test data, not a market recording.
    :func:`load_ohlcv` refuses that content unless ``allow_synthetic=True``,
    including copies under another name or a Parquet conversion.
    """
    return Path(__file__).resolve().parent / "fixtures" / FIXTURE_NAME


def load_ohlcv(
    path: str | Path,
    *,
    symbol: str | None = None,
    rth_only: bool = False,
    allow_synthetic: bool = False,
    drop_duplicates: bool = False,
) -> pd.DataFrame:
    """Read a Databento OHLCV-1m CSV or Parquet file.

    Parameters
    ----------
    path:
        Local ``.csv`` or ``.parquet`` file. Required columns are
        ``ts_event``, ``open``, ``high``, ``low``, ``close``, ``volume``,
        and ``symbol``. Other columns (``rtype``, ``publisher_id``,
        ``instrument_id``, and so on) are kept. ``ts_event`` is UTC, either
        ISO-8601 text with an explicit ``Z`` or ``+00:00`` offset, or integer
        nanoseconds since the Unix epoch.
    symbol:
        Keep only rows whose ``symbol`` equals this value after stripping
        whitespace. ``None`` keeps every symbol.
    rth_only:
        Keep bars inside CME equity regular trading hours. On a full session
        that is ``[09:30, 16:00)`` America/New_York. Early-close sessions,
        including holiday short sessions such as July 4, Thanksgiving, and
        MLK Day, end at the exchange halt. Weekends and full holidays are not
        regular hours.
    allow_synthetic:
        Must be ``True`` to load bars whose normalized content matches the
        bundled synthetic fixture. The check is a checksum of those rows, not
        the file name, so a renamed copy or a Parquet conversion still
        raises. The default refuses that content so fabricated bars cannot
        enter a backtest unnoticed.
    drop_duplicates:
        When ``False`` (the default), a repeated ``(symbol, ts_event)`` pair
        raises ``ValueError``. When ``True``, the first row of each pair is
        kept and the rest are dropped. Rows are ordered by a stable sort
        first, so "first" is the original file order among equal timestamps.

    Returns
    -------
    pandas.DataFrame
        Index ``ts_event`` is timezone-aware in America/New_York.
        OHLC columns are ``float64`` and ``volume`` is ``int64``.
        ``early_close`` is ``True`` on sessions whose CME equity halt is
        before 16:00 ET, including holiday short sessions (July 4,
        Thanksgiving, MLK Day, and the like). ``rth_gap_before`` counts
        missing RTH minutes immediately before that bar (zero when the
        previous RTH minute is present). Missing minutes are not inserted.
        The full gap table is ``attrs["rth_gaps"]`` and :func:`rth_gap_report`.
        Early-close sessions, including those holiday short sessions, stay
        eligible for that gap report.

        Early-close sessions stay in this frame so the flag is visible.
        :func:`signal_bars` always removes them. There is no switch to put
        them back.
    """
    path = Path(path)
    frame = _read_table(path)
    frame = _normalize(frame)
    if not allow_synthetic and _is_synthetic_content(frame):
        raise ValueError(
            "Refusing to load synthetic fixture content; "
            "pass allow_synthetic=True to load fabricated test data"
        )
    if symbol is not None:
        # Boolean NumPy masks keep rows even when timestamps are duplicated
        # across symbols. A Series mask would realign on that index and fail.
        wanted = str(symbol).strip()
        frame = frame.loc[(frame["symbol"] == wanted).to_numpy()].copy()
    frame = frame.sort_index(kind="mergesort")
    frame = _handle_duplicates(frame, drop_duplicates=drop_duplicates)
    windows, early_dates = _session_rth(frame.index)
    frame = frame.copy()
    frame["early_close"] = _early_close_flags(frame.index, early_dates)
    if rth_only:
        frame = frame.iloc[np.flatnonzero(_in_rth(frame.index, windows))].copy()
    return _attach_gaps(frame, windows)


def signal_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Return bars that later signal code may use.

    Early-close sessions are always excluded. This is a fixed rule: there is
    no parameter that puts those sessions back into the signal set. The
    ``early_close`` column on :func:`load_ohlcv` output is how a session is
    recognized; this function applies the exclusion.
    """
    if "early_close" not in bars.columns:
        raise ValueError(
            "bars have no early_close column; pass the frame returned by load_ohlcv"
        )
    keep = ~bars["early_close"].to_numpy(dtype=bool)
    return bars.iloc[np.flatnonzero(keep)].copy()


def rth_gap_report(bars: pd.DataFrame) -> pd.DataFrame:
    """Missing regular-hours minutes by symbol and session.

    The table describes gaps inside CME equity RTH. It never fills them.
    ``start`` is inclusive and ``end`` is exclusive, both America/New_York.
    ``session`` is the New York calendar date (``YYYY-MM-DD``). Holiday short
    sessions stay eligible: their missing minutes are measured to the early
    halt, not dropped from this report. A load result caches this table on
    ``attrs["rth_gaps"]``.
    """
    cached = bars.attrs.get("rth_gaps")
    if isinstance(cached, pd.DataFrame):
        return cached
    windows, _early = _session_rth(bars.index)
    return _build_gap_report(bars, windows)


def _is_synthetic_content(frame: pd.DataFrame) -> bool:
    """True when normalized rows match the bundled fabricated fixture.

    The comparison is a checksum of the timestamps and columns, so the file
    name and the container (CSV or Parquet) do not matter.
    """
    return _frame_fingerprint(frame) == _bundled_synthetic_fingerprint()


@lru_cache(maxsize=1)
def _bundled_synthetic_fingerprint() -> str:
    return _frame_fingerprint(_normalize(_read_table(sample_fixture_path())))


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    ordered = frame.sort_index(kind="mergesort")
    extras = sorted(
        name
        for name in ordered.columns
        if name not in LEADING_COLUMNS and name not in {"early_close", "rth_gap_before"}
    )
    hasher = hashlib.sha256()
    hasher.update(_utc_nanoseconds(ordered.index).tobytes())
    for name in list(LEADING_COLUMNS) + extras:
        hasher.update(name.encode())
        values = ordered[name]
        if name in OHLC_COLUMNS:
            payload = np.asarray(values, dtype="float64").tobytes()
        elif name == "volume":
            payload = np.asarray(values, dtype="int64").tobytes()
        else:
            payload = "\n".join(str(value) for value in values.tolist()).encode()
        hasher.update(payload)
    return hasher.hexdigest()


def _utc_nanoseconds(index: pd.DatetimeIndex) -> np.ndarray:
    utc = index.tz_convert("UTC")
    if hasattr(utc, "as_unit"):
        utc = utc.as_unit("ns")
    return np.ascontiguousarray(utc.asi8)


def _read_table(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        # Keep ts_event as text so a decimal or exponent cannot be inferred
        # as float64 (which cannot hold nanoseconds) before the UTC checks.
        return pd.read_csv(path, dtype={"ts_event": "string"})
    if suffix == ".parquet":
        try:
            return pd.read_parquet(path)
        except ImportError as exc:
            raise ImportError(
                "Reading Parquet requires an optional engine such as pyarrow. "
                "CSV does not."
            ) from exc
    raise ValueError(f"Unsupported file type {suffix!r}; expected .csv or .parquet")


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    if "ts_event" not in raw.columns and raw.index.name == "ts_event":
        raw = raw.reset_index()
    missing = [name for name in REQUIRED_COLUMNS if name not in raw.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    index = _timestamps_to_new_york(raw["ts_event"])
    extras = [
        name
        for name in raw.columns
        if name not in LEADING_COLUMNS and name != "ts_event"
    ]
    out = raw.loc[:, list(LEADING_COLUMNS) + extras].copy()
    out.index = index

    for column in OHLC_COLUMNS:
        out[column] = _to_numeric(out[column], column).astype("float64")
    _require_price_sanity(out)

    volume = _to_numeric(out["volume"], "volume")
    if not (volume % 1 == 0).all():
        raise ValueError("volume must be a whole number of contracts")
    if (volume < 0).any():
        raise ValueError("volume must be non-negative")
    out["volume"] = volume.astype("int64")
    out["symbol"] = out["symbol"].astype("string").str.strip()
    return out


def _to_numeric(series: pd.Series, column: str) -> pd.Series:
    text = series.astype("string").str.strip()
    if series.isna().any() or text.isna().any() or text.eq("").any():
        raise ValueError(f"{column} contains missing values")
    try:
        values = pd.to_numeric(text, errors="raise")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{column} contains non-numeric values") from exc
    if values.isna().any():
        raise ValueError(f"{column} contains missing values")
    return values


def _require_price_sanity(frame: pd.DataFrame) -> None:
    for column in OHLC_COLUMNS:
        values = frame[column]
        if np.isinf(values.to_numpy()).any():
            raise ValueError(f"{column} must be finite")
        if (values <= 0).any():
            raise ValueError(f"{column} must be positive")
    high = frame["high"]
    low = frame["low"]
    if (high < low).any():
        raise ValueError("high is below low")
    if ((frame["open"] < low) | (frame["open"] > high)).any():
        raise ValueError("open is outside [low, high]")
    if ((frame["close"] < low) | (frame["close"] > high)).any():
        raise ValueError("close is outside [low, high]")


def _handle_duplicates(frame: pd.DataFrame, *, drop_duplicates: bool) -> pd.DataFrame:
    if frame.empty:
        return frame
    keys = pd.MultiIndex.from_arrays(
        [frame.index, frame["symbol"].astype(str).to_numpy()],
        names=["ts_event", "symbol"],
    )
    duplicated = keys.duplicated(keep=False)
    if not duplicated.any():
        return frame
    if not drop_duplicates:
        sample = keys[duplicated]
        shown = []
        seen: set[tuple[str, str]] = set()
        for ts, symbol in sample:
            label = (str(symbol), pd.Timestamp(ts).isoformat())
            if label in seen:
                continue
            seen.add(label)
            shown.append(f"{label[0]} at {label[1]}")
            if len(shown) == 3:
                break
        raise ValueError(
            "Duplicate bars for the same symbol and timestamp: "
            + "; ".join(shown)
            + ". Pass drop_duplicates=True to keep the first bar"
        )
    keep = ~keys.duplicated(keep="first")
    return frame.iloc[np.flatnonzero(keep)].copy()


def _timestamps_to_new_york(raw: pd.Series) -> pd.DatetimeIndex:
    if len(raw) == 0:
        return pd.DatetimeIndex([], tz=NY_TZ, name="ts_event")
    if raw.isna().any():
        raise ValueError("ts_event contains missing timestamps")

    if pd.api.types.is_datetime64_any_dtype(raw):
        parsed = pd.DatetimeIndex(raw)
    elif pd.api.types.is_integer_dtype(raw):
        parsed = _parse_integer_ns(raw)
    elif pd.api.types.is_float_dtype(raw):
        raise ValueError(
            "ts_event floats cannot represent nanoseconds exactly; "
            "use ISO-8601 timestamps or integer nanoseconds"
        )
    else:
        parsed = _parse_timestamp_text(raw)
    return _to_new_york(parsed)


def _to_new_york(parsed: pd.DatetimeIndex) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(parsed)
    if index.isna().any():
        raise ValueError("ts_event could not be parsed as UTC timestamps")
    if index.tz is None:
        raise ValueError(
            "ts_event timestamps are naive; " + _UTC_REQUIRED
        )
    if not _tz_is_utc(index.tz):
        raise ValueError(
            f"ts_event must be UTC; found timezone {index.tz}. " + _UTC_REQUIRED
        )
    index = index.tz_convert(NY_TZ)
    index.name = "ts_event"
    return index


def _tz_is_utc(tz) -> bool:
    try:
        winter = pd.Timestamp("2024-01-16 12:00", tz="UTC").tz_convert(tz)
        summer = pd.Timestamp("2024-07-16 12:00", tz="UTC").tz_convert(tz)
    except (TypeError, ValueError):
        return False
    zero = timedelta(0)
    return winter.utcoffset() == zero and summer.utcoffset() == zero


def _parse_integer_ns(values) -> pd.DatetimeIndex:
    try:
        if pd.api.types.is_integer_dtype(values):
            stamps = values
        else:
            stamps = values.astype("int64")
        parsed = pd.to_datetime(stamps, unit="ns", utc=True)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError(
            "ts_event integer values are not valid nanosecond timestamps: "
            f"{exc}"
        ) from exc
    return pd.DatetimeIndex(parsed)


def _parse_timestamp_text(raw: pd.Series) -> pd.DatetimeIndex:
    text = raw.astype("string").str.strip()
    if text.isna().any() or text.eq("").any():
        raise ValueError("ts_event contains missing timestamps")

    is_int = text.str.fullmatch(_INTEGER_NS)
    is_digit_like = text.str.fullmatch(_DIGIT_LIKE)
    if bool(is_int.all()):
        return _parse_integer_ns(text)
    if bool(is_int.any()):
        raise ValueError(
            "ts_event mixes integer nanoseconds with other timestamp text; "
            "use integer nanoseconds or explicit UTC for every row"
        )
    if bool(is_digit_like.fillna(False).any()):
        sample = text[is_digit_like.fillna(False)].iloc[0]
        raise ValueError(
            "ts_event digit-like values must be non-negative integer "
            f"nanoseconds; got {sample!r}"
        )
    _require_explicit_utc(text)
    try:
        parsed = pd.to_datetime(text, utc=True, format="ISO8601")
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError(
            f"ts_event could not be parsed as UTC timestamps: {exc}"
        ) from exc
    return pd.DatetimeIndex(parsed)


def _require_explicit_utc(text: pd.Series) -> None:
    offsets: list[str | None] = [_explicit_offset(value) for value in text.tolist()]
    unique = set(offsets)
    naive = None in unique
    zones = {offset for offset in unique if offset is not None}
    if naive and zones:
        raise ValueError(
            "ts_event mixes naive timestamps with explicit offsets; "
            + _UTC_REQUIRED
        )
    if naive:
        raise ValueError("ts_event timestamps are naive; " + _UTC_REQUIRED)
    non_utc = {offset for offset in zones if offset != "UTC"}
    if len(zones) > 1 and non_utc:
        shown = ", ".join(sorted(zones))
        raise ValueError(
            f"ts_event mixes timezone offsets ({shown}); " + _UTC_REQUIRED + " only"
        )
    if non_utc:
        shown = ", ".join(sorted(non_utc))
        raise ValueError(
            f"ts_event must be UTC; found offset {shown}. " + _UTC_REQUIRED
        )


def _explicit_offset(value: str) -> str | None:
    match = _EXPLICIT_OFFSET.search(value)
    if match is None:
        return None
    token = match.group("off").upper()
    if token == "Z":
        return "UTC"
    compact = token.replace(":", "")
    if len(compact) == 3:
        compact = compact + "00"
    if compact in {"+0000", "-0000"}:
        return "UTC"
    return f"{compact[:3]}:{compact[3:]}"


@lru_cache(maxsize=1)
def _cme_equity_calendar():
    return mcal.get_calendar(CME_EQUITY_CALENDAR)


def _session_rth(
    index: pd.DatetimeIndex,
) -> tuple[dict, set]:
    """Map each session date to ``(rth_open, rth_close)`` and early-close dates.

    ``rth_close`` is exclusive. Dates with no RTH overlap are omitted from
    the window map. A date is early-close when CME equity halts before
    16:00 ET. That includes holiday short sessions (July 4, Thanksgiving,
    MLK Day, and similar days that still trade a shortened futures session)
    and halts at or before the 09:30 open.
    """
    if len(index) == 0:
        return {}, set()
    start = pd.Timestamp(index.min()).date()
    end = pd.Timestamp(index.max()).date()
    schedule = _cme_equity_calendar().schedule(start_date=start, end_date=end)
    windows: dict = {}
    early_dates: set = set()
    for session, row in schedule.iterrows():
        session_date = _session_date(session)
        close_ny = pd.Timestamp(row["market_close"]).tz_convert(NY_TZ)
        open_rth = _ny_at(session_date, RTH_OPEN)
        regular_close = _ny_at(session_date, RTH_CLOSE)
        if close_ny < regular_close:
            early_dates.add(session_date)
        if close_ny <= open_rth:
            continue
        windows[session_date] = (open_rth, min(close_ny, regular_close))
    return windows, early_dates


def _session_date(session) -> object:
    stamp = pd.Timestamp(session)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(NY_TZ)
    return stamp.date()


def _ny_at(session_date, clock: time) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(session_date, clock)).tz_localize(NY_TZ)


def _early_close_flags(index: pd.DatetimeIndex, early_dates: set) -> np.ndarray:
    if len(index) == 0:
        return np.array([], dtype=bool)
    return np.fromiter(
        (stamp.date() in early_dates for stamp in index),
        dtype=bool,
        count=len(index),
    )


def _in_rth(index: pd.DatetimeIndex, windows: dict) -> np.ndarray:
    if len(index) == 0:
        return np.array([], dtype=bool)
    minutes = index.hour * 60 + index.minute
    end_minutes = np.empty(len(index), dtype=np.int64)
    for i, stamp in enumerate(index):
        window = windows.get(stamp.date())
        if window is None:
            end_minutes[i] = -1
            continue
        close_at = window[1]
        end_minutes[i] = close_at.hour * 60 + close_at.minute
    return (end_minutes >= 0) & (minutes >= RTH_OPEN_MINUTES) & (minutes < end_minutes)


def _attach_gaps(frame: pd.DataFrame, windows: dict) -> pd.DataFrame:
    gap_before, report = _build_gaps(frame, windows)
    frame = frame.copy()
    frame["rth_gap_before"] = gap_before
    frame.attrs["rth_gaps"] = report
    return frame


def _build_gaps(
    frame: pd.DataFrame, windows: dict
) -> tuple[np.ndarray, pd.DataFrame]:
    gap_before = np.zeros(len(frame), dtype=np.int64)
    if frame.empty or "symbol" not in frame.columns:
        return gap_before, _build_gap_report_rows([])
    rows: list[dict[str, object]] = []
    for symbol, positions in frame.groupby("symbol", sort=False).indices.items():
        symbol_rows, symbol_gaps = _gaps_for_positions(
            frame, str(symbol), np.asarray(positions, dtype=int), windows
        )
        rows.extend(symbol_rows)
        for position, missing in symbol_gaps.items():
            gap_before[position] = missing
    return gap_before, _build_gap_report_rows(rows)


def _gaps_for_positions(
    frame: pd.DataFrame,
    symbol: str,
    positions: np.ndarray,
    windows: dict,
) -> tuple[list[dict[str, object]], dict[int, int]]:
    rows: list[dict[str, object]] = []
    gap_before: dict[int, int] = {}
    by_date: dict = {}
    for position in positions:
        by_date.setdefault(frame.index[position].date(), []).append(int(position))
    for session, session_positions in by_date.items():
        window = windows.get(session)
        if window is None:
            continue
        open_ts, close_ts = window
        rth_positions = [
            position
            for position in session_positions
            if open_ts <= frame.index[position] < close_ts
        ]
        present_list = [frame.index[position] for position in rth_positions]
        present = pd.DatetimeIndex(present_list).unique().sort_values()
        pos_by_ts: dict = {}
        for position in rth_positions:
            pos_by_ts.setdefault(frame.index[position], []).append(position)
        cursor = open_ts
        for stamp in present:
            missing = int((stamp - cursor) / pd.Timedelta(minutes=1))
            for position in pos_by_ts[stamp]:
                gap_before[position] = missing
            cursor = stamp + pd.Timedelta(minutes=1)
        expected = pd.date_range(open_ts, close_ts, freq="min", inclusive="left")
        missing_index = expected.difference(present)
        for start, end, count in _contiguous_minutes(missing_index):
            rows.append(
                {
                    "symbol": symbol,
                    "session": session.isoformat(),
                    "start": start,
                    "end": end,
                    "missing_minutes": count,
                }
            )
    return rows, gap_before


def _contiguous_minutes(missing: pd.DatetimeIndex):
    if len(missing) == 0:
        return []
    spans = []
    start = previous = missing[0]
    minute = pd.Timedelta(minutes=1)
    for stamp in missing[1:]:
        if stamp != previous + minute:
            spans.append((start, previous + minute, _minute_count(start, previous)))
            start = stamp
        previous = stamp
    spans.append((start, previous + minute, _minute_count(start, previous)))
    return spans


def _minute_count(start: pd.Timestamp, last: pd.Timestamp) -> int:
    return int((last - start) / pd.Timedelta(minutes=1)) + 1


def _build_gap_report(bars: pd.DataFrame, windows: dict) -> pd.DataFrame:
    _gap_before, report = _build_gaps(bars, windows)
    return report


def _build_gap_report_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    report = pd.DataFrame(rows, columns=list(_GAP_COLUMNS))
    if report.empty:
        return report
    report["symbol"] = report["symbol"].astype("string")
    report["session"] = report["session"].astype("string")
    report["missing_minutes"] = report["missing_minutes"].astype("int64")
    return report.sort_values(
        ["symbol", "start"], kind="mergesort"
    ).reset_index(drop=True)
