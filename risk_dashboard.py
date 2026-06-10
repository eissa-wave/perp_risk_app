"""
Local Streamlit dashboard for the risk script.

Reads the latest risk snapshot from the 'risk monitor' tab of the
'Portfolios' Google Sheet. The dashboard is read-only; to refresh the
underlying data, run the risk script (which rewrites the sheet).

Run:
  pip install streamlit gspread google-auth pandas
  streamlit run dashboard.py

Opens at http://localhost:8501
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials


# ---- Config (must match risk_monitor.py) ----
#GOOGLE_CREDS_FILE = "portfolio-pnl-e2fa6303206c.json"
SHEET_NAME = "Portfolios"
TAB_RISK = "perp_monitor"
GSHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_data(ttl=30, show_spinner=False)
def load_sheet():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=GSHEET_SCOPES
    )
    gc = gspread.authorize(creds)
    sh = gc.open(SHEET_NAME)
    ws = sh.worksheet(TAB_RISK)
    return ws.get_all_values()

EXCHANGE_CURRENCY = {
    "Hyperliquid": "USDC",
    "Hyperliquid:xyz": "USDC",
    "Binance": "USDT",
    "Bybit": "USD",
    "OKX": "USD",
}


st.set_page_config(page_title="Wave Perp Risk Dashboard", layout="wide")
st.title("Wave Perp Risk Dashboard")
st.caption("Live view across Hyperliquid, Binance, Bybit, and OKX (read from Google Sheets)")


def _parse_sheet(rows: list[list[str]]):
    """
    The sheet has labeled sections separated by blank rows:
      Per-Exchange Summary, Per-Position Detail, Strategy PnL (funding arb),
      Thresholds.
    Find each by section header and parse into a DataFrame.
    """
    snapshot_ts = ""
    if rows and len(rows[0]) >= 2 and rows[0][0].startswith("Risk monitor snapshot"):
        snapshot_ts = rows[0][1]

    def _section_slice(header_text: str):
        """Return rows between the section header and the next blank row."""
        try:
            start = next(i for i, r in enumerate(rows) if r and r[0] == header_text)
        except StopIteration:
            return []
        # header is at `start`; data table headers at `start + 1`; data starts at `start + 2`.
        out = []
        for r in rows[start + 1:]:
            if not r or all(cell == "" for cell in r):
                break
            out.append(r)
        return out

    summary_rows = _section_slice("Per-Exchange Summary")
    position_rows = _section_slice("Per-Position Detail")
    strategy_rows = _section_slice("Strategy PnL (funding arb)")
    threshold_rows = _section_slice("Thresholds")

    def _to_df(section_rows: list) -> pd.DataFrame:
        """Build a DataFrame from [header, *data], trimming the trailing empty
        columns that the sheet writer pads narrower tables with (duplicate ''
        column names break Streamlit/pyarrow serialization)."""
        if not section_rows:
            return pd.DataFrame()
        header = section_rows[0]
        n = len(header)
        while n > 0 and header[n - 1] == "":
            n -= 1
        if n == 0:
            return pd.DataFrame()
        header = header[:n]
        data = [r[:n] + [""] * (n - len(r[:n])) for r in section_rows[1:]]
        return pd.DataFrame(data, columns=header)

    summary_df = _to_df(summary_rows)
    if not summary_df.empty:
        # Numeric columns
        for col in ["Positions", "Removable", "Leverage", "Gross Notional",
                    "Net Delta", "Account Equity", "Withdrawable / adjEq",
                    "OKX mgnRatio", "Account mgnRatio"]:
            if col in summary_df.columns:
                summary_df[col] = pd.to_numeric(summary_df[col], errors="coerce")

    positions_df = _to_df(position_rows)
    if not positions_df.empty:
        for col in ["Size", "Notional (signed)", "Mark", "Liq Price",
                    "Dist to Liq %", "Isolated Removable", "Funding Collected"]:
            if col in positions_df.columns:
                positions_df[col] = pd.to_numeric(positions_df[col], errors="coerce")

    strategy_df = _to_df(strategy_rows)
    if not strategy_df.empty:
        for col in ["Total Funding", "Avg Leg Size", "Funding / Notional (%)",
                    "Funding Annualized (%)"]:
            if col in strategy_df.columns:
                strategy_df[col] = pd.to_numeric(strategy_df[col], errors="coerce")

    thresholds = {}
    # Unlike the table sections, Thresholds has no column-header row, so do not
    # skip the first row.
    for r in threshold_rows if threshold_rows else []:
        if len(r) >= 2 and r[0]:
            thresholds[r[0]] = r[1]

    return snapshot_ts, summary_df, positions_df, strategy_df, thresholds


# ============================================================
#  HEADER + REFRESH
# ============================================================
col_left, col_right = st.columns([1, 4])
with col_left:
    refresh = st.button("Refresh", type="primary")
with col_right:
    st.caption(f"Dashboard loaded: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")

if refresh:
    st.cache_data.clear()

with st.spinner("Loading from Google Sheets..."):
    try:
        rows = load_sheet()
    except Exception as exc:
        st.error(f"Failed to load sheet: {exc}")
        st.stop()

snapshot_ts, summary_df, positions_df, strategy_df, thresholds = _parse_sheet(rows)

if summary_df.empty:
    st.warning("Sheet is empty or could not be parsed. Run the risk script to populate it.")
    st.stop()

st.caption(f"Sheet snapshot: **{snapshot_ts}**")


# ============================================================
#  TOP-LEVEL SUMMARY
# ============================================================
all_ok = all(summary_df["Excess Collat"] == "YES")
status_emoji = "✅" if all_ok else "⚠️"
st.subheader(f"{status_emoji} Combined summary")

exchanges_in_sheet = list(summary_df["Exchange"])
cols = st.columns(len(exchanges_in_sheet))
for col, exch in zip(cols, exchanges_in_sheet):
    row = summary_df[summary_df["Exchange"] == exch].iloc[0]
    ok = row["Excess Collat"] == "YES"
    ccy = EXCHANGE_CURRENCY.get(exch, "USD")
    with col:
        st.metric(
            label=f"{exch} {'✅' if ok else '⚠️'}",
            value=f"{row['Leverage']:.2f}x" if pd.notna(row["Leverage"]) else "N/A",
            delta=f"removable: {row['Removable']:,.0f} {ccy}" if pd.notna(row["Removable"]) else "removable: N/A",
        )

st.divider()


# ============================================================
#  TABS: Combined + per-exchange + strategy pnl + raw
# ============================================================
tab_combined, *exchange_tabs, tab_pnl, tab_raw = st.tabs(
    ["Combined"] + exchanges_in_sheet + ["Strategy PnL", "Raw sheet"]
)


def _format_positions_df(df: pd.DataFrame) -> pd.DataFrame:
    """Add a Status column and sort by tightest liq distance first."""
    if df.empty:
        return df
    df = df.copy()

    def _status(row):
        dist = row.get("Dist to Liq %")
        if pd.isna(dist) or dist == "":
            mode = str(row.get("Margin Mode", ""))
            if "cross" in mode.lower():
                return "CROSS (mgnRatio)"
            return "NO LIQ"
        try:
            return "OK" if float(dist) > 25.0 else "TIGHT"
        except (TypeError, ValueError):
            return "?"

    df["Status"] = df.apply(_status, axis=1)
    # Sort by dist asc, NaN last
    df = df.sort_values("Dist to Liq %", na_position="last").reset_index(drop=True)
    return df


_POSITION_FMT = {
    "Size": "{:,.4f}",
    "Notional (signed)": "{:+,.0f}",
    "Mark": "{:,.6f}",
    "Liq Price": "{:,.6f}",
    "Dist to Liq %": "{:.2f}%",
    "Isolated Removable": "{:,.2f}",
    "Funding Collected": "{:+,.2f}",
}


# ---- Combined tab ----
with tab_combined:
    st.subheader("All exchanges, all positions")

    # Top-line aggregate metrics across all exchanges
    a, b, c, d = st.columns(4)
    total_positions = int(summary_df["Positions"].sum())
    total_gross = summary_df["Gross Notional"].sum()
    total_net = summary_df["Net Delta"].sum()
    total_removable = summary_df["Removable"].sum()
    a.metric("Total open positions", total_positions)
    b.metric("Total gross notional (USD)", f"{total_gross:,.0f}")
    c.metric("Aggregate net delta", f"{total_net:+,.0f}")
    d.metric("Total removable", f"{total_removable:,.0f}")

    st.markdown("##### Per-exchange summary")
    display_summary = summary_df.copy()
    # Format numeric columns for display
    for col, fmt in [
        ("Removable", "{:,.2f}"),
        ("Leverage", "{:.2f}x"),
        ("Gross Notional", "{:,.2f}"),
        ("Net Delta", "{:+,.2f}"),
        ("Account Equity", "{:,.2f}"),
        ("Withdrawable / adjEq", "{:,.2f}"),
        ("OKX mgnRatio", "{:.2f}x"),
        ("Account mgnRatio", "{:.2f}x"),
    ]:
        if col in display_summary.columns:
            display_summary[col] = display_summary[col].apply(
                lambda v, _fmt=fmt: _fmt.format(v) if pd.notna(v) else "—"
            )
    st.dataframe(display_summary, use_container_width=True, hide_index=True)

    st.markdown("##### All positions, sorted by tightest distance to liquidation")
    if positions_df.empty:
        st.info("No open positions across any exchange.")
    else:
        all_df = _format_positions_df(positions_df)
        st.dataframe(
            all_df.style.format(_POSITION_FMT, na_rep="—"),
            use_container_width=True,
            hide_index=True,
        )

    if thresholds:
        st.markdown("##### Thresholds")
        thr_df = pd.DataFrame(
            [(k, v) for k, v in thresholds.items()], columns=["Threshold", "Value"]
        )
        st.dataframe(thr_df, use_container_width=True, hide_index=True)


# ---- Per-exchange tabs ----
for tab, exch in zip(exchange_tabs, exchanges_in_sheet):
    with tab:
        row = summary_df[summary_df["Exchange"] == exch].iloc[0]
        ccy = EXCHANGE_CURRENCY.get(exch, "USD")
        st.subheader(f"{exch} positions ({ccy})")

        mgn_col = "Account mgnRatio" if "Account mgnRatio" in summary_df.columns else "OKX mgnRatio"
        mgn_val = row.get(mgn_col)

        # Per-exchange top metrics
        if pd.notna(mgn_val):
            a, b, c, d, e = st.columns(5)
            a.metric("Open positions", int(row["Positions"]) if pd.notna(row["Positions"]) else 0)
            b.metric("Net delta", f"{row['Net Delta']:+,.0f} {ccy}" if pd.notna(row["Net Delta"]) else "N/A")
            c.metric("Gross notional", f"{row['Gross Notional']:,.0f} {ccy}" if pd.notna(row["Gross Notional"]) else "N/A")
            d.metric("Removable", f"{row['Removable']:,.0f} {ccy}" if pd.notna(row["Removable"]) else "N/A")
            equity_buffer = (1 - 1.0 / mgn_val) * 100 if mgn_val and mgn_val > 0 else 0.0
            e.metric("Account mgnRatio", f"{mgn_val:.2f}x", delta=f"{equity_buffer:.1f}% equity buffer", delta_color="off")
        else:
            a, b, c, d = st.columns(4)
            a.metric("Open positions", int(row["Positions"]) if pd.notna(row["Positions"]) else 0)
            b.metric("Net delta", f"{row['Net Delta']:+,.0f} {ccy}" if pd.notna(row["Net Delta"]) else "N/A")
            c.metric("Gross notional", f"{row['Gross Notional']:,.0f} {ccy}" if pd.notna(row["Gross Notional"]) else "N/A")
            d.metric("Removable", f"{row['Removable']:,.0f} {ccy}" if pd.notna(row["Removable"]) else "N/A")

        # Per-exchange positions table
        ex_positions = positions_df[positions_df["Exchange"] == exch] if not positions_df.empty else pd.DataFrame()
        if ex_positions.empty:
            st.info("No open positions.")
        else:
            ex_positions = _format_positions_df(ex_positions)
            # Drop the Exchange column since this tab is already exchange-specific
            display_cols = [c for c in ex_positions.columns if c != "Exchange"]
            st.dataframe(
                ex_positions[display_cols].style.format(_POSITION_FMT, na_rep="—"),
                use_container_width=True,
                hide_index=True,
            )

        # OKX-specific footer explaining the framing
        if exch == "OKX":
            if pd.notna(mgn_val) and mgn_val > 0:
                buffer_pct = (1 - 1.0 / mgn_val) * 100
                st.caption(
                    f"OKX cross-margin safety is evaluated at the account level via mgnRatio "
                    f"(adjEq / mmr). Liquidation triggers at 1.00x; OKX itself warns at 3.00x. "
                    f"Current: **{mgn_val:.2f}x** ({buffer_pct:.1f}% equity buffer). "
                    f"Cross positions show no per-position liq price by design; safety is the account-level ratio."
                )


# ---- Strategy PnL tab ----
with tab_pnl:
    st.subheader("Strategy PnL (funding arb)")

    if strategy_df.empty:
        st.info(
            "No Strategy PnL section found in the sheet. "
            "Run the latest risk script (with funding collection) to populate it."
        )
    else:
        # Top-line metrics
        total_funding = strategy_df["Total Funding"].sum() if "Total Funding" in strategy_df.columns else float("nan")
        n_strats = len(strategy_df)
        best_idx = None
        if "Funding Annualized (%)" in strategy_df.columns and strategy_df["Funding Annualized (%)"].notna().any():
            best_idx = strategy_df["Funding Annualized (%)"].idxmax()

        m1, m2, m3 = st.columns(3)
        m1.metric("Strategies", n_strats)
        m2.metric("Total funding collected", f"{total_funding:+,.2f}" if pd.notna(total_funding) else "N/A")
        if best_idx is not None:
            best = strategy_df.loc[best_idx]
            m3.metric(
                f"Best annualized: {best['Strategy']}",
                f"{best['Funding Annualized (%)']:.2f}%",
            )
        else:
            m3.metric("Best annualized", "N/A")

        st.markdown("##### Per-strategy funding")
        display_strat = strategy_df.copy()
        if "Funding Annualized (%)" in display_strat.columns:
            display_strat = display_strat.sort_values(
                "Funding Annualized (%)", ascending=False, na_position="last"
            ).reset_index(drop=True)
        st.dataframe(
            display_strat.style.format({
                "Total Funding": "{:+,.2f}",
                "Avg Leg Size": "{:,.4f}",
                "Funding / Notional (%)": "{:.4f}%",
                "Funding Annualized (%)": "{:.2f}%",
            }, na_rep="—"),
            use_container_width=True,
            hide_index=True,
        )

        # Per-position funding breakdown (legs)
        if not positions_df.empty and "Funding Collected" in positions_df.columns:
            st.markdown("##### Per-leg funding detail")
            leg_cols = [c for c in ["Exchange", "Symbol", "Direction",
                                    "Notional (signed)", "Funding Collected"]
                        if c in positions_df.columns]
            legs_df = positions_df[leg_cols].sort_values("Funding Collected", ascending=False, na_position="last")
            st.dataframe(
                legs_df.style.format({
                    "Notional (signed)": "{:+,.0f}",
                    "Funding Collected": "{:+,.2f}",
                }, na_rep="—"),
                use_container_width=True,
                hide_index=True,
            )

        # Surface the funding-window context from Thresholds if present
        notes = {k: v for k, v in thresholds.items()
                 if k in ("Funding window", "Strategy note")}
        if notes:
            for k, v in notes.items():
                st.caption(f"**{k}:** {v}")


# ---- Raw sheet tab ----
with tab_raw:
    st.subheader("Raw sheet contents")
    st.caption(f"Snapshot: {snapshot_ts}")
    raw_df = pd.DataFrame(rows)
    st.dataframe(raw_df, use_container_width=True, hide_index=True)
