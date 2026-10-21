"""Bounded, in-memory MCP view of the validated compiler/materializer.

This module translates no scientific declarations and imports no FitPack code.
No input files, artifacts, campaigns, reader runs or jobs can be requested.
"""
from copy import deepcopy
import hashlib

from . import analysis_catalog as catalog
from . import compiled_input as materializer
from . import configuration_compiler as compiler
from . import configuration_snapshot as snapshot

SCHEMA = "femtomind.compiled_input_preview.v1"
SECTIONS = ("summary", "datasets", "normalizations", "settings", "overlays", "issues")


def _record(section, path, value):
    return {"layer": section, "json_pointer": path, "record": deepcopy(value)}


def _issue(code, path, message):
    return {"code": code, "path": path, "message": message, "severity": "error", "blocks_materialization": True}


def _prior(source):
    prior = source["source_verification"].get("current_reader_point_validation")
    report = {"available": isinstance(prior, dict), "origin": "pinned catalog; not newly executed"}
    if isinstance(prior, dict):
        report.update(artifact_sha256=prior.get("artifact_sha256"), method=prior.get("method"),
                      summary=deepcopy(prior.get("summary")), warnings=prior.get("warnings"))
    return report


def _records(ir, section, issues):
    """Expose existing declarations verbatim; never construct another conf."""
    if section in ("summary", "issues"):
        return [_record("issues", i["path"], i) for i in issues]
    source = ir["literal_catalog"]["source_verification"]
    reproduction = ir["reproduction"]
    if section in ("datasets", "normalizations"):
        records = []
        for reaction, block in reproduction["datasets"].items():
            for i, row in enumerate(block["entries"]):
                path = "/source_verification/datasets" + snapshot.pointer([reaction, "entries", str(i)])
                evidence = row.get("current_reader_verification", {})
                value = {"reaction": reaction, "dataset_id": row["id"]}
                if section == "datasets":
                    value.update(xlsx_path=row["xlsx_path"], filters_key_present=row["filters_key_present"],
                                 literal_filters=deepcopy(row["filters"]),
                                 cataloged_accepted_points=evidence.get("retained_count"),
                                 normalization_declared=row["normalization"] is not None)
                elif row["normalization"] is not None:
                    value["original_definition"] = deepcopy(row["normalization"]["definition"])
                    value["reproduction_override"] = None
                    if "relative_dN_first_retained" in evidence:
                        value["catalog_deltaN_evidence"] = evidence["relative_dN_first_retained"]
                    path += "/normalization"
                else:
                    continue
                records.append(_record(section, path, value))
        return records
    if section == "overlays":
        records = [_record(section, o["source_pointer"], o) for o in reproduction["overlays"]]
        records.append(_record(section, "/reproduction_configuration/Gaussian_initialization",
                               reproduction["policies"].get("Gaussian_initialization")))
        return records
    original = {r["key"]: r["value"] for r in source["global_input_settings"]}
    records = []
    for key, value in reproduction["native_settings"].items():
        row = {"key": key, "original_present": key in original, "reproduction_value": value}
        if key in original:
            row["original_value"] = original[key]
        records.append(_record(section, "/reproduction/native_settings" + snapshot.pointer([key]), row))
    records.append(_record(section, "/reproduction_configuration/Gaussian_initialization",
                           reproduction["policies"].get("Gaussian_initialization")))
    records.append(_record(section, "/source_verification/current_reader_point_validation",
                           {"prior_catalog_evidence": _prior(ir["literal_catalog"]),
                            "executed_this_request": False}))
    records.append(_record(section, "/catalog_validation", ir["literal_catalog"]["catalog_validation"]))
    records.append(_record(section, "/options", ir["options"]))
    records.append(_record(section, "/source_verification/named_input_constants",
                           {"present": "named_input_constants" in source,
                            **({"value": source["named_input_constants"]} if "named_input_constants" in source else {})}))
    for reaction, block in reproduction["datasets"].items():
        records.append(_record(section, "/source_verification/datasets" + snapshot.pointer([reaction]),
                               {"reaction": reaction, "filters_key_present": block["filters_key_present"],
                                "original_raw_filter_configuration": block["raw_filter_configuration"],
                                "reproduction_override": None}))
    return records


