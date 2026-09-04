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
        last = text(a, "LastName")
        inits = text(a, "Initials")
        if last:
            out.append(f"{last} {inits}".strip())
    if not out:
        return ""
    shown = ", ".join(out[:5])
    return shown + (" et al." if len(out) > 5 else "")


def parse_abstract(article):
    parts = []
    for at in article.findall("Abstract/AbstractText"):
        label = (at.get("Label") or "").strip()
        body = "".join(at.itertext()).strip()
        if not body:
            continue
        parts.append(f"{label}: {body}" if label else body)
    return "\n".join(parts)


def parse_doi(pubmed_article):
    article = pubmed_article.find("MedlineCitation/Article")
    for el in article.findall("ELocationID"):
        if el.get("EIdType") == "doi" and el.text:
            return el.text.strip()
    for aid in pubmed_article.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi" and aid.text:
            return aid.text.strip()
    return ""


def parse_study_type(article):
    types = {text_of(pt) for pt in article.findall("PublicationTypeList/PublicationType")}
    has_sr = "Systematic Review" in types
    has_ma = "Meta-Analysis" in types
    if has_sr and has_ma:
        return "Systematic review and meta-analysis"
    if has_ma:
        return "Meta-analysis"
    if has_sr:
        return "Systematic review"
    if "Randomized Controlled Trial" in types:
        return "Randomized controlled trial"
    return "Clinical trial"


def text_of(node):
    return (node.text or "").strip() if node is not None else ""


def guess_topic(title, abstract):
    hay = f" {title} {abstract} ".lower()
    for needle, topic in TOPIC_RULES:
        if needle in hay:
            return topic
    return ""


def build_article(pubmed_article):
    article = pubmed_article.find("MedlineCitation/Article")
    pmid = text(pubmed_article, "MedlineCitation/PMID")
    title_el = article.find("ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    journal = text(article, "Journal/ISOAbbreviation") or text(article, "Journal/Title")
    doi = parse_doi(pubmed_article)
    abstract = parse_abstract(article)
    return {
        "_abstract": abstract,  # used only for the sources pack; stripped from JSON
        "pmid": pmid,
        "doi": doi,
        "journal": journal,
        "date": parse_date(article),
        "authors": parse_authors(article),
        "title": title,
        "study_type": parse_study_type(article),
        "topic": guess_topic(title, abstract),
        "summary": "",
        "pico": {"population": "", "intervention": "", "comparison": "", "outcome": ""},
        "inclusion": [],
        "exclusion": [],
        "links": {
            "pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "doi": f"https://doi.org/{doi}" if doi else "",
        },
        "appraisal": {"bottom_line": "", "relevance": "", "caveat": ""},
    }


def write_outputs(articles, today):
    # The window describes when PubMed *indexed* these (DATETYPE="edat"); an
    # article's own publication date can legitimately fall outside it.
    start = (today - dt.timedelta(days=RELDATE)).isoformat()

    meta = {
        "title": "Nephrology Literature Update",
        "generated_at": today.isoformat(),
        "window": f"New to PubMed {start} to {today.isoformat()}",
        "query": "Clinical nephrology — RCTs, systematic reviews & meta-analyses (humans, English)",
        "source": "PubMed (NLM) abstracts; summaries authored by Google NotebookLM. "
                  "Each entry links to its PubMed record and DOI.",
        "count": len(articles),
        "note": "Summaries and PICO are NotebookLM analyses of the source abstracts; "
                "criteria marked “not specified” were not stated.",
        "appraisal_source": "Professor critical appraisal (clinical bottom-line, relevance, caveat).",
    }

    clean = []
    for a in articles:
        a = dict(a)
        a.pop("_abstract", None)
        clean.append(a)

    with open("nephro.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "articles": clean}, f, ensure_ascii=False, indent=2)
        f.write("\n")

    with open("nephro_sources.md", "w", encoding="utf-8") as f:
        f.write(sources_pack(articles, meta))


def sources_pack(articles, meta):
    schema = json.dumps({
        "topic": "short label, e.g. CKD / Transplantation / AKI",
        "summary": "2–4 sentences with the key effect sizes (HR/OR/RR/MD, 95% CI, p)",
        "pico": {"population": "", "intervention": "", "comparison": "", "outcome": ""},
        "inclusion": ["..."],
        "exclusion": ["..."],
        "appraisal": {"bottom_line": "", "relevance": "", "caveat": ""},
    }, indent=2)

    lines = [
        f"## 🩺 Weekly nephrology update — {meta['generated_at']}",
        "",
        f"**Window:** {meta['window']} · **{meta['count']} articles** from PubMed.",
        "",
        "### How to finish this update (≈5 min)",
        "1. Copy everything under **Sources** below into a new (or existing) "
        "NotebookLM notebook as a pasted text source.",
        "2. Paste the **Prompt** into NotebookLM.",
        "3. NotebookLM returns one JSON object per article — drop those into the "
        "`articles` array of `nephro.json` on this branch (the bibliographic fields "
        "are already filled; you're adding `summary`, `pico`, `inclusion`, "
        "`exclusion`, `appraisal`, and fixing `topic`).",
        "4. Mark the PR ready and merge. The live dashboard updates automatically.",
        "",
        "### Prompt",
        "> You are a nephrology professor. For **each** source article below, read the "
        "abstract and return a JSON object with exactly these keys, grounded only in the "
        "abstract (use \"not specified\" where the abstract is silent):",
        "",
        "```json",
        schema,
        "```",
        "",
        "Keep summaries quantitative (include HR/OR/RR/MD, 95% CI and p-values where "
        "reported). Return the objects in the same order as the sources.",
        "",
        "---",
        "### Sources",
        "",
    ]
    for i, a in enumerate(articles, 1):
        lines += [
            f"#### {i}. {a['title']}",
            f"- **PMID:** {a['pmid']} · **DOI:** {a['doi'] or '—'}",
            f"- **Journal / date:** {a['journal']} · {a['date']}",
            f"- **Study type:** {a['study_type']}",
            f"- **Authors:** {a['authors']}",
            f"- **PubMed:** {a['links']['pubmed']}",
            "",
            "**Abstract:**",
            "",
            a["_abstract"] or "_(no abstract available)_",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main():
    today = dt.date.today()
    print(f"Searching PubMed for the last {RELDATE} days ({DATETYPE})…")
    pmids = esearch()
    print(f"Found {len(pmids)} candidate articles.")
    if not pmids:
        set_output("count", "0")
        print("Nothing new this week — no PR will be opened.")
        return 0

    time.sleep(0.4)  # be polite between E-utilities calls
    root = efetch(pmids)
    articles = [build_article(pa) for pa in root.findall("PubmedArticle")]
    articles = [a for a in articles if a["pmid"] and a["title"]]
    print(f"Parsed {len(articles)} articles with usable metadata.")

    write_outputs(articles, today)
    set_output("count", str(len(articles)))
    print("Wrote nephro.json (scaffold) and nephro_sources.md (NotebookLM pack).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
