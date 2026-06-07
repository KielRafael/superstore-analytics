# ── STREAMLIT PAGE CONFIG (must be first Streamlit call) ──────────────────────
import streamlit as st

st.set_page_config(
    page_title="Superstore Management System",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── STDLIB ────────────────────────────────────────────────────────────────────
import hashlib
import os
import sqlite3
from typing import List, Optional

# ── THIRD-PARTY ───────────────────────────────────────────────────────────────
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
CSV_PATH = os.path.join(os.path.dirname(__file__), "Sample_-_Superstore.csv")
DB_PATH  = os.path.join(os.path.dirname(__file__), "superstore.db")

# ── DATABASE HELPERS ──────────────────────────────────────────────────────────

def is_sqlite() -> bool:
    return st.session_state.get("db_engine", "SQLite") == "SQLite"


def ph() -> str:
    return "?" if is_sqlite() else "%s"


@st.cache_resource
def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_conn():
    if is_sqlite():
        return get_sqlite_conn()
    cfg = st.session_state.get("mysql_cfg", {})
    try:
        import mysql.connector
        return mysql.connector.connect(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 3306)),
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", "superstore"),
        )
    except Exception as e:
        st.error(f"MySQL connection failed: {e}")
        return None


def query_df(sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    try:
        conn = get_conn()
        if conn is None:
            return pd.DataFrame()
        if is_sqlite():
            return pd.read_sql_query(sql, conn, params=params or ())
        else:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            cursor.close()
            conn.close()
            return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        st.error(f"Query error: {e}")
        return pd.DataFrame()


def write_db(sql: str, params: Optional[tuple] = None) -> bool:
    try:
        conn = get_conn()
        if conn is None:
            return False
        cursor = conn.cursor()
        cursor.execute(sql, params or ())
        conn.commit()
        if not is_sqlite():
            cursor.close()
            conn.close()
        return True
    except Exception as e:
        st.error(f"Write error: {e}")
        return False


def executemany_db(sql: str, data: List[tuple]) -> bool:
    try:
        conn = get_conn()
        if conn is None:
            return False
        cursor = conn.cursor()
        cursor.executemany(sql, data)
        conn.commit()
        if not is_sqlite():
            cursor.close()
            conn.close()
        return True
    except Exception as e:
        st.error(f"Bulk insert error: {e}")
        return False


# ── DDL ───────────────────────────────────────────────────────────────────────

def create_tables() -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS CUSTOMER (
            customer_id   TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            segment       TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS LOCATION (
            location_id TEXT PRIMARY KEY,
            city        TEXT NOT NULL,
            state       TEXT NOT NULL,
            postal_code TEXT,
            region      TEXT NOT NULL,
            country     TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS PRODUCT (
            product_id   TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            category     TEXT NOT NULL,
            sub_category TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS ORDER_HEADER (
            order_id    TEXT PRIMARY KEY,
            order_date  TEXT NOT NULL,
            ship_date   TEXT NOT NULL,
            ship_mode   TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            location_id TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES CUSTOMER(customer_id),
            FOREIGN KEY (location_id) REFERENCES LOCATION(location_id)
        )""",
        """CREATE TABLE IF NOT EXISTS ORDER_DETAIL (
            detail_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   TEXT NOT NULL,
            product_id TEXT NOT NULL,
            sales      REAL NOT NULL,
            quantity   INTEGER NOT NULL,
            discount   REAL NOT NULL,
            profit     REAL NOT NULL,
            FOREIGN KEY (order_id)   REFERENCES ORDER_HEADER(order_id),
            FOREIGN KEY (product_id) REFERENCES PRODUCT(product_id)
        )""",
    ]
    for stmt in stmts:
        write_db(stmt)


# ── ETL ───────────────────────────────────────────────────────────────────────

def _md5_id(prefix: str, *parts: str) -> str:
    """Generate a short deterministic ID via MD5 hash (used for LOCATION only)."""
    raw = "|".join(str(p).strip().lower() for p in parts)
    return f"{prefix}-{hashlib.md5(raw.encode()).hexdigest()[:8].upper()}"


def is_db_populated() -> bool:
    df = query_df("SELECT COUNT(*) AS cnt FROM ORDER_DETAIL")
    if df.empty:
        return False
    return int(df.iloc[0]["cnt"]) > 0


def import_csv(csv_path: str) -> None:
    """
    Load Sample_-_Superstore.csv into the relational database.

    Uses the real IDs and names from the CSV:
      - Customer  → Customer ID, Customer Name, Segment
      - Product   → Product ID, Product Name, Category, Sub-Category
      - Order     → Order ID, Order Date, Ship Date, Ship Mode
      - Location  → MD5 hash of (City, State, Postal Code)   [no natural key in CSV]
    """
    try:
        df = pd.read_csv(csv_path, encoding="latin1")
    except Exception as e:
        st.error(f"Cannot read CSV: {e}")
        return

    df.columns = [c.strip() for c in df.columns]

    # ── Date normalisation  (M/D/YYYY → YYYY-MM-DD) ──────────────────────────
    df["_order_date"] = (
        pd.to_datetime(df["Order Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    )
    df["_ship_date"] = (
        pd.to_datetime(df["Ship Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    )

    # ── Location ID (MD5 of city+state+postal) ────────────────────────────────
    df["_loc_id"] = (
        "LOC-"
        + (
            df["City"].str.strip().str.lower()
            + "|"
            + df["State"].str.strip().str.lower()
            + "|"
            + df["Postal Code"].astype(str).str.strip().str.lower()
        ).apply(lambda s: hashlib.md5(s.encode()).hexdigest()[:8].upper())
    )

    # ── CUSTOMER ──────────────────────────────────────────────────────────────
    cust_df = (
        df[["Customer ID", "Customer Name", "Segment"]]
        .drop_duplicates(subset="Customer ID")
    )
    customer_data = list(
        zip(cust_df["Customer ID"], cust_df["Customer Name"], cust_df["Segment"])
    )

    # ── LOCATION ──────────────────────────────────────────────────────────────
    loc_df = (
        df[["_loc_id", "City", "State", "Postal Code", "Region", "Country"]]
        .drop_duplicates(subset="_loc_id")
    )
    location_data = list(
        zip(
            loc_df["_loc_id"],
            loc_df["City"],
            loc_df["State"],
            loc_df["Postal Code"].astype(str),
            loc_df["Region"],
            loc_df["Country"],
        )
    )

    # ── PRODUCT ───────────────────────────────────────────────────────────────
    prod_df = (
        df[["Product ID", "Product Name", "Category", "Sub-Category"]]
        .drop_duplicates(subset="Product ID")
    )
    product_data = list(
        zip(
            prod_df["Product ID"],
            prod_df["Product Name"],
            prod_df["Category"],
            prod_df["Sub-Category"],
        )
    )

    # ── ORDER_HEADER (one row per unique Order ID) ────────────────────────────
    hdr_df = (
        df[["Order ID", "_order_date", "_ship_date", "Ship Mode",
            "Customer ID", "_loc_id"]]
        .drop_duplicates(subset="Order ID")
    )
    header_data = list(
        zip(
            hdr_df["Order ID"],
            hdr_df["_order_date"],
            hdr_df["_ship_date"],
            hdr_df["Ship Mode"],
            hdr_df["Customer ID"],
            hdr_df["_loc_id"],
        )
    )

    # ── ORDER_DETAIL (every row becomes one detail line) ─────────────────────
    detail_data = list(
        zip(
            df["Order ID"],
            df["Product ID"],
            df["Sales"].astype(float),
            df["Quantity"].astype(int),
            df["Discount"].astype(float),
            df["Profit"].astype(float),
        )
    )

    p = ph()
    executemany_db(
        f"INSERT OR IGNORE INTO CUSTOMER "
        f"(customer_id, customer_name, segment) VALUES ({p},{p},{p})",
        customer_data,
    )
    executemany_db(
        f"INSERT OR IGNORE INTO LOCATION "
        f"(location_id, city, state, postal_code, region, country) "
        f"VALUES ({p},{p},{p},{p},{p},{p})",
        location_data,
    )
    executemany_db(
        f"INSERT OR IGNORE INTO PRODUCT "
        f"(product_id, product_name, category, sub_category) VALUES ({p},{p},{p},{p})",
        product_data,
    )
    executemany_db(
        f"INSERT OR IGNORE INTO ORDER_HEADER "
        f"(order_id, order_date, ship_date, ship_mode, customer_id, location_id) "
        f"VALUES ({p},{p},{p},{p},{p},{p})",
        header_data,
    )
    executemany_db(
        f"INSERT INTO ORDER_DETAIL "
        f"(order_id, product_id, sales, quantity, discount, profit) "
        f"VALUES ({p},{p},{p},{p},{p},{p})",
        detail_data,
    )


# ── ANALYTICS QUERIES ─────────────────────────────────────────────────────────

def year_fn() -> str:
    return "strftime('%Y', oh.order_date)" if is_sqlite() else "YEAR(oh.order_date)"


def month_fn() -> str:
    return (
        "strftime('%Y-%m', oh.order_date)"
        if is_sqlite()
        else "DATE_FORMAT(oh.order_date, '%Y-%m')"
    )


ANALYTICS_QUERIES = {
    # ── Query 1 ──────────────────────────────────────────────────────────────
    "Top 10 Customers by Total Purchase": {
        "sql": lambda: """
            SELECT c.customer_name,
                   c.segment,
                   ROUND(SUM(od.sales),  2) AS total_sales,
                   ROUND(SUM(od.profit), 2) AS total_profit,
                   COUNT(DISTINCT oh.order_id) AS total_orders
            FROM CUSTOMER c
            JOIN ORDER_HEADER oh ON oh.customer_id = c.customer_id
            JOIN ORDER_DETAIL  od ON od.order_id   = oh.order_id
            GROUP BY c.customer_id, c.customer_name, c.segment
            ORDER BY total_sales DESC
            LIMIT 10
        """,
        "x": "customer_name",
        "y": "total_sales",
        "chart": "bar",
        "title": "Top 10 Customers by Total Sales",
    },
    # ── Query 2 ──────────────────────────────────────────────────────────────
    "Annual Sales Trend (2014-2017)": {
        "sql": lambda: f"""
            SELECT {year_fn()} AS year,
                   ROUND(SUM(od.sales),  2) AS total_sales,
                   ROUND(SUM(od.profit), 2) AS total_profit
            FROM ORDER_HEADER oh
            JOIN ORDER_DETAIL od ON od.order_id = oh.order_id
            GROUP BY year
            ORDER BY year
        """,
        "x": "year",
        "y": ["total_sales", "total_profit"],
        "chart": "line",
        "title": "Annual Sales and Profit Trend",
    },
    # ── Query 3 ──────────────────────────────────────────────────────────────
    "Profit by Sub-Category": {
        "sql": lambda: """
            SELECT p.category,
                   p.sub_category,
                   ROUND(SUM(od.sales),  2) AS total_sales,
                   ROUND(SUM(od.profit), 2) AS total_profit,
                   ROUND(AVG(od.discount) * 100, 1) AS avg_discount_pct
            FROM PRODUCT p
            JOIN ORDER_DETAIL od ON od.product_id = p.product_id
            GROUP BY p.category, p.sub_category
            ORDER BY total_profit DESC
        """,
        "x": "sub_category",
        "y": "total_profit",
        "chart": "bar",
        "title": "Profit by Sub-Category",
    },
    # ── Query 4 ──────────────────────────────────────────────────────────────
    "Sales by Region and Segment": {
        "sql": lambda: """
            SELECT l.region,
                   c.segment,
                   ROUND(SUM(od.sales), 2) AS total_sales
            FROM LOCATION l
            JOIN ORDER_HEADER oh ON oh.location_id = l.location_id
            JOIN ORDER_DETAIL  od ON od.order_id   = oh.order_id
            JOIN CUSTOMER       c ON  c.customer_id = oh.customer_id
            GROUP BY l.region, c.segment
            ORDER BY l.region, c.segment
        """,
        "x": "region",
        "y": "total_sales",
        "hue": "segment",
        "chart": "bar_grouped",
        "title": "Sales by Region and Segment",
    },
    # ── Query 5 ──────────────────────────────────────────────────────────────
    "Discount Impact on Profit": {
        "sql": lambda: """
            SELECT
                CASE
                    WHEN discount = 0       THEN 'No Discount'
                    WHEN discount <= 0.10   THEN '1-10%'
                    WHEN discount <= 0.20   THEN '11-20%'
                    WHEN discount <= 0.30   THEN '21-30%'
                    ELSE '>30%'
                END AS discount_bucket,
                COUNT(*)             AS num_orders,
                ROUND(AVG(profit),2) AS avg_profit,
                ROUND(SUM(sales), 2) AS total_sales
            FROM ORDER_DETAIL
            GROUP BY discount_bucket
            ORDER BY avg_profit DESC
        """,
        "x": "discount_bucket",
        "y": "avg_profit",
        "chart": "bar",
        "title": "Average Profit by Discount Bucket",
    },
    # ── Query 6 ──────────────────────────────────────────────────────────────
    "Top 10 States by Average Order Value (Profitable Orders Only)": {
        "sql": lambda: """
            SELECT l.state,
                   l.region,
                   COUNT(DISTINCT oh.order_id)  AS total_orders,
                   ROUND(AVG(od.sales), 2)      AS avg_order_value,
                   ROUND(SUM(od.profit), 2)     AS total_profit
            FROM LOCATION l
            JOIN ORDER_HEADER oh ON oh.location_id = l.location_id
            JOIN ORDER_DETAIL  od ON od.order_id   = oh.order_id
            WHERE od.profit > 0
            GROUP BY l.state, l.region
            ORDER BY avg_order_value DESC
            LIMIT 10
        """,
        "x": "state",
        "y": "avg_order_value",
        "chart": "bar",
        "title": "Top 10 States by Avg Order Value (Profitable Orders Only)",
    },
    # ── Query 7 ──────────────────────────────────────────────────────────────
    "Ship Mode Performance for Corporate Segment": {
        "sql": lambda: """
            SELECT oh.ship_mode,
                   COUNT(DISTINCT oh.order_id)  AS total_orders,
                   ROUND(SUM(od.sales),  2)     AS total_sales,
                   ROUND(AVG(od.sales),  2)     AS avg_sales_per_item,
                   ROUND(MAX(od.sales),  2)     AS max_single_sale,
                   ROUND(SUM(od.profit), 2)     AS total_profit
            FROM ORDER_HEADER oh
            JOIN ORDER_DETAIL od ON od.order_id   = oh.order_id
            JOIN CUSTOMER      c ON  c.customer_id = oh.customer_id
            WHERE c.segment = 'Corporate'
            GROUP BY oh.ship_mode
            ORDER BY total_sales DESC
        """,
        "x": "ship_mode",
        "y": "total_sales",
        "chart": "bar",
        "title": "Ship Mode Performance — Corporate Segment",
    },
    # ── Query 8 ──────────────────────────────────────────────────────────────
    "Product Category Sales Volume in West Region": {
        "sql": lambda: """
            SELECT p.category,
                   p.sub_category,
                   COUNT(DISTINCT oh.order_id)      AS total_orders,
                   SUM(od.quantity)                 AS total_quantity_sold,
                   ROUND(SUM(od.sales),  2)         AS total_sales,
                   ROUND(MIN(od.discount) * 100, 1) AS min_discount_pct,
                   ROUND(AVG(od.profit), 2)         AS avg_profit_per_item
            FROM PRODUCT p
            JOIN ORDER_DETAIL  od ON od.product_id  = p.product_id
            JOIN ORDER_HEADER  oh ON oh.order_id     = od.order_id
            JOIN LOCATION       l ON  l.location_id  = oh.location_id
            WHERE l.region = 'West'
            GROUP BY p.category, p.sub_category
            ORDER BY total_quantity_sold DESC
        """,
        "x": "sub_category",
        "y": "total_quantity_sold",
        "chart": "bar",
        "title": "Product Category Sales Volume — West Region",
    },
}


# ── CHART HELPERS ─────────────────────────────────────────────────────────────

BLUE = "#2563EB"


def _fmt_dollar(x, _pos):
    if abs(x) >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if abs(x) >= 1_000:
        return f"${x/1_000:.0f}K"
    return f"${x:,.0f}"


dollar_fmt = mticker.FuncFormatter(_fmt_dollar)


def _clean_axes(ax):
    ax.spines[["top", "right"]].set_visible(False)


def render_bar(df: pd.DataFrame, x: str, y: str, title: str):
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(df[x].astype(str), df[y], color=BLUE)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.yaxis.set_major_formatter(dollar_fmt)
    plt.xticks(rotation=45, ha="right", fontsize=8)
    _clean_axes(ax)
    plt.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def render_line(df: pd.DataFrame, x: str, y_cols: List[str], title: str):
    fig, ax = plt.subplots(figsize=(7, 4))
    colors = [BLUE, "#F59E0B"]
    for col, color in zip(y_cols, colors):
        ax.plot(df[x].astype(str), df[col], marker="o", label=col, color=color)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel(x)
    ax.yaxis.set_major_formatter(dollar_fmt)
    ax.legend()
    _clean_axes(ax)
    plt.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def render_bar_grouped(df: pd.DataFrame, x: str, y: str, hue: str, title: str):
    pivot = df.pivot_table(index=x, columns=hue, values=y, aggfunc="sum").fillna(0)
    fig, ax = plt.subplots(figsize=(7, 4))
    pivot.plot(kind="bar", ax=ax, colormap="Blues")
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.yaxis.set_major_formatter(dollar_fmt)
    plt.xticks(rotation=45, ha="right", fontsize=8)
    _clean_axes(ax)
    plt.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


# ── PAGE: DASHBOARD ───────────────────────────────────────────────────────────

def page_dashboard():
    st.title("Dashboard")

    metrics_sql = """
        SELECT
            ROUND(SUM(od.sales),  2)       AS total_sales,
            ROUND(SUM(od.profit), 2)       AS total_profit,
            COUNT(DISTINCT oh.order_id)    AS total_orders,
            COUNT(DISTINCT oh.customer_id) AS total_customers
        FROM ORDER_DETAIL od
        JOIN ORDER_HEADER oh ON oh.order_id = od.order_id
    """
    mdf = query_df(metrics_sql)

    if mdf.empty or mdf.iloc[0]["total_sales"] is None:
        st.warning("No data found. Use the sidebar button to initialize the database.")
        return

    total_sales     = float(mdf.iloc[0]["total_sales"])
    total_profit    = float(mdf.iloc[0]["total_profit"])
    total_orders    = int(mdf.iloc[0]["total_orders"])
    total_customers = int(mdf.iloc[0]["total_customers"])
    margin          = (total_profit / total_sales * 100) if total_sales else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Sales",     f"${total_sales:,.2f}")
    c2.metric("Total Profit",    f"${total_profit:,.2f}")
    c3.metric("Total Orders",    f"{total_orders:,}")
    c4.metric("Total Customers", f"{total_customers:,}")
    c5.metric("Profit Margin",   f"{margin:.1f}%")

    st.markdown("---")

    cat_sql = """
        SELECT p.category, ROUND(SUM(od.profit), 2) AS profit
        FROM PRODUCT p
        JOIN ORDER_DETAIL od ON od.product_id = p.product_id
        GROUP BY p.category ORDER BY profit DESC
    """
    reg_sql = """
        SELECT l.region, ROUND(SUM(od.sales), 2) AS sales
        FROM LOCATION l
        JOIN ORDER_HEADER oh ON oh.location_id = l.location_id
        JOIN ORDER_DETAIL  od ON od.order_id   = oh.order_id
        GROUP BY l.region ORDER BY sales DESC
    """
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Profit by Category")
        cdf = query_df(cat_sql)
        if not cdf.empty:
            st.bar_chart(cdf.set_index("category")["profit"])
    with col2:
        st.subheader("Sales by Region")
        rdf = query_df(reg_sql)
        if not rdf.empty:
            st.bar_chart(rdf.set_index("region")["sales"])

    st.markdown("---")
    st.subheader("Sales vs Profit by Sub-Category")
    sub_sql = """
        SELECT p.sub_category,
               ROUND(SUM(od.sales),  2) AS sales,
               ROUND(SUM(od.profit), 2) AS profit
        FROM PRODUCT p
        JOIN ORDER_DETAIL od ON od.product_id = p.product_id
        GROUP BY p.sub_category ORDER BY sales DESC
    """
    sdf = query_df(sub_sql)
    if not sdf.empty:
        st.bar_chart(sdf.set_index("sub_category")[["sales", "profit"]])


# ── PAGE: VIEW DATA ───────────────────────────────────────────────────────────

TABLES = ["CUSTOMER", "LOCATION", "PRODUCT", "ORDER_HEADER", "ORDER_DETAIL"]


def page_view_data():
    st.title("View Data")

    table = st.selectbox("Select Table", TABLES)

    with st.expander("Filter Options"):
        sample_df = query_df(f"SELECT * FROM {table} LIMIT 1")
        columns   = list(sample_df.columns) if not sample_df.empty else []
        filter_col = st.selectbox("Filter Column", ["(none)"] + columns)
        filter_val = st.text_input("Filter Value (contains, case-insensitive)")

    if filter_col != "(none)" and filter_val:
        p = ph()
        sql    = (
            f"SELECT * FROM {table} "
            f"WHERE LOWER(CAST({filter_col} AS TEXT)) LIKE LOWER({p})"
        )
        params = (f"%{filter_val}%",)
    else:
        sql    = f"SELECT * FROM {table}"
        params = None

    df = query_df(sql, params)
    st.caption(f"{len(df):,} record(s) displayed")
    st.dataframe(df, use_container_width=True, height=400)

    if not df.empty:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download as CSV",
            data=csv_bytes,
            file_name=f"{table.lower()}_export.csv",
            mime="text/csv",
        )


# ── PAGE: INSERT RECORD ───────────────────────────────────────────────────────

def page_insert():
    st.title("Insert Record")
    tab_cust, tab_prod, tab_loc = st.tabs(["Customer", "Product", "Location"])
    p = ph()

    # Tab 1 — Customer
    with tab_cust:
        with st.form("frm_customer", clear_on_submit=True):
            cname   = st.text_input("Customer Name")
            segment = st.selectbox("Segment", ["Consumer", "Corporate", "Home Office"])
            submitted = st.form_submit_button("Add Customer")
        if submitted:
            if not cname.strip():
                st.error("Customer Name is required.")
            else:
                cid = _md5_id("CUST", cname.strip(), segment)
                ok  = write_db(
                    f"INSERT OR IGNORE INTO CUSTOMER "
                    f"(customer_id, customer_name, segment) VALUES ({p},{p},{p})",
                    (cid, cname.strip(), segment),
                )
                if ok:
                    st.success(f"Customer added. ID: {cid}")

    # Tab 2 — Product
    with tab_prod:
        with st.form("frm_product", clear_on_submit=True):
            pname    = st.text_input("Product Name")
            category = st.selectbox("Category", ["Furniture", "Office Supplies", "Technology"])
            sub_cat  = st.text_input("Sub-Category")
            submitted2 = st.form_submit_button("Add Product")
        if submitted2:
            if not pname.strip() or not sub_cat.strip():
                st.error("Product Name and Sub-Category are required.")
            else:
                pid = _md5_id("PROD", pname.strip(), category, sub_cat.strip())
                ok  = write_db(
                    f"INSERT OR IGNORE INTO PRODUCT "
                    f"(product_id, product_name, category, sub_category) "
                    f"VALUES ({p},{p},{p},{p})",
                    (pid, pname.strip(), category, sub_cat.strip()),
                )
                if ok:
                    st.success(f"Product added. ID: {pid}")

    # Tab 3 — Location
    with tab_loc:
        with st.form("frm_location", clear_on_submit=True):
            city        = st.text_input("City")
            state       = st.text_input("State")
            postal_code = st.text_input("Postal Code")
            region      = st.selectbox("Region", ["Central", "East", "South", "West"])
            country     = st.text_input("Country", value="United States")
            submitted3  = st.form_submit_button("Add Location")
        if submitted3:
            if not city.strip() or not state.strip() or not country.strip():
                st.error("City, State, and Country are required.")
            else:
                lid = _md5_id("LOC", city.strip(), state.strip(), postal_code.strip())
                ok  = write_db(
                    f"INSERT OR IGNORE INTO LOCATION "
                    f"(location_id, city, state, postal_code, region, country) "
                    f"VALUES ({p},{p},{p},{p},{p},{p})",
                    (lid, city.strip(), state.strip(), postal_code.strip(),
                     region, country.strip()),
                )
                if ok:
                    st.success(f"Location added. ID: {lid}")


# ── PAGE: ANALYTICS ───────────────────────────────────────────────────────────

def page_analytics():
    st.title("Analytics and Insight")

    query_name = st.selectbox("Select Analysis", list(ANALYTICS_QUERIES.keys()))
    meta       = ANALYTICS_QUERIES[query_name]
    sql_str    = meta["sql"]()

    with st.expander("View SQL Query"):
        st.code(sql_str.strip(), language="sql")

    if st.button("Run Analysis", type="primary"):
        df = query_df(sql_str)
        if df.empty:
            st.warning("No results returned.")
            return

        col_tbl, col_chart = st.columns([2, 3])
        with col_tbl:
            st.dataframe(df, use_container_width=True, height=350)
        with col_chart:
            chart_type = meta["chart"]
            title      = meta["title"]
            x          = meta["x"]
            y          = meta["y"]

            if chart_type == "bar":
                render_bar(df, x, y, title)
            elif chart_type == "line":
                render_line(df, x, y, title)
            elif chart_type == "bar_grouped":
                render_bar_grouped(df, x, y, meta["hue"], title)

        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download Results as CSV",
            data=csv_bytes,
            file_name=f"analysis_{query_name[:20].replace(' ', '_').lower()}.csv",
            mime="text/csv",
        )


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

def render_sidebar():
    with st.sidebar:
        st.title("Superstore Management System")
        st.markdown("---")

        db_engine = st.radio("Database Engine", ["SQLite", "MySQL"])
        st.session_state["db_engine"] = db_engine

        if db_engine == "MySQL":
            with st.expander("MySQL Configuration"):
                host     = st.text_input("Host",     value="localhost")
                port     = st.text_input("Port",     value="3306")
                user     = st.text_input("User",     value="root")
                password = st.text_input("Password", type="password")
                database = st.text_input("Database", value="superstore")
                st.session_state["mysql_cfg"] = {
                    "host": host, "port": port, "user": user,
                    "password": password, "database": database,
                }

        st.markdown("---")
        nav = st.radio(
            "Navigation",
            ["Dashboard", "View Data", "Insert Record", "Analytics"],
        )
        st.markdown("---")

        if st.button("Initialize / Reset Database", use_container_width=True):
            # Drop all tables first to ensure a clean slate
            for tbl in ["ORDER_DETAIL", "ORDER_HEADER", "PRODUCT", "CUSTOMER", "LOCATION"]:
                write_db(f"DROP TABLE IF EXISTS {tbl}")
            create_tables()
            with st.spinner("Loading data from CSV..."):
                import_csv(CSV_PATH)
            st.session_state["app_initialized"] = True
            st.success("Database initialized.")

        st.caption(f"Engine: {db_engine}")

    return nav


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    # Auto-initialize on first load
    if "app_initialized" not in st.session_state:
        create_tables()
        if not is_db_populated():
            with st.spinner("Loading data from CSV into database..."):
                import_csv(CSV_PATH)
        st.session_state["app_initialized"] = True

    nav = render_sidebar()

    if nav == "Dashboard":
        page_dashboard()
    elif nav == "View Data":
        page_view_data()
    elif nav == "Insert Record":
        page_insert()
    elif nav == "Analytics":
        page_analytics()


if __name__ == "__main__":
    main()