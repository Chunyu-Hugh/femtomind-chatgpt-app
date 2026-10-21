"""Pure catalog-to-IR compiler. No readers, templates, campaign cards or I/O.

The only input authority is a freshly validated, revision-pinned catalog
snapshot. The full IR stays server-side; MCP exposes a bounded parity preview.
This module deliberately cannot create an input.py or authorize materialization.
"""
from collections import OrderedDict
from copy import deepcopy
import math
from pathlib import PurePosixPath
import re
from threading import Lock

from . import configuration_snapshot as snapshot

SCHEMA = "femtomind.scientific_configuration_ir.v1"
_CACHE = OrderedDict()
_LOCK = Lock()
_MAX_CACHE = 4
_NATIVE = {
    "bootstrap", "ht", "offpdf", "resample version", "beta version", "sia version",
    "sea_type", "strange off", "s_veto", "grid path", "grid-8 path", "ftol",
    "flat par", "dglap mode", "alphaSmode", "order", "Q20", "nuc", "tmc", "ht type", "ww",
}
_REPRODUCTION_METADATA = {"status", "numerical_authority", "do_not_change", "limitation"}
_BOOL_SETTINGS = {"bootstrap", "ht", "offpdf", "strange off", "s_veto", "flat par", "nuc", "ww"}
_NUMBER_SETTINGS = {"resample version", "beta version", "sia version", "sea_type", "ftol", "Q20"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _same(left, right):
    return snapshot.canonical(left) == snapshot.canonical(right)


def _issue(code, path, message):
    return {"code": code, "path": path, "message": message, "severity": "error", "blocks_materialization": True}


def _brief(value):
    text = snapshot.canonical(value)
    return value if len(text) <= 160 else {"sha256": snapshot.digest(value), "json_characters": len(text)}


def _differences(expected, actual, path=""):
    """Exact, typed JSON comparison, including missing vs null. No tolerances."""
    if _same(expected, actual):
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        issues = []
        for key in sorted(set(expected) | set(actual)):
            p = path + snapshot.pointer([key])
            if key not in expected or key not in actual:
                item = _issue("scientific_mismatch", p, "Field presence changed; missing is not null.")
                item.update(expected_present=key in expected, actual_present=key in actual)
                issues.append(item)
            else:
                issues.extend(_differences(expected[key], actual[key], p))
        return issues
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [issue for i, (a, b) in enumerate(zip(expected, actual)) for issue in _differences(a, b, path + "/" + str(i))]
    item = _issue("scientific_mismatch", path, "Scientific value, type, order or collection size changed.")
    item.update(expected=_brief(expected), actual=_brief(actual))
    return [item]


def _options(options):
    options = {} if options is None else deepcopy(options)
    if not isinstance(options, dict) or set(options) - {"reproduction_policy", "label", "normalization_probes"}:
        raise ValueError("Options accept only reproduction_policy, label and normalization_probes; scientific overrides are not accepted.")
    policy = options.get("reproduction_policy", "catalog_author_confirmed")
    if policy != "catalog_author_confirmed":
        raise ValueError("Only reproduction_policy=catalog_author_confirmed is supported.")
    label = options.get("label", "")
    if not isinstance(label, str) or len(label) > 80:
        raise ValueError("label must be a string of at most 80 characters.")
    probes = options.get("normalization_probes", [])
    if not isinstance(probes, list) or len(probes) > 8:
        raise ValueError("normalization_probes must be a list of at most eight admissibility assertions.")
    for probe in probes:
        if (not isinstance(probe, dict) or set(probe) != {"reaction", "dataset_id", "value"}
                or any(not isinstance(probe[k], str) or not 0 < len(probe[k]) <= 128 for k in ("reaction", "dataset_id"))
                or not _number(probe["value"])):
            raise ValueError("Each normalization probe needs bounded reaction/dataset_id strings and a finite numeric value.")
    return {"reproduction_policy": policy, "label": label, "normalization_probes": probes}


def _resolve(store, reference):
    if not isinstance(reference, str) or not re.fullmatch(r"configuration:[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[0-9a-f]{64}", reference):
        raise ValueError("A revision-pinned configuration_ref is required; aliases and catalog_ref are not accepted.")
    identity = reference[14:].split("@", 1)[0]
    from .analysis_catalog import Catalog
    fresh = Catalog(store.root)
    entry = fresh._entry(identity)
    snap = snapshot.snapshot(fresh.root, entry, fresh._reference(entry))
    if snap.ref != reference:
        raise ValueError("Stale configuration reference; retrieve the current configuration manifest.")
    return snap


def _mapping(value, path, issues):
    if not isinstance(value, dict):
        issues.append(_issue("missing_requirement", path, "An explicit mapping is required; no application default was substituted."))
        return {}
    return value


def _rows(value, path, issues):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        issues.append(_issue("missing_requirement", path, "An explicit record list is required."))
        return []
    return value


def _definition(entry, path, issues, normalization=False):
    entry = _mapping(entry, path, issues)
    for key in ("value", "min", "max", "fixed"):
        if key not in entry:
            issues.append(_issue("missing_requirement", path + "/" + key, "Authoritative declaration field is absent."))
    fixed = entry.get("fixed")
    valid_fixed = type(fixed) is bool or (type(fixed) is int if normalization else isinstance(fixed, str) and bool(fixed))
    if not valid_fixed:
        issues.append(_issue("unsupported_setting", path + "/fixed", "Unsupported fixed/link representation; it was not coerced."))
    if not _number(entry.get("value")):
        issues.append(_issue("missing_requirement", path + "/value", "A finite literal starting value is required."))
    if fixed is False:
        lo, hi, value = (entry.get(k) for k in ("min", "max", "value"))
        if not all(_number(v) for v in (lo, hi, value)) or not lo < hi or not lo <= value <= hi:
            issues.append(_issue("invalid_bounds", path, "Independent parameters require finite ordered bounds containing the declared start."))
    return entry


def _links(rows, path, issues, identity="name", normalization=False):
    indexed = {}
    for i, row in enumerate(rows):
        key = row.get(identity)
        if not isinstance(key, (str, int)) or isinstance(key, bool) or str(key) in indexed:
            issues.append(_issue("missing_requirement", path + "/" + str(i), "Record identity is absent, invalid or duplicated."))
            continue
        indexed[str(key)] = row
    links = {}
    for key, row in indexed.items():
        fixed = _dict(row.get("definition")).get("fixed")
        linked = type(fixed) is int if normalization else isinstance(fixed, str)
        if linked:
            links[key] = str(fixed)
            if str(fixed) not in indexed:
                issues.append(_issue("invalid_link", path, "Link target is absent in the same object/reaction: " + key))
    for start in links:
        seen, current = set(), start
        while current in links:
            if current in seen:
                issues.append(_issue("invalid_link", path, "Circular parameter link: " + start))
                break
            seen.add(current)
            current = links[current]


def _build(source):
    """Translate declarations and explicit author overlays, without a template."""
    issues, overlays = [], []
    sv = _mapping(source.get("source_verification"), "/source_verification", issues)
    parameters = deepcopy(_mapping(sv.get("parameter_definitions"), "/source_verification/parameter_definitions", issues))
    datasets = deepcopy(_mapping(sv.get("datasets"), "/source_verification/datasets", issues))
    declared = _rows(sv.get("global_input_settings"), "/source_verification/global_input_settings", issues)
    reproduction = deepcopy(_mapping(source.get("reproduction_configuration"), "/reproduction_configuration", issues))
    native = {}
    for i, row in enumerate(declared):
        key = row.get("key")
        path = "/source_verification/global_input_settings/" + str(i)
        if not isinstance(key, str) or not key or key in native or "value" not in row:
            issues.append(_issue("missing_requirement", path, "Setting key/value is missing or duplicated."))
            continue
        native[key] = deepcopy(row["value"])
        if key not in _NATIVE and not key.endswith((" parametrization", " positivity")):
            issues.append(_issue("unsupported_setting", path, "Unrecognized native setting retained verbatim: " + key))
        elif ((key in _BOOL_SETTINGS or key.endswith(" positivity")) and type(row["value"]) is not bool
              or key in _NUMBER_SETTINGS and not _number(row["value"])
              or key in ("Q20", "ftol") and _number(row["value"]) and row["value"] <= 0
              or key not in _BOOL_SETTINGS | _NUMBER_SETTINGS and not key.endswith(" positivity")
              and not (isinstance(row["value"], str) or key == "tmc" and type(row["value"]) is bool
                       or key.endswith(" parametrization") and type(row["value"]) is int)):
            issues.append(_issue("unsupported_setting", path, "Unsupported setting value type retained without conversion."))

    def overlay(key, value, source_path):
        before_present = key in native
        change = {"key": key, "before_present": before_present, "after": deepcopy(value), "source_pointer": source_path}
        change["changed"] = not before_present or not _same(native[key], value)
        if before_present:
            change["before"] = deepcopy(native[key])
        overlays.append(change)
        native[key] = deepcopy(value)

    for key, value in reproduction.items():
        path = "/reproduction_configuration/" + key
        if key == "Q20":
            if not isinstance(value, dict) or not _number(value.get("value")) or value["value"] <= 0:
                issues.append(_issue("missing_requirement", path, "Explicit positive reproduction Q20 is required."))
            else:
                overlay("Q20", value["value"], path)
        elif key == "positivity":
            flags = _mapping(value.get("fitpack_flags") if isinstance(value, dict) else None, path, issues)
            if not flags:
                issues.append(_issue("missing_requirement", path, "An explicit nonempty positivity overlay is required when declared."))
            for flag, enabled in flags.items():
                if not flag.endswith(" positivity") or type(enabled) is not bool:
                    issues.append(_issue("unsupported_setting", path, "Positivity flags require literal booleans."))
                else:
                    overlay(flag, enabled, path + "/fitpack_flags" + snapshot.pointer([flag]))
        elif key == "gaussian_template_parameter_penalties":
            if value is not False:
                issues.append(_issue("unsupported_setting", path, "Gaussian objective penalties are not supported or introduced by this compiler."))
        elif key == "Gaussian_initialization" or key in _REPRODUCTION_METADATA:
            continue
        elif isinstance(value, dict) and isinstance(value.get("status"), str):
            continue  # Non-executable restriction/provenance, preserved in full.
        else:
            issues.append(_issue("unsupported_setting", path, "Unrecognized reproduction setting retained without interpretation."))
    gaussian = reproduction.get("Gaussian_initialization")
    if gaussian is not None:
        if (not isinstance(gaussian, dict) or gaussian.get("parameter_key") != "gaussian"
                or gaussian.get("gaussian") is not True or gaussian.get("flat par") is not True):
            issues.append(_issue("unsupported_setting", "/reproduction_configuration/Gaussian_initialization", "Only explicit lowercase Gaussian sampling with flat par=True is supported."))
            gaussian = None
        else:
            overlay("flat par", True, "/reproduction_configuration/Gaussian_initialization")
            allowed = {"status", "source_id", "flat par", "parameter_key", "gaussian", "means_by_coefficient",
                       "standard_deviations_by_coefficient", "selection_rule", "parameter_values_and_bounds",
                       "linked_parameters", "sampler", "Gaussian_template_penalties", "FitPack_implementation_changes"}
            for key in set(gaussian) - allowed:
                issues.append(_issue("unsupported_setting", "/reproduction_configuration/Gaussian_initialization/" + key, "Unknown initialization setting retained without interpretation."))
            if gaussian.get("Gaussian_template_penalties", False) is not False or gaussian.get("FitPack_implementation_changes", False) is not False:
                issues.append(_issue("unsupported_setting", "/reproduction_configuration/Gaussian_initialization", "Sampling-code or objective changes are not supported."))
    for obj, rows in list(parameters.items()):
        path = "/source_verification/parameter_definitions" + snapshot.pointer([obj])
        rows = _rows(rows, path, issues)
        for i, row in enumerate(rows):
            definition = _definition(row.get("definition"), path + "/" + str(i) + "/definition", issues)
            if gaussian and definition.get("fixed") is False:
                coefficient = str(row.get("name", "")).rsplit(" ", 1)[-1]
                mean = _dict(gaussian.get("means_by_coefficient")).get(coefficient)
                std = _dict(gaussian.get("standard_deviations_by_coefficient")).get(coefficient)
                if not _number(mean) or not _number(std) or std <= 0:
                    issues.append(_issue("missing_requirement", path + "/" + str(i), "Missing valid coefficient-specific Gaussian mean/std; no default was invented."))
                else:
                    definition.update(gaussian=True, mean=mean, std=std)
        _links(rows, path, issues)
        expected_count = _dict(_dict(sv.get("parameter_counts")).get(obj)).get("definitions")
        if expected_count is not None and expected_count != len(rows):
            issues.append(_issue("declared_count_mismatch", path, "Parameter collection disagrees with the source's independent definition count."))
    for reaction, container in datasets.items():
        path = "/source_verification/datasets" + snapshot.pointer([reaction])
        container = _mapping(container, path, issues)
        for field in ("raw_filter_configuration", "filters_key_present"):
            if field not in container:
                issues.append(_issue("missing_requirement", path + "/" + field, "Literal reaction filter presence/configuration is required."))
        rows = _rows(container.get("entries"), path + "/entries", issues)
        if "declared_file_count" in container and container["declared_file_count"] != len(rows):
            issues.append(_issue("declared_count_mismatch", path, "Dataset collection disagrees with its declared count, including zero-point records."))
        norms = []
        for i, row in enumerate(rows):
            p = path + "/entries/" + str(i)
            xlsx = row.get("xlsx_path")
            if not isinstance(xlsx, str) or not xlsx or PurePosixPath(xlsx).is_absolute() or ".." in PurePosixPath(xlsx).parts or "\\" in xlsx or ":" in xlsx:
                issues.append(_issue("unsupported_setting", p + "/xlsx_path", "Dataset paths must be literal relative paths; no filesystem path was followed."))
            for field in ("filters", "filters_key_present", "normalization"):
                if field not in row:
                    issues.append(_issue("missing_requirement", p + "/" + field, "Explicit dataset field is required; absent and null were not conflated."))
            if type(row.get("filters_key_present")) is not bool or (row.get("filters") is not None and not isinstance(row["filters"], list)):
                issues.append(_issue("unsupported_setting", p + "/filters", "Unsupported dataset filter representation retained verbatim."))
            if row.get("normalization") is not None:
                norm = _mapping(row["normalization"], p + "/normalization", issues)
                definition = _definition(norm.get("definition"), p + "/normalization/definition", issues, normalization=True)
                norms.append({"id": row.get("id"), "definition": definition})
            count = _dict(row.get("current_reader_verification")).get("retained_count")
            if type(count) is not int or count < 0:
                issues.append(_issue("missing_requirement", p, "Cataloged retained count is missing; readers were not run."))
        _links(norms, path + "/normalizations", issues, identity="id", normalization=True)
    audit = deepcopy(sv.get("dataset_normalization_audit", {}))
    if not isinstance(audit, dict):
        issues.append(_issue("missing_requirement", "/source_verification/dataset_normalization_audit", "Normalization evidence must be a mapping."))
        audit = {}
    indexed = {(r, str(row.get("id"))): row for r, d in datasets.items() if isinstance(d, dict)
               for row in _list(d.get("entries")) if isinstance(row, dict)}
    audit_rows = _rows(audit["full_normalization_mapping"], "/source_verification/dataset_normalization_audit/full_normalization_mapping", issues) if "full_normalization_mapping" in audit else []
    for i, record in enumerate(audit_rows):
        row = indexed.get((record.get("reaction"), str(record.get("id"))))
        path = "/source_verification/dataset_normalization_audit/full_normalization_mapping/" + str(i)
        if row is None or not isinstance(row.get("normalization"), dict):
            issues.append(_issue("normalization_evidence_mismatch", path, "Normalization evidence has no configured declaration."))
            continue
        evidence = _dict(row.get("current_reader_verification"))
        if (not _same(record.get("definition"), row["normalization"].get("definition"))
                or not _same(record.get("input_relative_uncertainty"), evidence.get("relative_dN_first_retained"))
                or record.get("workbook_sha256") != evidence.get("sha256")):
            issues.append(_issue("normalization_evidence_mismatch", path, "Independent bounds, deltaN or workbook evidence disagree; nothing was repaired."))
    for (reaction, identity), row in indexed.items():
        if row.get("normalization") is not None:
            evidence = _dict(row.get("current_reader_verification"))
            if _number(evidence.get("retained_count")) and evidence["retained_count"] > 0 and (not _number(evidence.get("relative_dN_first_retained")) or evidence["relative_dN_first_retained"] <= 0):
                issues.append(_issue("missing_requirement", "/datasets" + snapshot.pointer([reaction, identity]), "Normalization uncertainty is absent/invalid; no zero penalty or tighter prior was substituted."))
    return {"parameter_definitions": parameters, "datasets": datasets, "native_settings": native,
            "policies": reproduction, "normalization_evidence": audit,
            "normalization_policy": {"mode": "preserve_original_fitpack_workbook_convention",
                                     "bounds_are_not_penalties": True, "replace_finite_deltaN_with_zero_penalty": False},
            "overlays": overlays}, issues


def validate_parity(ir, snap, options=None):
    """Public pure validator for the future materializer boundary and mutation tests."""
    options = _options(options)
    expected, issues = _build(snap.source)
    expected_ir = {"compiler_schema": SCHEMA, "canonical_encoding": snapshot.CANONICAL, "configuration_ref": snap.ref,
                   "source_catalog_ref": snap.catalog_ref, "source_documents": snap.manifest["source_documents"],
                   "literal_catalog": snap.source, "reproduction": expected, "options": options}
    issues += _differences(expected_ir, ir)
    if snap.unclassified:
        issues.extend(_issue("unsupported_setting", path, "Unclassified catalog branch is preserved but requires compiler review.") for path in snap.unclassified)
    indexed = {(rx, str(row.get("id"))): row for rx, data in expected["datasets"].items() if isinstance(data, dict)
               for row in _list(data.get("entries")) if isinstance(row, dict)}
    for probe in options["normalization_probes"]:
        row = indexed.get((probe["reaction"], probe["dataset_id"]), {})
        definition = _dict(_dict(row.get("normalization")).get("definition"))
        if not all(_number(definition.get(k)) for k in ("min", "max")) or not definition["min"] <= probe["value"] <= definition["max"]:
            issues.append(_issue("normalization_probe_failed", "/options/normalization_probes", "Required normalization value is outside authoritative bounds or its declaration is absent."))
    return issues


def _summary(ir, snap, issues):
    data = ir["reproduction"]
    params = [p for rows in data["parameter_definitions"].values() if isinstance(rows, list) for p in rows if isinstance(p, dict)]
    datasets = [(rx, row) for rx, d in data["datasets"].items() if isinstance(d, dict) for row in _list(d.get("entries")) if isinstance(row, dict)]
    norms = [(rx, row) for rx, row in datasets if isinstance(row.get("normalization"), dict)]
    counts = {"model_declarations": len(params), "model_links": sum(isinstance(_dict(p.get("definition")).get("fixed"), str) for p in params),
              "gaussian_independent_parameters": sum(_dict(p.get("definition")).get("fixed") is False and p["definition"].get("gaussian") is True for p in params),
              "normalization_declarations": len(norms), "normalization_links": sum(type(_dict(p["normalization"].get("definition")).get("fixed")) is int for _, p in norms),
              "configured_datasets": len(datasets), "nonempty_datasets": sum(_dict(p.get("current_reader_verification")).get("retained_count", 0) > 0 for _, p in datasets if type(_dict(p.get("current_reader_verification")).get("retained_count")) is int),
              "cataloged_accepted_points": sum(p["current_reader_verification"]["retained_count"] for _, p in datasets if type(_dict(p.get("current_reader_verification")).get("retained_count")) is int)}
    evidence = {(r.get("reaction"), str(r.get("id"))): r for r in _list(data["normalization_evidence"].get("full_normalization_mapping")) if isinstance(r, dict)}
    groups = {}
    for rx, row in norms:
        definition = _dict(row["normalization"].get("definition"))
        key = (rx, snapshot.canonical([definition.get("min"), definition.get("max")]))
        group = groups.setdefault(key, {"reaction": rx, "min": definition.get("min"), "max": definition.get("max"), "count": 0, "paper_unpenalized_finite_deltaN": 0, "other_finite_deltaN": 0})
        group["count"] += 1
        delta = _dict(row.get("current_reader_verification")).get("relative_dN_first_retained")
        if _number(delta) and delta > 0:
            weak = evidence.get((rx, str(row.get("id"))), {}).get("paper_unpenalized") is True
            group["paper_unpenalized_finite_deltaN" if weak else "other_finite_deltaN"] += 1
    probes = []
    for probe in ir["options"]["normalization_probes"]:
        row = next((p for rx, p in norms if rx == probe["reaction"] and str(p.get("id")) == probe["dataset_id"]), None)
        d = _dict(row["normalization"].get("definition")) if row else {}
        permitted = all(_number(d.get(k)) for k in ("min", "max")) and d["min"] <= probe["value"] <= d["max"]
        probes.append(dict(probe, permitted=permitted, declaration_unchanged=True))
    validation = snap.source["catalog_validation"]
    paths = [i["path"] for i in issues]
    families = {"literal_declarations": ("/literal_catalog",),
                "model_links_and_bounds": ("/reproduction/parameter_definitions", "/source_verification/parameter_definitions"),
                "datasets_paths_cuts": ("/reproduction/datasets", "/source_verification/datasets"),
                "normalizations_bounds_links": ("/reproduction/datasets", "/source_verification/datasets"),
                "normalization_uncertainties_and_penalty_convention": ("/reproduction/normalization", "/source_verification/dataset_normalization_audit", "/source_verification/datasets"),
                "native_settings_and_author_overlays": ("/reproduction/native_settings", "/reproduction/policies", "/reproduction_configuration", "/source_verification/global_input_settings")}
    weak = [r.get("input_relative_uncertainty") for r in evidence.values() if r.get("paper_unpenalized") is True]
    return {"counts": counts, "normalization_bounds": list(groups.values()), "normalization_probes": probes,
            "author_overlays": {"native_settings": data["overlays"],
                                "Gaussian_initialization": {k: v for k, v in _dict(data["policies"].get("Gaussian_initialization")).items()
                                                            if k in ("parameter_key", "gaussian", "flat par", "means_by_coefficient", "standard_deviations_by_coefficient")},
                                "independent_model_parameters_only": True,
                                "gaussian_template_parameter_penalties": data["policies"].get("gaussian_template_parameter_penalties")},
            "parity": {"passed": not issues, "comparison": "exact_typed_json", "hard_error_count": len(issues),
                       "scientific_mismatches": sum(i["code"] in {"scientific_mismatch", "normalization_evidence_mismatch", "declared_count_mismatch", "invalid_bounds", "invalid_link"} for i in issues),
                       "missing_requirements": sum(i["code"] == "missing_requirement" for i in issues), "unsupported_settings": sum(i["code"] == "unsupported_setting" for i in issues),
                       "checks": {key: not any(path.startswith(prefix) for path in paths for prefix in prefixes) for key, prefixes in families.items()}},
            "normalization_penalty_evidence": {"paper_unpenalized_finite_deltaN_count": len(weak), "deltaN_values": list(dict.fromkeys(weak)), "exact_zero_penalty_substitutions": 0},
            "historical_reproduction_ready": validation["reproduction_ready"], "historical_blocker_ids": validation.get("blocking_unresolved_items", []),
            "catalog_campaign_generation_allowed": validation.get("campaign_generation_allowed"),
            "reader_selection_verified": False, "point_count_notice": "Catalog counts only; no reader was run. Count equality is not proof of selected-point identity.",
            "materialization_allowed": False, "materialization_blockers": ["compiler_preview_only", "reader_selection_not_verified"] + (["scientific_parity_failed"] if issues else []),
            "penalty_convention": "Finite workbook deltaN is preserved verbatim, independently of bounds; no exactly-zero penalty conversion or objective change."}


def compile_configuration(store, configuration_ref, options=None):
    """Return a complete internal IR and compact report. Never write files."""
    options = _options(options)
    snap = _resolve(store, configuration_ref)
    reproduction, _ = _build(snap.source)
    ir = {"compiler_schema": SCHEMA, "canonical_encoding": snapshot.CANONICAL, "configuration_ref": snap.ref,
          "source_catalog_ref": snap.catalog_ref, "source_documents": deepcopy(snap.manifest["source_documents"]),
          "literal_catalog": deepcopy(snap.source), "reproduction": reproduction, "options": options}
    issues = validate_parity(ir, snap, options)
    report = _summary(ir, snap, issues)
    reference = "compiled:" + snap.source["analysis_id"] + "@" + snapshot.digest(ir)
    result = {"ir": ir, "issues": issues, "report": report, "compiled_ref": reference}
    # Cache only after fresh Catalog and configuration revision validation. The
    # reference binds the compiler/encoding, options, original source and full IR.
    with _LOCK:
        _CACHE[reference] = deepcopy(result)
        _CACHE.move_to_end(reference)
        while len(_CACHE) > _MAX_CACHE:
            _CACHE.popitem(last=False)
    return result


def resolve_compiled(store, compiled_ref):
    """Internal future-adapter boundary; no public materialization operation."""
    with _LOCK:
        saved = deepcopy(_CACHE.get(compiled_ref))
    if saved is None:
        raise ValueError("Compiled reference unavailable or evicted; repeat the preview.")
    snap = _resolve(store, saved["ir"]["configuration_ref"])
    if ("compiled:" + snap.source["analysis_id"] + "@" + snapshot.digest(saved["ir"]) != compiled_ref
            or validate_parity(saved["ir"], snap, saved["ir"]["options"]) or saved["issues"]):
        raise ValueError("Scientific parity failed; compiled configuration cannot be consumed.")
    return saved["ir"]


def preview(store, configuration_ref, options=None, cursor="", limit=10):
    result = compile_configuration(store, configuration_ref, options)
    base = {"section": "configuration_parity_preview", "response_format": "json_text", "compiler_schema": SCHEMA,
            "configuration_ref": result["ir"]["configuration_ref"], "compiled_ref": result["compiled_ref"],
            "source_catalog_ref": result["ir"]["source_catalog_ref"], "label": result["ir"]["options"]["label"],
            **result["report"], "issue_count": len(result["issues"])}
    # The report itself must fit even for future catalogs with hundreds of bound
    # groups or very large author metadata. Scientific issue records remain
    # lossless/paginated; only oversized summary fields become server-side refs.
    from .analysis_catalog import _fits
    for field in ("author_overlays", "normalization_bounds", "normalization_probes", "normalization_penalty_evidence", "historical_blocker_ids"):
        probe = dict(base, items=[], total_items=999999999, offset=999999999, next_cursor="x" * 200, complete=False)
        if _fits(probe):
            break
        value = base[field]
        base[field] = {"retained_server_side": True, "canonical_sha256": snapshot.digest(value),
                       "json_characters": len(snapshot.canonical(value)), "entries": len(value)}
    return store._page(base, result["issues"], cursor, limit)
