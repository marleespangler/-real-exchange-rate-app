# Real Exchange Rate Explorer
# ===========================
#
# Streamlit app that pulls monthly CPI and exchange-rate data from the IMF
# SDMX 3.0 API and computes the bilateral real exchange rate (RER) between any
# two IMF economies (including the Euro area, IMF code G163).
#
# Definitions (Country 1 = base, Country 2 = quote):
#
#     E_i(t)   = IMF exchange rate, units of country-i currency per 1 USD
#     S(t)     = E_2(t) / E_1(t)  -> units of Country 2 currency per 1 unit
#                                    of Country 1 currency
#     RER(t)   = S(t) * P1(t) / P2(t)
#     RER*     = mean of RER(t) over the valid months in the selected sample
#     Index(t) = 100 * RER(t) / RER*
#
# A rise in RER(t) is a real appreciation of Country 1's currency against
# Country 2's (Country 1's goods become relatively more expensive).
#
# Run with:  streamlit run app.py

from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("IMF_API_BASE", "https://api.imf.org/external/sdmx/3.0").rstrip("/")
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
ER_FLOW = "ER"
CPI_KEY = "{iso}.CPI._T.IX.M"  # headline CPI, all items, index, monthly
ER_KEY = "{iso}.XDC_USD.PA_RT.M"  # domestic currency per USD, period average, monthly

EURO_AREA = "G163"
USA = "USA"
SAMPLE_START = pd.Timestamp("1976-01-01")
CACHE_TTL_SECONDS = 12 * 60 * 60
REQUEST_TIMEOUT = 60  # seconds

# Versions to try: "+" = latest stable, "*" = all versions (we de-duplicate).
VERSIONS = ("+", "*")

DATA_ACCEPT_HEADERS = (
    "application/vnd.sdmx.data+json;version=2.0.0",
    "application/vnd.sdmx.data+json;version=1.0.0",
    "application/vnd.sdmx.data+csv;version=2.0.0",
    "application/vnd.sdmx.data+csv;version=1.0.0",
    "application/vnd.sdmx.structurespecificdata+xml;version=3.0.0",
)
STRUCTURE_ACCEPT_HEADERS = (
    "application/vnd.sdmx.structure+json;version=2.0.0",
    "application/vnd.sdmx.structure+json;version=1.0.0",
    "application/json",
)

# Colours chosen to be distinguishable for common colour-vision deficiencies.
REAL_COLOR = "#1f6feb"
NOMINAL_COLOR = "#d97706"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_key() -> str | None:
    """Optional IMF API subscription key from env var or Streamlit secrets."""
    key = os.environ.get("IMF_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("IMF_API_KEY")  # type: ignore[no-any-return]
    except Exception:  # no secrets.toml present
        return None


@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "RealExchangeRateExplorer/1.0 (python-requests; Streamlit)",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
        }
    )
    key = _api_key()
    if key:
        session.headers["Ocp-Apim-Subscription-Key"] = key
    return session


class IMFTransientError(RuntimeError):
    """Network, rate-limit, auth or format failures. Raised (not returned) so
    that Streamlit does not cache the failure; the next rerun tries again."""

    def __init__(self, message: str, url: str) -> None:
        super().__init__(message)
        self.url = url


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def empty_series() -> pd.Series:
    """An empty monthly series (with a DatetimeIndex, so date comparisons work)."""
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))


_PERIOD_RE = re.compile(r"^\s*(\d{4})(?:[-/]?M?(\d{1,2}))?")


def parse_month(value: object) -> pd.Timestamp:
    """Parse IMF monthly periods such as '1976-M01', '1976-01', '1976M1'."""
    if value is None:
        return pd.NaT
    m = _PERIOD_RE.match(str(value))
    if not m or m.group(2) is None:
        return pd.NaT
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return pd.NaT
    return pd.Timestamp(year=int(m.group(1)), month=month, day=1)


def _to_float(value: object) -> float:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return np.nan
    try:
        out = float(value)
    except (TypeError, ValueError):
        return np.nan
    return out if np.isfinite(out) else np.nan


