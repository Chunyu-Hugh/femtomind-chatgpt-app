# JAM24 current unresolved status after final author clarifications

# Final author clarifications — current reproduction policy

- **Belle kaon sia:2029 is resolved.** Original FitPack dataset files, readers and theoretical calculations are authoritative. Preserve its workbook observable, normalization, dataset and theory exactly; do not investigate or change them to match paper notation. The notation difference is historical documentation only, not an active unresolved issue or reproduction blocker.
- **Gaussian initialization is confirmed.** For the future JAM24 campaign use `conf['flat par'] = True` and lowercase `gaussian=True` with the cataloged mean/std on appropriate independently fitted model parameters. Retain authoritative parameter values and bounds; do not independently sample linked or fixed parameters. Use the existing FitPack sampler unchanged and add no Gaussian template penalties.
- **Unchanged:** Q20=1.6129, all four positivity flags False, all verified parameters,150 FF links and dataset selections. Publication-only evidence remains distinct from later author clarifications.


## Current resolution register

| ID | Status | Current answer / remaining gap |
| --- | --- | --- |
| U01 | partially_resolved | All 369 literal parameter definitions and configured values recovered. Distinguish stored starts and linked/sum-rule values from an ensemble posterior; historical calibrated starting ensemble remains unknown. |
| U02 | resolved_for_configuration | Authoritative bounds and fixed/link states preserved; author-supplied Gaussian mean/std values and existing lowercase sampling procedure confirmed. No Gaussian implementation concern remains. |
| U03 | partially_resolved | Exact 98 configured reaction-qualified IDs/paths recovered. Current native readers reproduce 5853 retained points in 96 nonempty datasets; the two extra SIA files retain zero points. Exact historical data/grid revisions remain unresolved. |
| U04 | resolved_for_configuration | Latest author confirms all four positivity flags disabled. Official input has no enabling flags and current native activation is conditional. Use all four False; earlier True author statement superseded. Positivity algorithm details do not block reproduction when disabled. |
| U05 | partially_resolved | Input bootstrap=True and resample version=0 recovered. Replica count, historical seeds/resampling implementation, convergence and ensemble selection remain unknown. |
| U06 | resolved_intended_initialization_procedure | Use flat par=True and lowercase gaussian=True with cataloged mean/std for appropriate independent model parameters, retaining bounds and links. No Gaussian template penalties or sampling-code changes. Historical ensemble/stage-transfer records, if sought, are separate provenance questions, not an unresolved initialization implementation. |
| U07 | resolved_for_current_data_configuration | 52 normalization declarations resolve to 43 free plus 9 links; starts/bounds, exact SLD groups and current workbook relative uncertainties recovered. Eight nominally unpenalized cases use finite dN approximately 1000. Historical data revisions remain part of U03. |
| U08 | resolved_for_configuration | Both shapes including gluon independently confirmed; all 150 coefficient links match flavor equalities and targets. Exact current template/total-hadron implementation inspected; historical code revision still belongs to U09. |
| U09 | partially_resolved | Official flags NLO,truncated,backward,AOT,nuc=True,ht=False,offpdf=False and selectors recovered. Current native defaults inspected separately; exact historical implementation/grid hashes and remaining inherited settings unresolved. |
| U10 | partially_resolved | Exact SIA cut windows and current reader behavior recorded. Belle kaon notation question is closed: preserve original FitPack implementation without further investigation. Historical data/runtime identity is covered separately by U03/U09. |
| U11 | partially_resolved | Input ftol=1e-3 recovered. Exact historical minimizer, convergence/caps and numerical integration settings remain unknown; do not substitute current app defaults. |
| U12 | unresolved | Input does not resolve exact credible-interval tail allocation, quantile estimator or ensemble weights. |
| U13 | partially_resolved | Publication annotations retained as historical documentation. Belle kaon difference is accepted and closed under original FitPack authority; no further observable investigation or correction is planned. |

## Remaining priorities

