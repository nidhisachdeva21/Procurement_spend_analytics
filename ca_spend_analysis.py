"""
CALIFORNIA STATE PROCUREMENT - SPEND ANALYTICS & VENDOR RATIONALISATION
FY2014-15 | Corrections & Rehabilitation, Water Resources, Correctional Health Care

Run:  python ca_spend_analysis.py
"""
import os, re, collections
from difflib import SequenceMatcher
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

FILE = "po_final.csv"
OUT = "output"
os.makedirs(OUT, exist_ok=True)
RESULTS = {}

def money(x):
    return pd.to_numeric(pd.Series(x).astype(str)
                         .str.replace(r"[\$,()]", "", regex=True).str.strip(),
                         errors="coerce")

def hdr(n, t):
    print("\n" + "=" * 78); print(f"STEP {n}  {t}"); print("=" * 78)

# =============================================================== STEP 1 CLEAN
hdr(1, "LOAD, CLEAN & DATA QUALITY AUDIT")
df = pd.read_csv(FILE, low_memory=False)
raw_rows = len(df)
print(f"Rows loaded: {raw_rows:,}")

df["unit_price"] = money(df["Unit Price"])
df["total"] = money(df["Total Price"])
df["qty"] = pd.to_numeric(df["Quantity"], errors="coerce")
df["purchase_date"] = pd.to_datetime(df["Purchase Date"], errors="coerce", format="mixed")

audit = []

n = df["total"].isna().sum() | 0
df = df.dropna(subset=["total", "Supplier Name"])
audit.append(("Rows with unparseable amount or missing supplier", raw_rows - len(df), np.nan))

before, spend_before = len(df), df["total"].sum()
df = df[df["total"] > 0]
audit.append(("Zero-value lines removed", before - len(df), 0.0))

# Duplicate purchase lines: identical supplier, item, qty, price, date, department
key = ["Department Name", "Supplier Name", "Item Name", "qty", "unit_price", "total", "Purchase Date"]
dupes = df.duplicated(subset=key, keep="first")
dupe_value = df.loc[dupes, "total"].sum()
df = df[~dupes]
audit.append(("Duplicate purchase lines removed", int(dupes.sum()), dupe_value))
print(f"  removed {dupes.sum():,} duplicate lines worth ${dupe_value:,.0f}")

# Dates outside the stated fiscal year = data integrity problem (kept, flagged)
fy_start, fy_end = pd.Timestamp("2014-07-01"), pd.Timestamp("2015-06-30")
out_of_fy = df["purchase_date"].notna() & ((df["purchase_date"] < fy_start) | (df["purchase_date"] > fy_end))
audit.append(("Lines dated outside FY2014-15 (flagged, kept)", int(out_of_fy.sum()),
              df.loc[out_of_fy, "total"].sum()))

unknown = df["Supplier Name"].str.strip().str.lower() == "unknown"
audit.append(("Lines with supplier recorded as 'Unknown'", int(unknown.sum()),
              df.loc[unknown, "total"].sum()))

audit_df = pd.DataFrame(audit, columns=["issue", "lines", "value"])
audit_df.to_csv(f"{OUT}/01_data_quality_audit.csv", index=False)
print("\nDATA QUALITY AUDIT")
for _, r in audit_df.iterrows():
    v = f"${r['value']:,.0f}" if pd.notna(r["value"]) else "-"
    print(f"  {r['issue']:<48} {r['lines']:>7,}   {v:>16}")

print(f"\nClean records: {len(df):,}   Total spend: ${df['total'].sum():,.0f}")
RESULTS["raw_rows"] = raw_rows
RESULTS["clean_rows"] = len(df)
RESULTS["total_spend"] = df["total"].sum()
RESULTS["dupe_lines"] = int(dupes.sum())
RESULTS["dupe_value"] = dupe_value

# ================================================== STEP 2 SUPPLIER NORMALISE
hdr(2, "SUPPLIER NAME NORMALISATION")
raw_suppliers = df["Supplier Name"].nunique()

