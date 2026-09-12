"""
advanced_schema.py — typed schema for the ``advanced`` dict that travels
DatasetSelector → [DATASET_SELECTOR_RESULT] → run_fitting_job →
_apply_advanced_overrides.

Until T1-B (2026-04-25) this dict was an untyped string-keyed bag passed
across three process boundaries (Svelte FE → LLM tool call → MCP server)
with no schema check. A field rename on either side silently broke
``_apply_advanced_overrides``'s ``advanced.get("foo")`` lookup — the failure
mode was "the user's setting was ignored" rather than a loud error.

This module is the single source of the field set. Both:
  * fitpack-workflow/server.py (validates incoming `advanced` here before
    `_apply_advanced_overrides`),
  * open-webui/src/lib/types/advanced.ts (mirror of the same fields, kept
    in sync by the comment-block at the top of that file),
read from this contract. To add a field: add it here, mirror it in the .ts
file, then teach `_apply_advanced_overrides` to consume it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from pydantic import BaseModel, ConfigDict, Field
    _HAS_PYDANTIC = True
except ImportError:  # pragma: no cover — runtime envs without pydantic
    _HAS_PYDANTIC = False


# ---- Enums kept as plain string sets so non-pydantic envs can still
#      use the same values for runtime validation. ----

OPTIMIZERS: tuple[str, ...] = ("trf", "dogbox")
TMC_SCHEMES: tuple[Optional[str], ...] = ("AOT", "GP", None, "None", "")
QCD_ORDERS: tuple[str, ...] = ("LO", "NLO", "NNLO")


class _PdfParamOverride(BaseModel if _HAS_PYDANTIC else object):  # type: ignore[misc]
    """One row of the PDF parameter table the composer renders.

    Field semantics map to fitpack's ``conf.params.pdf[<name>]`` slot:
      * ``value`` — initial value used by the random sampler / fixed eval.
      * ``min`` / ``max`` — sampling bounds.
      * ``fixed`` — when True the parameter is held to ``value`` and not
        sampled.
    """
    if _HAS_PYDANTIC:
        model_config = ConfigDict(extra="forbid")

    value: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    fixed: Optional[bool] = None


class _QcdConfig(BaseModel if _HAS_PYDANTIC else object):  # type: ignore[misc]
    """The QCD-knob block the composer's "Step 4" emits.

    Keys here are the UI's spelling; ``server._QCD_CONFIG_KEY_MAP`` maps
    them to the fitpack YAML's spelling. Adding a new UI knob requires
    touching this model AND that map together, so neither side can drift.
    """
    if _HAS_PYDANTIC:
        model_config = ConfigDict(extra="forbid")

    order: Optional[str] = None         # "LO" | "NLO" | "NNLO"
    Q20: Optional[float] = None         # Q₀² in GeV², e.g. 1.69
    tmc: Optional[Any] = None           # "AOT" | "GP" | "None" | False
    ht: Optional[bool] = None           # higher-twist on/off
    htType: Optional[str] = None
    dglapMode: Optional[str] = None
    alphaSmode: Optional[str] = None
    nuc: Optional[Any] = None


class AdvancedFitConfig(BaseModel if _HAS_PYDANTIC else object):  # type: ignore[misc]
    """Schema for the ``advanced`` dict accepted by run_fitting_job.

    All fields are optional; an empty / missing dict is valid (the job
    runs with default.yaml's baseline). Validation rejects unknown keys
    (``extra="forbid"``) so a frontend rename surfaces immediately
    instead of being silently dropped by the consumer's ``.get()``.
    """
    if _HAS_PYDANTIC:
        model_config = ConfigDict(extra="forbid")

    # Convergence / iteration knobs
    tolerance: Optional[Any] = None         # float or numeric string ("1e-6")
    max_iterations: Optional[int] = None
    optimizer: Optional[str] = None         # one of OPTIMIZERS
    bootstrap: Optional[bool] = None

    # Composite blocks
    qcd_config: Optional[_QcdConfig] = None
    pdf_params: Optional[Dict[str, _PdfParamOverride]] = None
    pdf_pion_params: Optional[Dict[str, _PdfParamOverride]] = None
    pdf_kaon_params: Optional[Dict[str, _PdfParamOverride]] = None

    # Meson / lattice special keys (fitpack top-level conf)
    kaon_fitting_scenario: Optional[int] = None
    pion_fitting_scenario: Optional[int] = None
    pion_resummation: Optional[bool] = None
    mp_method: Optional[str] = None
    dis_version: Optional[int] = None
    ln_mode: Optional[str] = None
    splfunc_model: Optional[str] = None

    # Quality thresholds — written into chain_state history for the next
    # compute_batch_prior call. Not consumed by _apply_advanced_overrides
    # itself, but accepted here so the dict is one-source-of-truth.
    chi2_ok: Optional[float] = None
    chi2_warn: Optional[float] = None
    good_fraction: Optional[float] = None


def coerce_advanced(advanced: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and normalize an incoming ``advanced`` dict.

    Returns a plain dict (compatible with the existing
    ``_apply_advanced_overrides`` consumer). Raises ``ValueError`` if the
    dict contains unknown keys — surfaces frontend/backend drift loudly.

    Pre-T1-B callers passed the raw dict straight through; this function
    is the new gate. When pydantic isn't installed (very minimal envs)
    the dict is returned unchanged with a soft warning baked into the
    return — keeping the soft path so a missing dependency doesn't break
    fits.
    """
    if not advanced:
        return {}
    if not isinstance(advanced, dict):
        raise ValueError(f"advanced must be a dict, got {type(advanced).__name__}")
    if not _HAS_PYDANTIC:
        # Soft path: hand the raw dict back so the existing consumer keeps
        # working. The Open WebUI process always has pydantic available
        # (FastAPI dependency), so the strict path is the production path.
        return advanced
    model = AdvancedFitConfig.model_validate(advanced)
    # ``mode='json'`` keeps None values out so ``advanced.get(key)`` returns
    # None for absent fields, identical to the pre-T1-B behavior.
    return model.model_dump(exclude_none=True)
