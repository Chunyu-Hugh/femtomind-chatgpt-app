# evaluation/ — the paper's evidence toolchain

Everything the paper claims about the JAM gateway as an agent for global QCD
analysis is produced, graded and summarized by the code in this directory,
from artefacts a reader can re-derive. The application under test is the
gateway in `lqcdmcp-deploy/`; the published analysis is JAM24
(arXiv:2501.00665), served by the analysis catalog in `analyses/JAM24/`.

Nothing here runs on the pilot's directories (`~/bench_nomcp`, `bench_nomcp/`,
their farm folders or jobs); the evaluation has its own roots (`~/bench_eval`
locally, `/w/ceph24/JAM/chunyu/bench_eval` on the farm, gateway ports 8766+).

## Claims and where their evidence comes from

| Claim | Instrument | Artefacts |
| --- | --- | --- |
| The catalog is the official JAM24 input, key for key, and the archive's saved configuration equals it | `l1/calibrate.py` (checks 1, 2) | `l1/calibration/<date>/grade.official.json`, `grade.bundle.json`, `runtime_keys.json` |
| The official input changed after the catalog read (2026-10-23; SIDIS filter and dataset 1115) | `l1/calibrate.py` (check 1, drift) | `l1/calibration/2026-10-26/calibration.json` |
| The author clarifications are exactly 650 overlay paths (645 Gaussian-initialization fields, flat par, 4 positivity flags) | `l1/calibrate.py` (check 3), `l1/expected.py` | `calibration.json`, test `test_evaluation_expected` |
| The compiler's rendered input is the reproduction configuration exactly | `l1/calibrate.py` (check 4) | `grade.compiler.json` |
| The theory at the archived parameters is recomputed at machine precision: 791 replicas, 96/96 datasets comparable, data chi2 per replica within 1e-14 relative; 49 datasets bit-identical; grid-based observables (jet, DY) differ by up to 6e4 ULP (historical grid versions, U09) | `analyses/JAM24/results/l2_recompute.py`, `l2/submit_l2.py`, `l2/l2_report.py` | `l2/report_2026-10-26_791replicas.md`, `~/bench_eval/l2/20261026_105815/` |
| Configuration recoverability under knowledge conditions A/B/C, with and without the gateway | `matrix/runner.py` → `l1/grade.py`, `conditions/leak_scan.py`, `matrix/classify.py`, `matrix/probe.py`, `matrix/aggregate.py` | `~/bench_eval/results/<run>/{classify,probe,grade,leak,fetch}.json`, `aggregate.{json,md}` |
| What a paper-only agent gets right and wrong (pilot run3: 215 free parameters, 20 wrong fixed/free, 11 wrong SIA ids, Q20, ftol, flat par) | `l1/calibrate.py` (checks 5, 6) | `grade.pilot_nomcp.json`, `grade.pilot_mcp.json` |
| A wrong card does not reach the queue quietly: every injected fault is caught or flagged, and the ones that are not (job settings) are named | `e1/runner.py` (validate_campaign on mutants of the official card), `e1/report.py` | `~/bench_eval/e1/e1_<stamp>.json`, the fault table |
| What each layer contributes: with validate / catalog parity / human gate removed in turn, the caught-fault rate falls back toward the raw-LLM arm | `JAM_GW_ABLATE` in `jam_gateway.py` (test gateways only) + `matrix/runner.py` | `~/bench_eval/results/<run>/…`, test `test_evaluation_e2` |

## Layout

