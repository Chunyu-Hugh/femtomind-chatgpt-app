"""Read-only compact configuration projection of paper_analysis_catalog 0.x.

Canonical JSON v1 is Python's shortest-roundtrip binary64 JSON representation,
sorted Unicode keys, ensure_ascii=True, compact separators, allow_nan=False,
encoded as UTF-8 without BOM/newline. Integers remain integers (including large
integers); 1 != 1.0, False != 0, and -0.0 != 0.0 in hashes. No Unicode
normalization, float rounding, link evaluation, or scientific arithmetic occurs.
This is a versioned Python JSON contract, NOT RFC 8785/JCS. See deploy README.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from threading import Lock

SCHEMA = "femtomind.configuration_snapshot.v1"
CANONICAL = "python-json-canonical-v1"
_CACHE = OrderedDict()
_LOCK = Lock()
_CACHE_SIZE = 4
_CHUNK = 1100


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _at(source, parts):
    for part in parts:
        source = source[part]
    return source


# These existing schema branches are historical/audit evidence, not executable
# configuration. Unknown branches are transmitted in full and flagged instead
# of being silently classified as evidence. Names here describe schema fields,
# never particular analyses, model objects, reactions, parameter names, or IDs.
_EVIDENCE_TOP = {
    "experimental_data", "publication_bibliography_as_listed", "publication_ambiguities",
    "FEMTOMIND_capability_assessment", "completeness_assessment", "paper_only_baseline",
    "application_followup", "unresolved_settings",
}
_EVIDENCE_SV = {
    "phase", "date", "ff_link_verification", "author_confirmations",
    "gaussian_sampling_audit", "paper_input_discrepancies", "previously_missed_paper_findings",
    "bounded_sia_file_checks", "current_reader_point_validation",
    "superseded_author_confirmation_history", "belle_kaon_followup",
}
_CONTEXT = {
    "named_input_constants", "parameter_counts", "normalization_counts", "remaining_unknowns",
    "pdf_native_basis_mapping", "latest_author_corrections", "final_author_clarifications",
}
_METADATA = {
    "reproduction_configuration", "catalog_validation", "catalog_schema",
    "historical_section_notice", "publication", "scope", "sources",
}
_ROW_EVIDENCE = {"reference", "implementation_reference", "references", "evidence_origin"}


class Snapshot:
    """Immutable-by-convention projection; all public replies are deep copies."""

    def __init__(self, entry, catalog_ref):
        source = self.source = entry["config"]
        self.catalog_ref = catalog_ref
        self.groups, self.paths, self.evidence, self.common = {}, {}, {}, {}
        self.unclassified = []
        sv = source.get("source_verification", {})

        def register(parts):
            self.evidence[pointer(parts)] = tuple(parts)

        def add(group, key, value, parts, header=False, missing_identity=False):
            rows = self.groups.setdefault(group, [])
            if header:
                row = {"kind": "configuration", "value": deepcopy(value)}
            elif missing_identity:
                row = {"source_index": parts[-1], "value": deepcopy(value)}
            else:
                row = {"key": key, "value": deepcopy(value)}
            if not header and not missing_identity and any(canonical(r["key"]) == canonical(key) for r in rows if "key" in r):
                raise ValueError("Duplicate configuration record key.")
            rows.append(row)
            self.paths.setdefault(group, []).append(pointer(parts))
            register(parts)

        def common_text(rows, location, field):
            # Only factor a field when EVERY record explicitly has the same
            # value. Missing and explicit null must never become equivalent.
            values = [location(row) for row in rows]
            if values and all(isinstance(v, dict) and field in v for v in values):
                first = values[0][field]
                if all(canonical(v[field]) == canonical(first) for v in values):
                    return True, deepcopy(first)
            return False, None

        parameter_rows = [r for rows in sv.get("parameter_definitions", {}).values() for r in rows]
        shared, text = common_text(parameter_rows, lambda r: r, "value_interpretation")
        if shared:
            self.common["parameter_value_interpretation"] = text
        for obj, rows in sv.get("parameter_definitions", {}).items():
            group = "parameters/" + pointer([obj])[1:]
            self.groups[group] = []
            self.paths[group] = [pointer(["source_verification", "parameter_definitions", obj])]
            register(["source_verification", "parameter_definitions", obj])
            for i, row in enumerate(rows):
                core = {k: v for k, v in row.items() if k not in _ROW_EVIDENCE | {"name", "evidence_status"}}
                if shared:
                    core.pop("value_interpretation")
                add(group, row.get("name"), core,
                    ["source_verification", "parameter_definitions", obj, i], missing_identity="name" not in row)

        norm_rows = [r for container in sv.get("datasets", {}).values() for r in container["entries"]
                     if isinstance(r.get("normalization"), dict)]
        shared, text = common_text(norm_rows, lambda r: r["normalization"], "value_interpretation")
        if shared:
            self.common["normalization_value_interpretation"] = text
        for reaction, container in sv.get("datasets", {}).items():
            group = "reactions/" + pointer([reaction])[1:]
            core = {k: v for k, v in container.items() if k not in ("entries", "filter_source_references")}
            add(group, None, core, ["source_verification", "datasets", reaction], header=True)
            for i, row in enumerate(container["entries"]):
                core = {k: deepcopy(v) for k, v in row.items() if k not in _ROW_EVIDENCE | {"id", "evidence_status"}}
                if isinstance(core.get("normalization"), dict):
                    core["normalization"].pop("reference", None)
                    if shared:
                        core["normalization"].pop("value_interpretation")
                add(group, row.get("id"), core, ["source_verification", "datasets", reaction, "entries", i], missing_identity="id" not in row)

        def settings(group, rows, path, identity):
            for i, row in enumerate(rows):
                core = {k: v for k, v in row.items() if k not in _ROW_EVIDENCE | {identity}}
                add(group, row.get(identity), core, path + [i], missing_identity=identity not in row)

        settings("declared_settings", sv.get("global_input_settings", []), ["source_verification", "global_input_settings"], "key")
        settings("publication_settings", source.get("scientific_settings", []), ["scientific_settings"], "id")
        settings("current_implementation", sv.get("current_native_findings", []), ["source_verification", "current_native_findings"], "id")
        settings("current_resolution_register", sv.get("resolution_register", []), ["source_verification", "resolution_register"], "id")
        historical = {r["id"]: r for r in source.get("unresolved_settings", [])}
        for row in self.groups.get("current_resolution_register", []):
            if row.get("key") in source["catalog_validation"].get("blocking_unresolved_items", []):
                old = historical.get(row["key"], {})
                row["active_requirement_context"] = {
                    "priority": old.get("priority", "unspecified"),
                    "why_it_matters": old.get("why_it_matters_to_FEMTOMIND", "Not specified in the catalog."),
                    "classification": "historical_information", "reproduction_blocker": True}

        known_sv = _EVIDENCE_SV | _CONTEXT | {
            "parameter_definitions", "datasets", "global_input_settings", "current_native_findings", "resolution_register",
        }
        for key in sv:
            if key in _CONTEXT:
                add("configuration_context", key, sv[key], ["source_verification", key])
            elif key.startswith("effective_") and key.endswith("_selection") and isinstance(sv[key], dict):
                # Schema-level selection summaries: preserve all rules, with
                # point-removal verification tables available as exact evidence.
                add("configuration_context", key, {k: v for k, v in sv[key].items() if k != "reader_evidence"}, ["source_verification", key])
                known_sv.add(key)
            elif key == "dataset_normalization_audit" and isinstance(sv[key], dict):
                if "semantics" in sv[key]:
                    add("configuration_context", "normalization_semantics", sv[key]["semantics"], ["source_verification", key, "semantics"])
                known_sv.add(key)
        for key in source:
            if key in _METADATA:
                add("reproduction_metadata", key, source[key], [key])
        known_top = _EVIDENCE_TOP | _METADATA | {"analysis_id", "source_verification", "scientific_settings"}
        for parent, values, known in (([], source, known_top), (["source_verification"], sv, known_sv)):
            for key in sorted(set(values) - known):
                path = parent + [key]
                self.unclassified.append(pointer(path))
                add("unclassified", pointer(path), values[key], path)
        # Exact source branches and all projected original rows are allowlisted.
        for key in source:
            register([key])
        for key in sv:
            register(["source_verification", key])

        self.hash = digest({"schema": SCHEMA, "canonical_encoding": CANONICAL, "source_catalog_ref": catalog_ref,
                            "common": self.common, "groups": self.groups})
        self.ref = "configuration:" + source["analysis_id"] + "@" + self.hash
        self.manifest = {
            "schema": SCHEMA, "canonical_encoding": CANONICAL,
            "configuration_ref": self.ref, "source_catalog_ref": catalog_ref, "configuration_sha256": self.hash,
            "configuration_records_complete": not self.unclassified, "unclassified_source_paths": self.unclassified,
            "reproduction_ready": source["catalog_validation"]["reproduction_ready"],
            "source_documents": {k: {"sha256": d["sha256"], "bytes": d["bytes"]} for k, d in entry["documents"].items()},
            "parts": {key: {"records": len(rows), "sha256": digest(rows),
                            "source_pointer": self.paths[key][0]} for key, rows in self.groups.items()},
            "common_semantics": self.common,
            "value_layers": {
                "parameters": "Literal definitions; links and sum-rule declarations are not replaced by effective values.",
                "declared_settings": "Original input. Reproduction overrides and initialization remain in reproduction_metadata/configuration_context.",
                "publication_settings": "Publication evidence and historical limits, not automatic executable overrides.",
                "normalizations": "Literal definitions; bool false is independent, integer fixed targets are reaction-qualified links.",
            },
            "evidence": {"section": "configuration_evidence", "record_ref_rule": "evidence:<configuration_sha256>:<registered source JSON pointer>",
                         "source_branches": {"source": list(source), "source_verification": list(sv)},
                         "row_pointers": "parameters: source_pointer/index; settings: replace final index in first source_pointer; reactions: header pointer, then /entries/index; context/metadata: source branch named by key (normalization_semantics uses dataset_normalization_audit/semantics). Empty parts have no rows.",
                         "documents": "Existing document sections remain exact source-text exports."},
            "continuation": {"tool": "get_analysis_catalog", "arguments": {
                "analysis_id": self.ref, "section": "configuration", "detail": "full", "response_format": "json_text", "limit": 50},
                "rule": "Pass next_cursor unchanged until complete, without restarting per part. Keep detail/format unchanged; maximum limit 50."},
        }
        if "campaign_generation_allowed" in source["catalog_validation"]:
            self.manifest["campaign_generation_allowed"] = source["catalog_validation"]["campaign_generation_allowed"]

    def evidence_record(self, record_ref):
        prefix = "evidence:" + self.hash + ":"
        if not isinstance(record_ref, str) or not record_ref.startswith(prefix):
            raise ValueError("Invalid or stale configuration evidence reference.")
        source_pointer = record_ref[len(prefix):]
        if source_pointer not in self.evidence:
            raise ValueError("Unknown registered configuration evidence record.")
        value = _at(self.source, self.evidence[source_pointer])
        return {"configuration_ref": self.ref, "source_catalog_ref": self.catalog_ref, "record_ref": record_ref,
                "source_pointer": source_pointer, "source_sha256": digest(value), "record": deepcopy(value)}


def snapshot(root, entry, catalog_ref):
    # Catalog has freshly read/validated/hashed every source document BEFORE this
    # cache lookup. Projection caches never bypass source validation.
    key = (str(root), catalog_ref, SCHEMA, CANONICAL)
    with _LOCK:
        found = _CACHE.get(key)
        if found is not None:
            _CACHE.move_to_end(key)
            return found
    found = Snapshot(entry, catalog_ref)
    with _LOCK:
        _CACHE[key] = found
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return found


def page(store, snap, section, record_ref, cursor, limit, detail, response_format):
    """Pack named parts across page boundaries; never split scientific arrays.

    Oversized records/manifests are instead losslessly serialized into ordered,
    checksum-protected fragments. Offsets/limit count wire records/fragments.
    """
    from .analysis_catalog import _fits, _validate_limit, _validate_text
    _validate_limit(limit)
    _validate_text(cursor, "cursor", 1024)
    _validate_text(record_ref, "record_ref", 4096)
    context = digest({"configuration_ref": snap.ref, "section": section, "record_ref": record_ref,
                      "detail": detail, "response_format": response_format, "packing": "named-parts-v1"})
    base = {"configuration_ref": snap.ref, "section": section}
    if response_format != "legacy":
        base["response_format"] = response_format
    probe = dict(base, offset=999999999, items=[], next_cursor=store._cursor(context, 999999999), complete=False)

    def fragments(value):
        text = canonical(value)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return [{"record_fragment": {"offset": i, "total_characters": len(text), "sha256": sha, "text": text[i:i + _CHUNK]}}
                for i in range(0, len(text), _CHUNK)]

    def expand(part, rows):
        result = []
        for row in rows:
            if _fits(dict(probe, items=[{"part": part, "records": [row]}])):
                result.append((part, row))
            else:
                # Part names can be arbitrary Unicode source keys: serialize
                # the enclosing part as well if its name itself is oversized.
                for fragment in fragments({"part": part, "record": row}):
                    result.append(("fragment", fragment))
        return result

    if section == "configuration_evidence":
        if not record_ref:
            raise ValueError("configuration_evidence requires a revision-bound record_ref from the configuration manifest.")
        expanded = expand("evidence", [snap.evidence_record(record_ref)])
    else:
        if record_ref:
            raise ValueError("record_ref is supported only for configuration_evidence.")
        manifest = deepcopy(snap.manifest)
        manifest["continuation"]["arguments"].update(detail=detail, response_format=response_format, limit=limit)
        if detail == "summary":
            manifest["full_configuration_request"] = deepcopy(snap.manifest["continuation"]["arguments"])
        manifest_page = dict(base, manifest=manifest, offset=0, items=[],
                             next_cursor=store._cursor(context, 0), complete=False)
        full_manifest = _fits(manifest_page)
        expanded = [] if full_manifest else expand("manifest", [manifest])
        if detail == "full":
            for part, rows in snap.groups.items():
                expanded.extend(expand(part, rows))
        if not cursor and full_manifest:
            return dict(manifest_page, complete=not expanded,
                        next_cursor=store._cursor(context, 0) if expanded else None)

    offset = store._offset(cursor, context, len(expanded))
    answer = dict(base, offset=offset, items=[], next_cursor=None, complete=True)
    for end in range(offset, min(offset + limit, len(expanded))):
        part, row = expanded[end]
        items = deepcopy(answer["items"])
        if items and items[-1]["part"] == part:
            items[-1]["records"].append(deepcopy(row))
        else:
            items.append({"part": part, "records": [deepcopy(row)]})
        candidate = dict(answer, items=items, complete=end + 1 == len(expanded),
                         next_cursor=store._cursor(context, end + 1) if end + 1 < len(expanded) else None)
        if not _fits(candidate):
            if not answer["items"]:
                raise ValueError("Configuration fragment exceeds the response budget.")
            break
        answer = candidate
    return answer
