"""Image-only delivery adapter; all scientific work stays in existing backends."""
import base64
from copy import deepcopy
import hashlib
import math
import struct
import zlib


BACKENDS = {
    "experimental": "analyze_by_experiment",
    "pdf": "campaign_pdf_plots",
    "ff": "campaign_ff_plots",
}
OPTIONS = {
    "experimental": {"datasets", "experiments", "credible_interval", "replicas"},
    "pdf": {"flavors", "object_key", "q2", "credible_interval", "mean_overlay",
            "x_min", "x_max", "band_color", "band_alpha", "replicas"},
    "ff": {"flavors", "q2", "credible_interval", "modes", "hadrons", "z_min",
           "z_max", "band_color", "band_alpha", "replicas"},
}


def schema(tools):
    """Reuse existing option schemas/defaults without attaching their UI metadata."""
    properties = {}
    for kind, backend in BACKENDS.items():
        if tools[backend].get("denied"):
            continue            # a family whose backend this deployment leaves out
        source = tools[backend]["schema"]["properties"]
        for key in sorted(OPTIONS[kind]):
            definition = deepcopy(source[key])
            # Defaults remain backend-owned; q2/style defaults differ by family.
            definition.pop("default", None)
            definition["description"] = (definition.get("description", "") +
                " Applicable families: " + ", ".join(k for k in BACKENDS if key in OPTIONS[k]) +
                ". Omission preserves that backend's default.").strip()
            properties[key] = definition
    # A plain object schema works with clients that flatten MCP tool arguments.
    # The mutually exclusive start/poll forms are enforced by get_plot as well.
    return {"type": "object", "additionalProperties": False,
            "description": "Start with plot_type and collection_id plus optional options; poll with job_id alone.",
            "properties": {
        "plot_type": {"type": "string", "enum": [k for k, b in BACKENDS.items() if not tools[b].get("denied")]},
        "collection_id": deepcopy(tools[BACKENDS["pdf"]]["schema"]["properties"]["collection_id"]),
        "options": {"type": "object", "additionalProperties": False, "properties": properties},
        "job_id": {"type": "string", "minLength": 1,
                   "description": "Poll only with get_thesis_plot(job_id); omit all other fields; never analysis_result or open_panel."}}}


def _validate(value, definition, path):
    """Validate transport types even when a client bypasses discovery validation."""
    kind = definition.get("type")
    valid = {"string": lambda: isinstance(value, str),
             "number": lambda: type(value) in (int, float) and math.isfinite(value),
             "boolean": lambda: type(value) is bool,
             "object": lambda: isinstance(value, dict),
             "integer": lambda: type(value) is int}.get(kind)
    if valid and not valid():
        raise ValueError(path + ": expected " + kind)
    if "enum" in definition and value not in definition["enum"]:
        raise ValueError(path + ": unsupported choice")
    if kind in ("number", "integer"):
        for key, fails in (("minimum", lambda v: value < v), ("maximum", lambda v: value > v),
                           ("exclusiveMinimum", lambda v: value <= v), ("exclusiveMaximum", lambda v: value >= v)):
            if key in definition and fails(definition[key]):
                raise ValueError(path + ": outside " + key)


