from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    # PyYAML is convenient locally, but remote HPC nodes may not have it. The
    # basic parser below supports the limited manifest shape this scaffold uses.
    yaml = None

from .input_materializer import materialize_stage_input, read_stage_input_preview
from .run_layout import build_run_layout, materialize_run_layout
from .schemas import CampaignDocument
from .stage_resolver import ResolvedStage, resolve_stage


def _parse_scalar(value: str):
    # Convert simple YAML-like scalars into Python values for the fallback
    # parser: null/bool/numbers/lists/dicts when ast.literal_eval can handle it.
    lower = value.lower()
    if lower in {'null', 'none'}:
        return None
    if lower == 'true':
        return True
    if lower == 'false':
        return False
    try:
        return ast.literal_eval(value)
    except Exception:
        return value


def _load_basic_yaml(text: str):
    # Minimal indentation-sensitive YAML reader for simple mappings/sequences.
    # It intentionally avoids the full YAML spec; use PyYAML when available.
    lines = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip()
        if stripped.startswith('#'):
            continue
        lines.append(raw_line.rstrip('\n'))

    index = 0

    def parse_block(indent: int):
        nonlocal index
        mapping = {}
        sequence = None

        # Parse one indentation block at a time. A block is either a mapping or a
        # sequence, never both, which matches the campaign files we generate.
        while index < len(lines):
            line = lines[index]
            current_indent = len(line) - len(line.lstrip(' '))
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(f'Invalid indentation near line: {line}')

            content = line.strip()
            if content.startswith('- '):
                if mapping:
                    raise ValueError('Cannot mix mapping and sequence at the same indentation level')
                if sequence is None:
                    sequence = []

                item_text = content[2:].strip()
                index += 1
                if not item_text:
                    sequence.append(parse_block(indent + 2))
                    continue

                if ': ' in item_text or item_text.endswith(':'):
                    key, _, value = item_text.partition(':')
                    item = {key.strip(): _parse_scalar(value.strip()) if value.strip() else None}
                    if index < len(lines):
                        next_indent = len(lines[index]) - len(lines[index].lstrip(' '))
                        if next_indent > indent:
                            nested = parse_block(indent + 2)
                            if item[key.strip()] is None:
                                item[key.strip()] = nested
                            elif isinstance(nested, dict):
                                item.update(nested)
                    sequence.append(item)
                else:
                    sequence.append(_parse_scalar(item_text))
                continue

            if sequence is not None:
                raise ValueError('Cannot mix sequence and mapping at the same indentation level')

            key, _, value = content.partition(':')
            if not _:
                raise ValueError(f'Invalid mapping line: {line}')
            key = key.strip()
            value = value.strip()
            index += 1

            if value:
                mapping[key] = _parse_scalar(value)
            else:
                if index < len(lines):
                    next_indent = len(lines[index]) - len(lines[index].lstrip(' '))
                    if next_indent > indent:
                        mapping[key] = parse_block(indent + 2)
                    else:
                        mapping[key] = {}
                else:
                    mapping[key] = {}

        return sequence if sequence is not None else mapping

    return parse_block(0)


def _load_manifest_payload(manifest_path: Path) -> dict:
    # JSON exponent literals such as 1e-10 are strings in PyYAML's YAML 1.1
    # resolver. Parse JSON with its own numerical rules, regardless of suffix.
    text = manifest_path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if yaml is not None:
        return yaml.safe_load(text) or {}
    return _load_basic_yaml(text) or {}


def _package_root() -> Path:
    # Used to resolve example manifests/templates relative to this package.
    return Path(__file__).resolve().parent


def resolve_campaign_file(path: str | Path) -> Path:
    # Accept either an absolute/local path or a path relative to the JAMAI
    # package. This helps both CLI use and remote materialization scripts.
    candidate = Path(path).expanduser()
    if candidate.exists():
        return candidate.resolve()

    package_candidate = _package_root() / candidate
    if package_candidate.exists():
        return package_candidate.resolve()

    raise FileNotFoundError(f'Campaign manifest not found: {path}')


def load_campaign_file(path: str | Path) -> CampaignDocument:
    # Load raw YAML, then validate/normalize it into schema objects.
    manifest_path = resolve_campaign_file(path)
    payload = _load_manifest_payload(manifest_path)
    return CampaignDocument.model_validate(payload)


def _resolve_template_path(document: CampaignDocument, stage: ResolvedStage) -> Path:
    # Input templates may be absolute, relative to FitPack, or relative to the
    # JAMAI package examples. Try those locations in that order.
    template = Path(stage.input_template).expanduser()
    if template.is_absolute():
        return template.resolve()

    if document.campaign.fitpack_root:
        fitpack_candidate = Path(document.campaign.fitpack_root).expanduser().resolve() / template
        if fitpack_candidate.exists():
            return fitpack_candidate

    manifest_candidate = resolve_campaign_file(stage.input_template)
    return manifest_candidate


def _build_lock_payload(document: CampaignDocument, resolved_stages: list[ResolvedStage]) -> dict:
    # The lock file records exactly what was materialized, including resolved
    # inherited/default values, so later analysis can reconstruct the run.
    return {
        'campaign': document.campaign.model_dump(),
        'stages': [stage.model_dump() for stage in resolved_stages],
    }


