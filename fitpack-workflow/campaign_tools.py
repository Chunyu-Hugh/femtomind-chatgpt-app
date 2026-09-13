"""JAM25 from-scratch reproduction tools (ladder + acceptance).

The FemtoMind app's reproduction capability: run the 4-step JAM25 ladder
(arXiv:2510.11979, 246 pts, 25 params) designed from the paper text, manage
posterior->prior ensemble waves, and compare results against the published
numbers (chi2 table, Table 1 lattice moments, member-0 LHAPDF grids, beta
exponents). All computation runs on the cluster in the group container.

Register with:  from campaign_tools import register_campaign_tools
                register_campaign_tools(mcp)
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from ssh_runner import ssh_run, ssh_read_file, ssh_write_file, ssh_mkdir

logger = logging.getLogger("fitpack-mcp")

L2 = "/w/ceph24/JAM/chunyu/jam25repro/l2"
FP = "/w/ceph24/JAM/chunyu/fitpack_legacy"
SIF = "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"
ENV = ("--env PYTHONPATH=%s:/usr/local/lib/python3.11/site-packages "
       "--env FITPACK=%s "
       "--env LHAPDF_DATA_PATH=/w/ceph24/JAM/chunyu/lhapdf_tables:/w/ceph24/JAM/apps/lhapdf_tables/more/more1") % (FP, FP)

# ladder definition: step -> card used for that step's fit
LADDER = {
    1: "input_l2_s1.py",   # dy-pion only, g1 N fixed
    2: "input_l2.py",      # + LN, g1 N free
    3: "input_l2_s3.py",   # + pion lattice moments
    4: "input_l2_s4.py",   # + NA3 ratio + kaon sector (full 246 pts)
}

PAPER = {
    "chi2": {
        "dy-pion": 0.78, "ln": 0.87, "NA3": 0.08,
        "lattice": 0.65, "total": 0.79,
    },
    "moments": {  # Table 1 JAM column (value, unc)
        ("kaon", 1, "u"): (0.269, 0.007), ("kaon", 1, "s"): (0.340, 0.011),
        ("kaon", 1, "g"): (0.333, 0.017), ("pion", 1, "u"): (0.256, 0.010),
        ("pion", 1, "g"): (0.439, 0.028),
    },
    "beta": {"pion ub": 1.16, "kaon ub": 1.6, "kaon s": 1.2},
}


def _shell(cmd: str, username: str, timeout: int = 300) -> Dict[str, Any]:
    # ssh_run takes argv elements; pass one pre-quoted script to bash -lc
    p = ssh_run(["bash", "-lc", cmd], timeout=timeout, username=username)
    return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-500:]}




def _submit_step(step: int, seed: int, prior_msr: str, username: str,
                 partition: str = "production") -> Dict[str, Any]:
    """Shared submit logic: write SLURM script and submit. Returns job info."""
    card = LADDER[step]
    ssh_mkdir(f"{L2}/appruns", username)
    out = f"{L2}/appruns/step{step}_{seed}"
    walltime = "04:00:00" if partition == "priority" else "10:00:00"
    script = f"""#!/bin/bash
#SBATCH --job-name=l2app{step}
#SBATCH --partition={partition}
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4
#SBATCH --time={walltime} --mem=4G
#SBATCH --output={out}.log --error={out}.err
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
cd {FP}
CEPH_GRIDS="/w/ceph24/JAM/share/fitpack_grids"
for obs in dy zrap wasym wzrv jet wc pjet dypion pp_hadron; do
  [ -d "$CEPH_GRIDS/$obs" ] && [ ! -e "{FP}/grids/$obs" ] && ln -sfn "$CEPH_GRIDS/$obs" "{FP}/grids/$obs"
