# Final author clarifications — current reproduction policy

- **Belle kaon sia:2029 is resolved.** Original FitPack dataset files, readers and theoretical calculations are authoritative. Preserve its workbook observable, normalization, dataset and theory exactly; do not investigate or change them to match paper notation. The notation difference is historical documentation only, not an active unresolved issue or reproduction blocker.
- **Gaussian initialization is confirmed.** For the future JAM24 campaign use `conf['flat par'] = True` and lowercase `gaussian=True` with the cataloged mean/std on appropriate independently fitted model parameters. Retain authoritative parameter values and bounds; do not independently sample linked or fixed parameters. Use the existing FitPack sampler unchanged and add no Gaussian template penalties.
- **Unchanged:** Q20=1.6129, all four positivity flags False, all verified parameters,150 FF links and dataset selections. Publication-only evidence remains distinct from later author clarifications.

# Current JAM24 author-confirmed configuration (2026-10-19 correction)

**Use Q20=1.6129 and disable all four positivity flags for JAM24 reproduction.** The original numerical input is authoritative. The paper-derived 1.6384 remains a documented source difference, not a value to fix in the input. The earlier author statement that positivity was enabled has been explicitly superseded; its historical record remains in JSON.

Historical Gaussian initialization was external; no Gaussian template-parameter penalties are to be added to the final objective. The original publication-only findings and citations below remain an identifiable historical evidence layer.

SIDIS effective configured criteria are Q²>1.69 and W²>10. Native-reader checks show W²SIDIS>0 and 0.2<z<0.8 remove no additional points. More precisely, W²>10 also removes none from these supplied files: only Q² removes rows, taking 1862 to1490. Jet x<1 has no effect on the selected data.

New FEMTOMIND campaigns continue to default all four positivity options to True. Each is now independently configurable; a future JAM24 campaign can explicitly supply all four False. The implementation is local and uncommitted, not deployed.

---

> Evidence-layer notice (2026-10-19): the original paper-only findings below are preserved as Phase 1. The Phase 2 section adds official-input, author and current-implementation evidence. Historical unknowns in Phase 1 are updated in the Phase 2 resolution register; the catalog remains preliminary.

# JAM24 final combined analysis: paper findings

> Preliminary publication-only reconstruction. Not reproduction-ready and not a FEMTOMIND campaign card. No input, campaign, replica or fit has been generated.

## Scope, evidence and source policy

Target: the final combined **JAM24** scenario, not the baseline, +SIDIS or +W-charm alternatives. The latter are mentioned only where calibration dependencies or diagnostic comparisons affect reproduction. [P, IV introduction, p11; IV.A, p13]

