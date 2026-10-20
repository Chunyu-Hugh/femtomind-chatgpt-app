"""Read-only, manifest-addressed numerical reference results.

The gateway needs only this stdlib module and small index manifests. Numerical
files stay on HPC storage. The same module is sent over the established owner
SSH connection; it never runs FitPack or recomputes scientific statistics.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat

MAX_RESPONSE_BYTES = 10 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
REV = re.compile(r"[0-9a-f]{16}\Z")


def encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_read(raw):
    def reject(value):
        raise ValueError("Nonfinite JSON number: " + value)
    return json.loads(raw, parse_constant=reject)


def regular(path, root):
    """Reject links, junctions and traversal, including intermediate components."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if ".." in path.parts or (path != root and root not in path.parents):
        raise ValueError("Results path outside registered root")
    for p in [path] + list(path.parents):
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Results links and junctions are forbidden")
        if p == path and not stat.S_ISREG(info.st_mode):
            raise ValueError("Results file must be regular")
    return path


def results_root(app_dir):
    for root in (Path(app_dir) / "reference_results", Path(app_dir).parent / "reference_results"):
        if root.exists() or root.is_symlink():
            return root
    raise ValueError("Reference results manifests are unavailable")


def validate_manifest(m, folder):
    if not isinstance(m, dict) or m.get("schema") != "femtomind.reference_results.index.1":
        raise ValueError("Unsupported results index")
    if not ID.fullmatch(folder) or not ID.fullmatch(m.get("analysis_id", "")):
        raise ValueError("Invalid registered analysis ID")
    if not REV.fullmatch(m.get("archive_id", "")) or not HASH.fullmatch(m.get("archive_manifest_sha256", "")):
        raise ValueError("Invalid archive revision")
    if not re.fullmatch(r"catalog:[A-Za-z0-9._-]+@[0-9a-f]{64}", m.get("catalog_ref", "")):
        raise ValueError("Invalid scientific catalog reference")
    if m["catalog_ref"].split("@", 1)[0] != "catalog:" + m["analysis_id"]:
        raise ValueError("Scientific catalog belongs to another analysis")
    if any(type(m.get(k)) is not int or m[k] <= 0 for k in ("replica_count", "datasets", "accepted_points")):
        raise ValueError("Invalid registered result counts")
    path = PurePosixPath(m.get("server_directory", ""))
    suffix = ("reference_results", folder, "data", m["archive_id"])
    if (not path.is_absolute() or str(path) != m["server_directory"] or ".." in path.parts
            or tuple(path.parts[-4:]) != suffix or m.get("relative_directory") != "data/" + m["archive_id"]):
        raise ValueError("Invalid registered archive path")
    return m


class Results:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.entries, self.files = {}, {}
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            path = folder / "reference_manifest.json"
            regular(path, self.root)
            if path.stat().st_size > 64 * 1024:
                raise ValueError("Oversized results index")
            m = validate_manifest(json_read(path.read_bytes()), folder.name)
            if m["analysis_id"] in self.entries:
                raise ValueError("Duplicate results analysis ID")
            self.entries[m["analysis_id"]] = m
            self.files["reference_results/" + folder.name + "/reference_manifest.json"] = path

    def deployment_files(self):
        if not self.files:
            raise ValueError("No registered reference-results manifests to deploy")
        return dict(self.files)

    def resolve(self, analysis_id):
        if not isinstance(analysis_id, str) or len(analysis_id) > 300:
            raise ValueError("Invalid analysis ID")
        key = analysis_id
        if analysis_id.startswith("results:"):
            key, sep, revision = analysis_id[8:].partition("@")
            if not sep or key not in self.entries or revision != self.entries[key]["archive_manifest_sha256"]:
                raise ValueError("Invalid or stale results_ref; rediscover results")
        if key not in self.entries:
            raise ValueError("Unknown results analysis ID")
        return self.entries[key]

    def list(self, cursor="", limit=10):
        entries = [{"analysis_id": m["analysis_id"], "results_ref": reference(m),
                    "archive_revision": m["archive_manifest_sha256"], "catalog_ref": m["catalog_ref"],
                    "replicas": m["replica_count"], "datasets": m["datasets"],
                    "accepted_points": m["accepted_points"],
                    "available": ["fit_statistics", "averaged_theory", "experimental_points"],
                    "access": "Numerical retrieval requires the authorized JLab connection."}
                   for m in self.entries.values()]
        return page({"view": "analysis_results", "revision": digest(encoded(entries).encode())}, entries, cursor, limit)


def reference(m):
    return "results:" + m["analysis_id"] + "@" + m["archive_manifest_sha256"]