1. Historical runtime/data/grid identity, replica count and seeds, resampling/selection and convergence details.
2. Exact interval construction and historical ensemble/stage-transfer records where needed.
3. Review the existing application patch before promotion. Belle kaon notation and the FitPack Gaussian sampling mechanism are not investigation tasks or reproduction blockers.

## Original publication-only unresolved layer (historical, preserved)

# JAM24 unresolved settings

> This catalog is not executable or reproduction-ready. No missing scientific value has been supplied from app defaults or old configurations.

Priority: P0 prevents a faithful concrete campaign; P1 affects precise model/theory/numerical equivalence; P2 concerns source clarification/benchmark interpretation. Each item has a null value in the JSON, an evidence status and a later verification route. No access to those later sources is authorized or attempted by this reconstruction.

## U01 [P0] Exact numerical parameter starts and fitted model coefficient tables

Evidence status: **not specified**. Value: unknown.

What: No numerical template coefficients are given for the final fit. Values and source basis/moment conventions must be recovered without adopting app library numbers.

Why / FEMTOMIND need: Required to construct a concrete campaign and faithfully reproduce calibration.

Potential later verification: Original final JAM24 input/parameter snapshots and author validation.

## U02 [P0] Bounds and Gaussian hyperparameters

Evidence status: **not specified**. Value: unknown.

What: No numerical min/max or Gaussian mu/sigma for template coefficients are tabulated. Model calibration priors cannot be instantiated.

Why / FEMTOMIND need: App requires bounds for FREE parameters; Gaussian metadata also needs explicit values.

Potential later verification: Author calibration configurations and stage logs.

## U03 [P0] Dataset file IDs, exact releases and point/covariance arrays

Evidence status: **not specified**. Value: unknown.

What: Paper names 96 rows but no application XLSX IDs/paths/checksums. HERA subenergy mappings and duplicate STAR jet rows are not fully resolved. Corrected h-minus SIDIS uncertainties and BaBar covariance must match.

Why / FEMTOMIND need: Exact data selection, alignment, cuts, correlations and accepted counts depend on these.

Potential later verification: Author data manifest/covariances and experimental tables, in a later approved stage.

> Historical paper-only entry below: U04 is now resolved by the latest author correction (all four flags False). Its old app mandatory-True observation predates the new local controls. The original paper-only unknown is retained solely for provenance.

## U04 [P0] Positivity policy and algorithm

Evidence status: **not specified**. Value: unknown.

What: No positivity constraint prescription, evaluation domain, penalty strength or per-object policy is stated. Absence of discussion is not evidence positivity is disabled.

Why / FEMTOMIND need: App forces all four positivity flags True; this cannot be declared paper matched.

Potential later verification: Original final input and native implementation, only after separate permission.

## U05 [P0] Final MC replica count and resampling construction

Evidence status: **not specified**. Value: unknown.

What: Data resampling is named but sample count, random seeds, uncorrelated/correlated fluctuations, failed-fit handling and final ensemble selection are absent.

Why / FEMTOMIND need: App needs replica count, bootstrap and initialization choices; errors depend on the sampling design.

Potential later verification: Author ensemble manifest, sampling driver and final run logs.

## U06 [P0] Calibration transfer and final starting ensemble

Evidence status: **unclear**. Value: unknown.

What: Qualitative stages and posterior-to-prior transfer are described, but stage ordering/details, transfer mechanics and whether Gaussian sampling remains in final starts are not fully stated.

Why / FEMTOMIND need: A single library/default or uniform-random start is not a validated reconstruction of the multistep method.

Potential later verification: Original staged driver, stage configurations and calibrated parameter samples.

## U07 [P0] Full 43-normalization input configuration

Evidence status: **not specified**. Value: unknown.

What: Fitted entries/shared groups and eight unpenalized cases are identified, but numerical deltaN experimental priors, starts, bounds and all coding conventions are absent. Printed posterior uncertainties cannot replace deltaN.

