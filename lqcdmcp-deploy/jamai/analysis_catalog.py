"""Read-only views of the approved, repository-owned analysis catalog.

Only the three named catalog documents are read. Source paths in those documents
are evidence, never filesystem instructions. All public views use bounded pages;
the document views additionally preserve the exact UTF-8 text and its checksum.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import re
import stat
from threading import Lock
from pathlib import Path


DOCUMENTS = {
    "analysis_config": "analysis_config.json",
    "paper_findings": "paper_findings.md",
    "unresolved_settings": "unresolved_settings.md",
}
SECTIONS = (
    "overview", "parametrization", "parameters", "datasets", "qcd",
    "statistics", "provenance", "analysis_config", "paper_findings",
    "unresolved_settings", "documents",
    "configuration", "configuration_evidence",
)
MAX_RESPONSE_BYTES = 10 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CHUNK = 1100
# Entries are addressed by the freshly validated document hash and fixed root.
# Keep only expanded views, never skip validation/read/hash on a new request.
_EXPANSIONS = OrderedDict()
_EXPANSIONS_LOCK = Lock()
_MAX_CACHED_VIEWS = 8


def catalog_root(app_dir):
    """Locate the deployed-adjacent or repository analyses collection."""
    app_dir = Path(app_dir)
    for candidate in (app_dir / "analyses", app_dir.parent / "analyses"):
        if candidate.exists() or candidate.is_symlink():
            return candidate
    raise ValueError("Analysis catalog is unavailable on this server.")


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value):
    raise ValueError("Nonfinite catalog numbers are not permitted.")


def _pointer(*parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _safe(path, root, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Catalog links and junctions are not permitted.")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("Catalog documents must be regular files in regular directories.")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or (resolved != root and root not in resolved.parents):
        raise ValueError("Catalog entry is outside the approved catalog directory.")


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50.")


def _validate_text(value, name, maximum=128):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(name + " must be a bounded string.")


def tool_result(page):
    """Use one formatter for catalog page sizing and successful MCP replies."""
    result = {"content": [{"type": "text", "text": _json(page)}], "isError": False}
    if page.get("response_format", "legacy") != "json_text":
        result["structuredContent"] = page
    return result


def _fits(page):
    # Include the selected representation, outer escaping and JSON-RPC headroom.
    return len(json.dumps(tool_result(page), ensure_ascii=True).encode("utf-8")) + 384 <= MAX_RESPONSE_BYTES


class Catalog:
    """A fixed server-selected root. Never accept this path as an MCP argument."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.entries = {}
        self.aliases = {}
        self.files = {}
        try:
            _safe(self.root, self.root, directory=True)
            for directory in sorted(self.root.iterdir()):
                info = directory.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Catalog links and junctions are not permitted.")
                if not directory.is_dir():
                    continue
                _safe(directory, self.root, directory=True)
                if not _ID.fullmatch(directory.name):
                    raise ValueError("Invalid catalog directory identifier.")
                catalog = directory / "catalog"
                _safe(catalog, self.root, directory=True)
                documents = {}
                for key, filename in DOCUMENTS.items():
                    path = catalog / filename
                    _safe(path, self.root)
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                    documents[key] = {"text": text, "sha256": _digest(raw), "bytes": len(raw)}
                    self.files["analyses/" + directory.name + "/catalog/" + filename] = path
                config = json.loads(documents["analysis_config"]["text"],
                                    parse_constant=_reject_constant)
                self._validate(config)
                analysis_id = config["analysis_id"]
                if analysis_id in self.entries:
                    raise ValueError("Duplicate catalog analysis identifier.")
                self.entries[analysis_id] = {"config": config, "documents": documents,
                    "folder": directory.name,
                    "hash": _digest(_json({"folder": directory.name,
                        "documents": {k: d["sha256"] for k, d in documents.items()}}).encode())}
                for alias in {analysis_id, directory.name}:
                    if alias in self.aliases and self.aliases[alias] != analysis_id:
                        raise ValueError("Ambiguous catalog analysis identifier.")
                    self.aliases[alias] = analysis_id
                defaults = catalog / 'campaign_defaults.json'
                if defaults.exists():
                    _safe(defaults, self.root)
                    raw = defaults.read_bytes()
                    profile = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
                    from .campaign_profiles import project
                    current = project(config, profile)
                    self._validate(current)
                    current_id = current['analysis_id']
                    if current_id in self.entries or not _ID.fullmatch(current_id):
                        raise ValueError('Invalid or duplicate current campaign identifier')
                    derived = deepcopy(documents)
                    text = json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
                    derived['analysis_config'] = {'text': text, 'sha256': _digest(text.encode()), 'bytes': len(text.encode())}
                    self.files['analyses/' + directory.name + '/catalog/campaign_defaults.json'] = defaults
                    self.entries[analysis_id].update(aliases=[], recommended_for_new_campaigns=False)
                    self.entries[current_id] = {'config': current, 'documents': derived, 'folder': directory.name,
                        'aliases': [directory.name], 'recommended_for_new_campaigns': True,
                        'hash': _digest(_json({'historical_catalog_hash': self.entries[analysis_id]['hash'],
                                             'projected_analysis_config_sha256': derived['analysis_config']['sha256'],
                                             'campaign_defaults_sha256': _digest(raw)}).encode())}
                    self.aliases[directory.name] = current_id
                    self.aliases[current_id] = current_id
        except ValueError:
            raise
        except (OSError, UnicodeError, TypeError, AttributeError, KeyError):
            raise ValueError("Analysis catalog is unavailable or invalid.") from None
        self.hash = _digest(_json({key: value["hash"] for key, value in self.entries.items()}).encode())

    @staticmethod
    def _validate(config):
        if not isinstance(config, dict):
            raise ValueError("Catalog configuration must be a JSON object.")
        schema = config.get("catalog_schema")
        if (not isinstance(schema, dict) or schema.get("name") != "femtomind.paper_analysis_catalog"
                or not isinstance(schema.get("version"), str)
                or not re.fullmatch(r"0\.\d+(?:\.\d+)?", schema["version"])):
            raise ValueError("Unsupported catalog schema.")
        analysis_id = config.get("analysis_id")
        if not isinstance(analysis_id, str) or not _ID.fullmatch(analysis_id):
            raise ValueError("Invalid catalog analysis identifier.")
        for name in ("publication", "scope", "catalog_validation"):
            if not isinstance(config.get(name), dict):
                raise ValueError("Required catalog metadata is missing.")
        publication = config["publication"]
        if not isinstance(publication.get("title"), str) or not publication["title"].strip():
            raise ValueError("Catalog publication title is missing.")
        if not any(isinstance(publication.get(k), str) and publication[k].strip()
                   for k in ("arxiv_id", "doi", "reference", "url")):
            raise ValueError("Catalog publication reference is missing.")
        validation = config["catalog_validation"]
        if not isinstance(validation.get("status"), str) or type(validation.get("reproduction_ready")) is not bool:
            raise ValueError("Catalog validation status is missing.")
        for key in ("scientific_settings", "unresolved_settings"):
            if key in config and (not isinstance(config[key], list)
                                  or any(not isinstance(item, dict) for item in config[key])):
                raise ValueError("Invalid catalog scientific record collection.")
        for key in ("source_verification", "reproduction_configuration", "sources", "historical_section_notice"):
            if key in config and not isinstance(config[key], dict):
                raise ValueError("Invalid catalog scientific metadata object.")
        source_verification = config.get("source_verification", {})
        for key in ("parameter_definitions", "datasets", "parameter_counts", "ff_link_verification",
                    "gaussian_sampling_audit", "normalization_counts", "named_input_constants",
                    "latest_author_corrections", "pdf_native_basis_mapping"):
            if key in source_verification and not isinstance(source_verification[key], dict):
                raise ValueError("Invalid catalog scientific mapping.")
        for records in source_verification.get("parameter_definitions", {}).values():
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise ValueError("Invalid catalog parameter definition collection.")
        for reaction in source_verification.get("datasets", {}).values():
            if (not isinstance(reaction, dict) or not isinstance(reaction.get("entries"), list)
                    or any(not isinstance(item, dict) for item in reaction["entries"])):
                raise ValueError("Invalid catalog dataset collection.")
        for key in ("global_input_settings", "resolution_register", "current_native_findings"):
            if key in source_verification and (not isinstance(source_verification[key], list)
                    or any(not isinstance(item, dict) for item in source_verification[key])):
                raise ValueError("Invalid catalog source verification record collection.")
        unknowns = source_verification.get("remaining_unknowns", [])
        blockers = validation.get("blocking_unresolved_items", [])
        if any(not isinstance(values, list) or any(not isinstance(item, str) for item in values)
               for values in (unknowns, blockers)):
            raise ValueError("Invalid catalog unresolved item collection.")
        resolutions = {}
        for item in source_verification.get("resolution_register", []):
            if (not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("status"), str) or not item["status"]
                    or not isinstance(item.get("evidence_sources"), list)
                    or any(not isinstance(source, str) for source in item["evidence_sources"])
                    or not isinstance(item.get("resolution_and_remaining_gap"), str)):
                raise ValueError("Invalid catalog current resolution record.")
            if item["id"] in resolutions:
                raise ValueError("Duplicate catalog current resolution identifier.")
            resolutions[item["id"]] = item
        if any(item not in resolutions or resolutions[item]["status"].startswith("resolved") for item in blockers):
            raise ValueError("Catalog blockers must have unresolved current resolution records.")
        for item in config.get("unresolved_settings", []):
            if not isinstance(item.get("id"), str):
                raise ValueError("Invalid catalog historical unresolved identifier.")
        for item in config.get("scientific_settings", []):
            if any(key in item and not isinstance(item[key], str) for key in ("id", "category")):
                raise ValueError("Invalid catalog scientific setting identifier or category.")
        audit = source_verification.get("gaussian_sampling_audit", {})
        findings = audit.get("findings", [])
        if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
            raise ValueError("Invalid catalog Gaussian audit record collection.")
        for finding in findings:
            changes = finding.get("future_changes_if_authorized", [])
            if not isinstance(changes, list) or any(not isinstance(item, str) for item in changes):
                raise ValueError("Invalid catalog conditional application follow-up collection.")

    def deployment_files(self):
        """Return only validated approved documents, keyed by deployment paths."""
        if not self.files:
            raise ValueError("Deployment requires at least one validated analysis catalog entry.")
        return dict(self.files)

    def _entry(self, analysis_id):
        _validate_text(analysis_id, "analysis_id", 256)
        if analysis_id.startswith("catalog:"):
            identity, separator, revision = analysis_id[8:].partition("@")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ValueError("Invalid catalog reference.")
            entry = self._entry(identity)
            if entry["hash"] != revision:
                raise ValueError("Stale catalog reference; discover the current revision again.")
            return entry
        if not _ID.fullmatch(analysis_id) or analysis_id not in self.aliases:
            raise ValueError("Unknown analysis identifier; use list_analysis_catalog.")
        return self.entries[self.aliases[analysis_id]]

    @staticmethod
    def _reference(entry):
        return "catalog:" + entry["config"]["analysis_id"] + "@" + entry["hash"]

    @staticmethod
    def _record(pointer, value, layer, object_key=""):
        record = {"json_pointer": pointer, "layer": layer, "record": value}
        if object_key:
            record["object_key"] = object_key
        return record

    def _base(self, entry, section, object_key):
        c = entry["config"]
        return {"analysis_id": c["analysis_id"], "section": section, "object_key": object_key,
                "catalog_ref": self._reference(entry),
                "catalog_hash": entry["hash"], "schema_version": c["catalog_schema"]["version"],
                "reproduction_ready": c["catalog_validation"]["reproduction_ready"],
                "source_context": {"document": "analysis_config", "sources_pointer": "/sources",
                                   "layer_policy_pointer": "/catalog_schema/layer_policy"}}

    @staticmethod
    def _cursor(context, offset):
        return base64.urlsafe_b64encode(_json({"v": 1, "context": context, "offset": offset}).encode()).decode().rstrip("=")

    @staticmethod
    def _offset(cursor, context, total):
        _validate_text(cursor, "cursor", 1024)
        if not cursor:
            return 0
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                raise ValueError()
            decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            value = json.loads(decoded)
            if (not isinstance(value, dict) or set(value) != {"v", "context", "offset"}
                    or type(value["v"]) is not int or value["v"] != 1 or value["context"] != context
                    or type(value["offset"]) is not int or not 0 <= value["offset"] < total):
                raise ValueError()
            return value["offset"]
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise ValueError("Invalid or stale cursor for this catalog, section, filter and response format.") from None

    def _page(self, base, records, cursor, limit):
        _validate_limit(limit)
        context = _digest(_json(base).encode())
        # Expand an oversized atomic record into a lossless serialized record.
        # Ordinary records stay intact, including their original nested values.
        cache_key = (str(self.root), context)
        with _EXPANSIONS_LOCK:
            expanded = _EXPANSIONS.get(cache_key)
            if expanded is not None:
                _EXPANSIONS.move_to_end(cache_key)
        if expanded is None:
            expanded = tuple(self._expand(base, records, context))
            with _EXPANSIONS_LOCK:
                _EXPANSIONS[cache_key] = expanded
                _EXPANSIONS.move_to_end(cache_key)
                while len(_EXPANSIONS) > _MAX_CACHED_VIEWS:
                    _EXPANSIONS.popitem(last=False)
        offset = self._offset(cursor, context, len(expanded))
        page = dict(base, items=[], total_items=len(expanded), offset=offset, next_cursor=None, complete=True)
        end = offset
        for record in expanded[offset:offset + limit]:
            record = deepcopy(record)
            candidate_end = end + 1
            candidate = dict(page, items=page["items"] + [record], complete=candidate_end == len(expanded),
                             next_cursor=self._cursor(context, candidate_end) if candidate_end < len(expanded) else None)
            if not _fits(candidate):
                if not page["items"]:
                    raise ValueError("Catalog record metadata exceeds the response budget.")
                break
            page = candidate
            end = candidate_end
        return page

    def _expand(self, base, records, context):
        expanded = []
        probe = dict(base, items=[], total_items=999999999, offset=999999999,
                     next_cursor=self._cursor(context, 999999999), complete=False)
        for record in records:
            if _fits(dict(probe, items=[record])):
                expanded.append(record)
                continue
            serialized = _json(record)
            for start in range(0, len(serialized), _CHUNK):
                expanded.append({"json_pointer": record.get("json_pointer", ""),
                    "layer": record.get("layer", "catalog_metadata"), "serialization": "json",
                    "fragment_offset": start, "fragment_total_chars": len(serialized),
                    "fragment_sha256": _digest(serialized.encode()),
                    "text": serialized[start:start + _CHUNK]})
        return expanded

    @staticmethod
    def _summary(entry):
        c = entry["config"]
        targets = next((s.get("value", {}).get("simultaneous_fit", []) for s in c.get("scientific_settings", [])
                        if s.get("id") == "target.final" and isinstance(s.get("value"), dict)), [])
        return {"analysis_id": c["analysis_id"], "aliases": entry.get('aliases', [entry["folder"]]),
                **({'recommended_for_new_campaigns': entry['recommended_for_new_campaigns']} if 'recommended_for_new_campaigns' in entry else {}),
                "catalog_ref": Catalog._reference(entry),
                "publication": {k: c["publication"][k] for k in ("title", "arxiv_id", "version", "doi", "reference", "url") if k in c["publication"]},
                "description": c["scope"].get("target", c["publication"]["title"]),
                "scientific_objects": targets or list(c.get("source_verification", {}).get("parameter_definitions", {})),
                "validation_status": c["catalog_validation"]["status"],
                "reproduction_ready": c["catalog_validation"]["reproduction_ready"],
                "schema_version": c["catalog_schema"]["version"], "catalog_hash": entry["hash"]}

    def list(self, cursor="", limit=10):
        base = {"section": "list", "catalog_hash": self.hash, "schema_version": "catalog-discovery-2",
                "source_context": "Each item describes validated approved catalog documents."}
        entries = sorted(self.entries.values(), key=lambda e: not e.get('recommended_for_new_campaigns', False))
        return self._page(base, [self._summary(e) for e in entries], cursor, limit)

    def get(self, analysis_id, section="overview", object_key="", cursor="", limit=10,
            detail="summary", group="", status="", dataset_id="", setting_id="",
            response_format="legacy", record_ref=""):
        _validate_text(section, "section")
        _validate_text(object_key, "object_key")
        _validate_text(response_format, "response_format")
        if response_format not in ("legacy", "json_text"):
            raise ValueError("response_format must be legacy or json_text.")
        for key, value in (("detail", detail), ("group", group), ("status", status),
                           ("dataset_id", dataset_id), ("setting_id", setting_id)):
            _validate_text(value, key)
        if detail not in ("summary", "full"):
            raise ValueError("detail must be summary or full.")
        if (group or status) and section != "parameters":
            raise ValueError("group and status filter only parameters.")
        if status and status not in ("free", "fixed", "linked"):
            raise ValueError("status must be free, fixed or linked.")
        if dataset_id and (section != "datasets" or not object_key):
            raise ValueError("dataset_id requires datasets and a reaction object_key.")
        if setting_id and section not in ("qcd", "statistics", "parametrization"):
            raise ValueError("setting_id filters qcd, statistics or parametrization.")
        if section not in SECTIONS:
            raise ValueError("Unsupported catalog section; supported sections: " + ", ".join(SECTIONS))
        if section in ("configuration", "configuration_evidence"):
            from . import configuration_snapshot
            if object_key or group or status or dataset_id or setting_id:
                raise ValueError("Configuration streams do not accept legacy record selectors; use configuration_evidence for targeted originals.")
            _validate_text(analysis_id, "analysis_id", 256)
            pinned = analysis_id.startswith("configuration:")
            identity, separator, revision = analysis_id[14:].partition("@") if pinned else (analysis_id, "", "")
            if pinned and (not separator or not re.fullmatch(r"[0-9a-f]{64}", revision)):
                raise ValueError("Invalid configuration reference.")
            entry = self._entry(identity)
            snap = configuration_snapshot.snapshot(self.root, entry, self._reference(entry))
            if pinned and revision != snap.hash:
                raise ValueError("Stale configuration reference; discover the current configuration again.")
            if section == "configuration_evidence" and not pinned:
                raise ValueError("configuration_evidence requires configuration_ref as analysis_id.")
            return configuration_snapshot.page(self, snap, section, record_ref, cursor, limit, detail, response_format)
        if record_ref:
            raise ValueError("record_ref is supported only for configuration_evidence.")
        entry = self._entry(analysis_id)
        c, records = entry["config"], []
        sv = c.get("source_verification", {})
        base = self._base(entry, section, object_key)
        base.update(detail=detail, filters={k: v for k, v in
                    (("group", group), ("status", status), ("dataset_id", dataset_id), ("setting_id", setting_id)) if v})
        # Leave legacy page/cursor contexts unchanged. Opt-in pages bind their
        # format into both the continuation context and expanded-view cache key.
        if response_format != "legacy":
            base["response_format"] = response_format
        if section == "documents" and not object_key:
            records = [{"document": key, "section": key, "document_sha256": d["sha256"],
                        "document_bytes": d["bytes"], "document_total_chars": len(d["text"]),
                        "encoding": "utf-8"} for key, d in entry["documents"].items()]
            return self._page(base, records, cursor, limit)
        if section in DOCUMENTS or section == "documents":
            document = object_key if section == "documents" else section
            if document not in DOCUMENTS or (section != "documents" and object_key):
                raise ValueError("Document must be analysis_config, paper_findings or unresolved_settings.")
            d = entry["documents"][document]
            base["detail"] = "full"  # A document section is an explicit lossless export.
            base.update(document=document, document_sha256=d["sha256"], document_bytes=d["bytes"],
                        document_total_chars=len(d["text"]), encoding="utf-8",
                        document_notice="Exact saved text; historical unresolved questions are superseded by the current requirements view.")
            records = [{"char_offset": i, "text": d["text"][i:i + _CHUNK]}
                       for i in range(0, len(d["text"]), _CHUNK)]
            return self._page(base, records, cursor, limit)
        if object_key and section not in ("parameters", "parametrization", "datasets"):
            raise ValueError("object_key is supported only for parameters, parametrization, datasets and documents.")

        def add(path, value, layer="source_verified", obj=""):
            records.append(self._record(path, value, layer, obj))

        def scientific(categories):
            for i, setting in enumerate(c.get("scientific_settings", [])):
                if setting.get("category") in categories:
                    add(_pointer("scientific_settings", i), setting, "publication")

        if section == "overview":
            add("", self._summary(entry), "catalog_metadata")
            if detail == "full":
                for key in ("scope", "catalog_validation", "historical_section_notice"):
                    if key in c:
                        add(_pointer(key), c[key], "catalog_metadata")
            add("", {"available_sections": list(SECTIONS), "parameter_objects": list(sv.get("parameter_definitions", {})),
                      "dataset_reactions": list(sv.get("datasets", {})),
                      "parameter_counts": sv.get("parameter_counts", {}),
                      "blocking_item_ids": c["catalog_validation"].get("blocking_unresolved_items", []),
                      "retrieval": "Use targeted filters; detail=full requests definitions. Reuse catalog_ref. limit is 1-50 (default 10), subject to 10 KiB. Pass next_cursor as cursor until complete; keep catalog_ref, section, selectors, detail and response_format unchanged. response_format=json_text sends one JSON text payload. section=documents lists document sections."}, "retrieval_guide")
        elif section == "parameters":
            mapping = sv.get("parameter_definitions", {})
            statuses = {"fixed", "fixed_true"} if status == "fixed" else {status}
            if object_key and object_key not in mapping:
                raise ValueError("Unknown parameter object_key.")
            for obj, parameters in mapping.items():
                if not object_key or object_key == obj:
                    selected = [(i, p) for i, p in enumerate(parameters)
                                if (not group or p.get("name", "").split(" ")[0] == group)
                                and (not status or p.get("status") in statuses)]
                    if detail == "summary" and selected:
                        counts = {}
                        for _, parameter in selected:
                            state = parameter.get("status", "unspecified")
                            counts[state] = counts.get(state, 0) + 1
                        add(_pointer("source_verification", "parameter_definitions", obj),
                            {"definition_count": len(selected), "status_counts": counts,
                             "groups": list(dict.fromkeys(p.get("name", "").split(" ")[0] for _, p in selected))},
                            "official_input_summary", obj)
                        continue
                    for i, parameter in selected:
                        add(_pointer("source_verification", "parameter_definitions", obj, i), parameter, "official_input", obj)
            if (group or status) and not records:
                raise ValueError("No parameter group/status matches this selection.")
        elif section == "datasets":
            mapping = sv.get("datasets", {})
            if object_key and object_key not in mapping:
                raise ValueError("Unknown dataset reaction object_key.")
            for reaction, data in mapping.items():
                if not object_key or object_key == reaction:
                    selected = [(i, d) for i, d in enumerate(data.get("entries", []))
                                if not dataset_id or str(d.get("id")) == dataset_id]
                    if detail == "summary" and not dataset_id:
                        add(_pointer("source_verification", "datasets", reaction),
                            {"dataset_ids": [d["id"] for _, d in selected], "dataset_count": len(selected),
                             "accepted_points": sum(d.get("current_reader_verification", {}).get("retained_count", 0) for _, d in selected)},
                            "official_input_and_current_reader_summary", reaction)
                        continue
                    for i, dataset in selected:
                        value = dataset if detail == "full" else {k: dataset[k] for k in
                            ("id", "xlsx_path", "filters", "filters_key_present", "normalization", "reference", "evidence_origin") if k in dataset}
                        add(_pointer("source_verification", "datasets", reaction, "entries", i), value, "official_input_and_current_reader", reaction)
            if dataset_id and not records:
                raise ValueError("Unknown dataset_id for this reaction.")
        elif section == "parametrization":
            objects = sv.get("parameter_definitions", {})
            if object_key and object_key not in objects:
                raise ValueError("Unknown parametrization object_key.")
            scientific({"parametrization", "model_constraints"} | ({"pdf"} if object_key == "pdf" else {"ff"} if object_key else {"pdf", "ff"}))
            for key in ("pdf_native_basis_mapping", "parameter_counts", "ff_link_verification", "current_native_findings"):
                if detail == "summary" and not setting_id and key in ("ff_link_verification", "current_native_findings"):
                    continue
                if key in sv and not (object_key and object_key != "pdf" and key == "pdf_native_basis_mapping"):
                    value = sv[key]
                    if key == "parameter_counts" and object_key:
                        if object_key in value:
                            add(_pointer("source_verification", key, object_key), value[object_key], obj=object_key)
                    elif key == "ff_link_verification":
                        for part, value in value.items():
                            add(_pointer("source_verification", key, part), value)
                    else:
                        add(_pointer("source_verification", key), value)
        elif section == "qcd":
            for key, value in c.get("reproduction_configuration", {}).items():
                if key in ("Q20", "positivity", "numerical_authority", "do_not_change"):
                    add(_pointer("reproduction_configuration", key), value, "author_confirmed_reproduction")
            scientific({"theory", "model_constraints"})
            for key in ("global_input_settings", "named_input_constants", "latest_author_corrections"):
                if key in sv:
                    value = sv[key]
                    if isinstance(value, list):
                        for i, item in enumerate(value):
                            add(_pointer("source_verification", key, i), item, "official_input")
                    else:
                        add(_pointer("source_verification", key), value,
                            "author_confirmation" if key == "latest_author_corrections" else "source_verified")
        elif section == "statistics":
            for key in ("Gaussian_initialization", "gaussian_template_parameter_penalties", "limitation"):
                if key in c.get("reproduction_configuration", {}):
                    add(_pointer("reproduction_configuration", key), c["reproduction_configuration"][key], "author_confirmed_reproduction")
            scientific({"statistics", "calibration"})
            audit = sv.get("gaussian_sampling_audit", {})
            for i, item in enumerate(audit.get("findings", [])):
                add(_pointer("source_verification", "gaussian_sampling_audit", "findings", i), item, "current_implementation_audit")
            for key in ("normalization_counts", "parameter_counts"):
                if key in sv:
                    add(_pointer("source_verification", key), sv[key])
        elif section == "provenance":
            for key in ("catalog_schema", "publication", "scope", "historical_section_notice", "paper_only_baseline"):
                if key in c:
                    add(_pointer(key), c[key], "catalog_metadata")
            for source_id, source in c.get("sources", {}).items():
                add(_pointer("sources", source_id), source, "source_registry", source_id)
            for key in ("paper_input_discrepancies", "final_author_clarifications"):
                if key in sv:
                    add(_pointer("source_verification", key), sv[key])
        if setting_id:
            records = [r for r in records if setting_id in
                       (r["json_pointer"].rsplit("/", 1)[-1],
                        r["record"].get("id") if isinstance(r["record"], dict) else None,
                        r["record"].get("key") if isinstance(r["record"], dict) else None)]
            if not records:
                raise ValueError("Unknown setting_id for this section.")
        if detail == "summary" and section in ("qcd", "statistics"):
            if not setting_id:
                records = [r for r in records if r["json_pointer"].startswith("/reproduction_configuration/")
                           or (section == "qcd" and r["json_pointer"].startswith("/source_verification/global_input_settings/"))]
            for record in records:
                value = record["record"]
                if isinstance(value, dict):
                    if record["json_pointer"].endswith("/positivity"):
                        keep = ("fitpack_flags", "evidence_source")
                    elif record["json_pointer"].endswith("/Gaussian_initialization"):
                        keep = ("status", "source_id", "flat par", "parameter_key", "gaussian", "means_by_coefficient",
                                "standard_deviations_by_coefficient", "Gaussian_template_penalties")
                    else:
                        keep = ("id", "key", "value", "evidence_origin", "evidence_status", "reference", "references", "evidence_sources")
                    record["record"] = {k: value[k] for k in keep if k in value}
        if section == "statistics" and "Gaussian_initialization" in c.get("reproduction_configuration", {}):
            base["current_initialization_context"] = {
                "json_pointer": "/reproduction_configuration/Gaussian_initialization",
                "note": "The current author-confirmed initialization policy supersedes older audit scope-limit text. Audit records are preserved verbatim as historical evidence."}
        return self._page(base, records, cursor, limit)

    def requirements(self, analysis_id, cursor="", limit=10, detail="summary", requirement_id=""):
        _validate_text(detail, "detail")
        _validate_text(requirement_id, "requirement_id")
        if detail not in ("summary", "full"):
            raise ValueError("detail must be summary or full.")
        entry = self._entry(analysis_id)
        c = entry["config"]
        sv = c.get("source_verification", {})
        historical = {item.get("id"): (i, item) for i, item in enumerate(c.get("unresolved_settings", []))}
        blockers = set(c["catalog_validation"].get("blocking_unresolved_items", []))
        records = []
        for i, current in enumerate(sv.get("resolution_register", [])):
            if current.get("id") not in blockers or str(current.get("status", "")).startswith("resolved"):
                continue
            old_i, old = historical.get(current["id"], (None, {}))
            requirement = dict(current, priority=old.get("priority", "unspecified"),
                why_it_matters=old.get("why_it_matters_to_FEMTOMIND", "Not specified in the catalog."),
                classification="historical_information", reproduction_blocker=True,
                historical_context_pointer=_pointer("unresolved_settings", old_i) if old_i is not None else None)
            records.append(self._record(_pointer("source_verification", "resolution_register", i), requirement, "current_resolution_register"))
        active = [r["record"]["id"] for r in records]
        for i, unknown in enumerate(sv.get("remaining_unknowns", [])):
            records.append(self._record(_pointer("source_verification", "remaining_unknowns", i),
                {"remaining_unknown": unknown, "classification": "historical_information", "priority": None,
                 "priority_status": "not_assigned_in_catalog",
                 "reproduction_blocker": False, "note": "Historical provenance note; overlaps current blockers where applicable."}, "source_verified_remaining_unknowns"))
        for i, finding in enumerate(sv.get("gaussian_sampling_audit", {}).get("findings", [])):
            if finding.get("future_changes_if_authorized"):
                records.append(self._record(_pointer("source_verification", "gaussian_sampling_audit", "findings", i, "future_changes_if_authorized"),
                    {"classification": "conditional_followup", "reproduction_blocker": False,
                     "future_changes_if_authorized": finding["future_changes_if_authorized"],
                     "note": "Conditional application integration; saved metadata does not assert present capability."}, "application_integration_note"))
        base = self._base(entry, "requirements", "")
        if detail == "summary":
            records = [r for r in records if r["record"].get("reproduction_blocker")]
        if requirement_id:
            records = [r for r in records if r["record"].get("id") == requirement_id]
            if not records:
                raise ValueError("Unknown active requirement_id.")
        base.update(detail=detail, requirement_id=requirement_id, blocking_item_ids=active,
                    historical_register_notice="Historical unresolved_settings is not the active register; only current catalog blockers are active.")
        return self._page(base, records, cursor, limit)
