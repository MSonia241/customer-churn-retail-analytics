# -*- coding: utf-8 -*-
"""
Pipeline complet - Analiza si predictie churn clienti (Online Retail II).
Produce fisierele CSV pentru importul in Power BI.

Pasi:
  1. Citire + curatare
  2. Split pe timp (evita information leakage)
  3. Caracteristici RFM per client
  4. Segmentare RFM (reguli) + K-Means (clustering)
  5. Model de churn (Random Forest) - evaluare out-of-fold
  6. Export CSV pentru Power BI
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import (confusion_matrix, precision_score, recall_score,
                             f1_score, accuracy_score)
import os

XLSX = r"D:\online+retail+ii\online_retail_II.xlsx"
CACHE = r"D:\clienti\_cache_all.csv"
OUTDIR = r"D:\clienti"
os.makedirs(OUTDIR, exist_ok=True)
RS = 42

# ------------------------------------------------------------------ 1. CITIRE
if os.path.exists(CACHE):
    print("Citesc din cache CSV...")
    df = pd.read_csv(CACHE, parse_dates=["InvoiceDate"])
else:
    print("Citesc Excel (2 foi, ~1 min)...")
    sheets = pd.read_excel(XLSX, sheet_name=None)
    df = pd.concat(sheets.values(), ignore_index=True)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Customer ID": "CustomerID"})
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"])
    df.to_csv(CACHE, index=False)
    print(f"[cache salvat: {CACHE}]")

n0 = len(df)
print(f"Randuri brute: {n0:,}")

# ------------------------------------------------------------------ CURATARE
df["Invoice"] = df["Invoice"].astype(str)
df = df[df["CustomerID"].notna()].copy()          # doar clienti identificabili
df = df[~df["Invoice"].str.startswith("C")]        # scoatem anularile
df = df[df["Quantity"] > 0]                        # scoatem retururi/qty<=0
df = df[df["Price"] > 0]                            # scoatem ajustari/pret<=0
# scoatem coduri non-produs (servicii/taxe)
non_prod = {"POST", "D", "M", "C2", "DOT", "CRUK", "BANK CHARGES",
            "AMAZONFEE", "S", "B", "ADJUST", "gift_0001", "TEST001", "TEST002"}
df = df[~df["StockCode"].astype(str).str.upper().isin(non_prod)]
df["CustomerID"] = df["CustomerID"].astype(int)
df["Revenue"] = df["Quantity"] * df["Price"]
print(f"Randuri dupa curatare: {len(df):,} ({100*len(df)/n0:.1f}% pastrate)")

# ------------------------------------------------------------------ 2. SPLIT TIMP
data_min, data_max = df["InvoiceDate"].min(), df["InvoiceDate"].max()
cutoff = data_max - pd.DateOffset(months=6)        # ultimele 6 luni = holdout
print(f"\nPerioada: {data_min.date()} -> {data_max.date()}")
print(f"Cutoff (granita): {cutoff.date()}")
print(f"  Observatie : {data_min.date()} -> {cutoff.date()} (caracteristici)")
print(f"  Holdout    : {cutoff.date()} -> {data_max.date()} (eticheta churn)")

obs = df[df["InvoiceDate"] <= cutoff].copy()
hold = df[df["InvoiceDate"] > cutoff].copy()
clienti_holdout = set(hold["CustomerID"].unique())

# ------------------------------------------------------------------ 3. RFM
g = obs.groupby("CustomerID")
rfm = pd.DataFrame({
    "Recency":    (cutoff - g["InvoiceDate"].max()).dt.days,
    "Frequency":  g["Invoice"].nunique(),
    "Monetary":   g["Revenue"].sum(),
    "NumItems":   g["Quantity"].sum(),
    "NumProducts": g["StockCode"].nunique(),
    "Tenure":     (cutoff - g["InvoiceDate"].min()).dt.days,
    "Country":    g["Country"].agg(lambda s: s.mode().iloc[0]),
})
rfm["AOV"] = (rfm["Monetary"] / rfm["Frequency"]).round(2)
rfm["AvgInterpurchase"] = (rfm["Tenure"] / rfm["Frequency"]).round(1)
# eticheta churn: 1 = NU a mai cumparat in holdout
rfm["Churn_real"] = (~rfm.index.isin(clienti_holdout)).astype(int)
print(f"\nClienti in modelare: {len(rfm):,}")
print(f"Rata churn reala: {100*rfm['Churn_real'].mean():.1f}% "
      f"({int(rfm['Churn_real'].sum()):,} churn / "
      f"{int((1-rfm['Churn_real']).sum()):,} raman)")

# ------------------------------------------------------------------ 4a. SEGMENTARE RFM (reguli)
rfm["R_score"] = pd.qcut(rfm["Recency"], 5, labels=[5, 4, 3, 2, 1]).astype(int)
rfm["F_score"] = pd.qcut(rfm["Frequency"].rank(method="first"), 5,
                         labels=[1, 2, 3, 4, 5]).astype(int)
rfm["M_score"] = pd.qcut(rfm["Monetary"], 5, labels=[1, 2, 3, 4, 5]).astype(int)

def segment(r, f):
    if r >= 4 and f >= 4:   return "Champions"
    if r >= 3 and f >= 3:   return "Loyal Customers"
    if r >= 4 and f <= 2:   return "New Customers"
    if r >= 3 and f <= 2:   return "Potential Loyalist"
    if r <= 2 and f >= 3:   return "At Risk"
    return "Hibernating / Lost"
rfm["RFM_Segment"] = [segment(r, f) for r, f in zip(rfm["R_score"], rfm["F_score"])]

# ------------------------------------------------------------------ 4b. K-MEANS
Xc = rfm[["Recency", "Frequency", "Monetary"]].copy()
Xc["Frequency"] = np.log1p(Xc["Frequency"])
Xc["Monetary"] = np.log1p(Xc["Monetary"])
Xcs = StandardScaler().fit_transform(Xc)
km = KMeans(n_clusters=4, random_state=RS, n_init=10)
rfm["KMeans_Cluster"] = km.fit_predict(Xcs)
# etichetam clusterele dupa Monetary mediu (VIP -> Slab)
ord_m = rfm.groupby("KMeans_Cluster")["Monetary"].mean().sort_values(ascending=False)
lab = {c: l for c, l in zip(ord_m.index, ["VIP", "High-Value", "Mid-Value", "Occasional"])}
rfm["Cluster_Label"] = rfm["KMeans_Cluster"].map(lab)

# ------------------------------------------------------------------ 5. MODEL CHURN
features = ["Recency", "Frequency", "Monetary", "AOV", "Tenure",
           "NumItems", "NumProducts", "AvgInterpurchase"]
X = rfm[features].values
y = rfm["Churn_real"].values
rf = RandomForestClassifier(n_estimators=300, max_depth=None,
                            min_samples_leaf=5, class_weight="balanced",
                            random_state=RS, n_jobs=-1)
skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RS)
# predictii out-of-fold pentru TOTI clientii (evaluare corecta, fara leakage)
proba = cross_val_predict(rf, X, y, cv=skf, method="predict_proba", n_jobs=-1)[:, 1]
pred = (proba >= 0.5).astype(int)
rfm["Churn_prob"] = proba.round(4)
rfm["Churn_pred"] = pred

acc = accuracy_score(y, pred)
prec = precision_score(y, pred)
rec = recall_score(y, pred)
f1 = f1_score(y, pred)
cm = confusion_matrix(y, pred)   # [[TN,FP],[FN,TP]]
tn, fp, fn, tp = cm.ravel()
print("\n" + "=" * 60)
print("EVALUARE MODEL CHURN (out-of-fold, clasa 'churn'=1)")
print("=" * 60)
print(f"  Accuracy : {acc:.3f}   <- inselator, il aratam doar ca sa contrastam")
print(f"  Precision: {prec:.3f}")
print(f"  Recall   : {rec:.3f}   <- cat din clientii care pleaca chiar ii prindem")
print(f"  F1       : {f1:.3f}")
print(f"  Matrice confuzie: TN={tn}  FP={fp}  FN={fn}  TP={tp}")

# importanta variabilelor (model pe toate datele)
rf.fit(X, y)
imp = pd.DataFrame({"Feature": features,
                    "Importance": rf.feature_importances_}
                   ).sort_values("Importance", ascending=False)
print("\nImportanta variabilelor:")
print(imp.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

# ------------------------------------------------------------------ status risc 3 niveluri
def status(p):
    if p >= 0.66: return "High Risk"
    if p >= 0.33: return "Medium Risk"
    return "Low Risk"
rfm["Status_Risc"] = rfm["Churn_prob"].apply(status)

# ------------------------------------------------------------------ 6. EXPORT
clienti = rfm.reset_index().rename(columns={"index": "CustomerID"})
col_order = ["CustomerID", "Country", "Recency", "Frequency", "Monetary",
             "AOV", "NumItems", "NumProducts", "Tenure", "AvgInterpurchase",
             "R_score", "F_score", "M_score", "RFM_Segment",
             "KMeans_Cluster", "Cluster_Label",
             "Churn_real", "Churn_prob", "Churn_pred", "Status_Risc"]
clienti = clienti[col_order]
clienti.to_csv(os.path.join(OUTDIR, "clienti.csv"), index=False)

# tranzactii curate (toata perioada) pentru pagina 1
tr_cols = ["Invoice", "StockCode", "Description", "Quantity", "Price",
           "Revenue", "InvoiceDate", "CustomerID", "Country"]
df[tr_cols].to_csv(os.path.join(OUTDIR, "tranzactii.csv"), index=False)

# metrici model
pd.DataFrame({
    "Metrica": ["Accuracy", "Precision", "Recall", "F1",
                "TN", "FP", "FN", "TP", "Rata_churn_reala"],
    "Valoare": [round(acc, 4), round(prec, 4), round(rec, 4), round(f1, 4),
                int(tn), int(fp), int(fn), int(tp),
                round(rfm["Churn_real"].mean(), 4)],
}).to_csv(os.path.join(OUTDIR, "churn_metrics.csv"), index=False)

imp.to_csv(os.path.join(OUTDIR, "feature_importance.csv"), index=False)

print("\n" + "=" * 60)
print("FISIERE EXPORTATE in D:\\clienti\\ :")
for f in ["clienti.csv", "tranzactii.csv", "churn_metrics.csv",
          "feature_importance.csv"]:
    p = os.path.join(OUTDIR, f)
    print(f"  {f:24s} {os.path.getsize(p)/1024:,.0f} KB")
print("\nDistributie segmente RFM:")
print(clienti["RFM_Segment"].value_counts().to_string())
print("\nDistributie status risc:")
print(clienti["Status_Risc"].value_counts().to_string())
print("\nGATA.")