def _pairs_to_series(pairs: list[tuple[pd.Timestamp, float]]) -> pd.Series:
    if not pairs:
        return empty_series()
    s = pd.Series([v for _, v in pairs], index=pd.DatetimeIndex([d for d, _ in pairs]), dtype="float64")
    s = s[~s.index.isna()]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _best_series(candidates: list[list[tuple[pd.Timestamp, float]]]) -> pd.Series:
    """If a response holds several series, keep the one with most valid values."""
    best = empty_series()
    for pairs in candidates:
        s = _pairs_to_series(pairs)
        if s.notna().sum() > best.notna().sum():
            best = s
    return best


def _period_label(v: dict) -> object:
    return v.get("id") or v.get("value") or v.get("start") or v.get("name")


def parse_sdmx_json_data(payload: dict) -> pd.Series:
    """Parse SDMX-JSON 1.0 / 2.0 data messages into a monthly series."""
    root = payload.get("data", payload)
    structures = root.get("structures")
    if structures is None:
        structure = root.get("structure") or payload.get("structure")
        structures = [structure] if structure else []
    datasets = root.get("dataSets") or payload.get("dataSets") or []

    candidates: list[list[tuple[pd.Timestamp, float]]] = []
    for ds in datasets:
        s_idx = ds.get("structure", 0) if isinstance(ds.get("structure", 0), int) else 0
        struct = structures[s_idx] if 0 <= s_idx < len(structures) else (structures[0] if structures else {})
        dims = (struct or {}).get("dimensions", {}) or {}
        obs_dims = dims.get("observation", []) or []

        # Locate the time dimension among the observation-level dimensions.
        time_pos, time_values = None, []
        for i, d in enumerate(obs_dims):
            if d.get("id") in ("TIME_PERIOD", "TIME") or d.get("role") in ("time", "TIME_PERIOD"):
                time_pos, time_values = i, [_period_label(v) for v in d.get("values", [])]
                break
        if time_pos is None and len(obs_dims) == 1:
            time_pos, time_values = 0, [_period_label(v) for v in obs_dims[0].get("values", [])]
        if time_pos is None:
            continue

        def obs_pairs(obs: dict, position: int) -> list[tuple[pd.Timestamp, float]]:
            out = []
            for k, v in (obs or {}).items():
                parts = str(k).split(":")
                try:
                    idx = int(parts[position])
                    label = time_values[idx]
                except (ValueError, IndexError):
                    continue
                out.append((parse_month(label), _to_float(v)))
            return out

        if ds.get("series"):
            for series in ds["series"].values():
                candidates.append(obs_pairs(series.get("observations", {}), 0))
        elif ds.get("observations"):
            # Flat format (dimensionAtObservation=AllDimensions).
            candidates.append(obs_pairs(ds["observations"], time_pos))

    return _best_series(candidates)


def parse_sdmx_csv_data(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text), dtype=str)
    cols = {c.upper().split(":")[0]: c for c in df.columns}
    t_col = cols.get("TIME_PERIOD") or cols.get("TIME")
    v_col = cols.get("OBS_VALUE") or cols.get("VALUE")
    if t_col is None or v_col is None:
        raise ValueError("CSV response lacks TIME_PERIOD/OBS_VALUE columns")
    pairs = [(parse_month(t), _to_float(v)) for t, v in zip(df[t_col], df[v_col])]
    return _pairs_to_series(pairs)


def parse_sdmx_xml_data(text: str) -> pd.Series:
    """Handles structure-specific (<Obs TIME_PERIOD= OBS_VALUE=>) and generic SDMX-ML."""
    root = ET.fromstring(text.encode("utf-8"))
    pairs = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "Obs":
            continue
        if "TIME_PERIOD" in el.attrib:
            pairs.append((parse_month(el.attrib["TIME_PERIOD"]), _to_float(el.attrib.get("OBS_VALUE"))))
            continue
        period, value = None, None
        for child in el:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag in ("ObsDimension", "ObsKey"):
                period = child.attrib.get("value") or child.attrib.get("id")
                if period is None:
                    for v in child:
                        if v.attrib.get("id") == "TIME_PERIOD":
                            period = v.attrib.get("value")
            elif ctag == "ObsValue":
                value = child.attrib.get("value")
        pairs.append((parse_month(period), _to_float(value)))
    return _pairs_to_series(pairs)


def parse_data_response(resp: requests.Response) -> pd.Series:
    ctype = resp.headers.get("Content-Type", "").lower()
    text = resp.text.lstrip("﻿").strip()
    if not text:
        return empty_series()
    if "json" in ctype or text.startswith("{"):
        return parse_sdmx_json_data(json.loads(text))
    if "xml" in ctype or text.startswith("<"):
        return parse_sdmx_xml_data(text)
    return parse_sdmx_csv_data(text)


