"""Write what an agent is given in one run: prompt, paper, reference files.

    python -m evaluation.conditions.materials --write-prompts
        regenerate evaluation/prompts/stage1_{nomcp,mcp}_{A,B,C}.txt from the
        pilot's prompts (kept verbatim as stage1_*_pilot.txt) and record their
        hashes in evaluation/prompts/MANIFEST.json
    python -m evaluation.conditions.materials --arm nomcp --condition B --run nomcp-B-x-r01 --out DIR
        write DIR/prompt.txt, DIR/JAM24_paper.pdf, DIR/reference/... and DIR/manifest.json

The official input of condition B is not a copy of the authors' file (which
has since changed on disk, see l1/calibration) but the catalog's literal
configuration rendered as a FitPack input: key for key the fit-time input,
as the calibration against the archive's saved conf shows. The clarifications
of condition C are generated from the catalog's author layer, deterministic
and complete: every statement the compiler turns into an overlay is here.
"""
import argparse
import hashlib
import json
import os
import shutil
import time

from evaluation.common import paths
from evaluation.l1 import expected

paths.gateway_on_path()
from jamai import compiled_input  # noqa: E402

ARMS = ("nomcp", "mcp")
CONDITIONS = ("A", "B", "C")
PROMPTS_DIR = os.path.join(paths.APP_ROOT, "evaluation", "prompts")
PAPER_PDF = os.path.expanduser(os.environ.get("FEMTOMIND_JAM24_PDF") or "~/bench_nomcp/paper/JAM24_paper.pdf")
REFERENCE_DIR = "reference"
INPUT_NAME = "jam24_input.py"
CLARIFICATIONS_NAME = "jam24_author_clarifications.txt"