def preview(store, configuration_ref, options=None, section="summary", cursor="", limit=10):
    """Compile, render, and check in memory on every request; use no writer."""
    if not isinstance(section, str) or section not in SECTIONS:
        raise ValueError("section must be summary, datasets, normalizations, settings, overlays or issues")
    catalog._validate_limit(limit)
    catalog._validate_text(cursor, "cursor", 1024)
    compiled = compiler.compile_configuration(store, configuration_ref, options)
    ir = compiled["ir"]
    issues = deepcopy(compiled["issues"])
    rendered, input_check, input_sha = None, None, None
    if not issues:
        try:
            rendered = materializer.render_compiled_input(store, compiled["compiled_ref"])
            if (rendered["compiled_ref"] != compiled["compiled_ref"]
                    or snapshot.digest(rendered["ir"]) != snapshot.digest(ir)
                    or rendered["schema"] != materializer.SCHEMA):
                issues.append(_issue("render_revision_mismatch", "/input", "Renderer returned a different compiler/configuration revision or materializer schema."))
            input_check = materializer.check_generated_input(ir, rendered["input_text"])
            issues.extend(dict(i, code="generated_input_mismatch") for i in input_check["issues"])
            digest = hashlib.sha256(rendered["input_text"].encode("utf-8")).hexdigest()
            if digest != rendered["input_sha256"]:
                issues.append(_issue("input_checksum_mismatch", "/input_sha256", "Generated input bytes do not match their claimed SHA256."))
            if not issues:
                input_sha = digest
        except ValueError as error:
            issues.append(_issue("generated_input_rejected", "/input", str(error)))
    counts = deepcopy(compiled["report"]["counts"])
    source = ir["literal_catalog"]
    validation = source["catalog_validation"]
    identity = source["analysis_id"]
    hard = bool(issues)
    blockers = ["read_only_preview_no_persistent_materialization", "readers_not_executed_by_preview"]
    if not validation["reproduction_ready"]:
        blockers.append("historical_reproduction_requirements_unresolved")
    if hard:
        blockers.append("scientific_parity_failed")
    base = {"analysis_id": identity, "section": "compiled_input_preview", "view": section,
            "response_format": "json_text", "preview_schema": SCHEMA,
            "compiler_schema": compiler.SCHEMA, "materializer_schema": materializer.SCHEMA,
            "configuration_ref": ir["configuration_ref"], "source_catalog_ref": ir["source_catalog_ref"],
            "compiled_ref": compiled["compiled_ref"],
            "input_ref": "compiled-input:" + identity + "@" + input_sha if input_sha else None,
            "input_sha256": input_sha, "status": "blocked" if hard else "preview_validated",
            "counts": counts, "count_scope": "Expected counts from pinned catalog evidence; not a current reader run.",
            "parity": {"passed": not hard, "compiler_passed": not compiled["issues"],
                       "generated_input_passed": input_check["passed"] if input_check is not None else False,
                       "comparison": "exact_typed_literal_assignments_and_pinned_IR",
                       "hard_failure_count": len(issues)},
            "issue_count": len(issues), "reader_validation": {"executed_this_request": False, "verified_this_request": False},
            "materialization_allowed": False, "campaign_execution_allowed": False,
            "materialization_blockers": blockers}
    if section == "normalizations":
        base["normalization_penalty_policy"] = deepcopy(ir["reproduction"]["normalization_policy"])
        base["uncertainty_scope"] = "Prior catalog evidence only; workbook/native preprocessing not executed. Literal linked values are not dereferenced."
    records = _records(ir, "issues" if hard else section, issues)
    base["record_section"] = "issues" if hard else section
    base["records_sha256"] = snapshot.digest(records)
    if section == "summary" and not hard:
        report = compiled["report"]
        base.update({
            "dataset_identities": {rx: [row["id"] for row in block["entries"]] for rx, block in ir["reproduction"]["datasets"].items()},
            "normalization_bounds": report["normalization_bounds"],
            "normalization_probes": report["normalization_probes"],
            "normalization_penalty_evidence": report["normalization_penalty_evidence"],
            "penalty_convention": report["penalty_convention"],
            "initialization": report["author_overlays"]["Gaussian_initialization"],
            "sampling_exclusions": "Reproduction Gaussian overlays apply only to independent model parameters, not fixed/linked entries or normalizations. No sampling is executed.",
            "gaussian_objective_penalties": ir["reproduction"]["policies"].get("gaussian_template_parameter_penalties"),
            "qcd_settings": ir["reproduction"]["native_settings"],
            "original_vs_reproduction_overlays": ir["reproduction"]["overlays"],
            "prior_reader_evidence": _prior(source),
            "historical_blocker_ids": validation.get("blocking_unresolved_items", []),
            "available_sections": list(SECTIONS),
            "retrieval": "Use sections datasets/normalizations/settings/overlays/issues for exact records; follow next_cursor with identical configuration_ref, options and section. limit 1-50. No input source is transmitted."})
        # Keep large future metadata retrievable through targeted sections while
        # reserving enough room for an escaped fragment and a bounded cursor.
        targets = {"dataset_identities": "datasets", "normalization_bounds": "normalizations",
                   "original_vs_reproduction_overlays": "overlays", "initialization": "settings",
                   "prior_reader_evidence": "settings", "qcd_settings": "settings",
                   "historical_blocker_ids": "settings", "normalization_probes": "settings",
                   "normalization_penalty_evidence": "normalizations"}
        for field, target in targets.items():
            probe = dict(base, items=[{"text": "x" * 1800}] if issues else [], total_items=999999999,
                         offset=999999999, next_cursor="x" * 200, complete=False)
            if catalog._fits(probe):
                break
            value = base[field]
            base[field] = {"retained_server_side": True, "canonical_sha256": snapshot.digest(value),
                           "json_characters": len(snapshot.canonical(value)), "retrieve_section": target}
    return store._page(base, records, cursor, limit)
