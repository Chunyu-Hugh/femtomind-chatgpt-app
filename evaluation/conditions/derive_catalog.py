"""The catalog of knowledge condition B: the author layer removed, provably.

    python -m evaluation.conditions.derive_catalog --out ROOT [--report FILE]

writes ROOT/analyses/JAM24/catalog/{analysis_config.json, campaign_defaults.json,
paper_findings.md, unresolved_settings.md}: the published catalog with every
statement that came from the authors' clarifications of 2026-10-19 taken out.
A gateway whose checkout has ROOT/analyses in place of its own serves this
variant unchanged in code (analysis_catalog.catalog_root finds it).

What is removed from analysis_config.json: the three author sources; the
author blocks of source_verification (author_confirmations, the superseded
history, latest_author_corrections, final_author_clarifications,
belle_kaon_followup, effective_sidis_selection); reproduction_configuration,
replaced by a metadata-only block that defines no overlay; the author-sourced
findings and the confirmed-procedure block of the Gaussian sampling audit;
the author directions of the normalization audit; the author text of the
paper-versus-input discrepancies; and, in the resolution register, every
resolution that cited an author source becomes unresolved and a blocker.
In the two Markdown documents the author sections go, and every remaining
line that mentions the authors.

The derivation is checked three ways (tests/test_evaluation_conditions.py):
the gateway's Catalog loads the variant and the compiler reports no issue;
the literal conf is unchanged while the reproduction conf now equals it; and
no string that existed only in the removed material survives anywhere in the
variant (the leak set is written next to the report).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import time
from copy import deepcopy

from evaluation.common import paths

CATALOG_DIR = os.path.join(paths.ANALYSES, "JAM24", "catalog")
AUTHOR_SOURCE_IDS = ("author_clarifications_20261019", "author_corrections_20261019_positivity_disabled",
                     "author_final_catalog_clarifications_20261019")
REMOVED_SV = ("author_confirmations", "superseded_author_confirmation_history", "latest_author_corrections",
              "final_author_clarifications", "belle_kaon_followup", "effective_sidis_selection")
# The word author(s), possessives and the compounds author-confirmed, author_...;
# not 'authoritative' or 'authorized', which say nothing about the authors.
AUTHOR = re.compile(r"\bauthors?(?:['’]s?)?\b|author[_-]", re.IGNORECASE)
WITHHELD = ("Resolution relied on correspondence with the JAM24 collaboration, which is withheld in "
            "evaluation condition B.")
# Paths whose remaining mentions of the authors are publication-only wording or
# schema vocabulary, not statements: reported, not removed.
WHITELIST = ("/catalog_schema/", "/unresolved_settings/", "/scientific_settings/")
STATUS_B = "unresolved_in_evaluation_condition_B"
MIN_LEAF = 12

REPRODUCTION_B = {
    "status": "evaluation_condition_B_literal_input_no_overlays",
    "numerical_authority": "official_final_input",
    "do_not_change": "Dataset definitions, literal source filters, numerical parameter values/bounds and FF links "
                     "are the exact source-verified records.",
    "limitation": "No correspondence with the collaboration is available in this evaluation condition; no "
                  "reproduction overlay is defined, so the reproduction configuration is the literal official input.",
}

# Markdown sections that came from the authors, by exact heading line.
PAPER_FINDINGS_DROP = (
    "# Final author clarifications — current reproduction policy",
    "# Current JAM24 author-confirmed configuration (2026-10-19 correction)",
    "## Author-confirmed information",
    "### gaussian.historical_reproduction",
    "## Updated resolution register",
    "# Phase 3: author correction, effective cuts and positivity controls",
    "## Remaining review boundary",
    "## Final clarification status",
)
UNRESOLVED_DROP = (
    "# JAM24 current unresolved status after final author clarifications",
    "# Final author clarifications — current reproduction policy",
    "## Current resolution register",
    "## Remaining priorities",
)


def _leaves(o, out):
    """Every string value of a JSON value (keys are structure, not statements)."""
    if isinstance(o, dict):
        for v in o.values():
            _leaves(v, out)
    elif isinstance(o, list):
        for v in o:
            _leaves(v, out)
    elif isinstance(o, str):
        out.append(o)
    return out


def derive_config(config):
    """(config_B, removed): removed is a list of JSON values taken out."""
    c = deepcopy(config)
    removed = []
    log = {"sources": [], "source_verification": [], "register_unresolved": [], "audit_findings": [],
           "discrepancy_status_replaced": [], "other": []}
    for sid in AUTHOR_SOURCE_IDS:
        if sid in c["sources"]:
            removed.append({sid: c["sources"].pop(sid)})
            log["sources"].append(sid)
    sv = c["source_verification"]
    for key in REMOVED_SV:
        if key in sv:
            removed.append({key: sv.pop(key)})
            log["source_verification"].append(key)
    removed.append({"reproduction_configuration": c.get("reproduction_configuration")})
    c["reproduction_configuration"] = deepcopy(REPRODUCTION_B)
    audit = sv.get("gaussian_sampling_audit", {})
    if "confirmed_intended_procedure" in audit:
        removed.append({"confirmed_intended_procedure": audit.pop("confirmed_intended_procedure")})
        log["other"].append("gaussian_sampling_audit.confirmed_intended_procedure")
    kept = []
    for finding in audit.get("findings", []):
        if AUTHOR.search(json.dumps(finding)):
            removed.append({"finding": finding})
            log["audit_findings"].append(finding.get("id"))
        else:
            kept.append(finding)
    if "findings" in audit:
        audit["findings"] = kept
    norm = sv.get("dataset_normalization_audit", {})
    if "resolved_author_directions" in norm:
        removed.append({"resolved_author_directions": norm.pop("resolved_author_directions")})
        log["other"].append("dataset_normalization_audit.resolved_author_directions")
    for i, row in enumerate(sv.get("paper_input_discrepancies", [])):
        if isinstance(row, dict) and AUTHOR.search(json.dumps(row.get("status", ""))):
            removed.append({"discrepancy_status": row["status"]})
            row["status"] = "Difference recorded. " + WITHHELD
            log["discrepancy_status_replaced"].append(row.get("id", i))
        for key in list(row):
            if key not in ("id", "subject", "paper", "status", "source", "official_input", "input") \
                    and AUTHOR.search(json.dumps(row[key])):
                removed.append({"discrepancy_" + key: row.pop(key)})
    blockers = set(c["catalog_validation"].get("blocking_unresolved_items", []))
    for item in sv.get("resolution_register", []):
        cited = [s for s in item.get("evidence_sources", []) if s in AUTHOR_SOURCE_IDS]
        if cited or AUTHOR.search(item.get("resolution_and_remaining_gap", "")):
            removed.append({"register_" + item["id"]: item.get("resolution_and_remaining_gap", "")})
            item["evidence_sources"] = [s for s in item["evidence_sources"] if s not in AUTHOR_SOURCE_IDS]
            item["status"] = STATUS_B
            item["resolution_and_remaining_gap"] = WITHHELD
            blockers.add(item["id"])
            log["register_unresolved"].append(item["id"])
    cv = c["catalog_validation"]
    cv["blocking_unresolved_items"] = sorted(blockers)
    cv["status"] = "evaluation_condition_B_paper_and_official_input_only"
    for key in ("phase3_status", "final_clarification_status"):
        if key in cv:
            removed.append({key: cv.pop(key)})
            log["other"].append("catalog_validation." + key)
    scope = c["scope"]
    scope["scientific_source_policy"] = "layered_publication_official_input_only_evaluation_condition_B"
    if "expanded_inspection_limit" in scope:
        removed.append({"expanded_inspection_limit": scope.pop("expanded_inspection_limit")})
    scope["evaluation_condition"] = {"id": "B", "withheld": "the collaboration's clarifications of 2026-10-19",
                                     "derived_by": "evaluation/conditions/derive_catalog.py"}
    sv["phase"] = "official-input-review-evaluation-condition-B"
    missed = sv.get("previously_missed_paper_findings", {})
    if isinstance(missed.get("assessment"), str) and AUTHOR.search(missed["assessment"]):
        removed.append({"previously_missed_assessment": missed["assessment"]})
        missed["assessment"] = missed["assessment"].split(";")[0].rstrip(".") + "."
    role = c["sources"].get("official_final_input", {})
    if isinstance(role.get("role"), str) and AUTHOR.search(role["role"]):
        removed.append({"official_input_role": role["role"]})
        role["role"] = ("Final JAM24 configuration file of the published fit; read-only AST extraction, "
                        "not copied into application repository.")
    notice = c.get("historical_section_notice", {})
    for key, value in list(notice.items()):
        if isinstance(value, str) and AUTHOR.search(value):
            removed.append({"historical_section_notice." + key: value})
            notice[key] = "Preserved publication-only evidence; see source_verification for the current register."
    # anything else that still names the authors is reported, not silently kept
    rest, whitelisted = [], []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + "/" + str(k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + "/" + str(i))
        elif isinstance(o, str) and AUTHOR.search(o):
            (whitelisted if path.startswith(WHITELIST) else rest).append(path)
    walk(c, "")
    log["author_mentions_left"] = rest
    log["author_mentions_whitelisted"] = whitelisted
    return c, removed, log


def _heading_level(line):
    m = re.match(r"^(#{1,6})\s", line)
    return len(m.group(1)) if m else 0


def drop_sections(text, headings):
    """Remove each named section: from its heading line up to the next heading
    of the same or a higher level. Returns (text, removed_lines)."""
    lines = text.split("\n")
    removed = []
    for heading in headings:
        level = _heading_level(heading)
        while heading in lines:
            start = lines.index(heading)
            end = start + 1
            while end < len(lines) and not (0 < _heading_level(lines[end]) <= level):
                end += 1
            removed.extend(lines[start:end])
            del lines[start:end]
    return "\n".join(lines), removed


def drop_author_lines(text):
    kept, removed = [], []
    for line in text.split("\n"):
        (removed if AUTHOR.search(line) else kept).append(line)
    return "\n".join(kept), removed


def derive_paper_findings(text):
    text, removed = drop_sections(text, PAPER_FINDINGS_DROP)
    text, more = drop_author_lines(text)
    return text, removed + more


def derive_unresolved(text, config_b):
    text, removed = drop_sections(text, UNRESOLVED_DROP)
    text, more = drop_author_lines(text)
    rows = ["| ID | Status | Note |", "| --- | --- | --- |"]
    for item in config_b["source_verification"].get("resolution_register", []):
        rows.append("| %s | %s | %s |" % (item["id"], item["status"], item["resolution_and_remaining_gap"]))
    head = ["# JAM24 unresolved status in evaluation condition B", "",
            "This catalog variant carries the publication and the official final input only; the collaboration's",
            "written clarifications are withheld. Items whose resolution relied on them are unresolved here.", "",
            "## Resolution register in this condition", ""] + rows + [""]
    return "\n".join(head) + text, removed + more


def leak_set(original_documents, removed_values, removed_lines):
    """Strings that existed only in the removed material: every string value
    of the removed JSON (>= MIN_LEAF chars, no quotes or backslashes) that
    occurs in the original catalog's documents no more often than in the
    removed material, and every removed Markdown line of 40+ characters."""
    everything = "\n".join(original_documents.values())
    removed_text = json.dumps(removed_values, ensure_ascii=False) + "\n" + "\n".join(removed_lines)
    leaves = set()
    for value in removed_values:
        for leaf in _leaves(value, []):
            if len(leaf) >= MIN_LEAF and '"' not in leaf and "\\" not in leaf:
                leaves.add(leaf)
    unique = {leaf for leaf in leaves if everything.count(leaf) <= removed_text.count(leaf)}
    lines = {ln.strip() for ln in removed_lines if len(ln.strip()) >= 40}
    return sorted(unique), sorted(lines)


def leaks(unique_leaves, removed_lines, documents):
    """(leaf, document) pairs that survived."""
    found = []
    for name, text in documents.items():
        for leaf in unique_leaves:
            if leaf in text:
                found.append({"leaf": leaf[:120], "document": name})
        for line in removed_lines:
            if line in text:
                found.append({"line": line[:120], "document": name})
    return found


def write_tree(out_root):
    """Write the variant under out_root/analyses/JAM24/catalog and return its report."""
    with open(os.path.join(CATALOG_DIR, "analysis_config.json"), encoding="utf-8") as fh:
        original = json.load(fh)
    config_b, removed, log = derive_config(original)
    with open(os.path.join(CATALOG_DIR, "paper_findings.md"), encoding="utf-8") as fh:
        findings_b, removed_lines = derive_paper_findings(fh.read())
    with open(os.path.join(CATALOG_DIR, "unresolved_settings.md"), encoding="utf-8") as fh:
        unresolved_b, more = derive_unresolved(fh.read(), config_b)
    removed_lines += more
    out_dir = os.path.join(out_root, "analyses", "JAM24", "catalog")
    os.makedirs(out_dir, exist_ok=True)
    config_text = json.dumps(config_b, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    documents = {"analysis_config.json": config_text, "paper_findings.md": findings_b,
                 "unresolved_settings.md": unresolved_b}
    for name, text in documents.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    shutil.copyfile(os.path.join(CATALOG_DIR, "campaign_defaults.json"), os.path.join(out_dir, "campaign_defaults.json"))
    with open(os.path.join(out_dir, "campaign_defaults.json"), encoding="utf-8") as fh:
        documents["campaign_defaults.json"] = fh.read()
    originals = {}
    for name in ("analysis_config.json", "paper_findings.md", "unresolved_settings.md", "campaign_defaults.json"):
        with open(os.path.join(CATALOG_DIR, name), encoding="utf-8") as fh:
            originals[name] = fh.read()
    unique, lines = leak_set(originals, removed, removed_lines)
    report = {"schema": "femtomind.derived_catalog_B.v1", "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "source_sha256": {n: hashlib.sha256(t.encode("utf-8")).hexdigest() for n, t in originals.items()},
              "variant_sha256": {n: hashlib.sha256(t.encode("utf-8")).hexdigest() for n, t in documents.items()},
              "out_dir": out_dir, "log": log, "removed_json_values": len(removed), "removed_markdown_lines": len(removed_lines),
              "leak_set": {"unique_leaves": len(unique), "lines": len(lines)},
              "leaks": leaks(unique, lines, documents)}
    with open(os.path.join(out_root, "derived_catalog_B.report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_root, "derived_catalog_B.leakset.json"), "w", encoding="utf-8") as fh:
        json.dump({"unique_leaves": unique, "lines": lines}, fh, indent=1)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.conditions.derive_catalog")
    ap.add_argument("--out", required=True, help="root that will hold analyses/JAM24/catalog")
    args = ap.parse_args(argv)
    report = write_tree(args.out)
    print(json.dumps({k: v for k, v in report.items() if k != "leaks"}, indent=1, sort_keys=True))
    print("leaks:", json.dumps(report["leaks"][:20], indent=1))


if __name__ == "__main__":
    main()
