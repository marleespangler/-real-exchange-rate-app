# Real Exchange Rate Explorer

A Streamlit app that computes the bilateral **real exchange rate (RER)** between any two IMF
economies, including the Euro area, from monthly IMF CPI and exchange-rate data (January 1976
onward, where available).

## Economics

| Symbol | Meaning |
|---|---|
| E1(t), E2(t) | IMF exchange rates: units of each country's currency **per USD** (period average) |
| S(t) = E2(t) / E1(t) | Units of **Country 2** currency per **1 unit of Country 1** currency |
| P1(t), P2(t) | Monthly headline CPI of Country 1 and Country 2 |
| RER(t) = S(t) × P1(t) / P2(t) | Bilateral real exchange rate |
| RER* | Mean of RER(t) over the **valid** months of the selected sample |
| Real Index(t) = 100 × RER(t) / RER* | Plotted real index (sample mean = 100) |

- A month is **valid** only when all four inputs exist and are positive. Invalid months are kept in
  the CSV (flagged `valid_month = False`), excluded from RER*, and shown as gaps in the chart.
- RER* is recomputed every time the start or end month changes.
- The nominal rate S(t) is plotted in actual currency units (never normalised). An optional log
  scale is available for high-inflation currencies; the units are unchanged.
- Index above 100: Country 1's currency is stronger in real terms than its sample average against
  Country 2's (real appreciation of Country 1). Below 100: real depreciation.
- The USD rate is 1 by definition, so the app does not request `USA.XDC_USD`.

## Data source

IMF SDMX 3.0 API, base `https://api.imf.org/external/sdmx/3.0`, agency `IMF.STA`:

| Series | Dataflow | Key |
|---|---|---|
| CPI (all items, index, monthly) | `CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate (LCU per USD, period average, monthly) | `ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Example request:
`https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/USA.CPI._T.IX.M`

The Euro area uses code **G163**. The country list is read from the IMF country codelist; if the
structure endpoint is unavailable, the app falls back to the ISO 3166 list (via `pycountry`) plus G163.

## Installation (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

### Optional settings

| Environment variable | Purpose |
|---|---|
| `IMF_API_KEY` | IMF API subscription key, sent as `Ocp-Apim-Subscription-Key` (only if your access requires one). Can also go in `.streamlit/secrets.toml`. |
| `IMF_API_BASE` | Override the API base URL. |

## Using the app

1. Pick **Country 1 (base)** and **Country 2 (quote)** in the sidebar (defaults: United States and Euro area).
2. Drag the **sample period** slider. Its range spans the months where all inputs exist.
3. Choose **Real**, **Nominal**, or **Both** (Both uses a left axis for the index and a right axis for S(t)).
4. Click **Download CSV** to export every month in the sample with all inputs and calculations.
5. Open **Data coverage and IMF requests** to see each series' coverage, status and request URL.

### CSV columns

`month, country1, country2, P1_CPI_<ISO1>, P2_CPI_<ISO2>, E1_<ISO1>_per_USD, E2_<ISO2>_per_USD,
S_<ISO2>_per_<ISO1>, RER, valid_month, RER_star, real_index`

## Implementation notes

- Responses are requested as SDMX-JSON 2.0 first, then SDMX-JSON 1.0, SDMX-CSV and SDMX-ML, so
  the parser keeps working if the IMF changes its default format. Period strings such as `1976-M01`
  and `1976-01` are both accepted.
- Successful downloads are cached for 12 hours. Network errors, rate limits (HTTP 429) and server
  errors are **not** cached, so a rerun retries automatically. **Refresh IMF data** in the sidebar
  clears the cache.
- HTTP 404 from the IMF means the series does not exist; the app reports which input is missing.

## Data caveats

- **Coverage varies.** Many countries lack monthly CPI or exchange rates for early years; the Euro
  area (G163) generally starts in the late 1990s.
- **Base years differ**, so the RER level has no economic meaning by itself; only the rebased index
  and its movements should be interpreted.
- **Redenominations:** the IMF usually expresses historical rates in today's currency units (for
  example, euro-area members' pre-1999 rates converted at the fixed euro conversion rates). Check
  the IMF metadata for a specific country before drawing conclusions about level breaks.
- Headline CPI is used; it can contain administered-price or methodology breaks in some countries.

