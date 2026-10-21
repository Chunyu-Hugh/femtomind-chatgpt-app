"""Isolated catalog-to-FitPack input transport; never a campaign operation.

No templates, campaign cards, readers, parameter initialization or fitting code
are imported. Public entry points always consume resolve_compiled().
"""
import ast
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from . import configuration_compiler as compiler
from . import configuration_snapshot as snapshot

SCHEMA = "femtomind.isolated_fitpack_input.v1"
OVERLAYS = "# FEMTOMIND AUTHOR-CONFIRMED REPRODUCTION OVERLAYS"


def _id(value):
    """Catalog canonical decimal IDs transport to native FitPack integer keys.

    Nondecimal IDs remain strings. Leading-zero spellings cannot be inferred
    safely and are rejected rather than silently normalized.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Catalog dataset/filter identities must be explicit strings")
    if re.fullmatch(r"[0-9]+", value):
        if str(int(value)) != value:
            raise ValueError("Ambiguous leading-zero dataset identity")
        return int(value)
    return value


def _filters(value):
    if isinstance(value, dict):
        return {_id(k): deepcopy(v) for k, v in value.items()}
    return deepcopy(value)


def _typed(value):
    # Unlike ordinary JSON, this comparison distinguishes int/string map keys,
    # bool/int values, negative zero, null and absent fields.
    if isinstance(value, dict):
        return ["dict", sorted([[_typed(k), _typed(v)] for k, v in value.items()], key=repr)]
    if isinstance(value, list):
        return ["list", [_typed(v) for v in value]]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite scientific number")
        return ["float", value.hex()]
    if type(value) not in (str, int, bool, type(None)):
        raise ValueError("Unsupported scientific literal")
    return [type(value).__name__, value]


def _expected(ir, reproduction):
    """Independent semantic projection used only by the parity checker."""
    source = ir["literal_catalog"]["source_verification"]
    rows = ir["reproduction"]["parameter_definitions"] if reproduction else source["parameter_definitions"]
    native = ir["reproduction"]["native_settings"] if reproduction else {r["key"]: r["value"] for r in source["global_input_settings"]}
    conf = deepcopy(native)
    if "params" in conf or "datasets" in conf:
        raise ValueError("Reserved native setting")
    conf["params"] = {obj: {r["name"]: deepcopy(r["definition"]) for r in entries} for obj, entries in rows.items()}
    conf["datasets"] = {}
    for reaction, block in source["datasets"].items():
        supported = {"entries", "declared_file_count", "raw_filter_configuration", "filters_key_present", "filter_source_references"}
        if set(block) - supported:
            raise ValueError("Unsupported dataset container fields: " + repr(sorted(set(block) - supported)))
        item = {"xlsx": {}, "norm": {}}
        if block["filters_key_present"]:
            item["filters"] = _filters(block["raw_filter_configuration"])
        elif block["raw_filter_configuration"] is not None:
            raise ValueError("Absent reaction filters have non-null declarations")
        for row in block["entries"]:
            supported_row = {"id", "xlsx_path", "normalization", "filters", "filters_key_present", "input_comment",
                             "reference", "evidence_origin", "evidence_status", "current_reader_verification"}
            if set(row) - supported_row:
                raise ValueError("Unsupported dataset declaration fields: " + repr(sorted(set(row) - supported_row)))
            key = _id(row["id"])
            item["xlsx"][key] = row["xlsx_path"]
            if row["normalization"] is not None:
                item["norm"][key] = deepcopy(row["normalization"]["definition"])
            filters = item.get("filters")
            present = "filters" in item and (not isinstance(filters, dict) or key in filters)
            effective = filters.get(key) if isinstance(filters, dict) else filters
            if present != row["filters_key_present"] or _typed(effective) != _typed(row["filters"]):
                raise ValueError("Reaction and dataset literal filters disagree: " + reaction + ":" + row["id"])
        conf["datasets"][reaction] = item
    return conf


def _parse(text):
    """Read only literal assignments, without exec/imports or evaluating calls."""
    lines = text.splitlines()
    if lines.count(OVERLAYS) != 1:
        raise ValueError("Exactly one literal/overlay boundary is required")
    boundary = lines.index(OVERLAYS) + 1
    namespace, literal = {}, None
    for node in ast.parse(text).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            raise ValueError("Input transport accepts only single literal assignments")
        if node.lineno > boundary and literal is None:
            literal = deepcopy(namespace)
        # Reject duplicate dictionary keys, including Python-equal bool/int keys.
        for child in ast.walk(node.value):
            if isinstance(child, ast.Dict):
                keys = [ast.literal_eval(k) for k in child.keys]
                if len(set(keys)) != len(keys):
                    raise ValueError("Duplicate literal dictionary key")
        value = ast.literal_eval(node.value)
        _typed(value)
        target = node.targets[0]
        if isinstance(target, ast.Name):
            if target.id in namespace:
                raise ValueError("Top-level declaration repeated")
            namespace[target.id] = value
            continue
        keys = []
        while isinstance(target, ast.Subscript):
            index = target.slice
            if isinstance(index, ast.Index):  # Python 3.8 compatibility
                index = index.value
            key = ast.literal_eval(index)
            if type(key) not in (str, int):
                raise ValueError("Unsupported assignment key")
            keys.insert(0, key)
            target = target.value
        if not isinstance(target, ast.Name) or target.id != "conf" or not keys:
            raise ValueError("Only conf subscript assignments are supported")
        dest = namespace["conf"]
        for key in keys[:-1]:
            dest = dest[key]
        dest[keys[-1]] = value
    if literal is None:
        literal = deepcopy(namespace)
    return literal, namespace


def _differences(expected, actual, path=""):
    if _typed(expected) == _typed(actual):
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        issues = []
        for key in sorted(set(expected) | set(actual), key=repr):
            child = path + "/" + str(key)
            if key not in expected or key not in actual:
                issues.append({"path": child, "reason": "field presence changed"})
            else:
                issues.extend(_differences(expected[key], actual[key], child))
        return issues or [{"path": path, "reason": "mapping key type changed"}]
    return [{"path": path, "reason": "literal value/type/order changed"}]


def check_generated_input(ir, text):
    """Independent AST parity of original and final phases against the full IR."""
    try:
        before, after = _parse(text)
        constants = deepcopy(ir["literal_catalog"]["source_verification"].get("named_input_constants", {}))
        expected_before = dict(constants, conf=_expected(ir, False))
        expected_after = dict(constants, conf=_expected(ir, True))
        issues = _differences(expected_before, before, "/literal") + _differences(expected_after, after, "/reproduction")
    except (ValueError, TypeError, KeyError, SyntaxError) as error:
        issues = [{"path": "/input", "reason": str(error)}]
    return {"passed": not issues, "comparison": "exact_typed_literal_assignments",
            "issues": [dict(i, severity="error", blocks_materialization=True) for i in issues],
            "reader_selection_verified": False, "campaign_execution_allowed": False}


def render_compiled_input(store, compiled_ref):
    """Resolve a pinned IR and return source text; no filesystem writes."""
    ir = compiler.resolve_compiled(store, compiled_ref)
    if ir["compiler_schema"] != compiler.SCHEMA:
        raise ValueError("Unsupported compiler projection")
    source = ir["literal_catalog"]["source_verification"]
    lines = ["# Isolated validation input; not an authorized campaign.",
             "# " + ir["configuration_ref"], "# " + compiled_ref, "conf = {}"]
    for name, value in source.get("named_input_constants", {}).items():
        if not isinstance(name, str) or not name.isidentifier() or name == "conf":
            raise ValueError("Unsupported named source constant")
        lines.append(name + " = " + repr(value))
    for row in source["global_input_settings"]:
        lines.append("conf[%r] = %r" % (row["key"], row["value"]))
    lines.append("conf['params'] = {}")
    for obj, rows in source["parameter_definitions"].items():
        lines.append("conf['params'][%r] = {}" % obj)
        for row in rows:
            lines.append("conf['params'][%r][%r] = %r" % (obj, row["name"], row["definition"]))
    lines.append("conf['datasets'] = {}")
    for reaction, block in source["datasets"].items():
        prefix = "conf['datasets'][%r]" % reaction
        lines.extend([prefix + " = {}", prefix + "['xlsx'] = {}", prefix + "['norm'] = {}"])
        if block["filters_key_present"]:
            lines.append(prefix + "['filters'] = " + repr(_filters(block["raw_filter_configuration"])))
        for row in block["entries"]:
            key = _id(row["id"])
            lines.append(prefix + "['xlsx'][%r] = %r" % (key, row["xlsx_path"]))
            if row["normalization"] is not None:
                lines.append(prefix + "['norm'][%r] = %r" % (key, row["normalization"]["definition"]))
    lines.append(OVERLAYS)
    for overlay in ir["reproduction"]["overlays"]:
        lines.append("conf[%r] = %r" % (overlay["key"], overlay["after"]))
    originals = source["parameter_definitions"]
    for obj, rows in ir["reproduction"]["parameter_definitions"].items():
        for original, row in zip(originals[obj], rows):
            for key, value in row["definition"].items():
                if key not in original["definition"] or _typed(value) != _typed(original["definition"][key]):
                    lines.append("conf['params'][%r][%r][%r] = %r" % (obj, row["name"], key, value))
    text = "\n".join(lines) + "\n"
    parity = check_generated_input(ir, text)
    if not parity["passed"]:
        raise ValueError("Generated-input parity failed: " + repr(parity["issues"]))
    return {"schema": SCHEMA, "input_text": text, "ir": ir, "parity": parity,
            "compiled_ref": compiled_ref, "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def write_isolated(store, compiled_ref, scratch_root):
    """Create a unique review directory directly under an owned JLab Ceph area.

    This internal API has no MCP registration and no user-selectable filename.
    It cannot overwrite an existing input or write inside application/FitPack
    repositories. The directory is deliberately retained for scientific review.
    """
    rendered = render_compiled_input(store, compiled_ref)
    root = Path(scratch_root)
    if os.name != "posix" or not re.fullmatch(r"/w/ceph24/JAM/[A-Za-z0-9_-]+", str(root)):
        raise ValueError("Scratch root must be a top-level JLab Ceph account directory")
    if root.resolve(strict=True) != root or root.stat().st_uid != os.getuid():
        raise ValueError("Scratch root must be owned by the caller and must not resolve through symlinks")
    directory = Path(tempfile.mkdtemp(prefix="femtomind-materialization-", dir=root))
    directory.chmod(0o700)
    evidence = {"schema": SCHEMA, "configuration_ref": rendered["ir"]["configuration_ref"],
                "compiled_ref": compiled_ref, "input_sha256": rendered["input_sha256"],
                "ir_sha256": snapshot.digest(rendered["ir"]), "parity": rendered["parity"],
                "normalization_penalty_policy": rendered["ir"]["reproduction"]["normalization_policy"],
                "historical_reproduction_ready": rendered["ir"]["literal_catalog"]["catalog_validation"]["reproduction_ready"],
                "campaign_execution_allowed": False}
    files = {"input.py": rendered["input_text"], "configuration-ir.json": snapshot.canonical(rendered["ir"]) + "\n",
             "materialization-report.json": json.dumps(evidence, indent=2, allow_nan=False) + "\n"}
    for name, text in files.items():
        with (directory / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    if hashlib.sha256((directory / "input.py").read_bytes()).hexdigest() != rendered["input_sha256"]:
        raise ValueError("Scratch input checksum mismatch")
    return dict(evidence, directory=str(directory), input_path=str(directory / "input.py"))