# ---------------------------------------------------------------------------
# IMF data access (cached)
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    series: pd.Series
    url: str
    message: str = ""  # empty when the fetch succeeded with data


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _fetch_imf_series_cached(flow: str, key: str) -> FetchResult:
    """Download one monthly series from the IMF SDMX 3.0 data endpoint.

    Successful downloads and definitive "no data" answers are cached;
    transient failures raise IMFTransientError and are not cached.
    """
    session = _session()
    last_msg = "No data returned."
    last_url = f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/{VERSIONS[0]}/{key}"

    for version in VERSIONS:
        url = f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/{version}/{key}"
        last_url = url
        not_found = False
        for accept in DATA_ACCEPT_HEADERS:
            try:
                resp = session.get(url, headers={"Accept": accept}, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                raise IMFTransientError(f"Network error contacting the IMF API: {exc}", url) from exc

            if resp.status_code == 404:
                not_found = True
                last_msg = "The IMF reports no data for this series (HTTP 404)."
                break  # try the next version string
            if resp.status_code in (401, 403):
                raise IMFTransientError(
                    f"Access denied by the IMF API (HTTP {resp.status_code}). "
                    "If you have an IMF API subscription key, set IMF_API_KEY.",
                    url,
                )
            if resp.status_code == 429:
                raise IMFTransientError("The IMF API rate limit was hit (HTTP 429). Please retry shortly.", url)
            if not resp.ok:
                last_msg = f"IMF API returned HTTP {resp.status_code} for format {accept}."
                continue  # e.g. 406 Not Acceptable -> try another format

            try:
                series = parse_data_response(resp)
            except Exception as exc:  # malformed / unexpected format -> try next
                last_msg = f"Could not parse IMF response ({accept}): {exc}"
                continue

            series = series.dropna()
            if series.empty:
                last_msg = "The IMF response contained no numeric observations."
                not_found = True
                break
            return FetchResult(series, url, "")
        if not not_found:
            # Every format failed for a reason other than "no data" (server error,
            # unparseable reply): report it without caching so a rerun retries.
            raise IMFTransientError(last_msg, last_url)

    return FetchResult(empty_series(), last_url, last_msg)


def fetch_imf_series(flow: str, key: str) -> FetchResult:
    try:
        return _fetch_imf_series_cached(flow, key)
    except IMFTransientError as exc:
        return FetchResult(empty_series(), exc.url, str(exc))


def get_cpi(iso: str) -> FetchResult:
    return fetch_imf_series(CPI_FLOW, CPI_KEY.format(iso=iso))


def get_usd_rate(iso: str) -> FetchResult:
    """Units of the country's currency per USD. The USD rate is 1 by definition."""
    if iso == USA:
        idx = pd.date_range(SAMPLE_START, pd.Timestamp.today().to_period("M").to_timestamp(), freq="MS")
        return FetchResult(pd.Series(1.0, index=idx), "(USD per USD = 1 by definition)", "")
    return fetch_imf_series(ER_FLOW, ER_KEY.format(iso=iso))


def _code_name(code: dict) -> str:
    name = code.get("name")
    if isinstance(name, dict):
        name = name.get("en") or next(iter(name.values()), None)
    if not name:
        names = code.get("names") or {}
        if isinstance(names, dict):
            name = names.get("en") or next(iter(names.values()), None)
    return str(name or code.get("id", ""))


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def _fetch_imf_country_codelist() -> tuple[dict[str, str], str]:
    """Read the country codelist of the CPI/ER dataflows from the IMF
    structure endpoint. Raises LookupError (not cached) if unavailable."""
    session = _session()
    for flow in (CPI_FLOW, ER_FLOW):
        for version in VERSIONS:
            url = f"{BASE_URL}/structure/dataflow/{AGENCY}/{flow}/{version}"
            for accept in STRUCTURE_ACCEPT_HEADERS:
                try:
                    resp = session.get(
                        url,
                        params={"references": "descendants", "detail": "full"},
                        headers={"Accept": accept},
                        timeout=REQUEST_TIMEOUT,
                    )
                except requests.RequestException:
                    break
                if resp.status_code == 404:
                    break
                if not resp.ok:
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    continue
                root = payload.get("data", payload)
                codelists = root.get("codelists") or []
                ranked = sorted(
                    (cl for cl in codelists if re.search(r"COUNTRY|AREA", str(cl.get("id", "")), re.I)),
                    key=lambda cl: (str(cl.get("id", "")).upper() != "CL_COUNTRY", -len(cl.get("codes") or [])),
                )
                for cl in ranked:
                    codes = {c["id"]: _code_name(c) for c in (cl.get("codes") or []) if c.get("id")}
                    if len(codes) > 20:
                        codes.setdefault(EURO_AREA, "Euro Area (EA)")
                        return codes, f"IMF codelist {cl.get('id')} ({flow} dataflow)"
    raise LookupError("IMF country codelist unavailable")


def fetch_country_list() -> tuple[dict[str, str], str]:
    """Return ({code: name}, source). Falls back to ISO 3166 (pycountry)."""
    try:
        return _fetch_imf_country_codelist()
    except LookupError:
        pass
    try:
        import pycountry

        codes = {c.alpha_3: getattr(c, "common_name", None) or c.name for c in pycountry.countries}
    except Exception:
        codes = {USA: "United States", "GBR": "United Kingdom", "JPN": "Japan", "CAN": "Canada"}
    codes[EURO_AREA] = "Euro Area (EA)"
    return codes, "ISO 3166 fallback list (IMF codelist unavailable)"


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------


@dataclass
class PairData:
    frame: pd.DataFrame
    results: dict[str, FetchResult] = field(default_factory=dict)


def build_pair_frame(p1: pd.Series, p2: pd.Series, e1: pd.Series, e2: pd.Series) -> pd.DataFrame:
    """Align the four monthly series and compute S(t) and RER(t)."""
    parts = {k: (s if isinstance(s.index, pd.DatetimeIndex) else empty_series()) for k, s in
             {"P1": p1, "P2": p2, "E1": e1, "E2": e2}.items()}
    df = pd.concat(parts, axis=1).sort_index()
    df = df[df.index >= SAMPLE_START]
    if df.empty:
        return df
    # Fill the full monthly calendar so gaps are explicit rows in the output.
    full_idx = pd.date_range(df.index.min(), df.index.max(), freq="MS")
    df = df.reindex(full_idx)
    df.index.name = "month"

    for col in ("P1", "P2", "E1", "E2"):
        df.loc[~(df[col] > 0), col] = np.nan  # non-positive values are unusable

    # IMF rates are LCU per USD:  S = (C2 per USD) / (C1 per USD) = C2 per C1.
    df["S"] = df["E2"] / df["E1"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    df["valid"] = np.isfinite(df["RER"]) & (df["RER"] > 0)
    df.loc[~df["valid"], "RER"] = np.nan
    return df


def apply_sample(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, float, int]:
    """Restrict to [start, end], compute RER* and the real index."""
    sample = df.loc[start:end].copy()
    valid = sample["valid"]
    n_valid = int(valid.sum())
    rer_star = float(sample.loc[valid, "RER"].mean()) if n_valid else np.nan
    sample["RER_star"] = rer_star
    sample["real_index"] = 100.0 * sample["RER"] / rer_star if n_valid else np.nan
    return sample, rer_star, n_valid


def coverage(series: pd.Series) -> str:
    if not isinstance(series.index, pd.DatetimeIndex):
        return "—"
    s = series.dropna()
    s = s[s.index >= SAMPLE_START]
    if s.empty:
        return "—"
    return f"{s.index.min():%Y-%m} → {s.index.max():%Y-%m} ({len(s)} obs)"


# ---------------------------------------------------------------------------
# Charts and export
# ---------------------------------------------------------------------------


def make_chart(sample: pd.DataFrame, mode: str, label1: str, label2: str, log_nominal: bool) -> go.Figure:
    x = sample.index
    nominal_title = f"S(t): {label2} currency per 1 {label1} currency"
    real_title = "Real index (sample mean = 100)"

    real_trace = go.Scatter(
        x=x,
        y=sample["real_index"],
        name="Real index",
        mode="lines",
        line=dict(color=REAL_COLOR, width=2),
        connectgaps=False,
        hovertemplate="%{x|%b %Y}<br>Real index: %{y:.2f}<extra></extra>",
    )
    nominal_trace = go.Scatter(
        x=x,
        y=sample["S"],
        name="Nominal S(t)",
        mode="lines",
        line=dict(color=NOMINAL_COLOR, width=2),
        connectgaps=False,
        hovertemplate="%{x|%b %Y}<br>S(t): %{y:,.6g}<extra></extra>",
    )

    if mode == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real_trace, secondary_y=False)
        fig.add_trace(nominal_trace, secondary_y=True)
        fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.6)
        fig.update_yaxes(title_text=real_title, secondary_y=False, title_font_color=REAL_COLOR)
        fig.update_yaxes(
            title_text=nominal_title,
            secondary_y=True,
            title_font_color=NOMINAL_COLOR,
            type="log" if log_nominal else "linear",
            showgrid=False,
        )
    else:
        fig = go.Figure()
        if mode == "Real":
            fig.add_trace(real_trace)
            fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.6)
            fig.update_yaxes(title_text=real_title)
        else:
            fig.add_trace(nominal_trace)
            fig.update_yaxes(title_text=nominal_title, type="log" if log_nominal else "linear")

    fig.update_layout(
        height=520,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    fig.update_xaxes(title_text="Month")
    return fig


def export_frame(sample: pd.DataFrame, iso1: str, iso2: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "month": sample.index.strftime("%Y-%m"),
            "country1": iso1,
            "country2": iso2,
            f"P1_CPI_{iso1}": sample["P1"].to_numpy(),
            f"P2_CPI_{iso2}": sample["P2"].to_numpy(),
            f"E1_{iso1}_per_USD": sample["E1"].to_numpy(),
            f"E2_{iso2}_per_USD": sample["E2"].to_numpy(),
            f"S_{iso2}_per_{iso1}": sample["S"].to_numpy(),
            "RER": sample["RER"].to_numpy(),
            "valid_month": sample["valid"].astype(bool).to_numpy(),
            "RER_star": sample["RER_star"].to_numpy(),
            "real_index": sample["real_index"].to_numpy(),
        }
    )
    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def country_selector(label: str, codes: dict[str, str], default: str, key: str) -> str:
    options = sorted(codes, key=lambda c: (c != EURO_AREA, codes[c].lower()))
    index = options.index(default) if default in options else 0
    return st.selectbox(
        label,
        options,
        index=index,
        key=key,
        format_func=lambda c: f"{codes[c]} ({c})",
    )


