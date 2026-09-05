#!/usr/bin/env python3
"""
Fill the nephro.json scaffold with summaries and appraisals via the Gemini API.

fetch_nephro.py leaves summary / PICO / inclusion / exclusion / appraisal blank
for a human to complete in NotebookLM. This script does that step automatically,
so the whole weekly refresh runs unattended and free (one request per week sits
far inside Gemini's free tier).

Reads : nephro.json (scaffold) + nephro_abstracts.json (pmid -> abstract)
Writes: nephro.json, completed

Requires GEMINI_API_KEY in the environment. Optional GEMINI_MODEL pins a model;
otherwise an available Flash model is discovered at runtime, so a model rename
on Google's side doesn't break the Monday run.

Standard library only - the GitHub Action needs no pip install.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Preference order when discovering a model; first substring match wins.
MODEL_PREFERENCE = ("3.5-flash", "3-flash", "2.5-flash", "2.0-flash", "flash")
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.0-flash")

SYSTEM = (
    "You are a nephrology professor compiling a weekly literature update for "
    "practising nephrologists. For each article, read the abstract and produce a "
    "structured appraisal grounded ONLY in that abstract. Never invent numbers, "
    "populations or conclusions the abstract does not state; where it is silent, "
    "write \"not specified\". Keep summaries quantitative: include effect sizes "
    "(HR/OR/RR/MD/SMD), 95% confidence intervals and p-values exactly as "
    "reported. The appraisal must be the honest clinical read: bottom_line states "
    "what the study actually showed, relevance says why a nephrologist should "
    "care, and caveat names the real methodological limitation (design, power, "
    "heterogeneity, surrogate endpoints). Never present observational evidence "
    "as causal. The PubMed publication type given to you is sometimes wrong (a "
    "case report tagged as a systematic review, an EHR-based target trial "
    "emulation tagged as a randomised trial); if the abstract contradicts it, say "
    "so plainly in the caveat rather than repeating the wrong label."
)

# Gemini's responseSchema is an OpenAPI subset: no additionalProperties.
_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pmid": {"type": "string", "description": "Echo the PMID exactly"},
                    "topic": {"type": "string", "description": "Short label, e.g. CKD or Transplantation"},
                    "summary": _STR,
                    "pico": {
                        "type": "object",
                        "properties": {
                            "population": _STR, "intervention": _STR,
                            "comparison": _STR, "outcome": _STR,
                        },
                        "required": ["population", "intervention", "comparison", "outcome"],
                    },
                    "inclusion": {"type": "array", "items": _STR},
                    "exclusion": {"type": "array", "items": _STR},
                    "appraisal": {
                        "type": "object",
                        "properties": {
                            "bottom_line": _STR, "relevance": _STR, "caveat": _STR,
                        },
                        "required": ["bottom_line", "relevance", "caveat"],
                    },
                },
                "required": ["pmid", "topic", "summary", "pico",
                             "inclusion", "exclusion", "appraisal"],
            },
        }
    },
    "required": ["analyses"],
}


class Retryable(RuntimeError):
    """Transient: rate limit or capacity. Worth another model or another go."""


def post(url, payload=None, method="GET", retries=3):
    """One model, a few attempts. Raises Retryable so the caller can try the
    next model - a capacity spike on the newest Flash says nothing about the
    older ones, which are far less contended."""
    body = json.dumps(payload).encode() if payload is not None else None
    backoff = (5, 15, 30)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("x-goog-api-key", API_KEY)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504):
                last = f"HTTP {e.code}: {detail}"
                if attempt < retries - 1:
                    time.sleep(backoff[min(attempt, len(backoff) - 1)])
                    continue
                raise Retryable(last) from None
            # 400/401/403/404 are our fault, not Google's - fail immediately.
            raise RuntimeError(f"Gemini API HTTP {e.code}: {detail}") from None
        except Exception as e:  # noqa: BLE001 - network blips
            last = e
            if attempt < retries - 1:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise Retryable(f"unreachable: {last}") from None
    raise Retryable(str(last))


def candidate_models():
    """Ordered models to try. GEMINI_MODEL pins one; otherwise discovered Flash
    models first, then known IDs, so an overloaded or renamed model is survivable."""
    pinned = os.environ.get("GEMINI_MODEL", "").strip()
    if pinned:
        return [pinned]

    discovered = []
    try:
        data = post(f"{API_ROOT}/models")
        names = [
            m["name"].split("/")[-1]
            for m in data.get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]
        for want in MODEL_PREFERENCE:            # non-lite first
            for n in names:
                if want in n and "lite" not in n and n not in discovered:
                    discovered.append(n)
        for want in MODEL_PREFERENCE:            # then anything Flash
            for n in names:
                if want in n and n not in discovered:
                    discovered.append(n)
    except Exception as e:  # noqa: BLE001 - discovery is best-effort
        print(f"Model discovery failed ({e}); using known IDs.")

    out = []
    for m in discovered + list(FALLBACK_MODELS):
        if m not in out:
            out.append(m)
    return out


def build_prompt(articles, abstracts):
    parts = [
        f"Appraise the following {len(articles)} nephrology articles. Return one "
        "object per article, echoing its pmid exactly so it can be matched back.",
        "",
    ]
    for a in articles:
        parts += [
            f"--- PMID {a['pmid']} ---",
            f"Title: {a.get('title', '')}",
            f"Journal / date: {a.get('journal', '')} - {a.get('date', '')}",
            f"PubMed publication type: {a.get('study_type', '')}",
            "Abstract:",
            abstracts.get(a["pmid"]) or "(no abstract available)",
            "",
        ]
    return "\n".join(parts)


def summarise(articles, abstracts):
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"parts": [{"text": build_prompt(articles, abstracts)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.2,
        },
    }

    models = candidate_models()
    print(f"Models to try, in order: {', '.join(models)}")
    problems = []
    for model in models:
        print(f"Trying {model} ...")
        try:
            data = post(f"{API_ROOT}/models/{model}:generateContent",
                        payload, method="POST")
        except Retryable as e:
            print(f"  {model} unavailable ({e}); falling back to the next model.")
            problems.append(f"{model}: {e}")
            continue

        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise RuntimeError(f"Request blocked by Gemini: {feedback['blockReason']}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"No candidates returned: {json.dumps(data)[:300]}")
        cand = candidates[0]
        if cand.get("finishReason") not in (None, "STOP"):
            raise RuntimeError(f"Generation stopped early: {cand.get('finishReason')}")
        chunks = [p["text"] for p in cand["content"]["parts"] if "text" in p]
        if not chunks:
            raise RuntimeError("No text part in the response.")
        print(f"  {model} succeeded.")
        return json.loads("".join(chunks))["analyses"]

    raise RuntimeError(
        "Every candidate model was unavailable. Google capacity is usually "
        "temporary - re-run the workflow shortly. Details: " + " | ".join(problems)
    )


def main():
    if not API_KEY:
        print("::error::GEMINI_API_KEY is not set.")
        return 1
    if not os.path.exists("nephro_abstracts.json"):
        print("::error::nephro_abstracts.json missing - run fetch_nephro.py first.")
        return 1

    data = json.load(open("nephro.json", encoding="utf-8"))
    abstracts = json.load(open("nephro_abstracts.json", encoding="utf-8"))
    articles = data.get("articles", [])
    if not articles:
        print("No articles to summarise.")
        return 0

    analyses = summarise(articles, abstracts)
    by_pmid = {a["pmid"]: a for a in analyses if a.get("pmid")}

    missing = [a["pmid"] for a in articles if a["pmid"] not in by_pmid]
    if missing:
        print(f"::error::No analysis returned for PMIDs: {', '.join(missing)}")
        return 1

    for art in articles:
        an = by_pmid[art["pmid"]]
        art["topic"] = an["topic"]
        art["summary"] = an["summary"]
        art["pico"] = an["pico"]
        art["inclusion"] = an["inclusion"]
        art["exclusion"] = an["exclusion"]
        art["appraisal"] = an["appraisal"]

    # Never publish a half-filled feed - a blank summary renders as an empty card.
    blank = [a["pmid"] for a in articles if not a["summary"].strip()]
    if blank:
        print(f"::error::Empty summary for PMIDs: {', '.join(blank)}")
        return 1

    data["meta"]["source"] = (
        "PubMed (NLM) abstracts; summaries and appraisals generated automatically "
        "from those abstracts. Each entry links to its PubMed record and DOI."
    )
    data["meta"]["note"] = (
        "Summaries and PICO are machine analyses of the source abstracts; criteria "
        "marked “not specified” were not stated. Read the source before acting "
        "clinically."
    )
    data["meta"]["appraisal_source"] = (
        "Automated critical appraisal (clinical bottom-line, relevance, caveat)."
    )

    with open("nephro.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Filled {len(articles)} articles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
