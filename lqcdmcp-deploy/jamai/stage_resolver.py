from __future__ import annotations

from .reaction_registry import get_required_objects, normalize_reactions
from .schemas import CampaignDocument, Stage


class ResolvedStage:
    # A lightweight, dependency-free model for the fully resolved stage. This is
    # deliberately not Pydantic so the materialized stage description works even
    # on bare HPC Python environments.
    def __init__(
        self,
        *,
        name: str,
        reactions: list[str],
        required_objects: list[str],
        fit_objects: list[str],
        frozen_objects: list[str],
        effective_fit_objects: list[str],
        input_template: str,
        nreplicas: int,
        previous_stage: str | None,
        output_dir: str,
        datasets: dict,
        cuts: dict,
        params: dict,
        qcd: dict,
        job: dict,
    ):
        self.name = name
        self.reactions = reactions
        self.required_objects = required_objects
        self.fit_objects = fit_objects
        self.frozen_objects = frozen_objects
        self.effective_fit_objects = effective_fit_objects
        self.input_template = input_template
        self.nreplicas = nreplicas
        self.previous_stage = previous_stage
        self.output_dir = output_dir
        self.datasets = datasets
        self.cuts = cuts
        self.params = params
        self.qcd = qcd
        self.job = job

    def model_dump(self, mode: str | None = None) -> dict:
        # Mirror Pydantic's method name so callers can serialize either schema
        # objects or resolved-stage objects with the same style.
        return {
            'name': self.name,
            'reactions': self.reactions,
            'required_objects': self.required_objects,
            'fit_objects': self.fit_objects,
            'frozen_objects': self.frozen_objects,
            'effective_fit_objects': self.effective_fit_objects,
            'input_template': self.input_template,
            'nreplicas': self.nreplicas,
            'previous_stage': self.previous_stage,
            'output_dir': self.output_dir,
            'datasets': self.datasets,
            'cuts': self.cuts,
            'params': self.params,
            'qcd': self.qcd,
            'job': self.job,
        }


def resolve_stage(document: CampaignDocument, stage: Stage) -> ResolvedStage:
    # Turn author-facing manifest values into the exact set of objects/datasets
    # the generated FitPack input card should activate.
    reactions = normalize_reactions(stage.reactions)
    required_objects = get_required_objects(reactions)
    fit_objects = [item for item in stage.fit_objects]
    frozen_objects = [item for item in stage.frozen_objects]

    # Every object required by the chosen reactions must be accounted for. It can
    # be fitted in this stage or frozen from a previous/default configuration.
    missing_required = [
        obj for obj in required_objects if obj not in fit_objects and obj not in frozen_objects
    ]
    if missing_required:
        raise ValueError(
            f"Stage '{stage.name}' is missing required fit coverage for objects: {missing_required}"
        )

    # Frozen objects remain present in the config, but their parameters should
    # not be active fit parameters in this stage.
    effective_fit_objects = [obj for obj in fit_objects if obj not in frozen_objects]
    output_dir = stage.output_dir or f'stages/{stage.name}'

    return ResolvedStage(
        name=stage.name,
        reactions=reactions,
        required_objects=required_objects,
        fit_objects=fit_objects,
        frozen_objects=frozen_objects,
        effective_fit_objects=effective_fit_objects,
        input_template=stage.input_template,
        nreplicas=stage.nreplicas,
        previous_stage=stage.previous_stage,
        output_dir=output_dir,
        datasets=stage.datasets,
        cuts=stage.cuts,
        params=stage.params,
        qcd=stage.qcd.model_dump(),
        job=stage.job,
    )