def _png_dimensions(raw):
    """Check PNG chunk integrity without decoding or changing scientific pixels."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Plot backend returned invalid PNG bytes")
    offset, dimensions, data_seen = 8, None, False
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        end = offset + 12 + length
        if end > len(raw):
            break
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:end - 4]
        if zlib.crc32(kind + data) != struct.unpack_from(">I", raw, end - 4)[0]:
            raise ValueError("PNG checksum failed")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG has no valid image header")
            dimensions = struct.unpack_from(">II", data)
            if not all(dimensions):
                raise ValueError("PNG dimensions must be positive")
        elif kind == b"IHDR":
            raise ValueError("PNG has duplicate image headers")
        data_seen |= kind == b"IDAT"
        if kind == b"IEND":
            if length or end != len(raw) or not data_seen:
                raise ValueError("PNG has invalid image termination")
            return dimensions
        offset = end
    raise ValueError("PNG is incomplete")


def _image_result(out):
    """Remove app payloads, retaining unchanged PNG bytes and scientific metadata."""
    if not isinstance(out, dict):
        raise ValueError("Plot backend returned an invalid result")
    # Existing result objects may be shared with interactive callers. Never mutate.
    clean = {key: deepcopy(value) for key, value in out.items() if not key.startswith("_")}
    clean["delivery"] = "native_png"
    if clean.get("status") == "running":
        clean["poll_tool"] = "get_thesis_plot"
        clean["poll_arguments"] = {"job_id": clean["job_id"]}
        clean["message"] = "Still rendering. Call get_thesis_plot with only this job_id until complete; do not open the app or restart the request."
        return clean
    images = out.get("_images") or []
    if clean.get("status") != "ok":
        return clean
    if not images:
        clean.update(status="error", message="The existing plotting backend returned no thesis PNG. See figure_error, rendering_errors and note; no interactive fallback was opened.")
        return clean
    plots = clean.get("plots") or []
    if len(plots) != len(images):
        raise ValueError("PNG and plot-metadata counts disagree")
    content = []
    for index, (plot, item) in enumerate(zip(plots, images)):
        if item.get("mimeType") != "image/png":
            raise ValueError("Plot backend returned a non-PNG image")
        raw = base64.b64decode(item["data"], validate=True)
        width, height = _png_dimensions(raw)
        # No bytes are re-encoded, resized or recomputed by this adapter.
        content.append({"data": item["data"], "mimeType": "image/png"})
        plot.pop("data_url", None)
        plot.update(image_index=index, mime_type="image/png", bytes=len(raw), width=width, height=height,
                    sha256=hashlib.sha256(raw).hexdigest())
    clean.update(plots=plots, image_count=len(content), _images=content)
    return clean


def get_plot(gateway, arguments):
    """Start or poll existing scientific jobs using a UI-free MCP descriptor."""
    if not isinstance(arguments, dict):
        raise ValueError("get_thesis_plot arguments must be an object")
    args = {key: value for key, value in arguments.items() if key != "username"}
    if "job_id" in args:
        if set(args) != {"job_id"} or not isinstance(args["job_id"], str) or not args["job_id"]:
            raise ValueError("Poll with only job_id")
        user, error = gateway._need_user("")
        if error:
            return _image_result(error)
        job = gateway._JOBS.get(args["job_id"])
        if not job or job["user"] != user or job["kind"] not in BACKENDS.values():
            raise ValueError("No accessible plotting job; jobs expire and do not survive a gateway restart")
        return _image_result(gateway.analysis_result(args["job_id"]))
    if set(args) - {"plot_type", "collection_id", "options"}:
        raise ValueError("Use plot_type, collection_id and optional options, or poll with job_id alone")
    kind = args.get("plot_type")
    if not isinstance(kind, str) or kind not in BACKENDS:
        raise ValueError("plot_type must be experimental, pdf or ff")
    if gateway.TOOLS[BACKENDS[kind]].get("denied"):
        raise ValueError("plot_type " + kind + " is not available on this gateway (its backend tool is left out)")
    cid = args.get("collection_id")
    if not isinstance(cid, str) or not cid.strip():
        raise ValueError("collection_id must be a campaign name or collection ID")
    options = args.get("options", {})
    if not isinstance(options, dict) or set(options) - OPTIONS[kind]:
        raise ValueError("Unsupported options for " + kind + "; use its discovery schema")
    definitions = gateway.TOOLS[BACKENDS[kind]]["schema"]["properties"]
    for key, value in options.items():
        _validate(value, definitions[key], "options." + key)
    kwargs = dict(options, collection_id=cid, export_png=True)
    if kind == "experimental":
        kwargs["plot"] = True
    return _image_result(getattr(gateway, BACKENDS[kind])(**kwargs))
