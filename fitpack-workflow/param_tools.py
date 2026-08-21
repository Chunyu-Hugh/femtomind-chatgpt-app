"""
Self-contained parameter management for the FitPack MCP server.

Provides read/write access to the fitting configuration (default.yaml),
parameter listing, physics explanations, and readiness checks.
No external dependencies on the agents/ module.
"""
import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from ssh_runner import ssh_read_file, ssh_write_file, ssh_file_exists, get_remote_base

# --- Paths ---
# Resolved at call time so they track the currently-active HPC user.
# ssh_runner reads the user from a ContextVar that server.py MCP tools set via
# `with set_hpc_user(user):` before invoking these helpers — so simply calling
# `get_remote_base()` without args picks up the caller's HPC home.
def _remote_default_yaml() -> str:
    return f"{get_remote_base()}/inputs/default.yaml"

def _remote_idis_readme() -> str:
    return f"{get_remote_base()}/database/idis/README.md"

EXPLANATIONS_YAML = Path(__file__).parent / "parameter_explanations.yaml"  # local


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_yaml_config() -> Dict[str, Any]:
    """Load the default YAML configuration file from HPC (always fresh)."""
    path = _remote_default_yaml()
    try:
        content = ssh_read_file(path)
        return yaml.safe_load(content) or {}
    except Exception as e:
        raise RuntimeError(f"Failed to load config from {path}: {e}")


def _save_yaml_config(config: Dict[str, Any]) -> None:
    """Write configuration back to default.yaml on HPC."""
    content = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)
    ssh_write_file(_remote_default_yaml(), content)