SUFFIX = r"\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc|llp|lp|the|and|of|a)\b"
def clean_name(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    s = re.sub(SUFFIX, " ", s)
    return re.sub(r"\s+", " ", s).strip()

df["sup_clean"] = df["Supplier Name"].map(clean_name)
after_rules = df["sup_clean"].nunique()

# Fuzzy pass, blocked on first token to keep it fast and avoid absurd merges
spend = df.groupby("sup_clean")["total"].sum().sort_values(ascending=False)
blocks = collections.defaultdict(list)
for nm in spend.index:
    blocks[nm[:4]].append(nm)

# Guard against false merges: two names may only be merged if they contain the
# same set of digits (Reclamation District 108 != 2085) and share no conflicting
# distinguishing token (UC Davis != UC Irvine, San Jose State != San Diego State).
DISTINGUISHERS = {"davis","irvine","berkeley","los","angeles","san","diego","jose",
                  "francisco","cruz","barbara","riverside","merced","east","west",
                  "north","south","valley","central","upper","lower"}
def may_merge(a, b):
    if set(re.findall(r"\d+", a)) != set(re.findall(r"\d+", b)):
        return False
    ta, tb = set(a.split()), set(b.split())
    if (ta ^ tb) & DISTINGUISHERS:          # a distinguishing token differs
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.93

canon, rejected = {}, 0
for _, members in blocks.items():
    survivors = []
    for nm in members:                      # spend-ordered within block
        hit = next((k for k in survivors if may_merge(nm, k)), None)
        canon[nm] = hit if hit else nm
        if not hit:
            survivors.append(nm)

df["supplier"] = df["sup_clean"].map(canon)
final_suppliers = df["supplier"].nunique()

log = (pd.DataFrame({"raw_cleaned": list(canon), "merged_into": list(canon.values())})
       .query("raw_cleaned != merged_into").sort_values("merged_into"))
log.to_csv(f"{OUT}/02_supplier_merge_log.csv", index=False)

print(f"Raw supplier name strings        : {raw_suppliers:,}")
print(f"After standardisation rules      : {after_rules:,}")
print(f"After guarded fuzzy match (>=0.93): {final_suppliers:,}")
print(f"--> {raw_suppliers - final_suppliers:,} duplicate identities consolidated "
      f"({(raw_suppliers-final_suppliers)/raw_suppliers:.1%} of raw names)")
print(f"    merge log: {OUT}/02_supplier_merge_log.csv ({len(log)} merges to review)")
RESULTS.update(raw_suppliers=raw_suppliers, final_suppliers=final_suppliers,
               merges=raw_suppliers - final_suppliers)

# ================================================== STEP 3 SEGMENT THE SPEND
hdr(3, "SPEND SEGMENTATION")
df["category"] = df["Family Title"].fillna("Unclassified").astype(str).str.strip().str.title()
df["segment"] = df["Segment Title"].fillna("Unclassified").astype(str).str.strip().str.title()
df["commodity"] = df["Commodity Title"].fillna("Unclassified").astype(str).str.strip().str.title()
print(f"UNSPSC classification present on {(df['category']!='Unclassified').mean():.1%} of lines")
print(f"  {df['segment'].nunique()} segments / {df['category'].nunique()} families / "
      f"{df['commodity'].nunique()} commodities")

_q = df["Supplier Qualifications"].fillna("")
df["sb"] = _q.str.contains("SB", na=False)
df["dvbe"] = _q.str.contains("DVBE", na=False)
COMPETITIVE = ["Formal Competitive", "Informal Competitive", "Statewide Contract",
               "WSCA/Coop", "CMAS", "SB/DVBE Option", "Master Purchase/Price Agreement",
               "State Price Schedule", "Software License Program"]
df["sourcing"] = np.where(df["Acquisition Method"].isin(COMPETITIVE),
                          "Competitive / leveraged", "Non-competitive or exempt")

df["type"] = np.where(df["Acquisition Type"].str.contains("Goods", na=False), "Goods", "Services")
seg = df.groupby("type").agg(lines=("total", "size"), spend=("total", "sum"),
                             median_line=("total", "median"))
print("\nGOODS vs SERVICES")
print(seg.assign(spend=lambda d: d["spend"].map("${:,.0f}".format),
                 median_line=lambda d: d["median_line"].map("${:,.0f}".format)).to_string())

goods = df[df["type"] == "Goods"].copy()
services = df[df["type"] == "Services"].copy()
RESULTS.update(goods_spend=goods["total"].sum(), goods_lines=len(goods),
               services_spend=services["total"].sum(), services_lines=len(services))

top10_share = df.nlargest(10, "total")["total"].sum() / df["total"].sum()
print(f"\nConcentration warning: the 10 largest lines are {top10_share:.1%} of total spend "
      f"(all major capital/services contracts).")
print("--> Category-management levers are scoped to GOODS, the repeat transactional")
print("    spend where consolidation and price harmonisation actually apply.")
RESULTS["top10_share"] = top10_share

# ============================================== STEP 4 ANALYSIS 1: PARETO
hdr(4, "ANALYSIS 1 - SPEND CONCENTRATION (GOODS)")
s = goods.groupby("supplier")["total"].sum().sort_values(ascending=False)
cum = s.cumsum() / s.sum()
n80 = int((cum <= 0.80).sum() + 1)
tail = s[cum > 0.80]
print(f"{n80:,} of {len(s):,} suppliers ({n80/len(s):.1%}) account for 80% of goods spend "
      f"(${s.sum():,.0f})")
print(f"Tail: {len(tail):,} suppliers ({len(tail)/len(s):.1%}) share ${tail.sum():,.0f} "
      f"({tail.sum()/s.sum():.1%}) - average ${tail.mean():,.0f} each")
pareto = pd.DataFrame({"supplier": s.index, "spend": s.values, "cum_pct": cum.values})
pareto["tail"] = pareto["cum_pct"] > 0.80
pareto.to_csv(f"{OUT}/03_pareto_suppliers.csv", index=False)
RESULTS.update(n80=n80, n_suppliers_goods=len(s), pct80=n80/len(s),
               tail_n=len(tail), tail_spend=tail.sum(), tail_share=tail.sum()/s.sum())

fig, ax = plt.subplots(figsize=(9, 5))
x = np.arange(1, len(s) + 1)
ax.fill_between(x, cum * 100, alpha=.25, color="#2980b9")
ax.plot(x, cum * 100, color="#1f4e79", lw=2)
ax.axhline(80, ls="--", c="crimson", lw=1); ax.axvline(n80, ls="--", c="crimson", lw=1)
ax.annotate(f"{n80} suppliers = 80% of spend", (n80, 80), xytext=(n80+120, 55),
            arrowprops=dict(arrowstyle="->", color="crimson"), color="crimson")
ax.set_xlabel("Suppliers ranked by spend"); ax.set_ylabel("Cumulative % of goods spend")
ax.set_title("Supplier spend concentration - goods, FY2014-15")
fig.tight_layout(); fig.savefig(f"{OUT}/chart_1_pareto.png", dpi=150); plt.close(fig)

# ==================================== ANALYSIS 2: CATEGORY FRAGMENTATION
hdr(4, "ANALYSIS 2 - CATEGORY FRAGMENTATION (GOODS)")
cat = (goods[goods["category"] != "Unclassified"]
       .groupby("category")
       .agg(spend=("total", "sum"), suppliers=("supplier", "nunique"), lines=("total", "size")))
cat = cat[cat["spend"] > 250_000]
cat["avg_per_supplier"] = cat["spend"] / cat["suppliers"]
cat["suppliers_per_$1m"] = cat["suppliers"] / (cat["spend"] / 1e6)
top_cat = cat.sort_values("spend", ascending=False).head(15)
print(top_cat.assign(spend=lambda d: d["spend"].map("${:,.0f}".format),
                     avg_per_supplier=lambda d: d["avg_per_supplier"].map("${:,.0f}".format),
                     **{"suppliers_per_$1m": lambda d: d["suppliers_per_$1m"].round(1)}).to_string())
frag = cat[cat["spend"] > 1e6].sort_values("suppliers_per_$1m", ascending=False).head(5)
print("\nMost fragmented categories (>$1M spend):")
for nm, r in frag.iterrows():
    print(f"  {nm[:45]:<45} {int(r['suppliers']):>4} suppliers / ${r['spend']:>12,.0f}")
cat.sort_values("spend", ascending=False).to_csv(f"{OUT}/04_category_fragmentation.csv")
RESULTS["frag_top"] = frag.head(1)

# ============ ANALYSIS 3: PRICE VARIANCE - TESTED AND REJECTED
hdr(4, "ANALYSIS 3 - PRICE COMPARABILITY TEST (RESULT: NOT SUPPORTABLE)")
pv = goods.dropna(subset=["unit_price", "qty"])
pv = pv[(pv["unit_price"] > 0) & (pv["qty"] > 0)]
qty1_lines = (pv["qty"] == 1).mean()
qty1_spend = pv.loc[pv["qty"] == 1, "total"].sum() / pv["total"].sum()
print("Tested whether the same item is bought at different unit prices across")
print("departments (the standard maverick-spend test).\n")
print(f"  {qty1_lines:.0%} of goods lines record Quantity = 1 with the full lot value")
print(f"  in Unit Price - representing {qty1_spend:.0%} of goods spend.")

t = pv.groupby(pv["Item Name"].astype(str).str.lower().str.strip()).agg(
    n=("unit_price", "size"), depts=("Department Name", "nunique"),
    pmin=("unit_price", "min"), pmax=("unit_price", "max"))
t = t[(t["n"] >= 5) & (t["depts"] >= 2)]
t["ratio"] = t["pmax"] / t["pmin"]
print(f"  Across {len(t)} repeat-bought items, the median max/min unit-price ratio")
print(f"  is {t['ratio'].median():,.0f}x - e.g. 'bread' appears at $0.11 and $20,747.")
print("\n  Unit-of-measure is not captured in this dataset vintage, so these ratios")
print("  reflect inconsistent data entry (each vs case vs lot), not price variance.")
print("\n  CONCLUSION: excluded from the savings model. Quantifying maverick spend")
print("  would require PO-level data with unit-of-measure. Reporting a number here")
print("  would have overstated savings by orders of magnitude.")
t.sort_values("ratio", ascending=False).to_csv(f"{OUT}/05_price_comparability_test.csv")
RESULTS.update(qty1_lines=qty1_lines, qty1_spend=qty1_spend,
               pv_items=len(t), pv_ratio=t["ratio"].median())

# ==================================== ANALYSIS 4: SOURCING METHOD
hdr(4, "ANALYSIS 4 - COMPETITIVE vs NON-COMPETITIVE SOURCING (ALL SPEND)")
m = df.groupby("sourcing")["total"].agg(["sum", "size"])
m["pct"] = m["sum"] / m["sum"].sum()
print(m.assign(sum=lambda d: d["sum"].map("${:,.0f}".format),
               pct=lambda d: d["pct"].map("{:.1%}".format)).to_string())
noncomp = m.loc["Non-competitive or exempt", "sum"]
noncomp_pct = m.loc["Non-competitive or exempt", "pct"]
print(f"\n${noncomp:,.0f} ({noncomp_pct:.1%}) of spend did not go through a competitive")
print("or leveraged route - the single largest governance exposure in the portfolio.")
meth = df.groupby("Acquisition Method")["total"].agg(["sum", "size"]).sort_values("sum", ascending=False)
meth.to_csv(f"{OUT}/06_sourcing_method.csv")
RESULTS.update(noncomp=noncomp, noncomp_pct=noncomp_pct)

# ==================================== ANALYSIS 5: KRALJIC
hdr(4, "ANALYSIS 5 - KRALJIC PORTFOLIO MATRIX (GOODS)")
k = cat[cat["spend"] > 1_000_000].copy()   # focus on material categories
dep = (goods.groupby(["category", "supplier"])["total"].sum()
       .groupby(level=0).apply(lambda x: x.max() / x.sum()))
k["top_supplier_share"] = dep
k = k.dropna(subset=["top_supplier_share"])
k["spend_score"] = k["spend"] / k["spend"].max()
scarcity = 1 / k["suppliers"]
norm = lambda x: (x - x.min()) / (x.max() - x.min() + 1e-9)
k["risk_score"] = 0.5 * norm(scarcity) + 0.5 * norm(k["top_supplier_share"])
sp_cut, rk_cut = k["spend"].median(), k["risk_score"].median()
k["quadrant"] = np.select(
    [(k.spend >= sp_cut) & (k.risk_score >= rk_cut), (k.spend >= sp_cut) & (k.risk_score < rk_cut),
     (k.spend < sp_cut) & (k.risk_score >= rk_cut)],
    ["Strategic", "Leverage", "Bottleneck"], default="Routine")
print(f"Thresholds: spend median ${sp_cut:,.0f}; risk median {rk_cut:.2f}")
STRAT = {"Strategic": "Partner - dual-source, joint planning",
         "Leverage": "Negotiate - tender, consolidate volume",
         "Bottleneck": "Secure - buffer stock, qualify alternates",
         "Routine": "Simplify - catalogue buying, e-procurement"}
k["strategy"] = k["quadrant"].map(STRAT)
print(k["quadrant"].value_counts().to_string())
print("\nBy quadrant:")
for q in ["Strategic", "Leverage", "Bottleneck", "Routine"]:
    sub = k[k["quadrant"] == q]
    if len(sub):
        print(f"  {q:<11} {len(sub):>3} categories  ${sub['spend'].sum():>14,.0f}  -> {STRAT[q]}")
k.sort_values("spend", ascending=False).to_csv(f"{OUT}/07_kraljic.csv")

fig, ax = plt.subplots(figsize=(9, 7))
cols = {"Strategic": "#c0392b", "Leverage": "#2980b9", "Bottleneck": "#e67e22", "Routine": "#95a5a6"}
for q, sub in k.groupby("quadrant"):
    ax.scatter(sub["spend_score"], sub["risk_score"], s=np.sqrt(sub["spend"])/9 + 30,
               alpha=.7, c=cols[q], label=q, edgecolors="white")
for nm, r in k.nlargest(14, "spend").iterrows():
    ax.annotate(str(nm)[:24], (r.spend_score, r.risk_score), fontsize=7.5,
                xytext=(5, 5), textcoords="offset points")
ax.axvline(sp_cut/k["spend"].max(), c="k", lw=.7); ax.axhline(rk_cut, c="k", lw=.7)
ax.set_xlabel("Relative spend value  →"); ax.set_ylabel("Supply risk  →")
ax.set_title("Kraljic portfolio matrix - goods categories, FY2014-15")
ax.legend(fontsize=8, loc="upper right")
fig.tight_layout(); fig.savefig(f"{OUT}/chart_2_kraljic.png", dpi=150); plt.close(fig)

# ==================================== ANALYSIS 6: DIVERSITY SPEND
hdr(4, "ANALYSIS 6 - SMALL BUSINESS / DVBE PARTICIPATION")
sb_share = df.loc[df["sb"], "total"].sum() / df["total"].sum()
dvbe_share = df.loc[df["dvbe"], "total"].sum() / df["total"].sum()
print(f"Certified Small Business spend : ${df.loc[df['sb'],'total'].sum():,.0f}  ({sb_share:.1%})")
print(f"Certified DVBE spend           : ${df.loc[df['dvbe'],'total'].sum():,.0f}  ({dvbe_share:.1%})")
print(f"\nStatutory targets: 25% Small Business, 3% DVBE.")
print("--> Consolidation must be constrained by these targets: the tail is where most")
print("    certified suppliers sit, so cutting it indiscriminately breaches the mandate.")
tail_names = set(pareto.loc[pareto["tail"], "supplier"])
tail_sb = goods[goods["supplier"].isin(tail_names) & goods["sb"]]["supplier"].nunique()
tail_all = len(tail_names)
print(f"    {tail_sb:,} of {tail_all:,} tail suppliers ({tail_sb/tail_all:.0%}) are SB-certified.")
RESULTS.update(sb_share=sb_share, dvbe_share=dvbe_share,
               tail_sb=tail_sb, tail_all=tail_all)

# ==================================== STEP 5 SAVINGS MODEL
hdr(5, "SAVINGS MODEL")
goods_noncomp = goods.loc[goods["sourcing"] == "Non-competitive or exempt", "total"].sum()
tail_saving = tail.sum() * 0.06
noncomp_saving = goods_noncomp * 0.05
dupe_saving = RESULTS["dupe_value"] * 0.5

levers = pd.DataFrame([
    dict(lever="Tail-supplier consolidation",
         basis=f"{len(tail):,} suppliers below the 80% threshold, ${tail.sum():,.0f} spend",
         assumption="6% negotiated discount on consolidated tail volume (industry benchmark)",
         saving=tail_saving, confidence="Medium - benchmark-based"),
    dict(lever="Route non-competitive goods spend to leveraged contracts",
         basis=f"${goods_noncomp:,.0f} of goods bought outside competitive/statewide routes",
         assumption="5% price improvement when moved onto existing leveraged agreements",
         saving=noncomp_saving, confidence="Medium - benchmark-based, overlaps with lever 1"),
    dict(lever="Duplicate purchase-line controls",
         basis=f"{RESULTS['dupe_lines']:,} identical lines in a single fiscal year, ${RESULTS['dupe_value']:,.0f}",
         assumption="50% represent genuine double-processing rather than legitimate repeat buys",
         saving=dupe_saving, confidence="Low - requires PO-level validation"),
    dict(lever="Maverick-spend / price harmonisation",
         basis="Not quantifiable - unit-of-measure absent from dataset",
         assumption="EXCLUDED. See price comparability test.",
         saving=0.0, confidence="Excluded - data does not support it"),
])
levers["pct_of_goods"] = levers["saving"] / goods["total"].sum()
levers.to_csv(f"{OUT}/08_savings_model.csv", index=False)
print(levers[["lever", "saving", "pct_of_goods", "confidence"]]
      .assign(saving=lambda d: d["saving"].map("${:,.0f}".format),
              pct_of_goods=lambda d: d["pct_of_goods"].map("{:.2%}".format))
      .to_string(index=False))

gross = levers["saving"].sum()
overlap = min(tail_saving, noncomp_saving) * 0.4   # conservative overlap haircut
net = gross - overlap
print(f"\nGross identified            : ${gross:,.0f}")
print(f"Less overlap (levers 1 & 2) : ${overlap:,.0f}   [40% haircut, deliberately conservative]")
print(f"NET IDENTIFIED SAVINGS      : ${net:,.0f}")
print(f"  = {net/goods['total'].sum():.1%} of ${goods['total'].sum():,.0f} addressable goods spend")
print(f"  = {net/df['total'].sum():.1%} of ${df['total'].sum():,.0f} total portfolio")
RESULTS.update(gross_sav=gross, net_sav=net, overlap=overlap,
               sav_pct_goods=net/goods["total"].sum(),
               sav_pct_total=net/df["total"].sum(), goods_noncomp=goods_noncomp)

# ==================================== STEP 6 STAR SCHEMA
hdr(6, "STAR-SCHEMA EXPORT")
dim_sup = (df.groupby("supplier").agg(spend=("total", "sum"), lines=("total", "size"),
                                      small_business=("sb", "max"), dvbe=("dvbe", "max"))
           .reset_index().reset_index(names="supplier_id"))
dim_cat = (df[["segment", "category", "commodity"]].drop_duplicates()
           .reset_index(drop=True).reset_index(names="category_id"))
fact = (df.merge(dim_sup[["supplier_id", "supplier"]], on="supplier")
          .merge(dim_cat, on=["segment", "category", "commodity"])
          [["supplier_id", "category_id", "Department Name", "purchase_date", "type",
            "sourcing", "Acquisition Method", "qty", "unit_price", "total"]]
          .rename(columns={"Department Name": "department", "purchase_date": "date",
                           "Acquisition Method": "acquisition_method"}))
dim_date = pd.DataFrame({"date": pd.date_range("2014-07-01", "2015-06-30")})
dim_date["year"] = dim_date.date.dt.year
dim_date["quarter"] = "Q" + dim_date.date.dt.quarter.astype(str)
dim_date["month"] = dim_date.date.dt.strftime("%b %Y")
for nm, d in [("fact_spend", fact), ("dim_supplier", dim_sup),
              ("dim_category", dim_cat), ("dim_date", dim_date)]:
    d.to_csv(f"{OUT}/{nm}.csv", index=False)
    print(f"  {nm}.csv  {len(d):,} rows")

# charts: category bar
fig, ax = plt.subplots(figsize=(9, 5.5))
tc = cat.sort_values("spend", ascending=False).head(12).sort_values("spend")
ax.barh([str(i)[:34] for i in tc.index], tc["spend"] / 1e6, color="#1f4e79")
ax.set_xlabel("Spend ($M)"); ax.set_title("Top goods categories by spend, FY2014-15")
fig.tight_layout(); fig.savefig(f"{OUT}/chart_3_categories.png", dpi=150); plt.close(fig)

import json
json.dump({k: (float(v) if isinstance(v, (int, float, np.floating)) else str(v))
           for k, v in RESULTS.items() if not isinstance(v, pd.DataFrame)},
          open(f"{OUT}/00_headline_numbers.json", "w"), indent=2)
print(f"\nDone. All outputs in ./{OUT}/")