[P] Trey Anderson, W. Melnitchouk and N. Sato, *Strangeness in the proton from W+charm production and SIDIS data*, [arXiv:2501.00665v3](https://arxiv.org/abs/2501.00665v3), fixed revision 15 Oct 2025; [PDF](https://arxiv.org/pdf/2501.00665v3). All 40 pages, Appendix A, tables, equations, figures and bibliography were reviewed. PDF printed pages equal PDF page indices. No separately linked publication-owned supplement was identified; cited earlier papers are not used to import missing scientific settings.

PDF SHA256: `c612bf3f049365c820388ce4ab533a3c336bf4eebffb82b4903e5870c6f8779b`. The PDF title date is 17 Oct 2025; experimental HTML displays 24 Aug 2026, differing from the arXiv history. The entry is pinned to v3 and checksum, not that HTML date.

Scientific evidence labels: **Explicit** = direct paper statement; **Derived** = stated inputs plus recorded deduction; **Unclear** = discussed but insufficiently specified; **Not specified** = absent required detail in the reviewed publication. Application observations have a separate evidence domain. No FitPack source, existing input/campaign configurations or previous fit settings were consulted to fill gaps. Only the local application source was inspected for capabilities.

The JSON uses a reusable claim-based schema: stable IDs, typed values, evidence labels and source locators. Unknown values are null, with unresolved-item records. Publication-row keys are catalog identifiers, not invented XLSX IDs. No new MCP catalog service is implemented.

## Scientific configuration ledger

The following is the human-readable view of the scientific claims in `analysis_config.json`. Each explicit/derived entry includes a locator; deductions and limits are stated separately.

### target.final [Explicit]

{"name": "JAM24", "simultaneous_fit": ["proton PDFs", "pion FFs", "kaon FFs", "residual-hadron FFs"], "data_scope": "baseline + SIDIS + W+charm"}

Evidence: [P] IV introduction, p11 / IV.A, p13.

### theory.order [Explicit]

NLO

Evidence: [P] II.A, p4,5,6 / IV.D, p18; Table III.

### theory.factorization [Explicit]

collinear QCD factorization

Evidence: [P] II.A, p4,5,6.

### theory.scheme [Explicit]

MSbar

Evidence: [P] II.A, p4,5,6.

### theory.evolution [Explicit]

{"equations": "DGLAP", "accuracy": "next-to-leading logarithmic", "heavy_flavor_scheme": "zero-mass variable flavor"}

Evidence: [P] II.A, p4,5,6.

### theory.alpha_s [Explicit]

{"alpha_s": 0.118, "reference_mass_GeV": 91.18, "reference_particle": "Z", "running": "numerical QCD beta functions"}

Evidence: [P] II.A, p4,5,6.

### theory.heavy_masses [Explicit]

{"charm_GeV": 1.28, "bottom_GeV": 4.18}

Evidence: [P] II.A, p4,5,6.

Limit: Values are the printed thresholds; mass-scheme and matching implementation details are not supplied.

### model.input_scale [Explicit]

{"PDF_mu0_GeV": 1.28, "FF_mu0_GeV": 1.28, "definition": "mu0 = mc"}

Evidence: [P] II.A, p4,5,6.

### model.input_scale_squared [Derived]

{"Q0_squared_GeV2": 1.6384}

Evidence: [P] II.A, p4,5,6.

Derivation: Square the printed mc=1.28 GeV: 1.28^2=1.6384 GeV^2. Do not substitute app Q20=1.6129.

### theory.scales_wc [Explicit]

{"mu_F": "MW", "mu_R": "MW", "partonic_program": "MCFM", "program_version": null}

Evidence: [P] II.A, p5; Eq 2.

Limit: MCFM is named; no version or full integration/grid settings are given.

### theory.scales_sidis [Explicit]

{"mu_F": "Q", "mu_R": "Q", "partonic_accuracy": "NLO"}

Evidence: [P] II.A, p5; Eq 4.

### model.isospin [Explicit]

{"PDF_example": "u/p = d/n", "assumption": "isospin symmetry for PDFs and FFs"}

Evidence: [P] II.B, p6,7,8; Eq 5,6,7,8,9,10,11,12,13.

### model.template [Explicit]

{"function": "f(t)=N/M * t^alpha * (1-t)^beta * (1+gamma*sqrt(t)+delta*t)", "M": "B(alpha+2,beta+1)+gamma*B(alpha+5/2,beta+1)+delta*B(alpha+3,beta+1)", "PDF_variable": "x", "FF_variable": "z", "coefficients": ["N", "alpha", "beta", "gamma", "delta"]}

Evidence: [P] II.B, p6,7; Eq 5.

### model.N_moment [Derived]

N equals the first momentum moment integral_0^1 dt t f(t), when convergent

Evidence: [P] II.B, p6; Eq 5.

Derivation: Integrate t times Eq.(5); the resulting three beta integrals equal the printed denominator M. N is not in general the number integral.

### pdf.basis [Explicit]

{"templates": ["u_v", "d_v", "g", "S1", "S2", "delta_ubar", "delta_dbar", "delta_s", "delta_sbar"], "relations": {"u_v": "u-ubar", "d_v": "d-dbar", "ubar": "S1+delta_ubar", "dbar": "S1+delta_dbar", "s": "S2+delta_s", "sbar": "S2+delta_sbar"}}

Evidence: [P] II.B, p6; Eq 6,7.

### pdf.final_sea [Explicit]

{"S1_S2": "independent", "s_sbar": "independently parametrized subject to zero net strangeness"}

Evidence: [P] II.B, p6,7; Eq 7,8 / IV.D, p22.

Limit: A result consistent with s-sbar=0 is not an imposed s=sbar constraint.

### pdf.fixed_polynomial [Explicit]

{"gamma_delta_zero": ["S2", "delta_s", "delta_sbar"], "gamma_delta_free": ["u_v", "d_v", "g", "S1", "delta_ubar", "delta_dbar"]}

Evidence: [P] IV.A, p12.

### pdf.sum_rules [Explicit]

{"number_integrals": {"u_v": 2, "d_v": 1, "s-sbar": 0}, "momentum_integral": 1, "determined_normalizations": ["u_v", "d_v", "delta_s", "g"]}

Evidence: [P] II.B, p7; Eq 8,9.

### pdf.free_parameter_count [Explicit]

{"total": 35, "shape": 30, "normalization": 5}

Evidence: [P] IV.A, p12.

Limit: Stated for calibrated baseline and retained in final 215-model-parameter construction.

### pdf.count_crosscheck [Derived]

35

Evidence: [P] II.B, p6,7,8; Eq 5,6,7,8,9,10,11,12,13 / IV.A, p11,12,13.

Derivation: Six full 5-coefficient templates plus three 3-coefficient templates give 39; four sum-rule normalizations leave 35. The remaining five N and 30 shape coefficients agree with the stated count.

### ff.flavor_relations [Explicit]

{"pion": {"independent": ["g", "u", "d", "s", "c", "b"], "equalities": ["u=dbar", "d=ubar", "s=sbar", "c=cbar", "b=bbar"]}, "kaon": {"independent": ["g", "u", "sbar", "d", "c", "b"], "equalities": ["d=ubar=dbar=s", "c=cbar", "b=bbar"]}, "residual": {"independent": ["g", "u", "d", "s", "c", "b"], "equalities": ["ubar=dbar=sbar=s", "c=cbar", "b=bbar"]}}

Evidence: [P] II.B, p7; Eq 10,11,13.

Limit: Relations are functional equalities. A numerical coefficient-by-coefficient link map still needs translation into implementation names.

### ff.charge_conjugation [Explicit]

D_q^(h-) = D_qbar^(h+) for all flavors

Evidence: [P] II.B continuation, p8.

### ff.total_hadron [Explicit]

D_q^(h+) = D_q^(pi+) + D_q^(K+) + D_q^(res+)

Evidence: [P] II.B, p7; Eq 12.

Limit: The residual term is the third parametrized component, not an additional independently fitted total-hadron FF.

### ff.two_quark_shapes [Explicit]

{"templates_per_independent_quark_function": 2}

Evidence: [P] IV.A, p12.

Limit: Both shapes are part of the published model; optional app shapes must not be left absent merely because defaults omit them.

### ff.parameters [Explicit]

{"per_species": 60, "normalizations_per_species": 12, "shape_parameters_per_species": 48, "species_count": 3, "total": 180}

Evidence: [P] IV.A, p12,13.

### ff.gluon_shape_count [Derived]

2

Evidence: [P] II.B, p7 / IV.A, p12,13.

Derivation: Six independent functions per species, five independent coefficients per template, and 60 free coefficients require 12 full templates. The five quark functions use 10; the remaining two are gluon templates, assuming the stated complete parameter accounting.

Limit: The prose explicitly says two templates for quarks but does not separately say this for the gluon. Treat the count-based inference as needing author/configuration confirmation.

### fit.model_parameter_count [Explicit]

{"pdf": 35, "ff": 180, "model_total": 215, "dataset_normalizations": 43, "reported_total": 258}

Evidence: [P] IV.A, p13.

Limit: The paper reports 258; do not subtract this from Ndat in its Eq.(15), or presume every analytically handled nuisance r is counted in 258.

### calibration.stages [Explicit]

[{"stage": "PDF DIS calibration", "condition": "symmetric sea: S1=S2 and all delta_f=0"}, {"stage": "add DY", "condition": "release delta_ubar and delta_dbar"}, {"stage": "add W/Z and jets", "condition": "incremental optimization"}, {"stage": "release strange-sector freedom", "condition": "S2 independent of S1; asymmetry wording contains a typo"}, {"stage": "FF calibration", "condition": "SIA-only calibration, two-template quark FFs"}, {"stage": "final JAM24", "condition": "use calibrated starting distributions and include baseline+SIDIS+Wcharm; remove template-parameter Gaussian penalties"}]

Evidence: [P] IV.A, p11,12,13.

Limit: This is a dependency description, not a runnable stage schedule; exact substage order, transfer and numerical starts remain unknown.

### calibration.gaussian [Explicit]

{"initial_sampling": "Gaussian template-parameter priors", "penalty": "sum_i ((a_i-mu_i)/sigma_i)^2", "hyperparameter_values": null}

Evidence: [P] IV.A, p11,12.

### calibration.final_template_penalty [Explicit]

false

Evidence: [P] IV.A, p13.

Limit: Only template calibration penalties are removed; experimental additive and normalization penalties remain.

### statistics.modified_theory [Explicit]

T_hat_i,e = T_i,e/N_e + sum_k r_e,k beta_i,e,k

Evidence: [P] IV.B, p13; Eq 14.

### statistics.experimental_penalties [Explicit]

{"additive": "sum_e,k r_e,k^2", "normalization": "sum_e (1-N_e)^2/(deltaN_e)^2", "unpenalized_exception": "SIA rows marked *"}

Evidence: [P] IV.B, p13,14; Eq 14,15,16 / Appendix A, p34; Table XII.

### statistics.correlated_scaling [Explicit]

{"theory_rescale_beta_by_T_over_D": false, "COMPASS_source": "point-to-point correlated, not dataset normalization"}

Evidence: [P] III, p8 / IV.B, p13.

### statistics.uncorrelated_errors [Explicit]

alpha_i,e is the quadrature sum of quoted uncorrelated point-by-point uncertainties

Evidence: [P] IV.B, p14; Eq 15.

### statistics.objective [Derived]

sum_i,e ((d_i,e-T_hat_i,e)/alpha_i,e)^2 + experimental nuisance penalties; template calibration penalty absent in final runs

Evidence: [P] IV.B, p13,14; Eq 14,15,16 / IV.A, p13.

Derivation: Combine the residual structure in Eq.(15) with the optimized nuisance parameters and penalties stated around Eq.(14), then apply the explicit final-stage removal of template penalties. Eq.(15) itself is a post-fit expectation-value diagnostic, not this complete objective.

Limit: Conceptual least-squares reconstruction only; exact implementation/profiling, covariance handling and optimizer details remain unresolved.

### statistics.reported_fit_quality [Explicit]

{"reduced_chi2": "sum_i,e ((d_i,e-E[T_hat_i,e])/alpha_i,e)^2 / Ndat", "Z": "Phi^-1(p), p from chi-square with Ndat degrees of freedom", "final_reduced_chi2": 0.99, "final_Z": -0.51}

Evidence: [P] IV.B, p13,14; Eq 14,15,16 / IV.D, p16; Table II.

### statistics.replica_method [Explicit]

multi-step Monte Carlo Bayesian reconstruction using data resampling for optimization

Evidence: [P] IV.A, p11.

Limit: No exact bootstrap distribution/correlated-resampling formula, seeds, fit-selection rule or final replica count is supplied.

### statistics.interval [Explicit]

{"coverage_percent": 95, "central_value": "ensemble mean of observable", "endpoints": "nonparametric inverse CDF estimate"}

Evidence: [P] IV.A, p12 / IV.E, p24,26,27,28; Fig 8,9,10,11.

### statistics.interval_tail [Unclear]

null

Evidence: [P] IV.A, p12.

Limit: Equal-tailed versus shortest/HPD interval and exact quantile estimator are not explicitly fixed. Do not label app equal-tailed implementation as paper-verified.

### data.final_counts [Explicit]

{"retained_points": 5853, "SIDIS_points": 1490, "SIA_points": 1090, "Wcharm_points": 37}

Evidence: [P] IV.D, p16; Table II.

Limit: Point totals are explicitly reported; input file IDs are not provided.

### data.final_row_counts [Derived]

{"rows": 96, "SIDIS_rows": 6, "SIA_rows": 53, "SIA_pion_rows": 20, "SIA_kaon_rows": 18, "SIA_hadron_rows": 15}

Evidence: [P] Appendix A, p31,32,33,34,35,36; Table IV,V,VI,VII,VIII,IX,X,XI,XII,XIII,XIV.

Derivation: Count the rows of the complete final Appendix dataset tables. These are publication-row counts, not numbers of unique experimental collaborations or inferred XLSX IDs.

### data.total_before [Derived]

7794

Evidence: [P] Appendix A, p31,32,33,34,35,36.

Derivation: Sum before-cut counts across all 96 Appendix rows; retained row counts sum to 5853.

### data.cut_DIS [Explicit]

{"Q2_GeV2": {"op": ">", "value": "mc^2"}, "W2_GeV2": {"op": ">", "value": 10}}

Evidence: [P] III; IV.C; IV.D, p9,14,15,16; Table I,II.

Limit: Nominal mc^2 cut retained for final HERA1185 points; Q2>3.5 comparison is not final.

### data.cut_SIDIS [Explicit]

{"additional_analysis_cut": "Q2 > mc^2", "target": "deuteron", "native_coverage": {"z_h": "0.2<z_h<0.8", "W2_SIDIS_GeV2": ">7"}, "p_hT_for_W2_Breit_GeV": 0}

Evidence: [P] IV.C, p14,15 / Appendix A, p33; Table XI.

Limit: Coverage bounds are not new extra reader cuts. No proton SIDIS row belongs to the final list.

### data.cut_SIA [Explicit]

{"high_energy": "0.02<z_h<0.9", "low_energy": "0.15<z_h<0.9", "BaBar_kaon": "0.20<z_h<0.9"}

Evidence: [P] III; IV.C, p9,15; Table I.

Limit: A numerical high/low-energy boundary and complete per-row cut assignment are not supplied.

### data.cut_jet [Explicit]

{"pT_GeV": {"op": ">", "value": 8}}

Evidence: [P] III, p9; Table I.

Limit: No extra x<1 filter is stated by this paper.

### data.fiducial_Wcharm [Explicit]

{"ATLAS7TeV_lepton_pT_GeV": ">20", "CMS7TeV_sum_and_ratio_lepton_pT_GeV": ">25", "CMS13TeV_lepton_pT_GeV": ">26"}

Evidence: [P] IV.D, p19; Fig 3.

Limit: Observable fiducial definitions shown in figure panels, not invented XLSX row filters. Full acceptance/integration definitions remain incomplete.

### data.fiducial_STAR [Explicit]

{"sqrt_s_GeV": 510, "lepton_pT_GeV": ">25"}

Evidence: [P] IV.D, p23; Fig 7.

### data.other_cuts [Not Specified]

null

Evidence: [P] III; IV.C; IV.D, p9,14,15,16; Table I,II.

Limit: Table I leaves DY/W/Z/Wcharm additional analysis-cut cells blank. This does not imply absence of detector fiducial requirements or justify inserting Q2>0 as a paper statement.

### data.normalization_structure [Derived]

{"independent": 43, "SIA_unpenalized": 8, "SLD_groups": 3, "SLD_rows_sharing": 12, "normalizations_by_process": {"DIS": 6, "Drell-Yan": 3, "W-lepton asymmetry": 0, "W charge asymmetry": 0, "Z rapidity": 1, "inclusive jet": 4, "W+charm": 3, "SIDIS": 0, "SIA pion": 10, "SIA kaon": 9, "SIA unidentified hadron": 7}}

Evidence: [P] IV.A, p13 / Appendix A, p31,32,33,34,35,36.

Derivation: There are 52 non-dash fitted-normalization rows; the four SLD rows within each of three hadron species share one normalization, removing nine repeated entries: 52-9=43. Eight pion/kaon Belle and TASSO12/14/22 rows carry the unpenalized marker.

### data.normalization_symbols [Explicit]

{"asterisk": "unpenalized fitted normalization", "dagger": "shared normalization", "star": "systematic covariance matrix supplied"}

Evidence: [P] Appendix A, p34,35,36; Table XII,XIII,XIV.

Limit: These publication symbols are unrelated to FEMTOMIND commented-out-dataset UI asterisks. Fitted norm(error) table values are output diagnostics, not starts or deltaN priors.

### data.hminus_error_correction [Explicit]

Current analysis corrects an earlier overestimate of h-minus SIDIS uncorrelated uncertainty

Evidence: [P] IV.D, p18.

Limit: Exact corrected per-point arrays are absent; a matching dataset-file revision must later be verified.

### data.not_in_final [Derived]

["neutrino-nucleus DIS", "inclusive pp->hX", "lattice moments or pseudo-ITDs", "Hall C SIDIS", "future JLab/EIC SIDIS"]

Evidence: [P] I, p2,3 / IV.C, p15 / V, p30 / Appendix A, p31,32,33,34,35,36.

Derivation: These processes are absent from the complete final dataset ledger; their contextual or future-work mentions do not add them to that ledger.

Limit: Context, cut caveat or future work; these are not extra final JAM24 fit components.

### output.reference_scales [Explicit]

{"PDF_Q2_GeV2": 4, "FF_Q2_GeV2": 100}

Evidence: [P] IV.E, p26,27; Fig 9,10.

Limit: Plot scales, not input scales or fitting scales.

## Full final dataset ledger

Every final Appendix row is listed below. `Ncor`, posterior fitted normalization and chi-squared/Z values are retained in the JSON as reported diagnostics. Posterior normalization errors are not experimental deltaN priors. IDs/filepaths remain unknown. TableXII symbols: *=unpenalized free normalization; dagger=shared; star=systematic covariance. These are not the app commented-out-dataset markers.

| Source row / printed page | Process | Experiment | Observable (TeX) | Accepted / original | Ncor | Reported fitted norm |
|---|---|---|---|---:|---|---|
| [JAM24.T04.R01](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | SLAC [39] | `F_{2}^{p}` | 218/661 | — | 0.984(9) |
| [JAM24.T04.R02](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | SLAC [39] | `F_{2}^{d}` | 228/692 | — | 0.976(9) |
| [JAM24.T04.R03](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | BCDMS [40] | `F_{2}^{p}` | 348/351 | 5 | 0.970(6) |
| [JAM24.T04.R04](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | BCDMS [40] | `F_{2}^{d}` | 254/254 | 5 | 0.986(8) |
| [JAM24.T04.R05](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | NMC [41] | `F_{2}^{p}` | 273/292 | 11 | 1.017(10) |
| [JAM24.T04.R06](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | NMC [42] | `F_{2}^{d}/F_{2}^{p}` | 174/260 | 5 | 0.997(6) |
| [JAM24.T04.R07](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA NC e^{+} (1) [43] | `\sigma^{p}_{\rm red}` | 402/485 | 169 | — |
| [JAM24.T04.R08](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA NC e^{+} (2) [43] | `\sigma^{p}_{\rm red}` | 75/112 | 169 | — |
| [JAM24.T04.R09](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA NC e^{+} (3) [43] | `\sigma^{p}_{\rm red}` | 259/260 | 169 | — |
| [JAM24.T04.R10](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA NC e^{+} (4) [43] | `\sigma^{p}_{\rm red}` | 209/209 | 169 | — |
| [JAM24.T04.R11](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA NC e^{-} [43] | `\sigma^{p}_{\rm red}` | 159/159 | 169 | — |
| [JAM24.T04.R12](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA CC e^{+} [43] | `\sigma^{p}_{\rm red}` | 39/39 | 169 | — |
| [JAM24.T04.R13](https://arxiv.org/html/2501.00665v3#A1.T4) / 31 | DIS | HERA CC e^{-} [43] | `\sigma^{p}_{\rm red}` | 42/42 | 169 | — |
| [JAM24.T05.R01](https://arxiv.org/html/2501.00665v3#A1.T5) / 32 | Drell-Yan | E866 (NuSea) [2] | `M^{3}\differential^{2}\sigma/\differential M\,\differential x_{F}` | 184/184 | — | 0.961(28) |
| [JAM24.T05.R02](https://arxiv.org/html/2501.00665v3#A1.T5) / 32 | Drell-Yan | E866 (NuSea) [2] | `\differential\sigma^{p/d}/2\,\differential\sigma^{pp}` | 15/15 | — | 0.990(13) |
| [JAM24.T05.R03](https://arxiv.org/html/2501.00665v3#A1.T5) / 32 | Drell-Yan | E906 (SeaQuest) [3] | `\differential\sigma^{p/d}/2\,\differential\sigma^{pp}` | 6/6 | 1 | 1.012(10) |
| [JAM24.T06.R01](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | CMS 8~TeV [23] | `A_{\mu}` | 11/11 | 6 | not tabulated |
| [JAM24.T06.R02](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | CMS 7~TeV [21] | `A_{\mu}` | 11/11 | 12 | not tabulated |
| [JAM24.T06.R03](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | CMS 7~TeV [20] | `A_{e}` | 11/11 | 2 | not tabulated |
| [JAM24.T06.R04](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | CMS 7~TeV [19] | `A_{e}` | 6/6 | 2 | not tabulated |
| [JAM24.T06.R05](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | CMS 7~TeV [19] | `A_{\mu}` | 6/6 | 2 | not tabulated |
| [JAM24.T06.R06](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | LHCb 7~TeV [46] | `A_{\mu}` | 8/8 | — | not tabulated |
| [JAM24.T06.R07](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | LHCb 8~TeV [47] | `A_{\mu}` | 8/8 | 1 | not tabulated |
| [JAM24.T06.R08](https://arxiv.org/html/2501.00665v3#A1.T6) / 32 | W-lepton asymmetry | STAR [50] | `\sigma^{W^{+}}/\sigma^{W^{-}}` | 9/9 | — | not tabulated |
| [JAM24.T07.R01](https://arxiv.org/html/2501.00665v3#A1.T7) / 32 | W charge asymmetry | CDF [49] | `A_{W}` | 13/13 | 5 | not tabulated |
| [JAM24.T07.R02](https://arxiv.org/html/2501.00665v3#A1.T7) / 32 | W charge asymmetry | DØ [48] | `A_{W}` | 14/14 | — | not tabulated |
| [JAM24.T08.R01](https://arxiv.org/html/2501.00665v3#A1.T8) / 32 | Z rapidity | CDF [52] | `\differential\sigma/\differential y` | 28/28 | 1 | 0.937(12) |
| [JAM24.T08.R02](https://arxiv.org/html/2501.00665v3#A1.T8) / 32 | Z rapidity | DØ [51] | `\sigma^{-1}\differential\sigma/\,\differential y` | 28/28 | 1 | — |
| [JAM24.T09.R01](https://arxiv.org/html/2501.00665v3#A1.T9) / 33 | inclusive jet | DØ [53] | `\differential^{2}\sigma/\differential\eta\,\differential p_{T}` | 110/110 | 22 | 1.137(34) |
| [JAM24.T09.R02](https://arxiv.org/html/2501.00665v3#A1.T9) / 33 | inclusive jet | CDF [54] | `\differential^{2}\sigma/\differential\eta\,\differential p_{T}` | 76/76 | 20 | 1.056(37) |
| [JAM24.T09.R03](https://arxiv.org/html/2501.00665v3#A1.T9) / 33 | inclusive jet | STAR [55] | `\differential^{2}\sigma/2\pi\,\differential\eta\,\differential p_{T}` | 3/5 | 1 | ​​1.013(3) |
| [JAM24.T09.R04](https://arxiv.org/html/2501.00665v3#A1.T9) / 33 | inclusive jet | STAR [55] | `\differential^{2}\sigma/2\pi\,\differential\eta\,\differential p_{T}` | 9/9 | 1 | 1.101(16) |
| [JAM24.T10.R01](https://arxiv.org/html/2501.00665v3#A1.T10) / 33 | W+charm | ATLAS 7 TeV [26] | `\differential\sigma^{W^{-}+c}/\differential\|\eta\|` | 11/11 | 86 | 0.995(55) |
| [JAM24.T10.R02](https://arxiv.org/html/2501.00665v3#A1.T10) / 33 | W+charm | ATLAS 7 TeV [26] | `\differential\sigma^{W^{-}+\bar{c}}/\differential\|\eta\|` | 11/11 | 86 | 0.994(54) |
| [JAM24.T10.R03](https://arxiv.org/html/2501.00665v3#A1.T10) / 33 | W+charm | CMS 7 TeV [25] | `\differential\sigma^{W+c}/\differential\|\eta\|` | 5/5 | 5 | — |
| [JAM24.T10.R04](https://arxiv.org/html/2501.00665v3#A1.T10) / 33 | W+charm | CMS 7 TeV [25] | `\sigma^{W^{+}+\bar{c}}/\sigma^{W^{-}+c}` | 5/5 | — | — |
| [JAM24.T10.R05](https://arxiv.org/html/2501.00665v3#A1.T10) / 33 | W+charm | CMS 13 TeV [27] | `\differential\sigma^{W+c}/\differential\|\eta\|` | 5/5 | 12 | 1.000(58) |
| [JAM24.T11.R01](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [44] | `\differential M^{\pi^{+}}/\differential z_{h}` | 249/311 | 1 | not tabulated |
| [JAM24.T11.R02](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [44] | `\differential M^{\pi^{-}}/\differential z_{h}` | 249/311 | 1 | not tabulated |
| [JAM24.T11.R03](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [45] | `\differential M^{K^{+}}/\differential z_{h}` | 247/309 | 1 | not tabulated |
| [JAM24.T11.R04](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [45] | `\differential M^{K^{-}}/\differential z_{h}` | 247/309 | 1 | not tabulated |
| [JAM24.T11.R05](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [44] | `\differential M^{h^{+}}/\differential z_{h}` | 249/311 | 1 | not tabulated |
| [JAM24.T11.R06](https://arxiv.org/html/2501.00665v3#A1.T11) / 33 | SIDIS | COMPASS [44] | `\differential M^{h^{-}}/\differential z_{h}` | 249/311 | 1 | not tabulated |
| [JAM24.T12.R01](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | Belle [58] | `\differential\sigma/\differential z` | 70/78 | — | 0.624(9)∗ |
| [JAM24.T12.R02](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TASSO 12 GeV [59] | `\differential\sigma/\differential z` | 1/5 | — | 2.7(3.1)∗ |
| [JAM24.T12.R03](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TASSO 14 GeV [60] | `\differential\sigma/\differential z` | 5/11 | — | 0.956(49)∗ |
| [JAM24.T12.R04](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TASSO 22 GeV [60] | `\differential\sigma/\differential z` | 5/13 | — | 1.075(72)∗ |
| [JAM24.T12.R05](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TASSO 34 GeV [61] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 6/16 | — | 0.952(24) |
| [JAM24.T12.R06](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TASSO 44 GeV [61] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 4/12 | — | 0.957(28) |
| [JAM24.T12.R07](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TPC(c) [62] | `(\sigma\beta)^{-1}\,\differential\sigma/\differential z (c)` | 4/15 | — | — |
| [JAM24.T12.R08](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TPC(b) [62] | `(\sigma\beta)^{-1}\,\differential\sigma/\differential z (b)` | 4/15 | — | — |
| [JAM24.T12.R09](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | TOPAZ [64] | `\sigma^{-1}\,\differential\sigma/\differential\xi` | 2/17 | — | — |
| [JAM24.T12.R10](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | SLD [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 34/40 | — | 1.003(3)^{\dagger} |
| [JAM24.T12.R11](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | SLD(uds) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (uds)` | 34/40 | — | 1.003(3)^{\dagger} |
| [JAM24.T12.R12](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | SLD(c) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (c)` | 34/40 | — | 1.003(3)^{\dagger} |
| [JAM24.T12.R13](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | SLD(b) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (b)` | 34/40 | — | 1.003(3)^{\dagger} |
| [JAM24.T12.R14](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | ALEPH [65] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 23/39 | — | 0.990(3) |
| [JAM24.T12.R15](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | OPAL [67] | `\sigma^{-1}\,\differential\sigma/\differential p` | 24/51 | — | — |
| [JAM24.T12.R16](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | DELPHI [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 21/23 | — | — |
| [JAM24.T12.R17](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | DELPHI(uds) [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (uds)` | 21/23 | — | — |
| [JAM24.T12.R18](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | DELPHI(b) [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (b)` | 21/23 | — | — |
| [JAM24.T12.R19](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | BaBar (prompt) [57] | `\sigma^{-1}\,\differential\sigma/\differential p` | 31/45 | 1⋆ | 0.938(5) |
| [JAM24.T12.R20](https://arxiv.org/html/2501.00665v3#A1.T12) / 34 | SIA pion | ARGUS [56] | `\sigma^{-1}\,\differential\sigma/\differential p` | 25/52 | — | 0.939(10) |
| [JAM24.T13.R01](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | Belle [58] | `\differential\sigma/\differential z` | 70/78 | — | 0.852(10)* |
| [JAM24.T13.R02](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TASSO 12 GeV [59] | `\differential\sigma/\differential z` | 1/3 | — | 0.8(2.6)* |
| [JAM24.T13.R03](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TASSO 14 GeV [60] | `\differential\sigma/\differential z` | 3/9 | — | 0.860(131)* |
| [JAM24.T13.R04](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TASSO 22 GeV [60] | `\differential\sigma/\differential z` | 3/10 | — | 1.180(227)* |
| [JAM24.T13.R05](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TASSO 34 GeV [61] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 3/11 | — | 0.975(22) |
| [JAM24.T13.R06](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TPC [63] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 7/21 | — | — |
| [JAM24.T13.R07](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | TOPAZ [64] | `\sigma^{-1}\,\differential\sigma/\differential\xi` | 2/12 | — | — |
| [JAM24.T13.R08](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | SLD [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 35/36 | — | 0.995(6)\dagger |
| [JAM24.T13.R09](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | SLD(uds) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (uds)` | 35/36 | — | 0.995(6)\dagger |
| [JAM24.T13.R10](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | SLD(c) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (c)` | 35/36 | — | 0.995(6)\dagger |
| [JAM24.T13.R11](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | SLD(b) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (b)` | 35/36 | — | 0.995(6)\dagger |
| [JAM24.T13.R12](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | ALEPH [65] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 19/29 | — | 1.021(12) |
| [JAM24.T13.R13](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | OPAL [67] | `\sigma^{-1}\,\differential\sigma/\differential p` | 10/33 | — | — |
| [JAM24.T13.R14](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | DELPHI [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 22/23 | — | — |
| [JAM24.T13.R15](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | DELPHI(uds) [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (uds)` | 22/23 | — | — |
| [JAM24.T13.R16](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | DELPHI(b) [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (b)` | 22/23 | — | — |
| [JAM24.T13.R17](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | BaBar (prompt) [57] | `\sigma^{-1}\,\differential\sigma/\differential p` | 28/45 | 1⋆ | 0.948(4) |
| [JAM24.T13.R18](https://arxiv.org/html/2501.00665v3#A1.T13) / 35 | SIA kaon | ARGUS [56] | `\sigma^{-1}\,\differential\sigma/\differential p` | 25/42 | — | 0.981(7) |
| [JAM24.T14.R01](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TASSO 12 GeV [59] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 5/7 | not tabulated | 0.925(22) |
| [JAM24.T14.R02](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TASSO 14 GeV [69] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 11/20 | not tabulated | 0.924(17) |
| [JAM24.T14.R03](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TASSO 22 GeV [69] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 11/20 | not tabulated | 0.931(17) |
| [JAM24.T14.R04](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TASSO 30 GeV [70] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 5/7 | not tabulated | 1.010(18) |
| [JAM24.T14.R05](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TASSO 44 GeV [61] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 11/20 | not tabulated | 0.932(15) |
| [JAM24.T14.R06](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | TPC [63] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 12/34 | not tabulated | — |
| [JAM24.T14.R07](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | SLD [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 34/40 | not tabulated | 1.011(2)\dagger |
| [JAM24.T14.R08](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | SLD(uds) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (uds)` | 34/40 | not tabulated | 1.011(2)\dagger |
| [JAM24.T14.R09](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | SLD(c) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (c)` | 34/40 | not tabulated | 1.011(2)\dagger |
| [JAM24.T14.R10](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | SLD(b) [68] | `\sigma^{-1}\,\differential\sigma/\differential z_{p} (b)` | 34/40 | not tabulated | 1.011(2)\dagger |
| [JAM24.T14.R11](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | ALEPH [71] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 37/46 | not tabulated | 1.014(3) |
| [JAM24.T14.R12](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | OPAL [72] | `\sigma^{-1}\,\differential\sigma/\differential z` | 19/22 | not tabulated | — |
| [JAM24.T14.R13](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | DELPHI [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 21/27 | not tabulated | — |
| [JAM24.T14.R14](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | DELPHI(uds) [66] | `\sigma^{-1}\,\differential\sigma/\differential p` | 21/27 | not tabulated | — |
| [JAM24.T14.R15](https://arxiv.org/html/2501.00665v3#A1.T14) / 36 | SIA unidentified hadron | DELPHI(b) [66] | `\sigma^{-1}\,\differential\sigma/\differential z_{p}` | 21/27 | not tabulated | — |

## Completeness assessment

| Category | Paper explicit | Derived | Missing for exact reproduction | FEMTOMIND exposure gap |
|---|---|---|---|---|
| PDF parametrization | Eqs. 5-9: basis, relations, zero gamma/delta strange terms; 35 coefficients | N is a momentum moment; 39-4 parameter count | Numerical starts/bounds; native basis mapping | Dynamic library does not prove paper basis; sum rules inherited |
| FF parametrization | Pion/kaon/residual equalities; two quark shapes; 60 parameters per species; charge conjugation | Two gluon shapes inferred from parameter count | Exact template implementation/gluon confirmation; coefficients/bounds | Enable optional shapes and reconcile light-flavor ties |
| Dataset selection | Appendix dataset names and row-level point counts; 5853 total points | 96 rows, including 53 SIA and 6 deuteron SIDIS; 7794 original points; 43-normalization reconciliation | XLSX IDs/releases/checksums and some run/acceptance mappings | App catalog of 69 SIA and 12 SIDIS datasets is broader |
| Kinematic cuts | DIS/SIDIS mc²; DIS W²>10; jet pT>8; SIA windows; figure fiducials | mc²=1.6384 GeV² | SIA high/low boundary and complete bin/fiducial conversions | SIA edits restricted; app DIS threshold 1.69 differs |
| Theoretical assumptions | NLO, MSbar, NLL DGLAP, ZMVF, alpha_s, masses, Wc/SIDIS scales | Q0²=1.6384 GeV² | Other scales, matching, EW/nuclear/TMC/grid details, positivity | Many settings are inherited or not exposed |
| Parameter bounds and constraints | PDF sum rules, FF equalities; 35+180+43 counts | N moment and parameter-count checks | Numeric bounds, prior hyperparameters, normalization deltaN | FREE bounds required; mandatory positivity and canonical SIA settings |
| Statistical methodology | Resampling and multistep calibration; template penalties removed in final stage; experimental penalties remain | Conceptual penalized residual objective | Minimizer, tolerances, covariance handling, exact final prior treatment | App optimizer defaults and Gaussian metadata are not paper evidence |
| Replica generation | MC resampling; 95% inverse-CDF intervals; mean central values | None that determines executable sampling | Replica count, seeds, resampling/correlation law, failure handling, weights, quantile details | App bootstrap, replica and seed defaults cannot be auto-adopted |
| Fit initialization | DIS symmetric sea, then DY, W/Z and jets, strange separation; SIA FF calibration before final fit | Final result depends on calibrated PDF/FF starts | Numerical starting ensembles and transfer/release schedule | No verified automatic paper multistage calibration orchestration |

## Application capability/default audit (not paper science)

Read-only application snapshot: commit `cf4368ef8b7931ee74b56b6adab5e8370ec02ecb`. Referenced paths are relative to the repository root. No inherited workspace template was opened.

### APP01

Simultaneous pdf/ffpion/ffkaon/ffhadron objects and SIDIS/SIA execution are supported; absent required objects may be frozen.

Impact: Final JAM24 requires fitting all four, not silently using frozen defaults. Stale configuration-only MCP prose is not execution truth.

Application evidence: `lqcdmcp-deploy/jamai/reaction_registry.py` lines 34-81; `lqcdmcp-deploy/jam_gateway.py` lines 932-958.

### APP02

Proton parameter library comes dynamically from a workspace example template; unknown parameter names are rejected.

Impact: No template or FitPack file was opened here. The exact paper-to-library basis and sum-rule implementation remain unverified. App display metadata advertises an xf form, whereas paper Eq5 defines f with beta-moment normalization M; exponent and normalization mappings must not be assumed from similarly named parameters.

Application evidence: `lqcdmcp-deploy/jamai/object_registry.py` lines 206-250; `lqcdmcp-deploy/jam_gateway.py` lines 922-926; `lqcdmcp-deploy/jam_gateway.py` lines 1656-1741.

### APP03

FF definitions contain first/optional second shapes, editable links and Gaussian metadata; heavy-antiquark links locked.

Impact: Default optional shapes and all kaon/residual light-flavor ties are not the complete paper model. Current kaon s is not default-linked to d; residual ubar is not default-linked to s. Preserve paper physical relations until mapping is verified.

Application evidence: `lqcdmcp-deploy/jamai/ff_registry.py` lines 17-28; `lqcdmcp-deploy/jamai/ff_registry.py` lines 102-140; `lqcdmcp-deploy/jamai/ff_registry.py` lines 185-266.

### APP04

QCD composer defaults NLO,Q20=1.6129,TMC=AOT,HT=False,DGLAP=truncated,alphaS-mode=backward; DIS Q2>1.69.

Impact: Paper-derived input scale/cut is 1.6384. AOT, truncated/backward native options are not fixed by the paper and must not be treated as its settings.

Application evidence: `lqcdmcp-deploy/jam_gateway.py` lines 1337-1353; `lqcdmcp-deploy/jam_gateway.py` lines 1983-1996.

### APP05

Serialized QCD fields limited to order,Q20,TMC,HT,DGLAPmode,asMode plus tolerance/grid path; alpha_s numeric,masses,scheme etc are not forwarded.

Impact: Paper alpha_s=.118,mc=1.28,mb=4.18,MSbar/ZMVF require later verified native configuration; arbitrary extra card keys do not solve this.

Application evidence: `lqcdmcp-deploy/jam_gateway.py` lines 2100-2149; `lqcdmcp-deploy/jamai/schemas.py` lines 25-43.

> Historical application baseline: the APP06 behavior below was observed before these local changes. It is superseded by the configurable controls documented in Phase 3; it is not the current application policy.

### APP06

All four positivity flags are always set True; no composer off switch.

Impact: Paper positivity is unknown; current mandatory policy is an unresolved reproduction assumption, not evidence of paper positivity.

Application evidence: `lqcdmcp-deploy/jamai/input_materializer.py` lines 176-181.

### APP07

SIDIS catalog has 12 rows and SIA has 69; SIA cuts/norms canonical, SIDIS Q2-only; dataset IDs reaction-qualified.

Impact: Paper final 6 deuteron SIDIS and 53 SIA rows require a later verified subset map, not all app defaults. Paper star symbols differ from app thirteen UI stars.

Application evidence: `lqcdmcp-deploy/jamai/dataset_catalog.py` lines 128-210; `lqcdmcp-deploy/jamai/dataset_catalog.py` lines 338-376; `lqcdmcp-deploy/jam_gateway.py` lines 1880-1909.

### APP08

Selected dataset norms support links; SIA norm alterations are rejected against canonical catalog and shared references must be selected.

Impact: 43 paper norms and unpenalized cases need comparison; no quoted deltaN/start/bound can be filled from table posterior values.

Application evidence: `lqcdmcp-deploy/jam_gateway.py` lines 1898-1981; `lqcdmcp-deploy/jamai/input_materializer.py` lines 95-101.

### APP09

Default or uniform-within-bounds initialization, bootstrap flag,seeds,replicas and trf/dogbox are exposed; default 10 replicas, 500 evaluation cap, tolerance 1e-6.

Impact: None are paper-verified. Frontend maxIterations maps to max_nfev, not an optimizer iteration count; metadata alone does not establish Gaussian-prior multistep training.

Application evidence: `lqcdmcp-deploy/jam_gateway.py` lines 1345-1353; `lqcdmcp-deploy/jam_gateway.py` lines 2010-2031; `lqcdmcp-deploy/jamai/run_replica.py` lines 26-38; `lqcdmcp-deploy/jamai/run_replica.py` lines 66-77.

### APP10

Common materializer retains inherited trusted template settings and appends overrides; library hashes checked.

Impact: Paper-only reconstruction is incomplete if native/template-required settings are missing; do not generate an input and quietly inherit defaults.

Application evidence: `lqcdmcp-deploy/jamai/input_materializer.py` lines 29-39; `lqcdmcp-deploy/jamai/input_materializer.py` lines 193-288.

## Source ambiguities to preserve

- A01: The strange-release sentence compares delta_sbar to itself. Keep the literal source problem; Eq7-8 support independent s/sbar modeling but do not establish an exact executable release schedule.
- A02: Second ATLAS observable prints W-minus+cbar; Eq1 discussion and Fig3 indicate the other physical charge W-plus+cbar. Row observable retained as printed; do not silently correct.
- A03: DELPHI(uds) prints differential p while neighboring rows print z_p. Observable retained; conversion requires confirmation.
- A04: Rs(x=.1) upper uncertainty is +.23 in abstract and p27 but +.22 in conclusion. These are validation-output numbers, not campaign settings.
- A05: arXiv v3 history is 15 Oct 2025; downloaded PDF title date 17 Oct 2025; live experimental HTML date 24 Aug 2026. Version and content checksum pinned; do not use HTML date as publication history.

## Exact reproduction boundary

Functional reconstruction and publication-row selection are substantially possible. An executable faithful campaign is blocked by numerical starts/bounds/prior calibration, the data-file map and covariance revisions, positivity, final ensemble/sampling details, normalization priors and optimizer/native-theory settings. See `unresolved_settings.md` for priority and verification route. Do not auto-adopt app defaults, enable all catalog datasets, convert fitted norm errors to priors, or interpret paper silence as an option being off.

Recommended next step: review this entry, then separately authorize comparison with the original final JAM24 configuration/data manifest and calibrated ensembles. That future comparison must add independent provenance without replacing this paper-only record. Only after all blocking unknowns and app capability gaps are resolved should a non-submitting campaign translation be considered.

---

# Phase 2: official input, author clarifications and current implementation

This section supplements the identifiable paper-only layer above. The original paper claims, publication-row mapping and original unknowns are preserved in JSON. No statement recovered from an input file or current implementation is retroactively called paper-explicit.

Official source: `/w/ceph24/JAM/treyand/pdf_plotting/strange_analysis/combined4/2-0/input.py`; SHA256 `218a8e633bf015899f9f7b68494a8a44d79b1998fb506ea99d2424c85c8f75a6`. Read by AST extraction without execution/import or modification. The source file itself is not copied into the application repository.

Current implementation inspection: FitPack `8070c674783b118878b2900fdd4074fa7272604d`. Its pre-existing tracked state was `M fitlib/ai.py`; no modifications were made. Current code is evidence of current semantics, not proof of the exact historical executable revision.

## Verified model accounting

| Object | Definitions | Free N | Free shape | Free total | Fixed true | Linked |
| --- | --- | --- | --- | --- | --- | --- |
| pdf | 39 | 5 | 30 | 35 | 4 | 0 |
| ffpion | 110 | 12 | 48 | 60 | 0 | 50 |
| ffkaon | 110 | 12 | 48 | 60 | 0 | 50 |
| ffhadron | 110 | 12 | 48 | 60 | 0 | 50 |

Total: 215 independent model coefficients, plus 43 independent normalizations = 258. All 150 FF links were checked for both shapes and every N/a/b/c/d coefficient. The four PDF fixed:true normalizations are sum-rule-determined in current native code; they are not four frozen physical numbers. Null normalization bounds remain null.

## Author-confirmed information

- Every independent FF flavor, including gluon, uses two shapes; symmetries apply separately to each shape. The input confirms this.
- Latest author correction: positivity was disabled for all four objects. This supersedes the earlier True statement, preserved only in the JSON author-confirmation history. The input contains no enabling flags.
- Historical Gaussian initialization was external. Do not add Gaussian template-parameter penalties to the proposed final objective. Experimental nuisance penalties remain separate.

| Coefficient | Gaussian mean | Gaussian std |
| --- | --- | --- |
| N | 0.02 | 0.005 |
| a | -0.5 | 2 |
| b | 3 | 2 |
| c | 0 | 1.5 |
| d | 0 | 1.5 |

These numbers come from the author’s follow-up reply, replacing the unavailable screenshot. They are not inferred from app defaults. The supplied db1 N example is linked to u1 N, so db1 N is not independently sampled. A complete applicability map, especially for PDF coefficients, and historical truncation/correlation/seed policy still need confirmation.

## Paper versus source differences

| ID | Subject | Paper | Official input / author | Assessment |
| --- | --- | --- | --- | --- |
| D01 | Input scale | mu0=mc=1.28 GeV, derived Q0²=1.6384 GeV² | Q20=1.27**2=1.6129 GeV², line 19 | Confirmed difference; preserve both sources. |
| D02 | DIS/SIDIS Q² cut | Q²>mc², nominal mc=1.28 GeV | Q2cut=1.3**2; literal query Q2>1.690000 | Confirmed difference; source file is authoritative for planned configuration. |
| D03 | Additional literal filters | Jet pT>8 only; SIDIS Q²-only additional cut with native coverage described separately | Jet also x<1.000000. SIDIS also W2>10.000000, W2SIDIS>0.000000, Z>0.2 and Z<0.8. | Implementation-level filters recovered; do not silently remove them or call them explicit paper cuts. |
| D04 | Positivity | Not specified | No explicit positivity flags Author: Latest author correction: all four False in final JAM24 | Disabled policy resolved by latest author correction; absent input flags agree with current conditional activation. |
| D05 | Declared versus published datasets | 96 publication rows including 53 SIA | 98 files including 55 SIA; extra sia1012 HRS pion and sia2006 TASSO44 kaon | Resolved count difference: current native readers omit zero-point sia1012 and sia2006, yielding 53 SIA/96 overall nonempty datasets and 5853 points; all 96 Appendix before/after counts match. |
| D06 | Gaussian initialization | Calibration Gaussian priors described without numerical hyperparameters | No Gaussian metadata; flat par=False Author: Historical initialization was external; numerical priors supplied in follow-up | Do not infer absence of historical Gaussian starts from this standalone input; no Gaussian objective penalties proposed. |
| D07 | Effective Jet x filter | Jet pT>8; no extra x condition stated | Literal x<1 query is present | Current native readers cannot resolve x and silently ignore this filter for all four Jet datasets; pT>8 is applied and accepted counts match. |
| D08 | Practical unpenalized normalizations | Eight SIA rows marked fitted without penalty | Eight freenorm file paths and free normalization entries | Current referenced workbook norm_c/value is approximately 1000, so normalization residual is approximately (N-1)/1000: finite very weak regularization, not mathematically zero. |
| D09 | Belle kaon observable metadata | Table XIII labels unnormalized dσ/dz | sia2029 uses 2029freenorm.xlsx | Current workbook observable metadata says 1/sig dsig/dz. Keep discrepancy; counts 70/78 match, and no observable or data was altered. |

No newly recovered numerical setting is identified as a missed paper-explicit result. Appendix symbols and the count-derived gluon two-shape inference were already present in the original catalog. Exact source cuts, starts, bounds, IDs and switches are new source evidence.

## Exact global input settings

| Key | Literal value | Input line |
| --- | --- | --- |
| bootstrap | true | [2] |
| ht | false | [3] |
| offpdf | false | [4] |
| resample version | 0 | [5] |
| beta version | 1 | [6] |
| sia version | 1 | [7] |
| sea_type | 2 | [9] |
| strange off | false | [10] |
| s_veto | false | [11] |
| grid path | "/work/JAM/share/fitpack_grids" | [12] |
| grid-8 path | "/work/JAM/treyand/grids-8" | [13] |
| ftol | 0.001 | [14] |
| flat par | false | [15] |
| dglap mode | "truncated" | [16] |
| alphaSmode | "backward" | [17] |
| order | "NLO" | [18] |
| Q20 | 1.6129 | [19] |
| nuc | true | [20] |
| tmc | "AOT" | [21] |
| ht type | "mult" | [22] |
| ww | false | [23] |
| ffpion parametrization | 0 | [447] |
| ffkaon parametrization | 0 | [586] |
| ffhadron parametrization | "sum2" | [723] |

These include implementation versions, nuclear/TMC switches and historical grid paths. They are configuration records, not instructions to reuse historical filesystem paths unchanged. alpha_s and masses are not explicitly assigned in the input; current defaults are recorded separately in JSON.

## Complete PDF and FF parameter definitions

Literal configured values, bounds and links are preserved below and in structured JSON. Linked stored values are not independent predictions; PDF sum-rule normalizations are recomputed by native setup. No fitted ensemble was evaluated.

### pdf

| Parameter | Value | Min | Max | Fixed / link | Input line |
| --- | --- | --- | --- | --- | --- |
| g1 N | 0.387592 | null | null | true | [389] |
| g1 a | -0.623068169 | -1.9 | 10 | false | [390] |
| g1 b | 9.25741583 | 0 | 20 | false | [391] |
| g1 c | 0.0 | -100 | 100 | false | [392] |
| g1 d | 0.0 | -100 | 100 | false | [393] |
| uv1 N | 0.347549 | null | null | true | [395] |
| uv1 a | -0.121835956 | -0.9 | 10 | false | [396] |
| uv1 b | 3.20766744 | 0 | 20 | false | [397] |
| uv1 c | 0.0 | -100 | 100 | false | [398] |
| uv1 d | 0.0 | -100 | 100 | false | [399] |
| dv1 N | 0.152089 | null | null | true | [401] |
| dv1 a | -0.239874967 | -0.9 | 10 | false | [402] |
| dv1 b | 3.8390262 | 0 | 20 | false | [403] |
| dv1 c | 0.0 | -100 | 100 | false | [404] |
| dv1 d | 0.0 | -100 | 100 | false | [405] |
| db1 N | 0.0367609928 | 0 | 1 | false | [410] |
| db1 a | -0.841360631 | -1 | 10 | false | [411] |
| db1 b | 5.31285539 | 0 | 20 | false | [412] |
| db1 c | 0.0 | -100 | 100 | false | [413] |
| db1 d | 0.0 | -100 | 100 | false | [414] |
| ub1 N | 0.0195464789 | 0 | 1 | false | [416] |
| ub1 a | -0.993659187 | -1 | 10 | false | [417] |
| ub1 b | 8.38905814 | 0 | 20 | false | [418] |
| ub1 c | 0.0 | -100 | 100 | false | [419] |
| ub1 d | 0.0 | -100 | 100 | false | [420] |
| s1 N | 0.0 | 0 | 1 | true | [422] |
| s1 a | 0.134706224 | -0.9 | 10 | false | [423] |
| s1 b | 6.00759596 | 0 | 20 | false | [424] |
| sb1 N | 7.46109845e-07 | 0 | 1 | false | [428] |
| sb1 a | 0.383495317 | -0.9 | 10 | false | [429] |
| sb1 b | 4.61209808 | 0 | 20 | false | [430] |
| sea1 N | 0.00571081196 | 0 | 1 | false | [434] |
| sea1 a | -1.36329697 | -1.9 | 10 | false | [435] |
| sea1 b | 4.7472105 | 0 | 20 | false | [436] |
| sea1 c | 0.0 | -100 | 100 | false | [437] |
| sea1 d | 0.0 | -100 | 100 | false | [438] |
| sea2 N | 0.00571081196 | 0 | 1 | false | [440] |
| sea2 a | -1.36329697 | -1.9 | 10 | false | [441] |
| sea2 b | 4.7472105 | 0 | 20 | false | [442] |

### ffpion

| Parameter | Value | Min | Max | Fixed / link | Input line |
| --- | --- | --- | --- | --- | --- |
| g1 N | 0.295437 | 0.0 | 1 | false | [450] |
| g1 a | 1.00469 | -1.8 | 10 | false | [451] |
| g1 b | 6.85766 | 0 | 30 | false | [452] |
| g1 c | 4.81521 | -10 | 10 | false | [453] |
| g1 d | 4.81521 | -10 | 10 | false | [454] |
| u1 N | 0.0267821 | 0.0 | 1 | false | [456] |
| u1 a | 0.176877 | -1.8 | 10 | false | [457] |
| u1 b | 4.81521 | 0 | 30 | false | [458] |
| u1 c | 4.81521 | -10 | 10 | false | [459] |
| u1 d | 4.81521 | -10 | 10 | false | [460] |
| d1 N | 0.299974 | 0.0 | 1 | false | [462] |
| d1 a | -0.689477 | -1.8 | 10 | false | [463] |
| d1 b | 4.79992 | 0 | 30 | false | [464] |
| d1 c | 4.81521 | -10 | 10 | false | [465] |
| d1 d | 4.81521 | -10 | 10 | false | [466] |
| s1 N | 0.154863 | 0.0 | 1 | false | [468] |
| s1 a | 0.300305 | -1.8 | 10 | false | [469] |
| s1 b | 1.83178 | 0 | 30 | false | [470] |
| s1 c | 4.81521 | -10 | 10 | false | [471] |
| s1 d | 4.81521 | -10 | 10 | false | [472] |
| c1 N | 0.18455 | 0.0 | 1 | false | [474] |
| c1 a | -0.0505798 | -1.8 | 10 | false | [475] |
| c1 b | 3.19952 | 0 | 30 | false | [476] |
| c1 c | 4.81521 | -10 | 10 | false | [477] |
| c1 d | 4.81521 | -10 | 10 | false | [478] |
| b1 N | 0.374125 | 0.0 | 1 | false | [480] |
| b1 a | -1.59541 | -1.8 | 10 | false | [481] |
| b1 b | 4.50102 | 0 | 30 | false | [482] |
| b1 c | 4.81521 | -10 | 10 | false | [483] |
| b1 d | 4.81521 | -10 | 10 | false | [484] |
| ub1 N | 0.299974 | 0.0 | 1 | "d1 N" | [486] |
| ub1 a | -0.689477 | -1.8 | 10 | "d1 a" | [487] |
| ub1 b | 4.79992 | 0 | 30 | "d1 b" | [488] |
| ub1 c | 4.81521 | -10 | 10 | "d1 c" | [489] |
| ub1 d | 4.81521 | -10 | 10 | "d1 d" | [490] |
| db1 N | 0.0267821 | 0.0 | 1 | "u1 N" | [492] |
| db1 a | 0.176877 | -1.8 | 10 | "u1 a" | [493] |
| db1 b | 4.81521 | 0 | 30 | "u1 b" | [494] |
| db1 c | 4.81521 | -10 | 10 | "u1 c" | [495] |
| db1 d | 4.81521 | -10 | 10 | "u1 d" | [496] |
| sb1 N | 0.154863 | 0.0 | 1 | "s1 N" | [498] |
| sb1 a | 0.300305 | -1.8 | 10 | "s1 a" | [499] |
| sb1 b | 1.83178 | 0 | 30 | "s1 b" | [500] |
| sb1 c | 4.81521 | -10 | 10 | "s1 c" | [501] |
| sb1 d | 4.81521 | -10 | 10 | "s1 d" | [502] |
| cb1 N | 0.18455 | 0.0 | 1 | "c1 N" | [504] |
| cb1 a | -0.0505798 | -1.8 | 10 | "c1 a" | [505] |
| cb1 b | 3.19952 | 0 | 30 | "c1 b" | [506] |
| cb1 c | 4.81521 | -10 | 10 | "c1 c" | [507] |
| cb1 d | 4.81521 | -10 | 10 | "c1 d" | [508] |
| bb1 N | 0.374125 | 0.0 | 1 | "b1 N" | [510] |
| bb1 a | -1.59541 | -1.8 | 10 | "b1 a" | [511] |
| bb1 b | 4.50102 | 0 | 30 | "b1 b" | [512] |
| bb1 c | 4.81521 | -10 | 10 | "b1 c" | [513] |
| bb1 d | 4.81521 | -10 | 10 | "b1 d" | [514] |
| g2 N | 0.295437 | 0.0 | 1 | false | [517] |
| g2 a | 1.00469 | -1.8 | 10 | false | [518] |
| g2 b | 6.85766 | 0 | 30 | false | [519] |
| g2 c | 4.81521 | -10 | 10 | false | [520] |
| g2 d | 4.81521 | -10 | 10 | false | [521] |
| u2 N | 0.0 | 0.0 | 1 | false | [523] |
| u2 a | 0.176877 | -1.8 | 10 | false | [524] |
| u2 b | 4.81521 | 0 | 30 | false | [525] |
| u2 c | 4.81521 | -10 | 10 | false | [526] |
| u2 d | 4.81521 | -10 | 10 | false | [527] |
| d2 N | 0.0 | 0.0 | 1 | false | [529] |
| d2 a | 0.176877 | -1.8 | 10 | false | [530] |
| d2 b | 4.81521 | 0 | 30 | false | [531] |
| d2 c | 4.81521 | -10 | 10 | false | [532] |
| d2 d | 4.81521 | -10 | 10 | false | [533] |
| s2 N | 0.154863 | 0.0 | 1 | false | [535] |
| s2 a | 0.300305 | -1.8 | 10 | false | [536] |
| s2 b | 1.83178 | 0 | 30 | false | [537] |
| s2 c | 4.81521 | -10 | 10 | false | [538] |
| s2 d | 4.81521 | -10 | 10 | false | [539] |
| c2 N | 0.18455 | 0.0 | 1 | false | [541] |
| c2 a | -0.0505798 | -1.8 | 10 | false | [542] |
| c2 b | 3.19952 | 0 | 30 | false | [543] |
| c2 c | 4.81521 | -10 | 10 | false | [544] |
| c2 d | 4.81521 | -10 | 10 | false | [545] |
| b2 N | 0.374125 | 0.0 | 1 | false | [547] |
| b2 a | -1.59541 | -1.8 | 10 | false | [548] |
| b2 b | 4.50102 | 0 | 30 | false | [549] |
| b2 c | 4.81521 | -10 | 10 | false | [550] |
| b2 d | 4.81521 | -10 | 10 | false | [551] |
| ub2 N | 0.0 | 0.0 | 1 | "d2 N" | [553] |
| ub2 a | 0.176877 | -1.8 | 10 | "d2 a" | [554] |
| ub2 b | 4.81521 | 0 | 30 | "d2 b" | [555] |
| ub2 c | 4.81521 | -10 | 10 | "d2 c" | [556] |
| ub2 d | 4.81521 | -10 | 10 | "d2 d" | [557] |
| db2 N | 0.0 | 0.0 | 1 | "u2 N" | [559] |
| db2 a | 0.176877 | -1.8 | 10 | "u2 a" | [560] |
| db2 b | 4.81521 | 0 | 30 | "u2 b" | [561] |
| db2 c | 4.81521 | -10 | 10 | "u2 c" | [562] |
| db2 d | 4.81521 | -10 | 10 | "u2 d" | [563] |
| sb2 N | 0.154863 | 0.0 | 1 | "s2 N" | [565] |
| sb2 a | 0.300305 | -1.8 | 10 | "s2 a" | [566] |
| sb2 b | 1.83178 | 0 | 30 | "s2 b" | [567] |
| sb2 c | 4.81521 | -10 | 10 | "s2 c" | [568] |
| sb2 d | 4.81521 | -10 | 10 | "s2 d" | [569] |
| cb2 N | 0.18455 | 0.0 | 1 | "c2 N" | [571] |
| cb2 a | -0.0505798 | -1.8 | 10 | "c2 a" | [572] |
| cb2 b | 3.19952 | 0 | 30 | "c2 b" | [573] |
| cb2 c | 4.81521 | -10 | 10 | "c2 c" | [574] |
| cb2 d | 4.81521 | -10 | 10 | "c2 d" | [575] |
| bb2 N | 0.374125 | 0.0 | 1 | "b2 N" | [577] |
| bb2 a | -1.59541 | -1.8 | 10 | "b2 a" | [578] |
| bb2 b | 4.50102 | 0 | 30 | "b2 b" | [579] |
| bb2 c | 4.81521 | -10 | 10 | "b2 c" | [580] |
| bb2 d | 4.81521 | -10 | 10 | "b2 d" | [581] |

### ffkaon

| Parameter | Value | Min | Max | Fixed / link | Input line |
| --- | --- | --- | --- | --- | --- |
| g1 N | 0.23332 | 0.0 | 1 | false | [588] |
| g1 a | 1.48737 | -1.8 | 10 | false | [589] |
| g1 b | 9.62755 | 0 | 30 | false | [590] |
| g1 c | 4.81521 | -10 | 10 | false | [591] |
| g1 d | 4.81521 | -10 | 10 | false | [592] |
| u1 N | 0.0403672 | 0.0 | 1 | false | [594] |
| u1 a | 1.26356 | -1.8 | 10 | false | [595] |
| u1 b | 1.62596 | 0 | 30 | false | [596] |
| u1 c | 4.81521 | -10 | 10 | false | [597] |
| u1 d | 4.81521 | -10 | 10 | false | [598] |
| d1 N | 0.251671 | 0.0 | 1 | false | [600] |
| d1 a | -1.43444 | -1.8 | 10 | false | [601] |
| d1 b | 5.65143 | 0 | 30 | false | [602] |
| d1 c | 4.81521 | -10 | 10 | false | [603] |
| d1 d | 4.81521 | -10 | 10 | false | [604] |
| s1 N | 0.251671 | 0.0 | 1 | "d1 N" | [606] |
| s1 a | -1.43444 | -1.8 | 10 | "d1 a" | [607] |
| s1 b | 5.65143 | 0 | 30 | "d1 b" | [608] |
| s1 c | 4.81521 | -10 | 10 | "d1 c" | [609] |
| s1 d | 4.81521 | -10 | 10 | "d1 d" | [610] |
| c1 N | 0.776923 | 0.0 | 1 | false | [612] |
| c1 a | -1.8 | -1.8 | 10 | false | [613] |
| c1 b | 2.50452 | 0 | 30 | false | [614] |
| c1 c | 4.81521 | -10 | 10 | false | [615] |
| c1 d | 4.81521 | -10 | 10 | false | [616] |
| b1 N | 0.566971 | 0.0 | 1 | false | [618] |
| b1 a | -1.8 | -1.8 | 10 | false | [619] |
| b1 b | 3.41727 | 0 | 30 | false | [620] |
| b1 c | 4.81521 | -10 | 10 | false | [621] |
| b1 d | 4.81521 | -10 | 10 | false | [622] |
| ub1 N | 0.0403672 | 0.0 | 1 | "d1 N" | [624] |
| ub1 a | 2.26356 | -1.8 | 10 | "d1 a" | [625] |
| ub1 b | 1.62596 | 0 | 30 | "d1 b" | [626] |
| ub1 c | 4.81521 | -10 | 10 | "d1 c" | [627] |
| ub1 d | 4.81521 | -10 | 10 | "d1 d" | [628] |
| db1 N | 0.251671 | 0.0 | 1 | "d1 N" | [630] |
| db1 a | -1.43444 | -1.8 | 10 | "d1 a" | [631] |
| db1 b | 1.65143 | 0 | 30 | "d1 b" | [632] |
| db1 c | 4.81521 | -10 | 10 | "d1 c" | [633] |
| db1 d | 4.81521 | -10 | 10 | "d1 d" | [634] |
| sb1 N | 0.251671 | 0.0 | 1 | false | [636] |
| sb1 a | -1.43444 | -1.8 | 10 | false | [637] |
| sb1 b | 1.65143 | 0 | 30 | false | [638] |
| sb1 c | 4.81521 | -10 | 10 | false | [639] |
| sb1 d | 4.81521 | -10 | 10 | false | [640] |
| cb1 N | 0.776923 | 0.0 | 1 | "c1 N" | [642] |
| cb1 a | -1.8 | -1.8 | 10 | "c1 a" | [643] |
| cb1 b | 2.50452 | 0 | 30 | "c1 b" | [644] |
| cb1 c | 4.81521 | -10 | 10 | "c1 c" | [645] |
| cb1 d | 4.81521 | -10 | 10 | "c1 d" | [646] |
| bb1 N | 0.566971 | 0.0 | 1 | "b1 N" | [648] |
| bb1 a | -1.8 | -1.8 | 10 | "b1 a" | [649] |
| bb1 b | 3.41727 | 0 | 30 | "b1 b" | [650] |
| bb1 c | 4.81521 | -10 | 10 | "b1 c" | [651] |
| bb1 d | 4.81521 | -10 | 10 | "b1 d" | [652] |
| g2 N | 0.23332 | 0.0 | 1 | false | [655] |
| g2 a | 1.48737 | -1.8 | 10 | false | [656] |
| g2 b | 9.62755 | 0 | 30 | false | [657] |
| g2 c | 4.81521 | -10 | 10 | false | [658] |
| g2 d | 4.81521 | -10 | 10 | false | [659] |
| u2 N | 0.0 | 0.0 | 1 | false | [661] |
| u2 a | 0.176877 | -1.8 | 10 | false | [662] |
| u2 b | 4.81521 | 0 | 30 | false | [663] |
| u2 c | 4.81521 | -10 | 10 | false | [664] |
| u2 d | 4.81521 | -10 | 10 | false | [665] |
| d2 N | 0.0 | 0.0 | 1 | false | [667] |
| d2 a | 0.176877 | -1.8 | 10 | false | [668] |
| d2 b | 4.81521 | 0 | 30 | false | [669] |
| d2 c | 4.81521 | -10 | 10 | false | [670] |
| d2 d | 4.81521 | -10 | 10 | false | [671] |
| s2 N | 0.251671 | 0.0 | 1 | "d2 N" | [673] |
| s2 a | -1.43444 | -1.8 | 10 | "d2 a" | [674] |
| s2 b | 5.65143 | 0 | 30 | "d2 b" | [675] |
| s2 c | 4.81521 | -10 | 10 | "d2 c" | [676] |
| s2 d | 4.81521 | -10 | 10 | "d2 d" | [677] |
| c2 N | 0.776923 | 0.0 | 1 | false | [679] |
| c2 a | -1.8 | -1.8 | 10 | false | [680] |
| c2 b | 2.50452 | 0 | 30 | false | [681] |
| c2 c | 4.81521 | -10 | 10 | false | [682] |
| c2 d | 4.81521 | -10 | 10 | false | [683] |
| b2 N | 0.566971 | 0.0 | 1 | false | [685] |
| b2 a | -1.8 | -1.8 | 10 | false | [686] |
| b2 b | 3.41727 | 0 | 30 | false | [687] |
| b2 c | 4.81521 | -10 | 10 | false | [688] |
| b2 d | 4.81521 | -10 | 10 | false | [689] |
| ub2 N | 0.0 | 0.0 | 1 | "d2 N" | [691] |
| ub2 a | 0.176877 | -1.8 | 10 | "d2 a" | [692] |
| ub2 b | 4.81521 | 0 | 30 | "d2 b" | [693] |
| ub2 c | 4.81521 | -10 | 10 | "d2 c" | [694] |
| ub2 d | 4.81521 | -10 | 10 | "d2 d" | [695] |
| db2 N | 0.0 | 0.0 | 1 | "d2 N" | [697] |
| db2 a | 0.176877 | -1.8 | 10 | "d2 a" | [698] |
| db2 b | 4.81521 | 0 | 30 | "d2 b" | [699] |
| db2 c | 4.81521 | -10 | 10 | "d2 c" | [700] |
| db2 d | 4.81521 | -10 | 10 | "d2 d" | [701] |
| sb2 N | 0.251671 | 0.0 | 1 | false | [703] |
| sb2 a | -1.43444 | -1.8 | 10 | false | [704] |
| sb2 b | 1.65143 | 0 | 30 | false | [705] |
| sb2 c | 4.81521 | -10 | 10 | false | [706] |
| sb2 d | 4.81521 | -10 | 10 | false | [707] |
| cb2 N | 0.776923 | 0.0 | 1 | "c2 N" | [709] |
| cb2 a | -1.8 | -1.8 | 10 | "c2 a" | [710] |
| cb2 b | 2.50452 | 0 | 30 | "c2 b" | [711] |
| cb2 c | 4.81521 | -10 | 10 | "c2 c" | [712] |
| cb2 d | 4.81521 | -10 | 10 | "c2 d" | [713] |
| bb2 N | 0.566971 | 0.0 | 1 | "b2 N" | [715] |
| bb2 a | -1.8 | -1.8 | 10 | "b2 a" | [716] |
| bb2 b | 3.41727 | 0 | 30 | "b2 b" | [717] |
| bb2 c | 4.81521 | -10 | 10 | "b2 c" | [718] |
| bb2 d | 4.81521 | -10 | 10 | "b2 d" | [719] |

### ffhadron

| Parameter | Value | Min | Max | Fixed / link | Input line |
| --- | --- | --- | --- | --- | --- |
| g1 N | 0.23332 | 0.0 | 1 | false | [725] |
| g1 a | 1.48737 | -1.8 | 10 | false | [726] |
| g1 b | 9.62755 | 0 | 30 | false | [727] |
| g1 c | 4.81521 | -10 | 10 | false | [728] |
| g1 d | 4.81521 | -10 | 10 | false | [729] |
| u1 N | 0.0403672 | 0.0 | 1 | false | [731] |
| u1 a | 1.26356 | -1.8 | 10 | false | [732] |
| u1 b | 1.62596 | 0 | 30 | false | [733] |
| u1 c | 4.81521 | -10 | 10 | false | [734] |
| u1 d | 4.81521 | -10 | 10 | false | [735] |
| d1 N | 0.251671 | 0.0 | 1 | false | [737] |
| d1 a | -1.43444 | -1.8 | 10 | false | [738] |
| d1 b | 5.65143 | 0 | 30 | false | [739] |
| d1 c | 4.81521 | -10 | 10 | false | [740] |
| d1 d | 4.81521 | -10 | 10 | false | [741] |
| s1 N | 0.251671 | 0.0 | 1 | false | [743] |
| s1 a | -1.43444 | -1.8 | 10 | false | [744] |
| s1 b | 5.65143 | 0 | 30 | false | [745] |
| s1 c | 4.81521 | -10 | 10 | false | [746] |
| s1 d | 4.81521 | -10 | 10 | false | [747] |
| c1 N | 0.776923 | 0.0 | 1 | false | [749] |
| c1 a | -1.8 | -1.8 | 10 | false | [750] |
| c1 b | 2.50452 | 0 | 30 | false | [751] |
| c1 c | 4.81521 | -10 | 10 | false | [752] |
| c1 d | 4.81521 | -10 | 10 | false | [753] |
| b1 N | 0.566971 | 0.0 | 1 | false | [755] |
| b1 a | -1.8 | -1.8 | 10 | false | [756] |
| b1 b | 3.41727 | 0 | 30 | false | [757] |
| b1 c | 4.81521 | -10 | 10 | false | [758] |
| b1 d | 4.81521 | -10 | 10 | false | [759] |
| ub1 N | 0.0403672 | 0.0 | 1 | "s1 N" | [761] |
| ub1 a | 2.26356 | -1.8 | 10 | "s1 a" | [762] |
| ub1 b | 1.62596 | 0 | 30 | "s1 b" | [763] |
| ub1 c | 4.81521 | -10 | 10 | "s1 c" | [764] |
| ub1 d | 4.81521 | -10 | 10 | "s1 d" | [765] |
| db1 N | 0.251671 | 0.0 | 1 | "s1 N" | [767] |
| db1 a | -1.43444 | -1.8 | 10 | "s1 a" | [768] |
| db1 b | 1.65143 | 0 | 30 | "s1 b" | [769] |
| db1 c | 4.81521 | -10 | 10 | "s1 c" | [770] |
| db1 d | 4.81521 | -10 | 10 | "s1 d" | [771] |
| sb1 N | 0.251671 | 0.0 | 1 | "s1 N" | [773] |
| sb1 a | -1.43444 | -1.8 | 10 | "s1 a" | [774] |
| sb1 b | 1.65143 | 0 | 30 | "s1 b" | [775] |
| sb1 c | 4.81521 | -10 | 10 | "s1 c" | [776] |
| sb1 d | 4.81521 | -10 | 10 | "s1 d" | [777] |
| cb1 N | 0.776923 | 0.0 | 1 | "c1 N" | [779] |
| cb1 a | -1.8 | -1.8 | 10 | "c1 a" | [780] |
| cb1 b | 2.50452 | 0 | 30 | "c1 b" | [781] |
| cb1 c | 4.81521 | -10 | 10 | "c1 c" | [782] |
| cb1 d | 4.81521 | -10 | 10 | "c1 d" | [783] |
| bb1 N | 0.566971 | 0.0 | 1 | "b1 N" | [785] |
| bb1 a | -1.8 | -1.8 | 10 | "b1 a" | [786] |
| bb1 b | 3.41727 | 0 | 30 | "b1 b" | [787] |
| bb1 c | 4.81521 | -10 | 10 | "b1 c" | [788] |
| bb1 d | 4.81521 | -10 | 10 | "b1 d" | [789] |
| g2 N | 0.23332 | 0.0 | 1 | false | [793] |
| g2 a | 1.48737 | -1.8 | 10 | false | [794] |
| g2 b | 9.62755 | 0 | 30 | false | [795] |
| g2 c | 4.81521 | -10 | 10 | false | [796] |
| g2 d | 4.81521 | -10 | 10 | false | [797] |
| u2 N | 0.0 | 0.0 | 1 | false | [799] |
| u2 a | 0.176877 | -1.8 | 10 | false | [800] |
| u2 b | 4.81521 | 0 | 30 | false | [801] |
| u2 c | 4.81521 | -10 | 10 | false | [802] |
| u2 d | 4.81521 | -10 | 10 | false | [803] |
| d2 N | 0.0 | 0.0 | 1 | false | [805] |
| d2 a | 0.176877 | -1.8 | 10 | false | [806] |
| d2 b | 4.81521 | 0 | 30 | false | [807] |
| d2 c | 4.81521 | -10 | 10 | false | [808] |
| d2 d | 4.81521 | -10 | 10 | false | [809] |
| s2 N | 0.251671 | 0.0 | 1 | false | [811] |
| s2 a | -1.43444 | -1.8 | 10 | false | [812] |
| s2 b | 5.65143 | 0 | 30 | false | [813] |
| s2 c | 4.81521 | -10 | 10 | false | [814] |
| s2 d | 4.81521 | -10 | 10 | false | [815] |
| c2 N | 0.776923 | 0.0 | 1 | false | [817] |
| c2 a | -1.8 | -1.8 | 10 | false | [818] |
| c2 b | 2.50452 | 0 | 30 | false | [819] |
| c2 c | 4.81521 | -10 | 10 | false | [820] |
| c2 d | 4.81521 | -10 | 10 | false | [821] |
| b2 N | 0.566971 | 0.0 | 1 | false | [823] |
| b2 a | -1.8 | -1.8 | 10 | false | [824] |
| b2 b | 3.41727 | 0 | 30 | false | [825] |
| b2 c | 4.81521 | -10 | 10 | false | [826] |
| b2 d | 4.81521 | -10 | 10 | false | [827] |
| ub2 N | 0.0 | 0.0 | 1 | "s2 N" | [829] |
| ub2 a | 0.176877 | -1.8 | 10 | "s2 a" | [830] |
| ub2 b | 4.81521 | 0 | 30 | "s2 b" | [831] |
| ub2 c | 4.81521 | -10 | 10 | "s2 c" | [832] |
| ub2 d | 4.81521 | -10 | 10 | "s2 d" | [833] |
| db2 N | 0.0 | 0.0 | 1 | "s2 N" | [835] |
| db2 a | 0.176877 | -1.8 | 10 | "s2 a" | [836] |
| db2 b | 4.81521 | 0 | 30 | "s2 b" | [837] |
| db2 c | 4.81521 | -10 | 10 | "s2 c" | [838] |
| db2 d | 4.81521 | -10 | 10 | "s2 d" | [839] |
| sb2 N | 0.251671 | 0.0 | 1 | "s2 N" | [841] |
| sb2 a | -1.43444 | -1.8 | 10 | "s2 a" | [842] |
| sb2 b | 1.65143 | 0 | 30 | "s2 b" | [843] |
| sb2 c | 4.81521 | -10 | 10 | "s2 c" | [844] |
| sb2 d | 4.81521 | -10 | 10 | "s2 d" | [845] |
| cb2 N | 0.776923 | 0.0 | 1 | "c2 N" | [847] |
| cb2 a | -1.8 | -1.8 | 10 | "c2 a" | [848] |
| cb2 b | 2.50452 | 0 | 30 | "c2 b" | [849] |
| cb2 c | 4.81521 | -10 | 10 | "c2 c" | [850] |
| cb2 d | 4.81521 | -10 | 10 | "c2 d" | [851] |
| bb2 N | 0.566971 | 0.0 | 1 | "b2 N" | [853] |
| bb2 a | -1.8 | -1.8 | 10 | "b2 a" | [854] |
| bb2 b | 3.41727 | 0 | 30 | "b2 b" | [855] |
| bb2 c | 4.81521 | -10 | 10 | "b2 c" | [856] |
| bb2 d | 4.81521 | -10 | 10 | "b2 d" | [857] |

## Exact declared dataset selections, cuts and normalizations

All IDs are reaction-qualified. A missing normalization remains absent, not an implicit unity-valued parameter. A missing wc filters key remains missing; empty lists are retained. The 98 declarations and 55 SIA files must be distinguished from accepted nonempty dataset rows.

### idis (13 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 10010 | idis/expdata/10010.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":1.04352,"min":0.8,"max":1.2,"fixed":false} | [39] |
| 10011 | idis/expdata/10011.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":1.04141,"min":0.8,"max":1.2,"fixed":false} | [52] |
| 10016 | idis/expdata/10016.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":0.989544,"min":0.8,"max":1.2,"fixed":false} | [40] |
| 10017 | idis/expdata/10017.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":1.01306,"min":0.8,"max":1.2,"fixed":false} | [53] |
| 10020 | idis/expdata/10020.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":1.02003,"min":0.8,"max":1.2,"fixed":false} | [41] |
| 10021 | idis/expdata/10021.xlsx | ["Q2>1.690000","W2>10.000000"] | {"value":1.0,"min":0.8,"max":1.2,"fixed":false} | [54] |
| 10026 | idis/expdata/10026.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [43] |
| 10027 | idis/expdata/10027.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [44] |
| 10028 | idis/expdata/10028.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [45] |
| 10029 | idis/expdata/10029.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [46] |
| 10030 | idis/expdata/10030.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [47] |
| 10031 | idis/expdata/10031.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [48] |
| 10032 | idis/expdata/10032.xlsx | ["Q2>1.690000","W2>10.000000"] | absent | [49] |

### dy (3 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 10001 | dy/expdata/10001.xlsx | ["Q2>0"] | {"value":1,"fixed":false,"min":0.5,"max":1.5} | [84] |
| 20001 | dy/expdata/20001.xlsx | ["Q2>0"] | {"value":1,"fixed":false,"min":0.5,"max":1.5} | [85] |
| 20002 | dy/expdata/20002.xlsx | ["Q2>0"] | {"value":1,"fixed":false,"min":0.5,"max":1.5} | [86] |

### wzrv (8 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 2010 | wzrv/expdata/2010.xlsx | [] | absent | [98] |
| 2011 | wzrv/expdata/2011.xlsx | [] | absent | [99] |
| 2012 | wzrv/expdata/2012.xlsx | [] | absent | [100] |
| 2013 | wzrv/expdata/2013.xlsx | [] | absent | [101] |
| 2014 | wzrv/expdata/2014.xlsx | [] | absent | [102] |
| 2016 | wzrv/expdata/2016.xlsx | [] | absent | [103] |
| 2017 | wzrv/expdata/2017.xlsx | [] | absent | [104] |
| 2020 | wzrv/expdata/2020.xlsx | [] | absent | [105] |

### wasym (2 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 1000 | wasym/expdata/1000.xlsx | [] | absent | [115] |
| 1001 | wasym/expdata/1001.xlsx | [] | absent | [116] |

### zrap (2 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 1000 | zrap/expdata/1000.xlsx | [] | {"value":1,"fixed":false,"min":0.5,"max":1.5} | [126] |
| 1001 | zrap/expdata/1001.xlsx | [] | absent | [127] |

### jet (4 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 10001 | jet/expdata/10001.xlsx | ["pT>8.000000","x<1.000000"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [143] |
| 10002 | jet/expdata/10002.xlsx | ["pT>8.000000","x<1.000000"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [144] |
| 10003 | jet/expdata/10003.xlsx | ["pT>8.000000","x<1.000000"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [145] |
| 10004 | jet/expdata/10004.xlsx | ["pT>8.000000","x<1.000000"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [146] |

### wc (5 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 10001 | wc/expdata/10001.xlsx | None | {"value":1.0,"min":0.8,"max":1.2,"fixed":false} | [157] |
| 10002 | wc/expdata/10002.xlsx | None | {"value":1.0,"min":0.8,"max":1.2,"fixed":false} | [158] |
| 20001 | wc/expdata/20001.xlsx | None | absent | [159] |
| 20002 | wc/expdata/20002.xlsx | None | absent | [160] |
| 20003 | wc/expdata/20003.xlsx | None | {"value":1.0,"min":0.8,"max":1.2,"fixed":false} | [161] |

### sia (55 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 1001 | sia/expdata_V1/1001freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.10478,"fixed":false,"min":0.1,"max":100} | [203] |
| 1002 | sia/expdata_V1/1002freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.982581,"fixed":false,"min":0.1,"max":100} | [204] |
| 1003 | sia/expdata_V1/1003freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.03054,"fixed":false,"min":0.1,"max":100} | [205] |
| 1005 | sia/expdata_V1/1005.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.03419,"fixed":false,"min":0.1,"max":100} | [207] |
| 1006 | sia/expdata_V1/1006.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.979162,"fixed":false,"min":0.1,"max":100} | [208] |
| 1010 | sia/expdata_V1/1010.xlsx | ["z < 0.9 and z > 0.15"] | absent | [211] |
| 1011 | sia/expdata_V1/1011.xlsx | ["z < 0.9 and z > 0.15"] | absent | [212] |
| 1012 | sia/expdata_V1/1012.xlsx | ["z < 0.9 and z > 0.15"] | absent | [213] |
| 1013 | sia/expdata_V1/1013.xlsx | ["z < 0.9 and z > 0.15"] | absent | [214] |
| 1014 | sia/expdata_V1/1014.xlsx | ["z < 0.9 and z > 0.02"] | {"value":0.99777,"fixed":false,"min":0.5,"max":1.5} | [215] |
| 1015 | sia/expdata_V1/1015.xlsx | ["z < 0.9 and z > 0.02"] | {"value":0.99777,"fixed":1014,"min":0.5,"max":1.5} | [216] |
| 1016 | sia/expdata_V1/1016.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.1892,"fixed":1014,"min":0.5,"max":1.5} | [217] |
| 1017 | sia/expdata_V1/1017.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.00345,"fixed":1014,"min":0.5,"max":1.5} | [218] |
| 1018 | sia/expdata_V1/1018.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.02378,"fixed":false,"min":0.5,"max":1.5} | [219] |
| 1019 | sia/expdata_V1/1019.xlsx | ["z < 0.9 and z > 0.02"] | absent | [220] |
| 1025 | sia/expdata_V1/1025.xlsx | ["z < 0.9 and z > 0.02"] | absent | [223] |
| 1026 | sia/expdata_V1/1026.xlsx | ["z < 0.9 and z > 0.02"] | absent | [224] |
| 1027 | sia/expdata_V1/1027.xlsx | ["z < 0.9 and z > 0.02"] | absent | [225] |
| 1028 | sia/expdata_V1/1028corrmat.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.976001,"fixed":false,"min":0.5,"max":1.5} | [226] |
| 1029 | sia/expdata_V1/1029freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.868358,"fixed":false,"min":0.1,"max":100} | [227] |
| 1030 | sia/expdata_V1/1030.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.01509,"fixed":false,"min":0.5,"max":1.5} | [228] |
| 2001 | sia/expdata_V1/2001freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.983394,"fixed":false,"min":0.1,"max":100} | [275] |
| 2002 | sia/expdata_V1/2002freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.983394,"fixed":false,"min":0.1,"max":100} | [276] |
| 2003 | sia/expdata_V1/2003freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.994421,"fixed":false,"min":0.1,"max":100} | [277] |
| 2005 | sia/expdata_V1/2005.xlsx | ["z < 0.9 and z > 0.15"] | {"value":0.992876,"fixed":false,"min":0.1,"max":100} | [279] |
| 2006 | sia/expdata_V1/2006.xlsx | ["z < 0.9 and z > 0.15"] | absent | [280] |
| 2008 | sia/expdata_V1/2008.xlsx | ["z < 0.9 and z > 0.15"] | absent | [282] |
| 2013 | sia/expdata_V1/2013.xlsx | ["z < 0.9 and z > 0.15"] | absent | [284] |
| 2014 | sia/expdata_V1/2014.xlsx | ["z < 0.9 and z > 0.02"] | {"value":0.912186,"fixed":false,"min":0.5,"max":1.5} | [285] |
| 2015 | sia/expdata_V1/2015.xlsx | ["z < 0.9 and z > 0.02"] | {"value":0.912186,"fixed":2014,"min":0.5,"max":1.5} | [286] |
| 2016 | sia/expdata_V1/2016.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.03996,"fixed":2014,"min":0.5,"max":1.5} | [287] |
| 2017 | sia/expdata_V1/2017.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.00015,"fixed":2014,"min":0.5,"max":1.5} | [288] |
| 2018 | sia/expdata_V1/2018.xlsx | ["z < 0.9 and z > 0.02"] | {"value":0.888453,"fixed":false,"min":0.5,"max":1.5} | [289] |
| 2019 | sia/expdata_V1/2019.xlsx | ["z < 0.9 and z > 0.02"] | absent | [290] |
| 2025 | sia/expdata_V1/2025.xlsx | ["z < 0.9 and z > 0.02"] | absent | [293] |
| 2026 | sia/expdata_V1/2026.xlsx | ["z < 0.9 and z > 0.02"] | absent | [294] |
| 2027 | sia/expdata_V1/2027.xlsx | ["z < 0.9 and z > 0.02"] | absent | [295] |
| 2028 | sia/expdata_V1/2028corrmat.xlsx | ["z < 0.9 and z > 0.2"] | {"value":0.997435,"fixed":false,"min":0.5,"max":1.5} | [296] |
| 2029 | sia/expdata_V1/2029freenorm.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.1,"max":100} | [297] |
| 2030 | sia/expdata_V1/2030.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.00482,"fixed":false,"min":0.5,"max":1.5} | [298] |
| 4000 | sia/expdata_V1/4000.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [336] |
| 4001 | sia/expdata_V1/4001.xlsx | ["z < 0.9 and z > 0.02"] | absent | [337] |
| 4002 | sia/expdata_V1/4002.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [338] |
| 4003 | sia/expdata_V1/4003.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [339] |
| 4004 | sia/expdata_V1/4004.xlsx | ["z < 0.9 and z > 0.15"] | absent | [340] |
| 4007 | sia/expdata_V1/4007.xlsx | ["z < 0.9 and z > 0.02"] | absent | [343] |
| 4008 | sia/expdata_V1/4008.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [344] |
| 4009 | sia/expdata_V1/4009.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [345] |
| 4010 | sia/expdata_V1/4010.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [346] |
| 4012 | sia/expdata_V1/4012.xlsx | ["z < 0.9 and z > 0.15"] | {"value":1.0,"fixed":false,"min":0.5,"max":1.5} | [348] |
| 4013 | sia/expdata_V1/4013.xlsx | ["z < 0.9 and z > 0.02"] | absent | [349] |
| 4014 | sia/expdata_V1/4014.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.0,"fixed":4002,"min":0.5,"max":1.5} | [350] |
| 4015 | sia/expdata_V1/4015.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.0,"fixed":4002,"min":0.5,"max":1.5} | [351] |
| 4016 | sia/expdata_V1/4016.xlsx | ["z < 0.9 and z > 0.02"] | {"value":1.0,"fixed":4002,"min":0.5,"max":1.5} | [352] |
| 4017 | sia/expdata_V1/4017.xlsx | ["z < 0.9 and z > 0.02"] | absent | [353] |

### sidis (6 declared files)

| ID | XLSX path | Literal filters | Normalization definition / absent | Input line |
| --- | --- | --- | --- | --- |
| 1005 | sidis/expdata/1005.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [375] |
| 1006 | sidis/expdata/1006.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [376] |
| 2005 | sidis/expdata/2005.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [377] |
| 2006 | sidis/expdata/2006.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [378] |
| 3000 | sidis/expdata/3000.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [379] |
| 3001 | sidis/expdata/3001.xlsx | ["Q2>1.690000","W2>10.000000","W2SIDIS>0.000000","Z>0.2 and Z<0.8"] | absent | [380] |

## Appendix normalization notation and implementation

The publication asterisk marks an unpenalized normalization, dagger marks shared normalization, and star marks supplied covariance information. These are distinct from the app’s selectable-dataset UI asterisks. Fitted normalization values and parenthesized output errors in the paper are not experimental input deltaN or parameter bounds.

| Reaction | Norm declarations | Independent free | Linked | Fixed true |
| --- | --- | --- | --- | --- |
| idis | 6 | 6 | 0 | 0 |
| dy | 3 | 3 | 0 | 0 |
| wzrv | 0 | 0 | 0 | 0 |
| wasym | 0 | 0 | 0 | 0 |
| zrap | 1 | 1 | 0 | 0 |
| jet | 4 | 4 | 0 | 0 |
| wc | 3 | 3 | 0 | 0 |
| sia | 35 | 26 | 9 | 0 |
| sidis | 0 | 0 | 0 | 0 |

SLD inclusive, uds, c and b rows share one normalization independently within each hadron species: 12 declarations reduce to three free normalizations. All exact targets appear in the dataset tables above. The detailed Appendix/data-file comparison is stored in source_verification.dataset_normalization_audit, with bounded data checks and limitations separately recorded.

# JAM24 dataset and normalization source review

## Findings

The original input declares **98 datasets**, including **55 SIA**. Two extra SIA tables are empty after the exact source cut: HRS pion1012 (7 original points, z=.042–.125) and TASSO44 kaon2006 (4 original xp bins, converted z≈.0276–.0371). The standard reader omits zero-point tables. A bounded native-reader audit independently confirms **5853 retained points**,96 nonempty tables and exact before/after counts for every publication row. All98 current workbook hashes are recorded. This validates current data selection, not historical data-file byte identity or fit reproduction.

There are **52 normalization entries:43 free +9 integer links**, no bool-fixed normalizations. Three SLD groups share one norm each. The input bounds and initial normalization values are exact config facts; paper fitted-normalization errors are output diagnostics, not input uncertainties.

The paper calls eight SIA norms unpenalized. Their exact referenced freenorm sheets encode **dN≈1000**, so current residual code gives a finite but weak ((N−1)/1000)^2 penalty. This distinction must be retained. Belle kaon2029 spreadsheet obs reads1/sig dsig/dz while the paper identifies unnormalized dσ/dz.

## Counts

| Reaction | Declared XLSX | Norm entries | Free | Linked |
|---|---:|---:|---:|---:|
| idis | 13 | 6 | 6 | 0 |
| dy | 3 | 3 | 3 | 0 |
| wzrv | 8 | 0 | 0 | 0 |
| wasym | 2 | 0 | 0 | 0 |
| zrap | 2 | 1 | 1 | 0 |
| jet | 4 | 4 | 4 | 0 |
| wc | 5 | 3 | 3 | 0 |
| sia | 55 | 35 | 26 | 9 |
| sidis | 6 | 0 | 0 | 0 |

## Publication symbols

- **asterisk**: Eight unnormalized SIA datasets, normalization fitted without penalty in paper caption. Current exact freenorm files use finite dN≈1000 rather than exact zero penalty.
- **dagger**: Four SLD inclusive/uds/c/b datasets share one normalization separately in each of pion, kaon, unidentified hadron categories.
- **star**: BaBar prompt pion1028 and kaon2028 have systematic covariance information. Exact corrmat XLSX files have syst_c plus45 cor_i_V correlation columns and norm_c; native readers loaded them, but no covariance decomposition/residual evaluation was run. Both independent normalization uncertainties dN=.01. The _S full-covariance normalization skip is not applicable to _V representation.
- **n_cor**: Publication per-row correlated source count preserved individually in paper_to_input_rows; a dash is not a missing normalization declaration.

## Full normalization mapping

| Reaction:ID | Source line | Initial value | Bounds | fixed/link | Workbook dN | Paper row | Paper fitted output |
|---|---:|---:|---|---|---:|---|---|
| idis:10010 | 68 | 1.04352 | [0.8, 1.2] | False | 0.021 | Table 4 row 1 | 0.984(9) |
| idis:10011 | 69 | 1.04141 | [0.8, 1.2] | False | 0.016999999999999998 | Table 4 row 2 | 0.976(9) |
| idis:10016 | 70 | 0.989544 | [0.8, 1.2] | False | 0.029999999999999995 | Table 4 row 3 | 0.970(6) |
| idis:10017 | 71 | 1.01306 | [0.8, 1.2] | False | 0.03 | Table 4 row 4 | 0.986(8) |
| idis:10020 | 72 | 1.02003 | [0.8, 1.2] | False | 0.02 | Table 4 row 5 | 1.017(10) |
| idis:10021 | 73 | 1.0 | [0.8, 1.2] | False | 0.0104 | Table 4 row 6 | 0.997(6) |
| dy:10001 | 89 | 1 | [0.5, 1.5] | False | 0.07 | Table 5 row 1 | 0.961(28) |
| dy:20001 | 90 | 1 | [0.5, 1.5] | False | 0.0093 | Table 5 row 2 | 0.990(13) |
| dy:20002 | 91 | 1 | [0.5, 1.5] | False | 0.02 | Table 5 row 3 | 1.012(10) |
| zrap:1000 | 130 | 1 | [0.5, 1.5] | False | 0.06 | Table 8 row 1 | 0.937(12) |
| jet:10001 | 148 | 1.0 | [0.5, 1.5] | False | 0.065 | Table 9 row 1 | 1.137(34) |
| jet:10002 | 149 | 1.0 | [0.5, 1.5] | False | 0.057999999999999996 | Table 9 row 2 | 1.056(37) |
| jet:10003 | 150 | 1.0 | [0.5, 1.5] | False | 0.08 | Table 9 row 3 | ​​1.013(3) |
| jet:10004 | 151 | 1.0 | [0.5, 1.5] | False | 0.08 | Table 9 row 4 | 1.101(16) |
| wc:10001 | 164 | 1.0 | [0.8, 1.2] | False | 0.018000000000000002 | Table 10 row 1 | 0.995(55) |
| wc:10002 | 165 | 1.0 | [0.8, 1.2] | False | 0.018000000000000002 | Table 10 row 2 | 0.994(54) |
| wc:20003 | 166 | 1.0 | [0.8, 1.2] | False | 0.025 | Table 10 row 5 | 1.000(58) |
| sia:1001 | 230 | 1.10478 | [0.1, 100] | False | 1000.0 | Table 12 row 2 | 2.7(3.1)∗ |
| sia:1002 | 231 | 0.982581 | [0.1, 100] | False | 1000.0 | Table 12 row 3 | 0.956(49)∗ |
| sia:1003 | 232 | 1.03054 | [0.1, 100] | False | 1000.0000000000001 | Table 12 row 4 | 1.075(72)∗ |
| sia:1005 | 234 | 1.03419 | [0.1, 100] | False | 0.060000000000000005 | Table 12 row 5 | 0.952(24) |
| sia:1006 | 235 | 0.979162 | [0.1, 100] | False | 0.06 | Table 12 row 6 | 0.957(28) |
| sia:1014 | 236 | 0.99777 | [0.5, 1.5] | False | 0.01 | Table 12 row 10 | 1.003(3)^{\dagger} |
| sia:1015 | 237 | 0.99777 | [0.5, 1.5] | 1014 | 0.01 | Table 12 row 11 | 1.003(3)^{\dagger} |
| sia:1016 | 238 | 1.1892 | [0.5, 1.5] | 1014 | 0.01 | Table 12 row 12 | 1.003(3)^{\dagger} |
| sia:1017 | 239 | 1.00345 | [0.5, 1.5] | 1014 | 0.01 | Table 12 row 13 | 1.003(3)^{\dagger} |
| sia:1018 | 240 | 1.02378 | [0.5, 1.5] | False | 0.03 | Table 12 row 14 | 0.990(3) |
| sia:1028 | 243 | 0.976001 | [0.5, 1.5] | False | 0.009800000000000001 | Table 12 row 19 | 0.938(5) |
| sia:1029 | 244 | 0.868358 | [0.1, 100] | False | 1000.0 | Table 12 row 1 | 0.624(9)∗ |
| sia:1030 | 245 | 1.01509 | [0.5, 1.5] | False | 0.018 | Table 12 row 20 | 0.939(10) |
| sia:2001 | 300 | 0.983394 | [0.1, 100] | False | 1000.0000000000001 | Table 13 row 2 | 0.8(2.6)* |
| sia:2002 | 301 | 0.983394 | [0.1, 100] | False | 1000.0 | Table 13 row 3 | 0.860(131)* |
| sia:2003 | 302 | 0.994421 | [0.1, 100] | False | 1000.0 | Table 13 row 4 | 1.180(227)* |
| sia:2005 | 304 | 0.992876 | [0.1, 100] | False | 0.060000000000000005 | Table 13 row 5 | 0.975(22) |
| sia:2014 | 305 | 0.912186 | [0.5, 1.5] | False | 0.01 | Table 13 row 8 | 0.995(6)\dagger |
| sia:2015 | 306 | 0.912186 | [0.5, 1.5] | 2014 | 0.01 | Table 13 row 9 | 0.995(6)\dagger |
| sia:2016 | 307 | 1.03996 | [0.5, 1.5] | 2014 | 0.01 | Table 13 row 10 | 0.995(6)\dagger |
| sia:2017 | 308 | 1.00015 | [0.5, 1.5] | 2014 | 0.01 | Table 13 row 11 | 0.995(6)\dagger |
| sia:2018 | 309 | 0.888453 | [0.5, 1.5] | False | 0.03 | Table 13 row 12 | 1.021(12) |
| sia:2028 | 312 | 0.997435 | [0.5, 1.5] | False | 0.009800000000000001 | Table 13 row 17 | 0.948(4) |
| sia:2029 | 313 | 1.0 | [0.1, 100] | False | 1000.0 | Table 13 row 1 | 0.852(10)* |
| sia:2030 | 314 | 1.00482 | [0.5, 1.5] | False | 0.018 | Table 13 row 18 | 0.981(7) |
| sia:4000 | 354 | 1.0 | [0.5, 1.5] | False | 0.01 | Table 14 row 11 | 1.014(3) |
| sia:4002 | 355 | 1.0 | [0.5, 1.5] | False | 0.01 | Table 14 row 7 | 1.011(2)\dagger |
| sia:4003 | 356 | 1.0 | [0.5, 1.5] | False | 0.045 | Table 14 row 1 | 0.925(22) |
| sia:4008 | 357 | 1.0 | [0.5, 1.5] | False | 0.045 | Table 14 row 2 | 0.924(17) |
| sia:4009 | 358 | 1.0 | [0.5, 1.5] | False | 0.045 | Table 14 row 3 | 0.931(17) |
| sia:4010 | 359 | 1.0 | [0.5, 1.5] | False | 0.045000000000000005 | Table 14 row 4 | 1.010(18) |
| sia:4012 | 361 | 1.0 | [0.5, 1.5] | False | 0.045000000000000005 | Table 14 row 5 | 0.932(15) |
| sia:4014 | 362 | 1.0 | [0.5, 1.5] | 4002 | 0.01 | Table 14 row 9 | 1.011(2)\dagger |
| sia:4015 | 363 | 1.0 | [0.5, 1.5] | 4002 | 0.01 | Table 14 row 10 | 1.011(2)\dagger |
| sia:4016 | 364 | 1.0 | [0.5, 1.5] | 4002 | 0.01 | Table 14 row 8 | 1.011(2)\dagger |

## Full publication row mapping

Every row is now supported by exact original-config path, current native-reader workbook metadata and matching before/after point counts. No IDs were inferred from publication alone. Historical workbook byte identity remains unverified.

| Publication row | Reaction:ID | Paper dataset | Original inline comment |
|---|---|---|---|
| Table 4 row 1 | idis:10010 | SLAC [39] | proton   \| F2            \| SLAC |
| Table 4 row 2 | idis:10011 | SLAC [39] | deuteron \| F2            \| SLAC |
| Table 4 row 3 | idis:10016 | BCDMS [40] | proton   \| F2            \| BCDMS |
| Table 4 row 4 | idis:10017 | BCDMS [40] | deuteron \| F2            \| BCDMS |
| Table 4 row 5 | idis:10020 | NMC [41] | proton   \| F2            \| NMC |
| Table 4 row 6 | idis:10021 | NMC [42] | d/p      \| F2d/F2p       \| NMC |
| Table 4 row 7 | idis:10026 | HERA NC e^{+} (1) [43] | proton   \| sigma red     \| HERA II NC e+ (1) |
| Table 4 row 8 | idis:10027 | HERA NC e^{+} (2) [43] | proton   \| sigma red     \| HERA II NC e+ (2) |
| Table 4 row 9 | idis:10028 | HERA NC e^{+} (3) [43] | proton   \| sigma red     \| HERA II NC e+ (3) |
| Table 4 row 10 | idis:10029 | HERA NC e^{+} (4) [43] | proton   \| sigma red     \| HERA II NC e+ (4) |
| Table 4 row 11 | idis:10030 | HERA NC e^{-} [43] | proton   \| sigma red     \| HERA II NC e- |
| Table 4 row 12 | idis:10031 | HERA CC e^{+} [43] | proton   \| sigma red     \| HERA II CC e+ |
| Table 4 row 13 | idis:10032 | HERA CC e^{-} [43] | proton   \| sigma red     \| HERA II CC e- |
| Table 5 row 1 | dy:10001 | E866 (NuSea) [2] | No comment; app-label candidate |
| Table 5 row 2 | dy:20001 | E866 (NuSea) [2] | No comment; app-label candidate |
| Table 5 row 3 | dy:20002 | E906 (SeaQuest) [3] | No comment; app-label candidate |
| Table 6 row 1 | wzrv:2010 | CMS 8~TeV [23] | No comment; app-label candidate |
| Table 6 row 2 | wzrv:2011 | CMS 7~TeV [21] | No comment; app-label candidate |
| Table 6 row 3 | wzrv:2012 | CMS 7~TeV [20] | No comment; app-label candidate |
| Table 6 row 4 | wzrv:2013 | CMS 7~TeV [19] | No comment; app-label candidate |
| Table 6 row 5 | wzrv:2014 | CMS 7~TeV [19] | No comment; app-label candidate |
| Table 6 row 6 | wzrv:2016 | LHCb 7~TeV [46] | No comment; app-label candidate |
| Table 6 row 7 | wzrv:2017 | LHCb 8~TeV [47] | No comment; app-label candidate |
| Table 6 row 8 | wzrv:2020 | STAR [50] | No comment; app-label candidate |
| Table 7 row 1 | wasym:1000 | CDF [49] | No comment; app-label candidate |
| Table 7 row 2 | wasym:1001 | DØ [48] | No comment; app-label candidate |
| Table 8 row 1 | zrap:1000 | CDF [52] | No comment; app-label candidate |
| Table 8 row 2 | zrap:1001 | DØ [51] | No comment; app-label candidate |
| Table 9 row 1 | jet:10001 | DØ [53] | # D0        dataset \| |
| Table 9 row 2 | jet:10002 | CDF [54] | # CDF       dataset \| |
| Table 9 row 3 | jet:10003 | STAR [55] | # STAR 2003 dataset \| |
| Table 9 row 4 | jet:10004 | STAR [55] | # STAR 2004 dataset \| |
| Table 10 row 1 | wc:10001 | ATLAS 7 TeV [26] | pp \| sig \| ATLAS7Wm |
| Table 10 row 2 | wc:10002 | ATLAS 7 TeV [26] | pp \| sig \| ATLAS7Wp |
| Table 10 row 3 | wc:20001 | CMS 7 TeV [25] | pp \| Wp+Wm \| CMS7WpWm_add |
| Table 10 row 4 | wc:20002 | CMS 7 TeV [25] | pp \| Wp/Wm \| CMS7WpWm_ratio |
| Table 10 row 5 | wc:20003 | CMS 13 TeV [27] | pp \| Wp+Wm \| CMS13WpWm_add |
| Table 11 row 1 | sidis:1005 | COMPASS [44] | deuteron , mult , pi+ , COMPASS |
| Table 11 row 2 | sidis:1006 | COMPASS [44] | deuteron , mult , pi- , COMPASS |
| Table 11 row 3 | sidis:2005 | COMPASS [45] | deuteron , mult , K+  , COMPASS |
| Table 11 row 4 | sidis:2006 | COMPASS [45] | deuteron , mult , K-  , COMPASS |
| Table 11 row 5 | sidis:3000 | COMPASS [44] | deuteron , mult , h+  , COMPASS |
| Table 11 row 6 | sidis:3001 | COMPASS [44] | deuteron , mult , h-  , COMPASS |
| Table 12 row 1 | sia:1029 | Belle [58] | BELL |
| Table 12 row 2 | sia:1001 | TASSO 12 GeV [59] | TASSO (12 GeV) |
| Table 12 row 3 | sia:1002 | TASSO 14 GeV [60] | TASSO (14 GeV) |
| Table 12 row 4 | sia:1003 | TASSO 22 GeV [60] | TASSO (22 GeV) |
| Table 12 row 5 | sia:1005 | TASSO 34 GeV [61] | TASSO (34 GeV) |
| Table 12 row 6 | sia:1006 | TASSO 44 GeV [61] | TASSO (44 GeV) |
| Table 12 row 7 | sia:1010 | TPC(c) [62] | TPC(c) |
| Table 12 row 8 | sia:1011 | TPC(b) [62] | TPC(b) |
| Table 12 row 9 | sia:1013 | TOPAZ [64] | TOPAZ |
| Table 12 row 10 | sia:1014 | SLD [68] | SLD |
| Table 12 row 11 | sia:1015 | SLD(uds) [68] | SLD(uds) |
| Table 12 row 12 | sia:1016 | SLD(c) [68] | SLD(c) |
| Table 12 row 13 | sia:1017 | SLD(b) [68] | SLD(b) |
| Table 12 row 14 | sia:1018 | ALEPH [65] | ALEPH |
| Table 12 row 15 | sia:1019 | OPAL [67] | OPAL |
| Table 12 row 16 | sia:1025 | DELPHI [66] | DELPHI |
| Table 12 row 17 | sia:1026 | DELPHI(uds) [66] | DELPHI(uds) |
| Table 12 row 18 | sia:1027 | DELPHI(b) [66] | DELPHI(b) |
| Table 12 row 19 | sia:1028 | BaBar (prompt) [57] | BABAR |
| Table 12 row 20 | sia:1030 | ARGUS [56] | ARGUS |
| Table 13 row 1 | sia:2029 | Belle [58] | BELL |
| Table 13 row 2 | sia:2001 | TASSO 12 GeV [59] | TASSO (12 GeV) |
| Table 13 row 3 | sia:2002 | TASSO 14 GeV [60] | TASSO (14 GeV) |
| Table 13 row 4 | sia:2003 | TASSO 22 GeV [60] | TASSO (22 GeV) |
| Table 13 row 5 | sia:2005 | TASSO 34 GeV [61] | TASSO (34 GeV) |
| Table 13 row 6 | sia:2008 | TPC [63] | TPC 1988 |
| Table 13 row 7 | sia:2013 | TOPAZ [64] | TOPAZ |
| Table 13 row 8 | sia:2014 | SLD [68] | SLD |
| Table 13 row 9 | sia:2015 | SLD(uds) [68] | SLD(uds) |
| Table 13 row 10 | sia:2016 | SLD(c) [68] | SLD(c) |
| Table 13 row 11 | sia:2017 | SLD(b) [68] | SLD(b) |
| Table 13 row 12 | sia:2018 | ALEPH [65] | ALEPH |
| Table 13 row 13 | sia:2019 | OPAL [67] | OPAL |
| Table 13 row 14 | sia:2025 | DELPHI [66] | DELPHI |
| Table 13 row 15 | sia:2026 | DELPHI(uds) [66] | DELPHI(uds) |
| Table 13 row 16 | sia:2027 | DELPHI(b) [66] | DELPHI(b) |
| Table 13 row 17 | sia:2028 | BaBar (prompt) [57] | BABAR |
| Table 13 row 18 | sia:2030 | ARGUS [56] | ARGUS |
| Table 14 row 1 | sia:4003 | TASSO 12 GeV [59] | TASSO (12 GeV) |
| Table 14 row 2 | sia:4008 | TASSO 14 GeV [69] | TASSO (14 GeV) |
| Table 14 row 3 | sia:4009 | TASSO 22 GeV [69] | TASSO (22 GeV) |
| Table 14 row 4 | sia:4010 | TASSO 30 GeV [70] | TASSO (30 GeV) |
| Table 14 row 5 | sia:4012 | TASSO 44 GeV [61] | TASSO (44 GeV) |
| Table 14 row 6 | sia:4004 | TPC [63] | TPC |
| Table 14 row 7 | sia:4002 | SLD [68] | SLD |
| Table 14 row 8 | sia:4016 | SLD(uds) [68] | SLD(uds) |
| Table 14 row 9 | sia:4014 | SLD(c) [68] | SLD(c) |
| Table 14 row 10 | sia:4015 | SLD(b) [68] | SLD(b) |
| Table 14 row 11 | sia:4000 | ALEPH [71] | ALEPH |
| Table 14 row 12 | sia:4007 | OPAL [72] | OPAL |
| Table 14 row 13 | sia:4001 | DELPHI [66] | DELPHI |
| Table 14 row 14 | sia:4017 | DELPHI(uds) [66] | DELPHI(uds) |
| Table 14 row 15 | sia:4013 | DELPHI(b) [66] | DELPHI(b) |

## Runtime semantics

- **normalization_scale**: Theory divided by current normalization in tools/residuals.py395–401.
- **normalization_dN**: First nonzero measured point norm_c/value, after percent-to-absolute conversion. tools/residuals.py15–29,52–78.
- **penalty**: _get_nres477–492 returns0 for absent/fixed/linked norm or explicit _S covariance; otherwise (N−1)/dN. Data norm_c excluded from ordinary correlated sources.
- **weak_penalty**: Eight freenorm files encode norm_c=1000×value (floating precision around1000); residual coefficient1e−3, squared penalty coefficient1e−6. This approximates paper unpenalized behavior but is not mathematically identical.
- **freenorm_belle_kaon_metadata**: 2029freenorm.xlsx obs says 1/sig dsig/dz; table XIII says dσ/dz with asterisk. Preserve this metadata discrepancy rather than silently relabel scientific observable.
- **empty_dataset_skip**: tools/reader.py32–50 drops zero-point tables, explaining98 declared vs96 retained dataset groups.
- **cut_errors**: Native audit confirms four jet x<1.000000 queries fail because x is absent and are silently ignored, preserving pT>8 only; accepted counts still exactly match publication. This is verified reader behavior, not a proposed code change.
- **covariance_matrix**: tools/residuals.py distinguishes fullcovariance _S, correlatedcovariance _C and correlation _V. BABAR files use _V + syst_c with separate norm_c, so keeping their independent fitted normalizations matches paper normpenalties. No claim that _Sskip applies to these files.

## Remaining limitations

- Exact historical workbook revisions: current98 selected workbook hashes captured, but not proven byte-identical to publication runtime.
- Original fit-code commit/runtime and covariance-construction version not established by current code inspection.
- Paper unpenalized wording vs finite dN1000 regularization discrepancy remains documented.
- Bellekaon2029 normalized-observable workbook metadata vs unnormalized paper row remains documented.

All changes in this subtask are review artifacts outside the application checkout. No physics executed and no remote files written.


### Read-only native reader validation

See the structured per-dataset result in `source_verification.current_reader_point_validation`. No theory, fit or replica evaluation was run. Summary:

```json
{
  "declared_datasets": 98,
  "retained_nonempty_datasets": 96,
  "retained_points": 5853,
  "reader_errors": [],
  "ignored_filter_errors": [
    {
      "reaction": "jet",
      "id": 10001,
      "filter": "x<1.000000",
      "error": "UndefinedVariableError(\"name 'x' is not defined\")"
    },
    {
      "reaction": "jet",
      "id": 10002,
      "filter": "x<1.000000",
      "error": "UndefinedVariableError(\"name 'x' is not defined\")"
    },
    {
      "reaction": "jet",
      "id": 10003,
      "filter": "x<1.000000",
      "error": "UndefinedVariableError(\"name 'x' is not defined\")"
    },
    {
      "reaction": "jet",
      "id": 10004,
      "filter": "x<1.000000",
      "error": "UndefinedVariableError(\"name 'x' is not defined\")"
    }
  ],
  "empty_dataset_ids": [
    "sia:1012",
    "sia:2006"
  ],
  "limitation": "Current pinned data/readers reproduce counts; this is not evidence of identical historical runtime, fitting, replica ensemble or physics predictions."
}
```

## Current Gaussian sampling and application contract

### gaussian.case

Recognized metadata key is lowercase 'gaussian'; uppercase 'Gaussian' is not recognized by the inspected parameter sampler. The hypothesis {'Gaussian': True} alone falls through to uniform initialization, not Gaussian sampling.

Sources: fitlib/parman.py:63-82.

### gaussian.mean_std

For a free model parameter with truthy lowercase gaussian, gen_flat reads mean and std directly and draws np.random.normal(loc=mean, scale=std), rejecting until within its min/max inclusive. Missing mean/std raises KeyError. FitPack sampler itself does not validate finiteness or positive standard deviation; invalid/degenerate configurations can error or fail to terminate.

Sources: fitlib/parman.py:24-36, fitlib/parman.py:63-82.

### gaussian.scope

Gaussian initialization applies only j==1 free distribution parameters (fixed is False). Fixed or linked parameters are absent from the free sampling vector. Dataset normalization parameters use j==2 and remain uniformly sampled within bounds, even if Gaussian metadata were attached.

Sources: fitlib/parman.py:24-50, fitlib/parman.py:65-79.

### gaussian.activation

Metadata alone is insufficient: MAXLIKE.get_guess calls gen_flat only when conf['flat par'] is true. With false, it uses input starting values. A supplied seed initializes NumPy before draws. Previous-step or buffer values may overwrite generated guesses afterward.

Sources: fitlib/maxlike.py:261-280, fitlib/maxlike.py:323-342.

### gaussian.objective

Lowercase gaussian metadata controls initialization and does not append a Gaussian parameter-prior penalty in the inspected current objective path. MAXLIKE.get_residuals combines RESMAN experimental/constraint residuals with correlated and normalization residuals; neither inspected component consumes gaussian/mean/std for parameter penalties. Existing experimental nuisance and positivity penalties remain distinct.

Sources: fitlib/maxlike.py:110-118, fitlib/maxlike.py:344-378, fitlib/resman.py:1150-1334.

### gaussian.app_ff

FF library returns deep-copied raw metadata; FF resolver preserves/accepts lowercase gaussian, mean, std along with value/min/max/fixed. It requires finite mean/std, std>0, and boolean gaussian. Uppercase Gaussian is outside the accepted field whitelist and is not preserved as an override. Gateway FF normalization and campaign snapshot carry the resolved lowercase metadata into materialized input.

Sources: lqcdmcp-deploy/jamai/ff_registry.py:143-179, lqcdmcp-deploy/jamai/ff_registry.py:184-227, lqcdmcp-deploy/jam_gateway.py:1630-1653, lqcdmcp-deploy/jam_gateway.py:2109-2139, lqcdmcp-deploy/jamai/input_materializer.py:216-245.

### gaussian.app_pdf

Non-FF PDF/pion gateway normalization reconstructs rows with name/fixed/value/min/max only, and common materializer non-FF overrides carry only value/min/max/fixed. Adding new Gaussian metadata through that card pathway is not supported. Existing unmodified template fields may survive template composition, but template metadata was not inspected and must not be assumed.

Sources: lqcdmcp-deploy/jam_gateway.py:1656-1677, lqcdmcp-deploy/jamai/input_materializer.py:119-135.

### gaussian.app_editor

The frontend editor merges library rows and edits; FF metadata survives ordinary edits, but visible controls cover enabled/fixed/links/value/min/max, not Gaussian prior settings. The UI has only default versus random_within_bounds initialization modes; no separately named Gaussian mode. For FFs random_within_bounds reaches FitPack gen_flat, whose per-parameter metadata can make those draws bounded Gaussian despite the generic UI label.

Sources: lqcdmcp-deploy/jam_panel.html:1016-1042, lqcdmcp-deploy/jam_panel.html:1121-1184, lqcdmcp-deploy/jam_panel.html:347, lqcdmcp-deploy/jam_gateway.py:1353, lqcdmcp-deploy/jam_gateway.py:2106-2139.

### gaussian.app_execution

Common input writes jamai parameter initialization metadata; app run_replica delegates to fitlib.ai. ai runtime sets flat par true exactly for random_within_bounds; runtime seed derives base_seed+replica_id when per-replica seeds enabled, then MAXLIKE.get_guess seeds NumPy before sampling. The audit and actual optimization independently prepare and seed the same path. No execution was performed in this investigation.

Sources: lqcdmcp-deploy/jamai/input_materializer.py:182-187, lqcdmcp-deploy/jamai/run_replica.py:65-74, fitlib/ai.py:290-329, fitlib/ai.py:587-622, fitlib/ai.py:657-662, fitlib/maxlike.py:261-269.

### gaussian.historical_reproduction

The current FitPack sampler can implement independent, bounds-conditioned Gaussian starts without changing observable physics, if the historical truncation/correlation/staging policy is compatible. Author subsequently supplied numerical prior means/stds directly, replacing the missing screenshot; these values are author-confirmed evidence and are not inferred from matching current app defaults. The input file alone does not establish exact historical reproduction: author says historical Gaussian initialization occurred externally, while historic truncation, correlations, seed, stage-transfer policy, and applicability to PDF parameters remain unverified. Dataset-normalization prior applicability must not be inferred.

Sources: author_clarifications_20261019; current_fitpack fitlib/parman.py:63-82 and fitlib/maxlike.py:261-280.

## Current native implementation evidence

### native.pdf_mapping

{"g1":"g","uv1":"u_v","dv1":"d_v","sea1":"S1","sea2":"S2","ub1":"delta_ubar","db1":"delta_dbar","s1":"delta_s","sb1":"delta_sbar"}

Sources: qcdlib/pdf.py:96-159.

### native.template_moments

Current PDF and FF moment functions normalize each template at moment N=2, agreeing with the paper beta-moment form; no arbitrary exponent shift is inferred from UI xf prose.

Sources: qcdlib/pdf.py:178-193, qcdlib/ffpion.py:172-189, qcdlib/ffkaon.py:170-186, qcdlib/ffsum.py:99-114.

### native.absent_parameters

Current parameter propagation sets omitted coefficients to zero. Therefore absent PDF s1/sb1/sea2 c,d implement zero polynomial coefficients, rather than six active fixed declarations.

Sources: fitlib/parman.py:252-278.

### native.total_hadron

Residual shape moments are combined with evolved pion and kaon objects to form total unidentified-hadron FFs. Pion and kaon selectors are 0; residual input selector is sum2, while current RESMAN instantiates ffsum.FF directly.

Sources: fitlib/resman.py:310-323, qcdlib/ffsum.py:196-225.

### native.current_defaults

{"mc_GeV":1.28,"mb_GeV":4.18,"mZ_GeV":91.1876,"alphaS_at_mZ":0.118,"nuclear_smearing_default":"paris"}

Sources: qcdlib/aux.py:22-75, qcdlib/alphaS.py:25-43, fitlib/resman.py:240-249.

### native.positivity_activation

Current RESMAN appends positivity residuals only when the corresponding space/underscore flag is truthy. Missing flags in the official input agree with the latest author-confirmed disabled policy.

Sources: fitlib/resman.py:1236-1257.

## Updated resolution register

| Original ID | Current state | Resolution / remaining gap |
| --- | --- | --- |
| U01 | partially_resolved | All 369 literal parameter definitions and configured values recovered. Distinguish stored starts and linked/sum-rule values from an ensemble posterior; historical calibrated starting ensemble remains unknown. |
| U02 | partially_resolved | Exact bounds and fixed/link states recovered. Author supplies coefficient-type Gaussian means/stds; complete applicability map and historical sampling/truncation policy remain unverified. |
| U03 | partially_resolved | Exact 98 configured reaction-qualified IDs/paths recovered. Current native readers reproduce 5853 retained points in 96 nonempty datasets; the two extra SIA files retain zero points. Exact historical data/grid revisions remain unresolved. |
| U04 | resolved_for_configuration | Latest author confirms all four positivity flags disabled. Official input has no enabling flags and current native activation is conditional. Use all four False; earlier True author statement superseded. Positivity algorithm details do not block reproduction when disabled. |
| U05 | partially_resolved | Input bootstrap=True and resample version=0 recovered. Replica count, historical seeds/resampling implementation, convergence and ensemble selection remain unknown. |
| U06 | partially_resolved | Author confirms external Gaussian starts and supplies coefficient-type hyperparameters. Input flat par=False is compatible with externally supplied starts; full historical transfer and correlation policy remains unknown. |
| U07 | resolved_for_current_data_configuration | 52 normalization declarations resolve to 43 free plus 9 links; starts/bounds, exact SLD groups and current workbook relative uncertainties recovered. Eight nominally unpenalized cases use finite dN approximately 1000. Historical data revisions remain part of U03. |
| U08 | resolved_for_configuration | Both shapes including gluon independently confirmed; all 150 coefficient links match flavor equalities and targets. Exact current template/total-hadron implementation inspected; historical code revision still belongs to U09. |
| U09 | partially_resolved | Official flags NLO,truncated,backward,AOT,nuc=True,ht=False,offpdf=False and selectors recovered. Current native defaults inspected separately; exact historical implementation/grid hashes and remaining inherited settings unresolved. |
| U10 | partially_resolved | Exact per-ID SIA windows recovered without inventing a universal energy threshold. Historical observable conversion/bin integration and data revisions remain to verify. |
| U11 | partially_resolved | Input ftol=1e-3 recovered. Exact historical minimizer, convergence/caps and numerical integration settings remain unknown; do not substitute current app defaults. |
| U12 | unresolved | Input does not resolve exact credible-interval tail allocation, quantile estimator or ensemble weights. |
| U13 | partially_resolved | Input comments help identify experiments and charges, but publication typos/output rounding and provenance require explicit retained notes; source-to-paper numeric differences are now recorded. |

## Review boundary

The catalog is not reproduction-ready. Current readers and files reproduce all 96 Appendix row counts and 5853 accepted points. Historical runtime/data/grid versions, sampling/stage details and exact optimizer/ensemble/interval details remain review items. Only these three catalog files are changed. No new input, campaign, fit, replica evaluation, app feature, FitPack code, MCP tool, commit, push, synchronization or deployment was performed.

---

# Phase 3: author correction, effective cuts and positivity controls

The latest author correction supersedes only prior author positivity=True evidence. The paper-only scientific ledger, exact source parameter definitions and declared dataset configurations are unchanged.

| FitPack setting | JAM24 reproduction | New app campaign default |
| --- | --- | --- |
| pdf positivity | False | True |
| ffpion positivity | False | True |
| ffkaon positivity | False | True |
| ffhadron positivity | False | True |

The local app uses independent strict-boolean qcd fields: `pdfPositivity`, `ffpionPositivity`, `ffkaonPositivity`, `ffhadronPositivity`. Missing legacy fields retain True, matching the previous always-True materializer. Saved campaigns are not rewritten. Explicit False values survive serialization, validation and regeneration. No FitPack implementation is changed.

# Read-only SIDIS and Belle kaon follow-up

## Scope and provenance

Read on 2026-10-19 through `ifarm-copilot` (`ifarm2402.jlab.org`). Original numerical input: `/w/ceph24/JAM/treyand/pdf_plotting/strange_analysis/combined4/2-0/input.py`, SHA256 `218a8e633bf015899f9f7b68494a8a44d79b1998fb506ea99d2424c85c8f75a6`, unchanged from the previous extraction. Current referenced FitPack HEAD: `8070c674783b118878b2900fdd4074fa7272604d`. This is the current reader implementation, not proof of the historical runtime revision. Native kinematics/data-reader execution only, in existing theory-notebook image with read-only Ceph bind, Python -B and no remote artifacts. No source input execution, theory evaluation, replicas, fitting, or modifications.

## SIDIS effective cuts

Original input lines 367–380 configure Q2>1.690000, W2>10.000000, W2SIDIS>0.000000 and Z>0.2 and Z<0.8 for six deuteron datasets. Reader `obslib/sidis/reader.py` lines 42–58 computes W2 and W2SIDIS if absent; lines 64–73 computes kinematics before applying filters. `tools/reader.py` lines 13–30 applies queries sequentially.

| ID | Original count | After Q² | After W² | After W²SIDIS | After z |
|---|---:|---:|---:|---:|---:|
|1005|311|249|249|249|249|
|1006|311|249|249|249|249|
|2005|309|247|247|247|247|
|2006|309|247|247|247|247|
|3000|311|249|249|249|249|
|3001|311|249|249|249|249|
|Total|1862|1490|1490|1490|1490|

Leave-one-filter-out checks likewise confirm removing W2SIDIS>0 or the z range does not alter the selection. Across raw workbooks, W2 minimum is 31.82046193785521 GeV², W2SIDIS minimum is 7.1730846073978505 GeV², z ranges are 0.223–0.796. Thus W2SIDIS=0 is inactive for these data, and z bounds remove nothing. Q² and W² are the intended effective selection criteria; more precisely, only Q² actually removes rows in these supplied workbooks because all already satisfy W²>10. Do not claim W² removes additional rows. The cut fields remain unchanged; this is interpretation of their actual effect.

## Belle kaon metadata discrepancy

1. Reaction-qualified ID `sia:2029`; original-input path `sia/expdata_V1/2029freenorm.xlsx` (input line297), absolute `/w/ceph24/JAM/treyand/fitpack_legacy_mcp/database/sia/expdata_V1/2029freenorm.xlsx`. SHA256 `c067110f495c4c2764c52ef090829eeb5b06b99302f15764a57e601a65ea9a77`.
2. Workbook column `obs` is exactly `1/sig dsig/dz` on all78 rows. Other metadata: `col=BELLE`, `hadron=kaon`, `RS=10.52`; there is no `units` column. The source's z cut retains70 of78 rows.
3. Paper Table XIII p35 prints Belle observable `dσ/dz`, without σ⁻¹. The asterisk is on its fitted normalization `0.852(10)*`, denoting the unpenalized normalization convention; the asterisk is not attached to the observable.
4. Current SIA reader lines156–168 recognizes `/sig` as normalized; lines196–197 sets jacobian1; lines215–235 defaults `units-val=1` when no units column; lines237–248 makes it untagged. Lines44–57/117–118 derive a bin-average factor 1/(zmax−zmin), approximately100 for these .01-wide bins. Native reader output confirms all of these fields, normalized=True,70 retained rows.
5. `obslib/sia/residuals.py` lines23–51 obtains bin-integrated hadronic numerator and inclusive denominator from theory at Q²=RS², uses Nf=4 for BELLE, multiplies numerator by jac×units×bin_jac and **divides by sigtot** because normalized=True. `obslib/sia/theory.py` lines92–95 declares numerator/total outputs; lines149–156 constructs sigtot and lines170–182 handles bin integration/returns. This is a genuine computational normalization convention, not merely display text. `tools/residuals.py` lines395–401 subsequently divides by the separate fitted dataset normalization (source start1, bounds.1–100, fixedFalse, input line313); this is distinct from dividing by sigtot. Units-val1 does not introduce a cross-section-unit conversion; normalized differential distribution has no cross-section dimension.
6. For faithful original-input reconstruction, preserve the workbook value, reader branch and normalization definition exactly. No workbook or physics change is warranted. The author has closed this notation question for reproduction: original FitPack is authoritative. Do not investigate further, relabel the observable or insert a conversion. Changing `obs` to `dsig/dz` would disable division by sigtot; moreover the current reader's generic fallback at lines207–208 would assign jacobian0.5. It must not be used to 'correct' this discrepancy.

Machine evidence: `reader-followup.json`; source hashes included there. This report and scripts reside outside the application checkout.

### Inclusive denominator and possible free-normalization absorption

The returned `sigtot` is not computed from fitted FF values. Its formula is `sum_i sig0[i] * Nf_mask[i] * (1 + alphaS/pi)` (theory.py149–156), with sigma0 electromagnetic/electroweak factors at lines55–90, alphaS and perturbative order at102, and threshold/tag masks130–147. FFs enter numerator terms150–154 and158–164, not that inclusive denominator. For the fixed Belle energy, identical untagged settings and fixed theory constants this denominator is a common scale across points; it is not a z-dependent shape transformation. This statement follows code inspection; no theory was evaluated.

An ideal entirely unconstrained dataset normalization could mathematically absorb a common multiplicative scale: if numerator is U and denominator C, predictions U/(C N) could be reproduced in an unnormalized convention U/N' with N'=C N. That does not establish equivalence of this actual configuration: it has finite .1–100 bounds, an experimental norm penalty (very weak dN≈1000 in the referenced Belle workbook), no units conversion column, and the current reader's unrecognized-observable fallback changes its Jacobian. The necessary transformed normalization and any feasibility within bounds have not been evaluated and must not be assumed. Preserve the normalized workbook convention for reproduction; do not change numeric data, units, reader behavior, or the separate norm setting to match a table label.


## Remaining review boundary

Positivity policy is resolved: disabled for original JAM24, enabled by default for new generic app campaigns. Historical sampling applicability/truncation/correlations/seeds, calibrated transfer, original runtime/data/grid identity, ensemble/convergence and exact interval convention remain unresolved. No campaign, input for JAM24, fit, replica evaluation, commit, push, synchronization or deployment was performed. Temporary generated inputs for positivity regression tests are kept outside the checkout.

## Final clarification status

Belle kaon is resolved under original FitPack authority. Gaussian initialization is confirmed using the existing sampler, flat par=True and lowercase metadata; earlier uncertainty statements in the historical evidence layers do not describe current intended-procedure status. No campaign or input has been generated by this clarification update.