def _load_explanations() -> Dict[str, Any]:
    """Load parameter explanations YAML; return empty dict on failure."""
    try:
        with open(EXPLANATIONS_YAML, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _get_dataset_index_info() -> Dict[str, Dict[str, str]]:
    """Parse dataset index information from the IDIS README table on HPC."""
    try:
        readme_path = _remote_idis_readme()
        if not ssh_file_exists(readme_path):
            return {}
        readme_content = ssh_read_file(readme_path)
    except Exception:
        return {}

    dataset_info = {}
    pattern = r'\|\s*(\d+)\s*\|\s*[^\|]*\|\s*([^\|]+)\|\s*([^\|]+)\|\s*([^\|]+)\|'
    for match in re.findall(pattern, readme_content):
        index, target, obs, experiment = match[0], match[1], match[2], match[3]
        dataset_info[index.strip()] = {
            "target": target.strip(),
            "obs": obs.strip(),
            "experiment": experiment.strip(),
        }
    return dataset_info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_params() -> str:
    """
    List all available PDF computation parameters from default.yaml.
    Returns a formatted Markdown string.
    """
    config = _load_yaml_config()
    conf = config.get("conf", {})
    explanations = _load_explanations()
    param_explanations = explanations.get("parameters", {})

    lines = ["Here are all available parameters from the default configuration:\n"]

    # Global configuration keys (skip structural keys)
    lines.append("**Global Configuration:**")
    for key, value in conf.items():
        if key not in ("datasets", "params"):
            desc = param_explanations.get(key, {}).get("description", "No description available")
            lines.append(f"- **{key}**: {desc} (default: {value})")

    # Dataset parameters
    if "datasets" in conf and "idis" in conf["datasets"]:
        lines.append("\n**Dataset parameters (DIS - Deep Inelastic Scattering):**")
        idis_config = conf["datasets"]["idis"]
        if "filters" in idis_config:
            lines.append(f"- **filters**: Data filtering conditions (current: {idis_config['filters']})")
        if "xlsx" in idis_config:
            lines.append(f"- **xlsx**: Dataset file mappings ({len(idis_config['xlsx'])} datasets configured)")
        if "norm" in idis_config:
            lines.append(f"- **norm**: Normalization parameters ({len(idis_config['norm'])} datasets)")

    # PDF functional form parameters
    if "params" in conf and "pdf" in conf["params"]:
        lines.append("\n**PDF Functional Form Parameters:**")
        pdf_params = conf["params"]["pdf"]
        groups: Dict[str, list] = {}
        for p_name in pdf_params:
            prefix = p_name.split()[0] if ' ' in p_name else p_name
            groups.setdefault(prefix, []).append(p_name)

        for prefix in sorted(groups.keys()):
            p_summaries = []
            for p_name in sorted(groups[prefix]):
                p_info = pdf_params[p_name]
                fixed_mark = "*" if p_info.get("fixed") else ""
                val = p_info.get("value")
                p_summaries.append(f"{p_name.split()[-1]}{fixed_mark}={val}")
            lines.append(f"- **{prefix}**: {', '.join(p_summaries)}")
        lines.append("\n*(Note: * indicates fixed parameters)*")

    return "\n".join(lines)


def explain_params(param_names: Optional[List[str]] = None) -> str:
    """
    Get detailed physics explanations for parameters.

    Args:
        param_names: Optional list of names to explain; if None, explains all.
    """
    config = _load_yaml_config()
    conf = config.get("conf", {})
    explanations = _load_explanations()
    param_explanations = explanations.get("parameters", {})
    dataset_explanations = explanations.get("datasets", {})

    def _fmt_top_level(key: str, value: Any) -> List[str]:
        exp = param_explanations.get(key, {})
        out = [
            f"- **{key}**: {exp.get('description', 'No description available')}",
            f"  - Type: {exp.get('type', 'unknown')}",
            f"  - Default: {value}",
        ]
        if exp.get("options"):
            out.append(f"  - Options: {', '.join(exp['options'])}")
        out.append("")
        return out

    if not param_names:
        lines = ["Here are the parameter explanations:\n"]
        for key, value in conf.items():
            if key not in ("datasets", "params"):
                lines.extend(_fmt_top_level(key, value))

        if "datasets" in conf and "idis" in conf["datasets"]:
            lines.append("**DIS (Deep Inelastic Scattering) Dataset Parameters:**\n")
            idis_exp = dataset_explanations.get("idis", {})
            for field in ("filters", "xlsx", "norm"):
                exp = idis_exp.get(field, {})
                lines.append(f"- **{field}**: {exp.get('description', 'No description')}")
                if "structure" in exp:
                    for k, v in exp["structure"].items():
                        lines.append(f"  - {k}: {v}")
            dataset_info = _get_dataset_index_info()
            if dataset_info:
                lines.append("\n**Available Dataset Indices:**")
                for idx, info in list(dataset_info.items())[:10]:
                    lines.append(f"- {idx}: {info.get('experiment', 'Unknown')} "
                                 f"({info.get('target', '?')}, {info.get('obs', '?')})")
                if len(dataset_info) > 10:
                    lines.append(f"... and {len(dataset_info) - 10} more datasets")
    else:
        lines = ["Here are explanations for the requested parameters:\n"]
        for param_name in param_names:
            if param_name in conf and param_name not in ("datasets", "params"):
                lines.extend(_fmt_top_level(param_name, conf[param_name]))
            elif param_name in ("filters", "xlsx", "norm"):
                idis_exp = dataset_explanations.get("idis", {})
                param_exp = idis_exp.get(param_name, {})
                lines.append(f"- **{param_name}** (DIS dataset): {param_exp.get('description', 'No description')}")
                if "structure" in param_exp:
                    for k, v in param_exp["structure"].items():
                        lines.append(f"  - {k}: {v}")
            else:
                lines.append(f"- **{param_name}**: Parameter not found or not accessible")

    return "\n".join(lines)


def get_current_params() -> Dict[str, Any]:
    """Return the full configuration dict read from default.yaml."""
    return _load_yaml_config()


def modify_params(params: Dict[str, Any]) -> str:
    """
    Modify parameter values and persist changes to default.yaml.

    Args:
        params: Mapping of dot-notation paths to new values.
                Supported paths:
                  - conf.params.pdf.<PARAM_NAME>.<field>
                    e.g. "conf.params.pdf.g1 a.value"
                  - conf.datasets.idis.norm.<DATASET_IDX>.<field>
                    e.g. "conf.datasets.idis.norm.10010.value"
                  - conf.datasets.idis.filters  (any simple path under idis)

    Returns:
        Status message listing modifications and errors.
    """
    current = _load_yaml_config()
    modified: List[str] = []
    invalid: List[str] = []

    ALLOWED_PREFIXES = ("conf.params.pdf", "conf.datasets.")

    for param_path, value in params.items():
        if not any(param_path.startswith(p) for p in ALLOWED_PREFIXES):
            invalid.append(
                f"{param_path}: Only conf.params.pdf.* or conf.datasets.<observable>.* can be modified."
            )
            continue

        try:
            # Normalise to always start with 'conf'
            parts = param_path.split(".")
            if parts[0] != "conf":
                parts = ["conf"] + parts

            # --- PDF parameters ---
            # Format: conf.params.pdf.<param_name>.<field>
            # Example parts: ['conf','params','pdf','g1 a','value']
            if len(parts) >= 2 and parts[1] == "params" and len(parts) >= 3 and parts[2] == "pdf":
                if len(parts) < 5:
                    invalid.append(f"{param_path}: PDF path must be conf.params.pdf.<name>.<field>")
                    continue
                param_name = parts[3]   # e.g. "g1 a"
                field_name = parts[4]   # e.g. "value"
                if field_name in ("value", "min", "max"):
                    value = float(value)
                elif field_name == "fixed":
                    value = (value.lower() in ("true", "1", "yes", "on")
                             if isinstance(value, str) else bool(value))
                (current
                 .setdefault("conf", {})
                 .setdefault("params", {})
                 .setdefault("pdf", {})
                 .setdefault(param_name, {})
                 )[field_name] = value
                modified.append(f"{param_path} = {value}")

            # --- Norm parameters ---
            # Format: conf.datasets.<observable>.norm.<dataset_idx>.<field>
            # Example parts: ['conf','datasets','idis','norm','10010','value']
            elif (len(parts) >= 4 and parts[1] == "datasets"
                  and parts[3] == "norm"):
                if len(parts) < 6:
                    invalid.append(
                        f"{param_path}: Norm path must be conf.datasets.<obs>.norm.<idx>.<field>"
                    )
                    continue
                obs_key = parts[2]  # e.g. "idis", "dy", "sidis"
                dataset_idx = parts[4]
                norm_key = parts[5]
                if norm_key in ("value", "min", "max"):
                    value = float(value)
                elif norm_key == "fixed":
                    value = (value.lower() in ("true", "1", "yes", "on")
                             if isinstance(value, str) else bool(value))
                (current
                 .setdefault("conf", {})
                 .setdefault("datasets", {})
                 .setdefault(obs_key, {})
                 .setdefault("norm", {})
                 .setdefault(dataset_idx, {})
                 )[norm_key] = value
                modified.append(f"{param_path} = {value}")

            # --- Other parameters (filters, xlsx, …) ---
            else:
                target = current
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value
                modified.append(f"{param_path} = {value}")

        except Exception as e:
            invalid.append(f"{param_path}: Error - {e}")

    # Persist to disk
    if modified:
        _save_yaml_config(current)

    result_lines: List[str] = []
    if modified:
        result_lines.append("Successfully modified parameters:")
        result_lines.extend(f"  - {m}" for m in modified)
    if invalid:
        result_lines.append("\nInvalid parameters:")
        result_lines.extend(f"  - {i}" for i in invalid)
    return "\n".join(result_lines) if result_lines else "No parameters were modified."


def check_params_readiness() -> Dict[str, Any]:
    """
    Check whether the configuration file is present and valid for fitting.

    Returns a dict with keys: ready, has_params, message, suggestions.
    """
    result: Dict[str, Any] = {
        "ready": False,
        "has_params": False,
        "message": "",
        "suggestions": [],
    }

    default_yaml = _remote_default_yaml()
    if not ssh_file_exists(default_yaml):
        result["message"] = f"Configuration file not found: {default_yaml}"
        result["suggestions"] = ["Ensure inputs/default.yaml exists on the HPC."]
        return result

    try:
        config = _load_yaml_config()
        pdf_params = config.get("conf", {}).get("params", {}).get("pdf", {})

        if not pdf_params:
            result["message"] = "No PDF parameters found in conf.params.pdf."
            result["suggestions"] = [
                "Check that inputs/default.yaml contains a conf.params.pdf section."
            ]
            return result

        result["has_params"] = True
        non_fixed = [k for k, v in pdf_params.items() if not v.get("fixed", False)]
        result["ready"] = True
        result["message"] = (
            f"Parameters ready. {len(pdf_params)} total, "
            f"{len(non_fixed)} non-fixed (will be randomised during task generation)."
        )

    except Exception as e:
        result["message"] = f"Error reading configuration: {e}"

    return result
