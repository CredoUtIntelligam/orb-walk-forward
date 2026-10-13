# SYNTHETIC TEST DATA

`SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv` is fabricated. It exists so the loader
and later opening-range code have a small Databento-shaped file. It is not a
Databento download, not a recording of MES or ES, and not input for P&L,
backtests, or any performance number. `load_ohlcv` recognizes these rows by
a checksum of the normalized bars, not by the file name. A copy, a lowercase
rename, or a Parquet conversion raises unless `allow_synthetic=True` is
passed explicitly.

## Format

Databento OHLCV-1m CSV columns:

`ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol`

`ts_event` is UTC ISO-8601 with a nanosecond fraction. `rtype` 33 is the
OHLCV-1m record type. `publisher_id` and `instrument_id` are placeholders.
The symbol is `MES`. Prices are on a 0.25 grid only so they resemble
equity-index futures; they are not historical prints.

## Sessions

Three regular-hours sessions in January 2024 (EST). Each has the 390
one-minute bars from 09:30 through 15:59 America/New_York, stamped at the
start of the minute, plus three bars outside RTH (09:29, 16:00, and 16:15 ET)
so the loader's RTH filter can be checked.

The 09:30 and 09:31 bars print the high and low in the table. Bars through
09:59 stay inside that range, so a 5-, 15-, or 30-minute opening range has
the same high and low. The breakout, when there is one, is at 10:00 ET.

| Date (ET) | Later use | Range low | Range high | From 10:00 ET |
| --- | --- | --- | --- | --- |
| 2024-01-16 | long breakout | 5000 | 5008 | trades above 5008 and stays above 5000 |
| 2024-01-17 | short breakout | 5050 | 5058 | trades below 5050 and stays below 5058 |
| 2024-01-18 | no breakout | 5100 | 5110 | stays strictly inside the range |

Daylight-saving conversion is covered in `tests/test_loader.py` with explicit
UTC timestamps on both sides of the March and November transitions. These
three sessions are all standard time on purpose.
