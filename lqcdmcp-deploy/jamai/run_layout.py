from __future__ import annotations

import json
from pathlib import Path

from .schemas import CampaignDocument, Stage


def build_run_layout(document: CampaignDocument, stage: Stage) -> dict[str, Path]:
    # Keep all per-stage files under one campaign root. The returned dictionary
    # is the contract used by materialization, Slurm submission, and later
    # collection/analysis code.
    campaign_root = document.campaign_root_path()
    stage_dir_name = stage.output_dir or f'stages/{stage.name}'
    stage_root = campaign_root / stage_dir_name

    return {
        'campaign_root': campaign_root,
        'campaign_yaml': campaign_root / 'campaign.yaml',
        'campaign_lock': campaign_root / 'campaign.lock.json',
        'stages_root': campaign_root / 'stages',
        'stage_root': stage_root,
        'stage_input': stage_root / 'input.py',
        'stage_manifest': stage_root / 'manifest.resolved.yaml',
        'stage_logs': stage_root / 'logs',
        'stage_replicas': stage_root / 'replicas',
    }


def materialize_run_layout(layout: dict[str, Path], lock_payload: dict) -> dict[str, str]:
    # Create directories before writing generated files. The lock file records
    # the resolved campaign state so a submitted run can be inspected later even
    # if the source YAML changes.
    layout['campaign_root'].mkdir(parents=True, exist_ok=True)
    layout['stages_root'].mkdir(parents=True, exist_ok=True)
    layout['stage_root'].mkdir(parents=True, exist_ok=True)
    layout['stage_logs'].mkdir(parents=True, exist_ok=True)
    layout['stage_replicas'].mkdir(parents=True, exist_ok=True)

    layout['campaign_lock'].write_text(json.dumps(lock_payload, indent=2, sort_keys=True) + '\n')

    return {key: str(value) for key, value in layout.items()}
