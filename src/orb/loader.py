"""Load local Databento-style OHLCV-1m bars.

No network calls and no API keys. Timestamps are treated as UTC and converted
to America/New_York, so US daylight saving time follows the IANA rules.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

NY_TZ = ZoneInfo("America/New_York")

# Databento OHLCV bars are stamped at the start of the interval, so the last
# RTH minute is the 15:59 bar. 16:00 is exclusive.
RTH_OPEN_MINUTES = 9 * 60 + 30
RTH_CLOSE_MINUTES = 16 * 60

REQUIRED_COLUMNS = ("ts_event", "open", "high", "low", "close", "volume", "symbol")
OHLC_COLUMNS = ("open", "high", "low", "close")
LEADING_COLUMNS = ("open", "high", "low", "close", "volume", "symbol")

FIXTURE_NAME = "SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv"


def sample_fixture_path() -> Path:
    """Return the bundled synthetic MES OHLCV-1m CSV.

    The file is fabricated test data, not a market recording.
    """
    return Path(__file__).resolve().parent / "fixtures" / FIXTURE_NAME


def load_ohlcv(
    path: str | Path,
    *,
    symbol: str | None = None,
    rth_only: bool = False,
) -> pd.DataFrame:
    """Read a Databento OHLCV-1m CSV or Parquet file.

    Parameters
    ----------
    path:
        Local ``.csv`` or ``.parquet`` file. Required columns are
        ``ts_event``, ``open``, ``high``, ``low``, ``close``, ``volume``,
        and ``symbol``. Other columns (``rtype``, ``publisher_id``,
        ``instrument_id``, and so on) are kept. ``ts_event`` is UTC, either
        ISO-8601 text or integer nanoseconds since the Unix epoch. Naive
        timestamps are interpreted as UTC.
    symbol:
        Keep only rows whose ``symbol`` equals this value after stripping
        whitespace. ``None`` keeps every symbol.
    rth_only:
        Keep bars whose America/New_York clock time is in ``[09:30, 16:00)``.
        The comparison is done after the timezone conversion, so the UTC
        window shifts by one hour between EST and EDT.

    Returns
    -------
    pandas.DataFrame
        Index ``ts_event`` is timezone-aware in America/New_York.
        OHLC columns are ``float64`` and ``volume`` is ``int64``.
    """
    frame = _read_table(Path(path))
    frame = _normalize(frame)
    if symbol is not None:
        # Boolean NumPy masks keep rows even when timestamps are duplicated
        # across symbols. A Series mask would realign on that index and fail.
        wanted = str(symbol).strip()
        frame = frame.loc[(frame["symbol"] == wanted).to_numpy()].copy()
    if rth_only:
        frame = frame.loc[_in_rth(frame.index)].copy()
    return frame.sort_index(kind="mergesort")


def _read_table(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
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
        values = pd.to_numeric(out[column], errors="raise")
        if values.isna().any():
            raise ValueError(f"{column} contains missing values")
        out[column] = values.astype("float64")

    volume = pd.to_numeric(out["volume"], errors="raise")
    if volume.isna().any():
        raise ValueError("volume contains missing values")
    if not (volume % 1 == 0).all():
        raise ValueError("volume must be a whole number of contracts")
    out["volume"] = volume.astype("int64")
    out["symbol"] = out["symbol"].astype("string").str.strip()
    return out


def _timestamps_to_new_york(raw: pd.Series) -> pd.DatetimeIndex:
    if raw.isna().any():
        raise ValueError("ts_event contains missing timestamps")

    if pd.api.types.is_datetime64_any_dtype(raw):
        parsed = pd.to_datetime(raw, utc=True)
    elif pd.api.types.is_integer_dtype(raw):
        parsed = pd.to_datetime(raw, unit="ns", utc=True)
    elif pd.api.types.is_float_dtype(raw):
        raise ValueError(
            "ts_event floats cannot represent nanoseconds exactly; "
            "use ISO-8601 timestamps or integer nanoseconds"
        )
    else:
        text = raw.astype("string").str.strip()
        if text.str.fullmatch(r"\d+").all():
            parsed = pd.to_datetime(text.astype("int64"), unit="ns", utc=True)
        else:
            parsed = _parse_iso8601(text)

    index = pd.DatetimeIndex(parsed)
    if index.tz is None:
        index = index.tz_localize("UTC")
    if index.isna().any():
        raise ValueError("ts_event could not be parsed as UTC timestamps")
    index = index.tz_convert(NY_TZ)
    index.name = "ts_event"
    return index


def _parse_iso8601(text: pd.Series) -> pd.DatetimeIndex:
    try:
        return pd.to_datetime(text, utc=True, format="ISO8601")
    except ValueError:
        return pd.to_datetime(text, utc=True)


def _in_rth(index: pd.DatetimeIndex):
    minutes = index.hour * 60 + index.minute
    return (minutes >= RTH_OPEN_MINUTES) & (minutes < RTH_CLOSE_MINUTES)