- `common/` — `paths.py` (roots), `stats.py` (Wilson, Clopper–Pearson, pass@k, pass^k, bootstrap), `ssh.py` (programs to the farm over a ControlMaster; stdin only; container with read-only binds).
- `l1/` — the L1 grader. `expected.py` compiles the catalog to the literal and reproduction confs of `JAM24.final` and `JAM24.current`; `remote_dump.py` loads a candidate input exactly as FitPack's `tools.config.load_config` does (chains: the last input; `conf['steps']`: projected as `MAXLIKE.get_conf`), and emits the typed conf; `canonicalize.py` holds the three tiers (0 exact typed parity, 1 scientific equivalence, 2 structural coverage); `grade.py` writes `femtomind.l1_grade.v1` reports; `calibrate.py` runs the six calibration checks; `cli.py` is the command line.
- `conditions/` — `materials.py` (a run's prompt, paper and reference files; `prompts/` holds the pilot's prompts verbatim and the generated A/B/C variants with hashes), `derive_catalog.py` (the condition-B catalog: author layer removed, leak-checked), `deny_lists.py` (hidden tools per condition), `leak_scan.py` (what a transcript saw that its condition withheld).
- `matrix/` — `box.sh` (the pilot's bubblewrap/apptainer sandbox with the evaluation's roots), `remote/` and `local/` (farm-side scripts and the agent's three entry commands), `gateway.py` (a local gateway from a pinned commit, catalog variant and deny list), `classify.py` (what happened in a run), `probe.py` (watch the job; cancel it once the fit started or failed), `fetch.py` (artefacts → grade → leak scan), `aggregate.py`, `runner.py`.
- `e1/` — fault injection. `mutants.py` builds one realistic error per mutant from a card the gateway produced (so the base is parity-checked, not hand-written), `runner.py` asks the gateway's `validate_campaign` about each one and records refused/flagged/silent with the stable code, `report.py` renders the table. Read-only: `validate_campaign` never queues.

## Running

Tests (stdlib unittest, in the sibling checkout `../femtomind-tests/tests`):

    FEMTOMIND_APP_ROOT=$PWD python3 -B -m unittest discover -s ../femtomind-tests/tests -p 'test_evaluation_*.py'

E1 fault table (any gateway with its token; no cluster work of its own, since validate does the reads on the node):

    python3 -B -m evaluation.e1.runner --url https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp \
        --token-file ~/.config/jam_gateway/jam-gateway-<you>.token --out ~/bench_eval/e1/e1_$(date +%F).json
    python3 -B -m evaluation.e1.report ~/bench_eval/e1/e1_2026-10-30.json --md e1.md

E2 ablation: the E1 sweep re-run against four local gateways — full, no validate, no catalog parity, no human gate — and the table says which layer each fault depends on.

    python3 -B -m evaluation.e2.runner --commit HEAD --port 8766 [--dry-run]
    # -> ~/bench_eval/e2/e2.md: fault x layer, plus "caught by"

A gateway only accepts `JAM_GW_ABLATE` on loopback or with `JAM_GW_TEST_TOKEN`; a deployed gateway ignores it and says so in its log. Every reply produced with a layer removed carries `"ablation": [...]`, so a blinded run cannot be mistaken for a normal one:

    JAM_GW_BIND=127.0.0.1 JAM_GW_ABLATE=validate,catalog python3 -B lqcdmcp-deploy/jam_gateway.py

Calibration (needs a ControlMaster to ifarm; `FEMTOMIND_IFARM_SOCK` names its socket):

    FEMTOMIND_IFARM_SOCK=~/.ssh/controlmasters/ifarm-eval python3 -B -m evaluation.l1.calibrate --out evaluation/l1/calibration/$(date +%F)

Matrix (sign in once with `./ifarm_controlmaster.sh` using `IFARM_SOCK=~/.ssh/controlmasters/ifarm-eval`; point `~/fm-cm/{gateway,ifarm}-chunyu` at that socket for the gateway):

    evaluation/matrix/box.sh install
    python3 -B -m evaluation.matrix.runner --n 3 --dry-run         # the plan and manifest
    python3 -B -m evaluation.matrix.runner --n 3 --conditions A B C --features ab

Each run costs what the pilot measured (no-MCP ≈ 23 min, $4.6; MCP ≈ 3 min, $1) plus the probe's wait for the job to start fitting.

## Knowledge conditions

- A: the paper (`JAM24_paper.pdf`); without MCP also FitPack's examples and database; with MCP the catalog and archive tools are hidden.
- B: A plus `reference/jam24_input.py`, the catalog's literal configuration rendered as a FitPack input (key for key the fit-time official input; the authors' file on disk changed on 2026-10-23); with MCP the gateway serves the derived catalog without the author layer.
- C: B plus `reference/jam24_author_clarifications.txt`, generated from the catalog's author layer; with MCP the full catalog.

Grading: A and B against the literal configuration, C against the reproduction configuration (literal + 650 overlay paths). `JAM24.current` is graded alongside with its by-construction differences listed, so a card built for one target is not marked wrong against the other without saying why.
