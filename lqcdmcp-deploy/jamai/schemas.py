from __future__ import annotations

from pathlib import Path
from typing import Literal

try:
    # Pydantic is the preferred path: it gives the web/HPC workflow strict
    # validation and a `.model_dump()` API for writing resolved manifests.
    from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

    HAVE_PYDANTIC = True
except ModuleNotFoundError:  # pragma: no cover - compatibility path for bare system python
    # Remote login nodes sometimes have only a minimal Python install. The
    # fallback classes below keep manifest validation usable without requiring a
    # full Open WebUI dependency stack on the cluster.
    HAVE_PYDANTIC = False
    BaseModel = object  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
    field_validator = None  # type: ignore[assignment]
    model_validator = None  # type: ignore[assignment]


if HAVE_PYDANTIC:

    class QCDSettings(BaseModel):
        # Settings copied into the generated FitPack `input.py` override block.
        # The names intentionally follow FitPack/JAM conventions instead of
        # frontend naming style.
        order: str = 'NLO'
        Q20: float = 1.6129
        ftol: float = 1.0e-6
        grid_path: str | None = None
        tmc: str = 'AOT'
        higherTwist: bool = False
        dglapMode: str = 'truncated'
        asMode: str = 'backward'
        betaVersion: int = 1
        siaVersion: int = 1
        pdfPositivity: StrictBool = True
        ffpionPositivity: StrictBool = True
        ffkaonPositivity: StrictBool = True
        ffhadronPositivity: StrictBool = True

        @field_validator('betaVersion', 'siaVersion', mode='before')
        @classmethod
        def validate_versions(cls, value):
            if type(value) is not int or value not in (0, 1):
                raise ValueError('betaVersion and siaVersion must be integer 0 or 1')
            return value


    class SeedPolicy(BaseModel):
        # Seed policy is captured in the manifest now so replica generation can
        # later be reproduced or intentionally randomized.
        mode: Literal['deterministic', 'random'] = 'deterministic'
        base_seed: int = 12345


    class Stage(BaseModel):
        # A stage is one FitPack run: choose reactions/datasets, decide which
        # non-perturbative objects are fitted or frozen, then materialize an
        # input card and job layout for that run.
        name: str
        reactions: list[str] = Field(default_factory=list)
        fit_objects: list[str] = Field(default_factory=list)
        frozen_objects: list[str] = Field(default_factory=list)
        input_template: str
        nreplicas: int = 1
        previous_stage: str | None = None
        output_dir: str | None = None
        datasets: dict = Field(default_factory=dict)
        cuts: dict = Field(default_factory=dict)
        params: dict = Field(default_factory=dict)
        qcd: QCDSettings = Field(default_factory=QCDSettings)
        job: dict = Field(default_factory=dict)

        @field_validator('name')
        @classmethod
        def validate_name(cls, value: str) -> str:
            # Trim early so stage names are safe for directory and link checks.
            value = value.strip()
            if not value:
                raise ValueError('Stage name is required')
            return value

        @field_validator('reactions', 'fit_objects', 'frozen_objects')
        @classmethod
        def normalize_string_lists(cls, values: list[str]) -> list[str]:
            # Preserve author order but remove empty strings and duplicates.
            normalized = []
            for value in values:
                item = value.strip()
                if item and item not in normalized:
                    normalized.append(item)
            return normalized

        @field_validator('nreplicas')
        @classmethod
        def validate_nreplicas(cls, value: int) -> int:
            if value < 1:
                raise ValueError('nreplicas must be at least 1')
            return value


    class Campaign(BaseModel):
        # Campaign-level defaults are shared across stages. Individual stages can
        # still override QCD/job/dataset details.
        name: str
        fitpack_root: str | None = None
        template_path: str | None = None
        campaign_root: str
        default_qcd: QCDSettings = Field(default_factory=QCDSettings)
        seed_policy: SeedPolicy = Field(default_factory=SeedPolicy)

        @field_validator('name')
        @classmethod
        def validate_name(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError('Campaign name is required')
            return value

        @field_validator('campaign_root')
        @classmethod
        def validate_campaign_root(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError('campaign_root is required')
            return value


    class CampaignDocument(BaseModel):
        # The complete YAML document: one campaign header plus a sequence of
        # stages that may point at previous stages.
        campaign: Campaign
        stages: list[Stage]

        @model_validator(mode='after')
        def validate_stage_links(self) -> 'CampaignDocument':
            # Catch broken stage graphs before any directories or input cards are
            # written to disk.
            stage_names = [stage.name for stage in self.stages]
            duplicate_names = {name for name in stage_names if stage_names.count(name) > 1}
            if duplicate_names:
                raise ValueError(f'Duplicate stage names: {sorted(duplicate_names)}')

            for stage in self.stages:
                if stage.previous_stage and stage.previous_stage not in stage_names:
                    raise ValueError(
                        f"Stage '{stage.name}' references unknown previous_stage '{stage.previous_stage}'"
                    )

            return self

        def stage_map(self) -> dict[str, Stage]:
            return {stage.name: stage for stage in self.stages}

        def campaign_root_path(self) -> Path:
            return Path(self.campaign.campaign_root).expanduser().resolve()

else:

    class _CompatModel:
        # Minimal subset of the Pydantic API used by the rest of the scaffold.
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        @classmethod
        def model_validate(cls, payload):
            if isinstance(payload, cls):
                return payload
            return cls.from_mapping(payload)

        def model_dump(self, mode: str | None = None):
            data = {}
            for key, value in self.__dict__.items():
                if isinstance(value, _CompatModel):
                    data[key] = value.model_dump(mode=mode)
                elif isinstance(value, list):
                    items = []
                    for item in value:
                        items.append(item.model_dump(mode=mode) if isinstance(item, _CompatModel) else item)
                    data[key] = items
                else:
                    data[key] = value
            return data


    def _normalize_unique_strings(values: list[str]) -> list[str]:
        # Same normalization as the Pydantic validators above, implemented
        # manually for bare Python environments.
        normalized = []
        for value in values or []:
            item = str(value).strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized


    class QCDSettings(_CompatModel):
        # Compatibility twin of the Pydantic QCDSettings model.
        def __init__(
            self,
            order='NLO',
            Q20=1.6129,
            ftol=1.0e-6,
            grid_path=None,
            tmc='AOT',
            higherTwist=False,
            dglapMode='truncated',
            asMode='backward',
            betaVersion=1,
            siaVersion=1,
            pdfPositivity=True,
            ffpionPositivity=True,
            ffkaonPositivity=True,
            ffhadronPositivity=True,
        ):
            positivity = dict(pdfPositivity=pdfPositivity, ffpionPositivity=ffpionPositivity,
                              ffkaonPositivity=ffkaonPositivity, ffhadronPositivity=ffhadronPositivity)
            if any(type(value) is not bool for value in positivity.values()):
                raise ValueError('Positivity settings must be true or false')
            if any(type(v) is not int or v not in (0, 1) for v in (betaVersion, siaVersion)):
                raise ValueError('betaVersion and siaVersion must be integer 0 or 1')
            super().__init__(
                order=order,
                Q20=float(Q20),
                ftol=float(ftol),
                grid_path=grid_path,
                tmc=tmc,
                higherTwist=bool(higherTwist),
                dglapMode=dglapMode,
                asMode=asMode,
                betaVersion=betaVersion, siaVersion=siaVersion,
                **positivity,
            )

        @classmethod
        def from_mapping(cls, payload):
            payload = payload or {}
            return cls(
                order=payload.get('order', 'NLO'),
                Q20=payload.get('Q20', 1.6129),
                ftol=payload.get('ftol', 1.0e-6),
                grid_path=payload.get('grid_path'),
                tmc=payload.get('tmc', 'AOT'),
                higherTwist=payload.get('higherTwist', False),
                dglapMode=payload.get('dglapMode', 'truncated'),
                asMode=payload.get('asMode', 'backward'),
                betaVersion=payload.get('betaVersion', 1), siaVersion=payload.get('siaVersion', 1),
                **{f'{obj}Positivity': payload.get(f'{obj}Positivity', True)
                   for obj in ('pdf', 'ffpion', 'ffkaon', 'ffhadron')},
            )


    class SeedPolicy(_CompatModel):
        # Compatibility twin of the Pydantic SeedPolicy model.
        def __init__(self, mode='deterministic', base_seed=12345):
            if mode not in {'deterministic', 'random'}:
                raise ValueError("SeedPolicy mode must be 'deterministic' or 'random'")
            super().__init__(mode=mode, base_seed=int(base_seed))

        @classmethod
        def from_mapping(cls, payload):
            payload = payload or {}
            return cls(
                mode=payload.get('mode', 'deterministic'),
                base_seed=payload.get('base_seed', 12345),
            )


    class Stage(_CompatModel):
        # Compatibility twin of the Pydantic Stage model.
        def __init__(
            self,
            *,
            name,
            reactions=None,
            fit_objects=None,
            frozen_objects=None,
            input_template,
            nreplicas=1,
            previous_stage=None,
            output_dir=None,
            datasets=None,
            cuts=None,
            params=None,
            qcd=None,
            job=None,
        ):
            name = str(name).strip()
            if not name:
                raise ValueError('Stage name is required')
            nreplicas = int(nreplicas)
            if nreplicas < 1:
                raise ValueError('nreplicas must be at least 1')
            super().__init__(
                name=name,
                reactions=_normalize_unique_strings(reactions or []),
                fit_objects=_normalize_unique_strings(fit_objects or []),
                frozen_objects=_normalize_unique_strings(frozen_objects or []),
                input_template=str(input_template),
                nreplicas=nreplicas,
                previous_stage=previous_stage,
                output_dir=output_dir,
                datasets=datasets or {},
                cuts=cuts or {},
                params=params or {},
                qcd=QCDSettings.model_validate(qcd or {}),
                job=job or {},
            )

        @classmethod
        def from_mapping(cls, payload):
            return cls(**payload)


    class Campaign(_CompatModel):
        # Compatibility twin of the Pydantic Campaign model.
        def __init__(
            self,
            *,
            name,
            campaign_root,
            fitpack_root=None,
            template_path=None,
            default_qcd=None,
            seed_policy=None,
        ):
            name = str(name).strip()
            campaign_root = str(campaign_root).strip()
            if not name:
                raise ValueError('Campaign name is required')
            if not campaign_root:
                raise ValueError('campaign_root is required')
            super().__init__(
                name=name,
                fitpack_root=fitpack_root,
                template_path=template_path,
                campaign_root=campaign_root,
                default_qcd=QCDSettings.model_validate(default_qcd or {}),
                seed_policy=SeedPolicy.model_validate(seed_policy or {}),
            )

        @classmethod
        def from_mapping(cls, payload):
            return cls(**payload)


    class CampaignDocument(_CompatModel):
        # Compatibility twin of the Pydantic CampaignDocument model, including
        # duplicate-name and previous-stage validation.
        def __init__(self, *, campaign, stages):
            campaign_model = Campaign.model_validate(campaign)
            stage_models = [Stage.model_validate(stage) for stage in stages]
            stage_names = [stage.name for stage in stage_models]
            duplicate_names = {name for name in stage_names if stage_names.count(name) > 1}
            if duplicate_names:
                raise ValueError(f'Duplicate stage names: {sorted(duplicate_names)}')
            for stage in stage_models:
                if stage.previous_stage and stage.previous_stage not in stage_names:
                    raise ValueError(
                        f"Stage '{stage.name}' references unknown previous_stage '{stage.previous_stage}'"
                    )
            super().__init__(campaign=campaign_model, stages=stage_models)

        @classmethod
        def from_mapping(cls, payload):
            return cls(campaign=payload.get('campaign', {}), stages=payload.get('stages', []))

        def stage_map(self) -> dict[str, Stage]:
            return {stage.name: stage for stage in self.stages}

        def campaign_root_path(self) -> Path:
            return Path(self.campaign.campaign_root).expanduser().resolve()