def page(base, records, cursor="", limit=10):
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ValueError("Invalid results cursor")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    context = digest(encoded(base).encode())
    offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                raise ValueError()
            value = json_read(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            offset = value["offset"]
            if value["context"] != context or type(offset) is not int or not 0 < offset < len(records):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid or stale results cursor for these selectors") from None
    result = dict(base, items=[], offset=offset, total_items=len(records), complete=True, next_cursor=None)
    for record in records[offset:offset + limit]:
        end = offset + len(result["items"]) + 1
        next_cursor = (base64.urlsafe_b64encode(encoded({"context": context, "offset": end}).encode()).decode().rstrip("=")
                       if end < len(records) else None)
        candidate = dict(result, items=result["items"] + [record], complete=next_cursor is None, next_cursor=next_cursor)
        wrapper = {"content": [{"type": "text", "text": encoded(candidate)}], "structuredContent": candidate, "isError": False}
        if len(encoded(wrapper).encode()) + 384 > MAX_RESPONSE_BYTES:
            if not result["items"]:
                raise ValueError("One result record exceeds the response limit; request fewer fields")
            break
        result = candidate
    return result


def validate_request(operation, args):
    allowed = {"statistics": {"analysis_id", "scope", "reaction", "dataset_id", "cursor", "limit"},
               "predictions": {"analysis_id", "reaction", "dataset_id", "detail", "fields", "cursor", "limit"}}[operation]
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError("Unsupported results arguments")
    if "analysis_id" not in args:
        raise ValueError("analysis_id is required")
    cursor = args.get("cursor", "")
    if not isinstance(cursor, str) or len(cursor) > 512 or (cursor and not re.fullmatch(r"[A-Za-z0-9_-]+", cursor)):
        raise ValueError("Invalid results cursor")
    for key in ("reaction", "dataset_id"):
        val = args.get(key, "")
        if not isinstance(val, str) or (val and not (ID if key == "reaction" else re.compile(r"[0-9]{1,10}\Z")).fullmatch(val)):
            raise ValueError("Invalid " + key)
    if args.get("dataset_id") and not args.get("reaction"):
        raise ValueError("dataset_id requires its reaction")
    if operation == "statistics" and args.get("scope", "datasets") not in ("overall", "reactions", "datasets"):
        raise ValueError("Invalid statistics scope")
    if operation == "statistics" and args.get("scope") == "overall" and (args.get("reaction") or args.get("dataset_id")):
        raise ValueError("Overall statistics do not accept dataset selectors")
    if operation == "predictions":
        if not args.get("reaction") or not args.get("dataset_id"):
            raise ValueError("Predictions require reaction and dataset_id")
        if args.get("detail", "summary") not in ("summary", "points"):
            raise ValueError("Invalid predictions detail")
        fields = args.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 12 or any(not isinstance(f, str) or len(f) > 128 for f in fields) or len(set(fields)) != len(fields):
            raise ValueError("fields must contain at most 12 distinct column names")
        if fields and args.get("detail", "summary") != "points":
            raise ValueError("Numerical fields require detail=points")
    if type(args.get("limit", 10)) is not int or not 1 <= args.get("limit", 10) <= 50:
        raise ValueError("Invalid results limit")


class Archive:
    """HPC-side checksum-verified selective reader; no pickle/replica evaluation."""
    def __init__(self, manifest):
        self.m = manifest
        self.root = Path(manifest["server_directory"])
        path = regular(self.root / "manifest.json", self.root)
        if path.stat().st_size > 256 * 1024:
            raise ValueError("Oversized archive manifest")
        raw = path.read_bytes()
        if digest(raw) != manifest["archive_manifest_sha256"]:
            raise ValueError("Archive manifest checksum mismatch")
        self.inventory = json_read(raw)
        for k in ("archive_id", "catalog_ref"):
            if self.inventory[k] != manifest[k]:
                raise ValueError("Archive revision or scientific configuration mismatch")

    def read(self, rel):
        # rel is constructed internally from verified dataset records, never a tool path.
        item = self.inventory["files"][rel]
        if not HASH.fullmatch(item.get("sha256", "")) or type(item.get("bytes")) is not int:
            raise ValueError("Invalid archive member inventory")
        path = regular(self.root / rel, self.root)
        if path.stat().st_size != item["bytes"] or item["bytes"] > 4 * 1024 * 1024:
            raise ValueError("Unexpected archive member size")
        raw = path.read_bytes()
        if digest(raw) != item["sha256"]:
            raise ValueError("Archive member checksum mismatch")
        return raw

    def summary(self):
        s = json_read(self.read("summary.json"))
        for k in ("analysis_id", "archive_id", "catalog_ref"):
            if s[k] != self.m[k]:
                raise ValueError("Results summary revision mismatch")
        if len(s["datasets"]) != self.m["datasets"] or s["replica_count"] != self.m["replica_count"]:
            raise ValueError("Results summary count mismatch")
        return s

    def retrieve(self, operation, args):
        validate_request(operation, args)
        s = self.summary()
        reaction, dataset = args.get("reaction", ""), args.get("dataset_id", "")
        if reaction and reaction not in s["reactions"]:
            raise ValueError("Unknown archived reaction")
        selected = [d for d in s["datasets"] if (not reaction or d["reaction"] == reaction) and (not dataset or d["dataset_id"] == dataset)]
        if dataset and not selected:
            raise ValueError("Unknown archived reaction-qualified dataset")
        base = {"analysis_id": self.m["analysis_id"], "results_ref": reference(self.m),
                "catalog_ref": self.m["catalog_ref"], "view": operation,
                "reaction": reaction, "dataset_id": dataset}
        if operation == "statistics":
            scope = args.get("scope", "datasets")
            base["scope"] = scope
            base["definitions"] = {"chi2_data": "Total experimental-point chi-square from saved ensemble-mean predictions.",
                                   "chi2_objective_like_at_ensemble_means": "Saved data + correlated + unique normalization contributions; not a new fit or mean replica objective."}
            if scope == "overall":
                records = [s["overall"]]
            elif scope == "reactions":
                if dataset:
                    raise ValueError("Reaction summaries do not accept dataset_id")
                records = [dict(v, reaction=k) for k, v in s["reactions"].items() if not reaction or k == reaction]
            else:
                # Keep exact precomputed values, including explicit null reasons.
                keys = ("reaction", "dataset_id", "experiment", "observable", "accepted_points", "original_points", "n_cor",
                        "chi2_data", "chi2_red", "z_score", "z_score_missing_reason", "chi2_cor", "chi2_cor_missing_reason",
                        "normalization", "normalization_root_id", "chi2_objective_like_at_ensemble_means")
                records = [{k: d[k] for k in keys if k in d} for d in selected]
            return page(base, records, args.get("cursor", ""), args.get("limit", 10))
        return self.predictions(base, selected[0], args)

    def predictions(self, base, dataset, args):
        import numpy as np
        stem = dataset["reaction"] + "_" + dataset["dataset_id"] + ".npz"
        with np.load(io.BytesIO(self.read("means/" + stem)), allow_pickle=False) as f:
            means = {k: f[k] for k in f.files}
        with np.load(io.BytesIO(self.read("datasets/" + stem)), allow_pickle=False) as f:
            exp = {k: f[k] for k in f.files}
        n = dataset["accepted_points"]
        if any(len(means[k]) != n for k in ("thy", "value", "alpha", "source_row")):
            raise ValueError("Archived point alignment mismatch")
        if not np.array_equal(means["value"], exp["value"]) or not np.array_equal(means["alpha"], exp["alpha"]):
            raise ValueError("Archived experimental values or uncertainties mismatch")
        if not np.array_equal(means["source_row"], exp["archive_source_row"]):
            raise ValueError("Archived experimental point ordering mismatch")
        columns = {"theory": means["thy"], "value": means["value"], "alpha": means["alpha"],
                   "source_row": means["source_row"], "prediction_std": means["prediction_std"],
                   "shift": means["shift"], "shift_std": means["shift_std"], "point_residual": means["point_residual"]}
        json_columns = json.loads(str(exp.get("__archive_json_columns__", "[]")))
        for k, a in exp.items():
            if a.ndim == 1 and len(a) == n and k != "correlated_column_names":
                columns["exp." + k] = np.asarray([json.loads(x) for x in a]) if k in json_columns else a
        base = dict(base, detail=args.get("detail", "summary"), fields=args.get("fields", []),
                    points=n, replica_count=self.m["replica_count"],
                    theory_definition="Saved mean fitted prediction; normalization and profiled shifts already included. Do not apply again.",
                    uncertainty_definition="alpha is the saved experimental uncertainty; prediction_std is replica population standard deviation, not a credible interval.")
        if base["detail"] == "summary":
            records = [{"experiment": dataset["experiment"], "observable": dataset["observable"],
                        "default_point_fields": ["source_row", "theory", "value", "alpha"],
                        "ordering": "Original accepted experimental point order"}]
            fields = sorted(columns)
            records.extend({"available_fields": fields[i:i + 24]} for i in range(0, len(fields), 24))
        else:
            fields = args.get("fields") or ["source_row", "theory", "value", "alpha"]
            if set(fields) - columns.keys():
                raise ValueError("Unknown prediction field")
            def scalar(x):
                x = x.item() if isinstance(x, np.generic) else x
                return None if isinstance(x, float) and not math.isfinite(x) else x
            records = [{"point_index": i, **{k: scalar(columns[k][i]) for k in fields}} for i in range(n)]
            base["missing_values"] = "null preserves missing/nonfinite metadata; scientific prediction/value/alpha must be finite"
            for k in ("theory", "value", "alpha"):
                if not np.isfinite(columns[k]).all():
                    raise ValueError("Nonfinite archived scientific array")
        return page(base, records, args.get("cursor", ""), args.get("limit", 10))


def remote_request(request):
    """Entry point in the authorized HPC Python process."""
    try:
        manifest = request["manifest"]
        validate_manifest(manifest, PurePosixPath(manifest["server_directory"]).parts[-3])
        result = Archive(manifest).retrieve(request["operation"], request["arguments"])
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": "Reference results unavailable or invalid: " + str(exc)}