def load_pair(iso1: str, iso2: str) -> PairData:
    results = {
        "P1": get_cpi(iso1),
        "P2": get_cpi(iso2),
        "E1": get_usd_rate(iso1),
        "E2": get_usd_rate(iso2),
    }
    frame = build_pair_frame(results["P1"].series, results["P2"].series, results["E1"].series, results["E2"].series)
    return PairData(frame=frame, results=results)


def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="💱", layout="wide")
    st.title("Real Exchange Rate Explorer")
    st.caption(
        "RER(t) = S(t) × P1(t) / P2(t), where S(t) is units of Country 2 currency per 1 unit of "
        "Country 1 currency. Monthly IMF CPI and exchange-rate data from January 1976 where available."
    )

    with st.spinner("Loading IMF country list…"):
        codes, codes_source = fetch_country_list()

    with st.sidebar:
        st.header("Countries")
        iso1 = country_selector("Country 1 (base)", codes, USA, "country1")
        iso2 = country_selector("Country 2 (quote)", codes, EURO_AREA, "country2")
        st.caption(f"Country list: {codes_source}")
        st.divider()
        if st.button("Refresh IMF data", help="Clear cached downloads and fetch again"):
            st.cache_data.clear()
            st.rerun()

    name1, name2 = codes.get(iso1, iso1), codes.get(iso2, iso2)
    if iso1 == iso2:
        st.warning("Country 1 and Country 2 are the same, so S(t) = 1 and RER(t) = 1 by construction.")

    with st.spinner(f"Fetching IMF data for {name1} and {name2}…"):
        pair = load_pair(iso1, iso2)
    df, results = pair.frame, pair.results

    labels = {
        "P1": f"CPI — {name1}",
        "P2": f"CPI — {name2}",
        "E1": f"{name1} currency per USD",
        "E2": f"{name2} currency per USD",
    }
    with st.expander("Data coverage and IMF requests", expanded=bool(df.empty or not df["valid"].any())):
        rows = [
            {
                "Series": labels[k],
                "Coverage since 1976": coverage(r.series),
                "Status": r.message or "OK",
                "Request": r.url,
            }
            for k, r in results.items()
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    problems = [f"{labels[k]}: {r.message}" for k, r in results.items() if r.message]
    if problems:
        st.warning("Some IMF series could not be retrieved:\n\n" + "\n\n".join(f"• {p}" for p in problems))

    if df.empty or not df["valid"].any():
        st.error(
            "No month has all four inputs (both CPIs and both USD exchange rates), so the real exchange "
            "rate cannot be computed for this pair. Try another pair or refresh the data."
        )
        st.stop()

    valid_months = df.index[df["valid"]]
    months = pd.date_range(valid_months.min(), valid_months.max(), freq="MS")
    month_labels = [m.strftime("%Y-%m") for m in months]

    st.subheader(f"{name1} vs {name2}")
    start_label, end_label = st.select_slider(
        "Sample period (RER* is the mean RER over valid months in this window)",
        options=month_labels,
        value=(month_labels[0], month_labels[-1]),
        key=f"sample_{iso1}_{iso2}",  # new pair -> range resets to full coverage
    )
    start, end = pd.Timestamp(start_label + "-01"), pd.Timestamp(end_label + "-01")
    sample, rer_star, n_valid = apply_sample(df, start, end)

    if n_valid == 0:
        st.error("The selected window contains no valid months. Widen the sample period.")
        st.stop()

    n_months = len(sample)
    last_valid = sample[sample["valid"]].iloc[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("RER* (sample mean)", f"{rer_star:,.6g}")
    c2.metric("Valid months", f"{n_valid} / {n_months}")
    c3.metric(f"Real index, {last_valid.name:%b %Y}", f"{last_valid['real_index']:.1f}")
    c4.metric(f"S(t), {last_valid.name:%b %Y}", f"{last_valid['S']:,.6g}", help=f"{name2} currency per 1 {name1} currency")

    if n_valid < n_months:
        st.info(
            f"{n_months - n_valid} month(s) in the window lack at least one input; they are excluded "
            "from RER* and shown as gaps in the chart."
        )

    col_mode, col_log = st.columns([3, 1])
    with col_mode:
        mode = st.radio("Chart", ["Real", "Nominal", "Both"], index=2, horizontal=True)
    with col_log:
        log_nominal = st.checkbox(
            "Log scale for S(t)",
            value=False,
            disabled=mode == "Real",
            help="Still in actual currency units; helpful for high-inflation currencies.",
        )

    st.plotly_chart(make_chart(sample, mode, name1, name2, log_nominal), key="rer_chart")

    export = export_frame(sample, iso1, iso2)
    st.download_button(
        "Download CSV (all observations and calculations in the sample)",
        data=export.to_csv(index=False).encode("utf-8"),
        file_name=f"rer_{iso1}_{iso2}_{start_label}_{end_label}.csv",
        mime="text/csv",
        type="primary",
    )

    with st.expander("Show data table"):
        st.dataframe(export, hide_index=True)

    with st.expander("Method and interpretation"):
        st.markdown(
            f"""
- **Exchange rates:** IMF `ER` dataflow, key `{{ISO3}}.XDC_USD.PA_RT.M` (period-average units of
  domestic currency per USD). With E1 and E2 the two countries' rates,
  **S(t) = E2(t) / E1(t)** = units of {name2} currency per 1 {name1} currency. The USD rate is 1.
- **Prices:** IMF `CPI` dataflow, key `{{ISO3}}.CPI._T.IX.M` (headline CPI, all items, index).
- **Real exchange rate:** RER(t) = S(t) × P1(t) / P2(t). Its level depends on each CPI's base year, so
  only its movements are meaningful; the index therefore rebases it to the sample mean:
  Real index(t) = 100 × RER(t) / RER*.
- **Reading the index:** above 100 means {name1}'s currency is *stronger in real terms* than its
  sample average against {name2}'s (a real appreciation of Country 1); below 100 means weaker.
- **Valid months** need all four inputs, positive. RER* is recomputed whenever the window changes.
- **Gaps:** IMF coverage varies by country; the Euro area (G163) generally starts in the 1990s.
  Historical rates are as published by the IMF (usually expressed in today's currency units after
  redenominations).
"""
        )


if __name__ == "__main__":
    main()