PAPER_LINE = "- The JAM24 paper: ./JAM24_paper.pdf"
EXTRA_LINES = {
    "B": ["- The configuration file of the published JAM24 fit, as its authors ran it:",
          "    ./reference/" + INPUT_NAME + "   (FitPack input format)"],
    "C": ["- Written clarifications from the JAM24 authors about the fit's configuration:",
          "    ./reference/" + CLARIFICATIONS_NAME],
}
CATALOG_VARIANT = {"A": "denied", "B": "derived_B", "C": "full"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(name):
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def prompt_text(arm, condition):
    """The pilot's prompt with the condition's extra materials listed."""
    if arm not in ARMS or condition not in CONDITIONS:
        raise ValueError("arm in %s, condition in %s" % (ARMS, CONDITIONS))
    base = _read("stage1_%s_pilot.txt" % arm)
    extra = []
    for c in CONDITIONS[1:]:
        if condition >= c:
            extra.extend(EXTRA_LINES[c])
    lines = base.splitlines()
    if arm == "nomcp":
        at = lines.index(PAPER_LINE)
        lines[at + 1:at + 1] = extra
    else:
        # the pilot's MCP prompt named no materials; every condition now has the paper
        at = lines.index("")  # after the first paragraph
        lines[at + 1:at + 1] = ["What you have:", PAPER_LINE] + extra + [""]
    return "\n".join(lines) + "\n"


def write_prompts():
    manifest = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pilot": {}, "prompts": {}}
    for arm in ARMS:
        manifest["pilot"][arm] = sha256_text(_read("stage1_%s_pilot.txt" % arm))
        for condition in CONDITIONS:
            text = prompt_text(arm, condition)
            name = "stage1_%s_%s.txt" % (arm, condition)
            with open(os.path.join(PROMPTS_DIR, name), "w", encoding="utf-8") as fh:
                fh.write(text)
            manifest["prompts"][name] = sha256_text(text)
    with open(os.path.join(PROMPTS_DIR, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest


def official_input_text(store=None):
    """The literal JAM24.final configuration as a FitPack input, with the
    compiler's overlay section and header removed."""
    store = store or expected.catalog_store()
    ir, refs = expected.compiled("JAM24.final", store)
    rendered = compiled_input.render_compiled_input(store, refs["compiled_ref"])["input_text"]
    body = rendered.split(compiled_input.OVERLAYS)[0].splitlines()
    while body and body[0].startswith("#"):
        body.pop(0)
    official = store._entry("JAM24.final")["config"]["sources"]["official_final_input"]
    head = ["# JAM24 final-fit configuration in FitPack's input format.",
            "# Rendered key for key from the audited analysis catalog of arXiv:2501.00665;",
            "# equal in every setting, dataset, cut, normalization and parameter to the",
            "# authors' input.py of the published fit (sha256 %s)." % official["sha256"],
            "# Site paths ('grid path', 'grid-8 path') are the authors' and must be adapted."]
    return "\n".join(head + body) + "\n", refs


def _fmt(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def author_clarifications_text(store=None):
    """The authors' statements, from the catalog's author layer, in the order a
    reader needs them. Superseded statements are left out; the statement that
    superseded them is marked."""
    store = store or expected.catalog_store()
    config = store._entry("JAM24.final")["config"]
    sv = config["source_verification"]
    latest = sv["latest_author_corrections"]
    final = sv["final_author_clarifications"]
    confirmed = sv["author_confirmations"]
    repro = config["reproduction_configuration"]
    gi = repro["Gaussian_initialization"]
    st = latest["statements"]
    by_id = {s["id"]: s for s in confirmed["statements"]}
    out = ["JAM24 (arXiv:2501.00665): written clarifications from the authors",
           "",
           "Collected on %s; reproduced as recorded in the analysis catalog (source ids %s)."
           % (" and ".join(sorted({confirmed["date"], latest["date"]})),
              ", ".join(sorted({confirmed["source_id"], latest["source_id"], final["source_id"]}))),
           "Where a later statement supersedes an earlier one, only the later statement is given.",
           ""]
    n = 0

    def item(title, *lines):
        nonlocal n
        n += 1
        out.append("%d. %s" % (n, title))
        out.extend("   " + ln for ln in lines if ln)
        out.append("")

    item("Numerical authority", st["numerical_authority"])
    item("Input scale", "Q20 = %s GeV^2 (the configured value)." % _fmt(st["Q20"]), st["input_scale_difference"])
    item("Positivity constraints",
         ", ".join("%s = %s" % (k, _fmt(v)) for k, v in sorted(st["positivity"].items())) + ".",
         "Supersedes: " + latest["supersedes"])
    item("SIDIS selection", st["sidis"])
    item("Jet selection", st["jet"])
    item("Fragmentation-function shapes", by_id["author.ff_two_shapes"]["value"])
    means = gi["means_by_coefficient"]
    stds = gi["standard_deviations_by_coefficient"]
    item("Gaussian initialization of the fit parameters",
         st["gaussian_initialization"], final["Gaussian_initialization"],
         "flat par = %s; parameter key '%s' = %s." % (_fmt(gi["flat par"]), gi["parameter_key"], _fmt(gi["gaussian"])),
         "Means by coefficient: " + ", ".join("%s %s" % (k, _fmt(means[k])) for k in ("N", "a", "b", "c", "d")) + ".",
         "Standard deviations by coefficient: " + ", ".join("%s %s" % (k, _fmt(stds[k])) for k in ("N", "a", "b", "c", "d")) + ".",
         "Selection rule: " + gi["selection_rule"],
         "Linked parameters: " + gi["linked_parameters"],
         "Sampler: " + gi["sampler"],
         "Example given by the authors: " + json.dumps(confirmed["numeric_prior_followup"]["example"], sort_keys=True))
    item("Gaussian template penalties in the final objective",
         "%s." % _fmt(st["gaussian_template_penalties_in_final_objective"]), by_id["author.no_gaussian_penalties"]["value"])
    item("Belle kaon data set (sia:2029)", final["Belle_kaon"])
    return "\n".join(out)


def build(arm, condition, out_dir, run_name, paper_pdf=PAPER_PDF, store=None):
    """Write one run's materials; returns the manifest (also written)."""
    store = store or expected.catalog_store()
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"schema": "femtomind.eval_materials.v1", "arm": arm, "condition": condition, "run": run_name,
                "catalog_variant": CATALOG_VARIANT[condition],
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": {}}
    text = prompt_text(arm, condition).replace("@RUN@", run_name)
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    manifest["files"]["prompt.txt"] = sha256_text(text)
    manifest["prompt_template"] = "stage1_%s_%s.txt" % (arm, condition)
    expected_pdf = store._entry("JAM24.final")["config"]["publication"].get("pdf_sha256")
    if not os.path.isfile(paper_pdf):
        raise FileNotFoundError("paper PDF not found at %s (FEMTOMIND_JAM24_PDF)" % paper_pdf)
    got = sha256_file(paper_pdf)
    if expected_pdf and got != expected_pdf:
        raise ValueError("paper PDF sha256 %s is not the catalog's %s" % (got, expected_pdf))
    shutil.copyfile(paper_pdf, os.path.join(out_dir, "JAM24_paper.pdf"))
    manifest["files"]["JAM24_paper.pdf"] = got
    if condition in ("B", "C"):
        os.makedirs(os.path.join(out_dir, REFERENCE_DIR), exist_ok=True)
        text, refs = official_input_text(store)
        with open(os.path.join(out_dir, REFERENCE_DIR, INPUT_NAME), "w", encoding="utf-8") as fh:
            fh.write(text)
        manifest["files"][REFERENCE_DIR + "/" + INPUT_NAME] = sha256_text(text)
        manifest["official_input"] = {"rendered_from": refs, "equals_official_input_sha256":
                                      store._entry("JAM24.final")["config"]["sources"]["official_final_input"]["sha256"]}
    if condition == "C":
        text = author_clarifications_text(store)
        with open(os.path.join(out_dir, REFERENCE_DIR, CLARIFICATIONS_NAME), "w", encoding="utf-8") as fh:
            fh.write(text)
        manifest["files"][REFERENCE_DIR + "/" + CLARIFICATIONS_NAME] = sha256_text(text)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.conditions.materials")
    ap.add_argument("--write-prompts", action="store_true")
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--condition", choices=CONDITIONS)
    ap.add_argument("--run")
    ap.add_argument("--out")
    ap.add_argument("--show", choices=["input", "clarifications"])
    args = ap.parse_args(argv)
    if args.write_prompts:
        print(json.dumps(write_prompts(), indent=1))
    elif args.show == "input":
        print(official_input_text()[0], end="")
    elif args.show == "clarifications":
        print(author_clarifications_text())
    elif args.arm and args.condition and args.run and args.out:
        print(json.dumps(build(args.arm, args.condition, args.out, args.run), indent=1))
    else:
        ap.error("--write-prompts, --show, or --arm/--condition/--run/--out")


if __name__ == "__main__":
    main()