done
mkdir -p {out}
ARGS="{L2}/{card} {out} -n 4 -v 10 -s {seed}"
[ -n "{prior_msr}" ] && ARGS="$ARGS -p {prior_msr}"
apptainer exec --bind /w/ceph24 {ENV} {SIF} python3 -m fitlib.maxlike_jam $ARGS
echo "EXIT=$?"
"""
    ssh_write_file(f"{L2}/appruns/submit_{step}_{seed}.sh",
                   script.format(step=step, seed=seed, prior_msr=prior_msr,
                                 out=out, partition=partition, L2=L2, FP=FP,
                                 ENV=ENV, SIF=SIF), username=username)
    p = _shell(f"cd {FP} && sbatch {L2}/appruns/submit_{step}_{seed}.sh", username)
    m = re.search(r"Submitted batch job (\d+)", p["stdout"])
    return {"job_id": m.group(1) if m else None,
            "out": out, "step": step, "card": card}


def _wait_job(job_id: str, username: str, timeout_min: int = 180) -> Dict[str, Any]:
    """Poll squeue until the job leaves the queue; return state + msr path."""
    import time
    waited = 0
    while waited < timeout_min * 60:
        p = _shell(f"squeue -j {job_id} -h", username, timeout=30)
        if not p["stdout"].strip():
            break
        time.sleep(60)
        waited += 60
    return {"waited_min": waited // 60}


def register_campaign_tools(mcp, insert_task=None) -> None:


    @mcp.tool()
    def ladder_overview(chat_id: str = "") -> Dict[str, Any]:
        """Overview of the JAM25 from-scratch ladder (steps, cards, data, paper targets)."""
        return {
            "paper": "arXiv:2510.11979v2 (JAM25 pion+kaon, NLO+NLL, 246 pts, 25 params)",
            "ladder": [
                {"step": s, "card": LADDER[s],
                 "adds": ["dy-pion (117 pts)", "+LN (108 pts)",
                          "+pion lattice (6 pts)", "+NA3 (6 pts) + kaon (9+6 lattice)"][s - 1],
                 "prior": "flat (seed)" if s == 1 else f"step {s-1} msr"}
                for s in LADDER
            ],
            "paper_chi2": PAPER["chi2"],
            "paper_moments": {f"<x^{n}>_{f}^{h}": v for (h, n, f), v in PAPER["moments"].items()},
            "paper_beta": PAPER["beta"],
            "status": "chain2 (physics-prior full chain) validating; 10-chain wave next",
        }

    @mcp.tool()
    def ladder_msr_status(chat_id: str = "", username: str = "") -> Dict[str, Any]:
        """List which ladder-step msr files currently exist on the cluster."""
        p = _shell(f"ls {L2}/chain2/step*/msr/ 2>/dev/null; ls {L2}/smoke4/step4/msr/ 2>/dev/null", username)
        return {"stdout": p["stdout"] or "(none yet)"}

    @mcp.tool()
    def run_ladder_step(step: int, seed: int, chat_id: str = "",
                        prior_msr: str = "", username: str = "",
                        partition: str = "production") -> Dict[str, Any]:
        """Run one step of the JAM25 ladder on the cluster (apptainer + maxlike_jam).

        step: 1-4. prior_msr: required for steps 2-4 (the previous step's msr path).
        Submits a SLURM job on the production partition.
        """
        if step not in LADDER:
            return {"status": "error", "message": "step must be 1..4"}
        if step > 1 and not prior_msr:
            return {"status": "error", "message": "prior_msr required for steps 2-4"}
        info = _submit_step(step, seed, prior_msr, username, partition)
        return {
            "status": "submitted" if info["job_id"] else "error",
            "slurm_job_id": info["job_id"],
            "step": step, "card": info["card"], "seed": seed,
            "output_dir": info["out"],
            "next": f"after completion use prior_msr={info['out']}/msr/<name>.msr for step {step+1}"
            if step < 4 else "chain complete: run compare_with_paper",
        }

    @mcp.tool()
    def launch_ladder_wave(nchains: int, seed: int, concurrent: int = 4,
                           chat_id: str = "", username: str = "",
                           partition: str = "production", wave_dir: str = "") -> Dict[str, Any]:
        """Launch an ensemble wave: nchains x (step1->step2->step3->step4) on SLURM.

        Queue guard: refuses to submit when the account already has >80 active jobs.
        """
        p = _shell("squeue -u chunyu -h | wc -l", username)
        try:
            active = int((p["stdout"] or "0").strip().splitlines()[-1])
        except Exception:
            active = 0
        if active > 80:
            return {"status": "abort", "message": f"queue guard: {active} active jobs"}
        ssh_mkdir(f"{L2}/appwave", username)
        wave = f"{L2}/appwave/{seed}" if not wave_dir else wave_dir
        walltime = "04:00:00" if partition == "priority" else "10:00:00"
        script = f"""#!/bin/bash
#SBATCH --job-name=l2appwave
#SBATCH --partition={partition}
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4
#SBATCH --time={walltime} --mem=4G
#SBATCH --array=1-{nchains}%{concurrent}
#SBATCH --output={wave}/logs/slurm_%A_%a.out --error={wave}/logs/slurm_%A_%a.err
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
cd {FP}
CEPH_GRIDS="/w/ceph24/JAM/share/fitpack_grids"
for obs in dy zrap wasym wzrv jet wc pjet dypion pp_hadron; do
  [ -d "$CEPH_GRIDS/$obs" ] && [ ! -e "{FP}/grids/$obs" ] && ln -sfn "$CEPH_GRIDS/$obs" "{FP}/grids/$obs"
done
CHAIN=$(printf "%03d" $SLURM_ARRAY_TASK_ID)
OUT={wave}/$CHAIN
mkdir -p $OUT/step1 $OUT/step2 $OUT/step3 $OUT/step4
S=$(({seed} + SLURM_ARRAY_TASK_ID))
RUN() {{ apptainer exec --bind /w/ceph24 {ENV} {SIF} python3 -m fitlib.maxlike_jam "$@"; }}
RUN {L2}/input_l2_s1.py $OUT/step1 -n 4 -v 10 -s $S || exit 1
cp "$OUT/step1/progress.json" "$OUT/progress.json" 2>/dev/null || true
P1=$(ls $OUT/step1/msr/*.msr | head -1)
RUN {L2}/input_l2.py    $OUT/step2 -n 4 -v 10 -s $S -p $P1 || exit 1
cp "$OUT/step2/progress.json" "$OUT/progress.json" 2>/dev/null || true
P2=$(ls $OUT/step2/msr/*.msr | head -1)
RUN {L2}/input_l2_s3.py $OUT/step3 -n 4 -v 10 -s $S -p $P2 || exit 1
cp "$OUT/step3/progress.json" "$OUT/progress.json" 2>/dev/null || true
P3=$(ls $OUT/step3/msr/*.msr | head -1)
apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages \
  {SIF} python3 {L2}/update_s4_shapes.py $P3 || exit 1
RUN {L2}/input_l2_s4.py $OUT/step4 -n 4 -v 10 -s $S -p $P3 || exit 1
cp "$OUT/step4/progress.json" "$OUT/progress.json" 2>/dev/null || true
echo "CHAIN=$CHAIN DONE"
"""
        ssh_write_file(f"{L2}/appwave/submit.sh", script, username=username)
        p = _shell(f"cd {FP} && sbatch {L2}/appwave/submit.sh", username)
        m = re.search(r"Submitted batch job (\d+)", p["stdout"])
        return {"status": "submitted" if m else "error",
                "slurm_array_id": m.group(1) if m else None,
                "nchains": nchains, "seed": seed, "wave_dir": wave}

    # Generic ladder order (same as server._LADDER_ORDER; sia is the beta track).
    _GEN_LADDER_ORDER = ["idis", "dy", "wzrv", "wasym", "zrap", "jet", "sia", "sidis", "wc"]

    @mcp.tool()
    def run_staged_ladder(
        chat_id: str = "",
        families: Optional[List[Dict[str, Any]]] = None,
        count: int = 10,
        partition: str = "production",
        cpus: int = 0,
        time_min: int = 0,
        mem_mb: int = 0,
        advanced: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run a MULTI-REACTION selection as an automatic sequential ladder.

        The user picks several reaction families at once (e.g. DIS + DY + jets);
        this tool orders them by the standard JAM ladder, then runs them ONE BY
        ONE: stage 1 fits the first family alone, stage 2 refits the first two
        together starting from stage 1's posterior, and so on — each stage
        inherits the previous stage's per-replica posteriors as initial values.

        `families` is a list of {"observable": "idis", "selected_ids": [...]}.
        Leave selected_ids empty to use each family's default datasets. The
        standard order is DIS -> DY -> W/Z -> jets -> SIA -> SIDIS -> W+charm;
        families not in that table keep their given position.

        This is a RESUME-STYLE state machine: call it repeatedly, each call
        advances one unit of work (submit a stage / collect a finished stage and
        submit the next / finalize). The returned dict has "stage", "status"
        ("submitted"/"running"/"diverged"/"complete") and a "hint" telling you
        when to call again. If a stage diverges (chi2/N > 5) it STOPS and asks
        the human to decide — that is the confirm-parameters step of the loop."""
        import os as _os
        import time as _time
        from dataset_catalog import DATASET_CATALOG, DATASET_FILTERS

        state_file = _os.path.join("/tmp", f"fm_ladder_{chat_id or 'default'}.json")

        def load_state():
            try:
                with open(state_file) as f:
                    return json.load(f)
            except Exception:
                return None

        def save_state(st):
            with open(state_file, "w") as f:
                json.dump(st, f)

        def submit_stage(plan, k):
            from server import run_fitting_job
            fams = plan["families"]
            primary = fams[0]["observable"]
            extra = [{"observable": fams[j]["observable"],
                      "selected_ids": fams[j]["selected_ids"]} for j in range(1, k + 1)]
            res = run_fitting_job(
                chat_id=chat_id,
                observable=primary,
                selected_ids=[str(i) for i in fams[0]["selected_ids"]],
                count=count,
                job_number=f"lad{plan['nonce']}",
                batch_label=f"auto-ladder-{chat_id or 'default'[:8]}",
                extra_observables=extra,
                fresh_start=(k == 0),
                partition=partition,
                cpus=cpus,
                time_min=time_min,
                mem_mb=mem_mb,
                advanced=advanced,
            )
            if isinstance(res, dict) and (res.get("error") or res.get("status") == "error"):
                return {"error": res}
            batch_id = res.get("batch_id") or ""
            plan["submitted"] = {"stage": k, "batch_id": batch_id}
            save_state(plan)
            return {"stage": k + 1, "status": "submitted", "batch_id": batch_id,
                    "hint": "call again in a few minutes to check the stage"}

        def check_batch(batch_id):
            from server import _is_batch_completed
            try:
                return bool(_is_batch_completed(batch_id))
            except Exception:
                return False

        def batch_chi2n(batch_id):
            try:
                import chi2_bootstrap_false as _chi2bf
                st = _chi2bf.check_status(batch_id)
                if st.get("status") == "done":
                    res = st.get("result") or {}
                    cpp = res.get("chi2_per_point")
                    if cpp is None and isinstance(res.get("global"), dict):
                        cpp = res["global"].get("chi2_per_point")
                    return cpp
            except Exception:
                pass
            return None

        fams = families or []
        if not fams:
            return {"status": "error", "message": "families is required (list of {observable, selected_ids})"}

        state = load_state()
        if state is None:
            plan_fams = []
            seen = set()
            for fam in fams:
                obs = str(fam.get("observable") or "")
                if obs in seen or obs not in DATASET_CATALOG:
                    continue
                ids = [str(i) for i in (fam.get("selected_ids") or [])]
                if not ids:
                    ids = [str(ds["id"]) for ds in DATASET_CATALOG[obs] if ds.get("default_on")]
                if not ids:
                    continue
                seen.add(obs)
                plan_fams.append({"observable": obs, "selected_ids": ids})
            if not plan_fams:
                return {"status": "error", "message": "no valid families with datasets"}
            order = {obs: i for i, obs in enumerate(self._GEN_LADDER_ORDER)} if hasattr(self, "_GEN_LADDER_ORDER") else {}
            plan_fams.sort(key=lambda f: (order.get(f["observable"], 99),))
            plan = {
                "families": plan_fams,
                "nonce": str(int(_time.time()))[-6:],
                "started": _time.time(),
                "submitted": None,
                "stages": [],
            }
            save_state(plan)
            return submit_stage(plan, 0)

        # resume
        fams_n = len(state["families"])
        if state.get("submitted"):
            k = state["submitted"]["stage"]
            batch_id = state["submitted"]["batch_id"]
            if not check_batch(batch_id):
                return {"stage": k + 1, "status": "running", "batch_id": batch_id,
                        "hint": "stage still fitting; call again later"}
            chi2n = batch_chi2n(batch_id)
            state["stages"].append({"stage": k + 1, "batch_id": batch_id,
                                    "observables": [f["observable"] for f in state["families"][:k + 1]],
                                    "chi2_per_point": chi2n})
            state["submitted"] = None
            save_state(state)
            if chi2n is not None and chi2n > 5.0:
                return {"stage": k + 1, "status": "diverged", "batch_id": batch_id,
                        "chi2_per_point": chi2n,
                        "hint": "this stage diverged (chi2/N > 5). Review the datasets and parameters with the human before continuing — remove/adjust a problematic family and restart the ladder."}
            if k + 1 < fams_n:
                return submit_stage(state, k + 1)
            return {"stage": fams_n, "status": "complete", "stages": state["stages"],
                    "hint": "ladder finished. Review each stage's chi2, then finalize (compute_batch_prior_tool / Stage B pairing)."}

        # all stages recorded but nothing submitted -> complete
        return {"stage": fams_n, "status": "complete", "stages": state["stages"],
                "hint": "ladder finished."}


    @mcp.tool()
    def orchestrate_jam25(seed: int, chat_id: str = "",
                          username: str = "", partition: str = "production") -> Dict[str, Any]:
        """Resume-style orchestrator: run the full JAM25 ladder step by step.

        Each call advances the state machine by one stage (submit a step, or
        collect a finished step and submit the next). The LLM calls this tool
        repeatedly; no other tool is needed. State lives in
        {L2}/appruns/orch_<seed>.json so any LLM session can resume.

        Returns per-call progress: {"stage", "job_id", "step", "prior_msr",
        "chi2N_so_far", "done"}.
        """
        import time
        state_path = f"{L2}/appruns/orch_{seed}.json"
        p = _shell(f"cat {state_path} 2>/dev/null", username, timeout=30)
        state = json.loads(p["stdout"]) if p["stdout"].strip().startswith("{") else {}

        def save_state():
            ssh_write_file(state_path, json.dumps(state), username=username)

        def log_chi2(out: str) -> float:
            """exp chi2/N from the fit log JAM FITTER summary (chi2 & npts lines)."""
            p = _shell(f"grep -E '^chi2\\s*=|^npts\\s*=' {out}.log 2>/dev/null | tail -2",
                       username, timeout=60)
            txt = p["stdout"]
            mc = re.search(r"chi2\s*=\s*([\d.eE+-]+)", txt)
            mn = re.search(r"npts\s*=\s*([\d]+)", txt)
            if mc and mn and int(mn.group(1)) > 0:
                return float(mc.group(1)) / int(mn.group(1))
            return float("nan")

        done = state.get("done", False)
        if done:
            return {"stage": "done", "message": "ladder complete",
                    "prior_msr": state.get("prior"), "chi2N_so_far": state.get("chi2N", [])}

        stage = state.get("stage")
        if stage is None:
            info = _submit_step(1, seed, "", username, partition)
            state.update({"stage": "s1_running", "job_id": info["job_id"],
                          "out": info["out"], "chi2N": [], "partition": partition,
                          "retries": 0, "prior_input": ""})
            save_state()
            return {"stage": "s1_running", "step": 1, "job_id": info["job_id"],
                    "hint": "call again to continue"}

        job_id = state.get("job_id")
        p = _shell(f"squeue -j {job_id} -h", username, timeout=30)
        if p["stdout"].strip():
            return {"stage": stage, "step": state.get("step", 1), "job_id": job_id,
                    "status": "still running", "hint": "call again later"}

        # job finished -> collect msr and advance
        out = state["out"]
        step = state.get("step", {"s1_running": 1, "s2_running": 2,
                                  "s3_running": 3, "s4_running": 4}.get(stage, 1))
        p = _shell(f"ls {out}/msr/*.msr 2>/dev/null | head -1", username, timeout=30)
        msr = p["stdout"].strip()
        chi2n = log_chi2(out)
        state["chi2N"].append({"step": step, "chi2N": chi2n, "msr": msr})
        state["prior"] = msr
        # external kill (job died without msr and without a diverged chi2) -> retry
        if not msr:
            retries = state.get("retries", 0)
            if retries < 2:
                prior_in = state.get("prior_input", "")
                info = _submit_step(step, seed, prior_in, username, state.get("partition", "production"))
                state.update({"job_id": info["job_id"], "out": info["out"], "retries": retries + 1})
                save_state()
                return {"stage": stage, "step": step, "job_id": info["job_id"],
                        "retry": retries + 1, "message": "job was killed externally; resubmitted",
                        "hint": "call again later"}
        # expert guardrail: abort if exp chi2/N exceeds 5x the paper's 0.80
        # guardrail: real chi2 available AND exceeds 5x the paper's exp total
        guard_fired = (chi2n == chi2n) and (chi2n > 5.0)
        if chi2n != chi2n:
            state.setdefault("chi2n_missing", []).append(step)
        if not msr or guard_fired:
            state["done"] = True
            state["diverged"] = True
            save_state()
            return {"stage": "diverged", "step": step, "chi2N": chi2n,
                    "message": "step diverged (chi2/N > 5); ladder aborted"}

        if step == 4:
            state["done"] = True
            save_state()
            return {"stage": "complete", "step": 4, "prior_msr": msr,
                    "chi2N_so_far": state["chi2N"],
                    "next": "run compare_with_paper with this msr"}
        if step == 3:
            # per-chain shape fix before step 4
            _shell(f"apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages "
                   f"{SIF} python3 {L2}/update_s4_shapes.py {msr}", username, timeout=120)
        info = _submit_step(step + 1, seed, msr, username, partition)
        state.update({"stage": f"s{step+1}_running", "job_id": info["job_id"],
                      "out": info["out"], "step": step + 1, "partition": partition,
                      "prior_input": msr, "retries": 0})
        save_state()
        return {"stage": f"s{step+1}_running", "step": step + 1,
                "job_id": info["job_id"], "prior_msr": msr,
                "chi2N_so_far": state["chi2N"], "hint": "call again to continue"}

    @mcp.tool()
    def run_ladder_job(count: int, seed: int, chat_id: str = "",
                       username: str = "", partition: str = "production",
                       concurrent: int = 10) -> Dict[str, Any]:
        """UI-facing ladder batch: launch `count` JAM25 ladder chains and
        register them in the app's task ledger so FittingMonitor shows them.

        Each chain runs the 4-step from-scratch ladder with our cards
        (identical to the reproduction); progress.json is copied to the chain
        root after each step so the standard monitor reads it.
        """
        import time
        ssh_mkdir(f"{L2}/appwave", username)
        batch_id = f"l2ladder_{seed}_{int(time.time())}"
        wave = f"{L2}/appwave/{batch_id}"
        ssh_mkdir(f"{wave}", username)
        script = f"""#!/bin/bash
#SBATCH --job-name=l2ui
#SBATCH --partition={partition}
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4
#SBATCH --time={"04:00:00" if partition == "priority" else "10:00:00"} --mem=4G
#SBATCH --array=1-{count}%{concurrent}
#SBATCH --output={wave}/logs/slurm_%A_%a.out --error={wave}/logs/slurm_%A_%a.err
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
cd {FP}
CEPH_GRIDS="/w/ceph24/JAM/share/fitpack_grids"
for obs in dy zrap wasym wzrv jet wc pjet dypion pp_hadron; do
  [ -d "$CEPH_GRIDS/$obs" ] && [ ! -e "{FP}/grids/$obs" ] && ln -sfn "$CEPH_GRIDS/$obs" "{FP}/grids/$obs"
done
CHAIN=$(printf "%03d" $SLURM_ARRAY_TASK_ID)
OUT={wave}/$CHAIN
mkdir -p $OUT/step1 $OUT/step2 $OUT/step3 $OUT/step4
S=$(({seed} + SLURM_ARRAY_TASK_ID))
RUN() {{ apptainer exec --bind /w/ceph24 {ENV} {SIF} python3 -m fitlib.maxlike_jam "$@"; }}
RUN {L2}/input_l2_s1.py $OUT/step1 -n 4 -v 10 -s $S || exit 1
cp "$OUT/step1/progress.json" "$OUT/progress.json" 2>/dev/null || true
P1=$(ls $OUT/step1/msr/*.msr | head -1)
RUN {L2}/input_l2.py    $OUT/step2 -n 4 -v 10 -s $S -p $P1 || exit 1
cp "$OUT/step2/progress.json" "$OUT/progress.json" 2>/dev/null || true
P2=$(ls $OUT/step2/msr/*.msr | head -1)
RUN {L2}/input_l2_s3.py $OUT/step3 -n 4 -v 10 -s $S -p $P2 || exit 1
cp "$OUT/step3/progress.json" "$OUT/progress.json" 2>/dev/null || true
P3=$(ls $OUT/step3/msr/*.msr | head -1)
apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages \
  {SIF} python3 {L2}/update_s4_shapes.py $P3 || exit 1
RUN {L2}/input_l2_s4.py $OUT/step4 -n 4 -v 10 -s $S -p $P3 || exit 1
cp "$OUT/step4/progress.json" "$OUT/progress.json" 2>/dev/null || true
echo "CHAIN=$CHAIN DONE"
"""
        ssh_write_file(f"{wave}/submit.sh", script, username=username)
        p = _shell(f"cd {FP} && sbatch {wave}/submit.sh", username)
        m = re.search(r"Submitted batch job (\d+)", p["stdout"])
        array_id = m.group(1) if m else None
        if array_id and insert_task is not None:
            for i in range(1, count + 1):
                insert_task({
                    "chat_id": chat_id, "batch_id": batch_id,
                    "batch_label": f"JAM25 ladder seed {seed}",
                    "file_id": f"{i:03d}", "path": f"{wave}/{i:03d}",
                    "parameters_file": "input_l2_s1.py",
                    "step_index": 0, "parent_batch_id": "",
                    "hpc_username": username, "observable": "dy-pion",
                })
        return {"status": "submitted" if array_id else "error",
                "batch_id": batch_id, "slurm_array_id": array_id,
                "nchains": count, "seed": seed, "wave_dir": wave,
                "monitor": "chain progress visible in FittingMonitor via progress.json"}

    @mcp.tool()
    def compare_with_paper(chat_id: str = "", msr: str = "",
                           username: str = "") -> Dict[str, Any]:
        """Compare a finished ladder msr against the paper's published numbers.

        Computes on the cluster: per-dataset chi2 table, Table 1 lattice moments,
        LHAPDF grid comparison vs member-0, and effective beta exponents.
        msr: path to the step-4 msr (default: the app's latest chain2 result).
        """
        if not msr:
            p = _shell(f"ls {L2}/chain2/step4/msr/*.msr 2>/dev/null | head -1", username)
            msr = (p["stdout"] or "").strip()
        if not msr:
            return {"status": "error", "message": "no step-4 msr found; pass msr= explicitly"}
        cmds = {
            "chi2_table": (f"apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages "
                           f"{SIF} python3 {L2}/analyze_msr_remote.py {msr}"),
            "lattice_moments": (f"apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages "
                                f"{SIF} python3 {L2}/test_lqcd_mom.py {L2}/input_l2_s4.py {msr}"),
            "pdf_grid": (f"apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages "
                         f"--env LHAPDF_DATA_PATH=/w/ceph24/JAM/chunyu/jam25repro:{L2.replace('/l2','')}/lhapdf_tables:/w/ceph24/JAM/apps/lhapdf_tables/more/more1 "
                         f"{SIF} python3 {L2}/l2_pdf_cmp.py {msr}"),
            "beta": (f"apptainer exec --bind /w/ceph24 --env PYTHONPATH={FP}:/usr/local/lib/python3.11/site-packages "
                     f"{SIF} python3 {L2}/beta_cmp.py {msr}"),
        }
        out = {}
        for name, cmd in cmds.items():
            p = _shell(cmd, username, timeout=600)
            out[name] = (p["stdout"] or "")[-3000:]
        out["paper_targets"] = PAPER
        out["msr"] = msr
        return out
