#!/usr/bin/env python3
"""
Weekly nephrology literature gatherer.


Queries PubMed (NCBI E-utilities) for the last 7 days of clinical-nephrology
randomized controlled trials, systematic reviews and meta-analyses, then writes:


  * nephro.json        — a SCAFFOLD the dashboard can already render: all the
                         bibliographic metadata is filled in, but the summary,
                         PICO, inclusion/exclusion and appraisal fields are left
                         blank for a human to complete from NotebookLM.
  * nephro_sources.md  — a "sources pack": every abstract plus a ready-to-paste
                         prompt, formatted to drop straight into NotebookLM. Used
                         as the body of the weekly pull request (not committed).


NotebookLM has no public API, so the summarising step stays manual: paste the
sources pack into NotebookLM, copy the JSON it returns into nephro.json, merge.


Only the Python standard library is used so the GitHub Action needs no install.
"""


import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# ── Search configuration ──────────────────────────────────────────────────────
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RELDATE = 7            # how many days back to look
# "edat" (date added to PubMed) is what you want for a "what's new" feed. "pdat"
# uses a normalised publication date that often sits months before indexing, so
# it silently returns papers you already covered.
DATETYPE = "edat"
RETMAX = 15            # cap; the dashboard grid comfortably shows up to ~12
API_KEY = os.environ.get("NCBI_API_KEY", "")  # optional, raises rate limits
USER_AGENT = "nephro-weekly-update/1.0 (+https://github.com/; dashboard)"


# Clinical nephrology, RCTs + systematic reviews/meta-analyses, humans, English.
# MeSH *Major Topic* ([Majr]) keeps the paper genuinely ABOUT kidney disease.
# A title/abstract keyword search ("renal" OR "kidney" …) drags in cardiac and
# physiotherapy trials that merely mention renal function in passing.
TERM = (
    '("Renal Insufficiency, Chronic"[Majr] OR "Acute Kidney Injury"[Majr] '
    'OR "Renal Dialysis"[Majr] OR "Kidney Transplantation"[Majr] '
    'OR "Glomerulonephritis"[Majr] OR "Diabetic Nephropathies"[Majr]) '
    "AND (Randomized Controlled Trial[Publication Type] OR Systematic Review[Publication Type] "
    "OR Meta-Analysis[Publication Type]) "
    "AND English[Language] AND humans[MeSH Terms]"
)


MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


# Rough topic guess from the title/abstract — a convenience the human can correct.
TOPIC_RULES = [
    ("transplant", "Transplantation"),
    ("iga nephropathy", "IgA Nephropathy"),
    ("lupus", "Glomerular Disease"),
    ("glomerul", "Glomerular Disease"),
    ("acute kidney injury", "AKI"),
    (" aki ", "AKI"),
    ("hemodialysis", "Dialysis"),
    ("haemodialysis", "Dialysis"),
    ("peritoneal dialysis", "Dialysis"),
    ("dialysis", "Dialysis"),
    ("sglt2", "SGLT2i"),
    ("diabet", "Diabetic Kidney Disease"),
    ("hypertension", "Hypertension"),
    ("biopsy", "Kidney Biopsy"),
    ("chronic kidney disease", "CKD"),
    ("ckd", "CKD"),
]




def http_get(url, retries=4):
    """GET with a couple of polite retries; E-utilities can be flaky."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 — surface after retries
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n{last}")




def _key(params):
    if API_KEY:
        params = {**params, "api_key": API_KEY}
    return params




def esearch():
    params = _key({
        "db": "pubmed",
        "term": TERM,
        "reldate": str(RELDATE),
        "datetype": DATETYPE,
        "retmax": str(RETMAX),
        "retmode": "json",
        "sort": "pub_date",
    })
    url = f"{EUTILS}/esearch.fcgi?" + urllib.parse.urlencode(params)
    data = json.loads(http_get(url))
    return data.get("esearchresult", {}).get("idlist", [])




def efetch(pmids):
    params = _key({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    url = f"{EUTILS}/efetch.fcgi?" + urllib.parse.urlencode(params)
    return ET.fromstring(http_get(url))




def text(elem, path):
    node = elem.find(path)
    return node.text.strip() if node is not None and node.text else ""




def parse_date(article):
    """Best-effort YYYY-MM-DD, preferring the electronic ArticleDate."""
    ad = article.find("ArticleDate")
    src = ad if ad is not None else article.find("Journal/JournalIssue/PubDate")
    if src is None:
        return ""
    year = text(src, "Year")
    if not year:
        return ""
    mraw = text(src, "Month")
    month = MONTHS.get(mraw[:3], None)
    if month is None:
        month = int(mraw) if mraw.isdigit() else 1
    draw = text(src, "Day")
    day = int(draw) if draw.isdigit() else 1
    return f"{int(year):04d}-{month:02d}-{day:02d}"




def parse_authors(article):
    out = []
    for a in article.findall("AuthorList/Author"):
        coll = a.find("CollectiveName")
        if coll is not None and coll.text:
            out.append(coll.text.strip())
            continue