def verify_materialized_priors(document: CampaignDocument, resolved_stages: list[ResolvedStage]) -> dict:
    """Check final runner manifests and literal input snapshots without executing input.py.

    This is part of materialization, not an optional validation flag. A failure
    makes the existing materialize && compile && sbatch chain stop before Slurm.
    """
    from .prior_replicas import verify
    checked = []; saved_documents = None
    def same_snapshot(actual, expected, location):
        if not isinstance(actual, dict):
            raise ValueError('Materialized prior snapshot missing from ' + location)
        verify(actual)
        if actual != expected:
            raise ValueError('Materialized prior differs from requested snapshot: ' + location)
    for stage in resolved_stages:
        expected = stage.params.get('prior_replicas')
        if expected is None:
            continue
        verify(expected)
        layout = build_run_layout(document, document.stage_map()[stage.name])
        if saved_documents is None:
            saved_documents = [(str(path), _load_manifest_payload(path)) for path in
                               (layout['campaign_lock'], layout['campaign_yaml'])]
        for location, saved in saved_documents:
            record = next((s for s in saved.get('stages', []) if s.get('name') == stage.name), {})
            same_snapshot(record.get('params', {}).get('prior_replicas'), expected, location + ':' + stage.name)
        manifest = _load_manifest_payload(layout['stage_manifest'])
        actual = manifest.get('params', {}).get('prior_replicas')
        same_snapshot(actual, expected, 'stage manifest:' + stage.name)
        tree = ast.parse(layout['stage_input'].read_text())
        assignments = [node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Subscript)
                               and isinstance(target.value, ast.Name) and target.value.id == 'conf'
                               and isinstance(target.slice, ast.Constant)
                               and target.slice.value == 'jamai prior replicas'
                               for target in node.targets)]
        if not assignments:
            raise ValueError('Materialized prior snapshot missing from input.py: ' + stage.name)
        actual = ast.literal_eval(assignments[-1])
        same_snapshot(actual, expected, 'input.py:' + stage.name)
        checked.append({'stage': stage.name, 'sha256': expected['sha256'],
                        'replicas': len(expected['replicas']), 'snapshots_checked': 4, 'status': 'verified'})
    return {'stages': checked, 'status': 'verified' if checked else 'not_applicable'}


def materialize_campaign(document: CampaignDocument, resolved_stages: list[ResolvedStage]) -> dict:
    # Write the campaign directory tree and one generated input.py per stage.
    written = []
    lock_payload = _build_lock_payload(document, resolved_stages)

    campaign_root = document.campaign_root_path()
    campaign_root.mkdir(parents=True, exist_ok=True)

    for resolved_stage in resolved_stages:
        # Use the original Stage for layout naming, and the ResolvedStage for
        # the generated manifest/input override content.
        source_stage = document.stage_map()[resolved_stage.name]
        layout = build_run_layout(document, source_stage)
        materialize_run_layout(layout, lock_payload)

        resolved_manifest = yaml.safe_dump(
            resolved_stage.model_dump(mode='json'),
            sort_keys=False,
        ) if yaml is not None else json.dumps(resolved_stage.model_dump(mode='json'), indent=2)
        layout['stage_manifest'].write_text(resolved_manifest)

        template_path = _resolve_template_path(document, resolved_stage)
        materialize_stage_input(template_path, layout['stage_input'], resolved_stage,
                                document.campaign.fitpack_root)

        written.append(
            {
                'stage': resolved_stage.name,
                'stage_root': str(layout['stage_root']),
                'input_path': str(layout['stage_input']),
                'manifest_path': str(layout['stage_manifest']),
                'input_preview': read_stage_input_preview(layout['stage_input']),
            }
        )

    manifest_path = campaign_root / 'campaign.yaml'
    # Store the normalized manifest next to the lock file. This is friendlier to
    # humans than the lock JSON, while the lock remains the exact machine record.
    manifest_text = (
        yaml.safe_dump(document.model_dump(mode='json'), sort_keys=False)
        if yaml is not None
        else json.dumps(document.model_dump(mode='json'), indent=2)
    )
    manifest_path.write_text(manifest_text)

    # Must finish successfully before the CLI can return success to submission.
    prior_integrity = verify_materialized_priors(document, resolved_stages)

    return {
        'campaign_root': str(campaign_root),
        'campaign_yaml': str(manifest_path),
        'campaign_lock': str(campaign_root / 'campaign.lock.json'),
        'stages': written,
        'prior_integrity': prior_integrity,
    }


def resolve_document(document: CampaignDocument) -> list[ResolvedStage]:
    # Resolve every stage after cross-stage validation has already happened in
    # CampaignDocument.
    return [resolve_stage(document, stage) for stage in document.stages]


def main() -> int:
    # Small CLI used locally and from remote shell scripts in the HPC router.
    parser = argparse.ArgumentParser(description='Validate and resolve a JAM campaign manifest')
    parser.add_argument('manifest', help='Path to campaign YAML')
    parser.add_argument(
        '--materialize',
        action='store_true',
        help='Create campaign/stage directories and copy stage input templates',
    )
    args = parser.parse_args()

    document = load_campaign_file(args.manifest)
    resolved_stages = resolve_document(document)

    output = {
        'campaign': document.campaign.model_dump(),
        'resolved_stages': [stage.model_dump() for stage in resolved_stages],
    }

    if args.materialize:
        output['materialized'] = materialize_campaign(document, resolved_stages)

    print(json.dumps(output, indent=2, sort_keys=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