Why / FEMTOMIND need: App has canonical SIA norm definitions and no normalizer-penalty control; mismatch could change objective.

Potential later verification: Original normalization configuration and quoted experimental systematic sources.

## U08 [P1] Precise FF two-shape implementation and links

Evidence status: **unclear**. Value: unknown.

What: Quark two-shape statement and 60-per-species count are known; gluon two shapes is count-derived. Exact implementation basis, shape summation, heavy-flavor boundary details and coefficient links need confirmation.

Why / FEMTOMIND need: App has optional shapes and default ties that do not automatically reproduce all functional equalities.

Potential later verification: Author model specification and final FF parameter map.

## U09 [P1] Remaining perturbative and nuclear/power-correction choices

Evidence status: **not specified**. Value: unknown.

What: No per-process scale prescriptions beyond Wcharm/SIDIS, MCFM version/grids, full electroweak/CKM inputs, nuclear/off-shell model, exact TMC/HT switches or heavy-matching details are fixed.

Why / FEMTOMIND need: Most are inherited rather than exposed in current composer.

Potential later verification: Original theoretical configuration and grids; cited primary experimental/theory references if authorized.

## U10 [P1] SIA high/low-energy classification and variable conversions

Evidence status: **unclear**. Value: unknown.

What: The three numerical z windows are explicit, but no numeric energy threshold or executable row assignment, xp/p/xi conversion and bin integration conventions are supplied.

Why / FEMTOMIND need: SIA cuts are canonical in app and cannot be freely overwritten by a paper catalog.

Potential later verification: Author SIA data map/readers and cited experimental definitions.

## U11 [P1] Optimizer, convergence and numerical settings

Evidence status: **not specified**. Value: unknown.

What: No named final minimizer, tolerances, evaluation caps, convergence test, integration accuracy or profiling solver is provided.

Why / FEMTOMIND need: App uses SciPy trf/dogbox and max_nfev; successful execution is not a converged reproduction.

Potential later verification: Author optimization code/configuration/logs.

## U12 [P1] Exact credible-interval convention

Evidence status: **unclear**. Value: unknown.

What: 95% inverse-CDF intervals and mean central values are stated; tail allocation, quantile algorithm and ensemble weights are not fixed.

Why / FEMTOMIND need: App uses equal-tailed pointwise intervals; exact paper correspondence is unverified.

Potential later verification: Author analysis/quantile specification.

## U13 [P2] Publication ambiguities and provenance

Evidence status: **unclear**. Value: unknown.

What: Repeated strange-asymmetry variable in p12 prose, second ATLAS charge in Table X, DELPHI(uds) p/xp-like variable in Table XIV, PDF versus HTML dates, and Rs upper uncertainty +.22 versus +.23 need explicit resolution.

Why / FEMTOMIND need: Avoid silently repairing labels or using a diagnostic 3.5GeV2 HERA scenario as final.

Potential later verification: Author errata/clarification; keep original printed facts in this preliminary entry.

## What must remain separate

1. Paper statements and count/formula deductions remain in scientific_settings, with evidence locators.
2. Original code/configuration/data would be a new evidence source, not a retroactive paper statement.
3. Current application defaults/capabilities describe what the composer does, not what JAM24 did.
4. Posterior fitted results (including norm(error), chi-squared and plotted Rs) are benchmarks, not initialization, priors or settings.

## Recommended sequence after user review

1. Resolve source ambiguities and confirm the final-model basis/two-shape count with the authors.
2. Obtain the exact final data manifest, corrections/covariance payloads, coefficient definitions, normalization priors and calibration/final-run snapshots under separate authorization.
3. Record all recovered values with new source IDs; compare against this immutable paper version and the 5853-point/258-parameter checks.
4. Audit exposed versus inherited application choices, especially positivity, alpha_s/masses/matching, canonical SIA cuts/norms, light-flavor FF links and calibration orchestration.
5. Only after approval and complete mapping, validate a proposed campaign without submission. No input generation or fit is authorized at this stage.


