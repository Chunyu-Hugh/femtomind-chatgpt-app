"""The grading targets: what the catalog says a published analysis is.

For each catalog entry (JAM24.final: the published configuration;
JAM24.current: the same fit on the updated workbooks) the compiler's
projection gives two FitPack confs: the literal official input, and the
official input plus the author-confirmed overlays (positivity flags, Q20,
Gaussian starting values). Candidates from knowledge conditions A and B are
graded against the literal conf, condition C against the overlaid one. Both
are computed from the repository alone; no cluster is needed.
"""
from evaluation.common import paths

paths.gateway_on_path()
from jamai import analysis_catalog, compiled_input, configuration_compiler, configuration_snapshot  # noqa: E402

TARGETS = ("JAM24.final", "JAM24.current")


def catalog_store():
    return analysis_catalog.Catalog(analysis_catalog.catalog_root(paths.DEPLOY))


def compiled(analysis_id, store=None):
    """(ir, references) of a catalog entry's compiled configuration."""
    store = store or catalog_store()
    entry = store._entry(analysis_id)
    snap = configuration_snapshot.snapshot(store.root, entry, store._reference(entry))
    result = configuration_compiler.compile_configuration(store, snap.ref)
    refs = {"analysis_id": entry["config"]["analysis_id"], "configuration_ref": snap.ref,
            "compiled_ref": result["compiled_ref"], "catalog_ref": store._reference(entry),
            "parity_passed": not result["issues"]}
    return result["ir"], refs


def expected_confs(analysis_id, store=None):
    """{"literal": conf, "reproduction": conf, "refs": {...}} for one target."""
    ir, refs = compiled(analysis_id, store)
    return {"literal": compiled_input._expected(ir, False),
            "reproduction": compiled_input._expected(ir, True), "refs": refs}


def all_targets(store=None):
    store = store or catalog_store()
    return {t: expected_confs(t, store) for t in TARGETS}


def differences(expected, actual):
    """The compiler's exact typed comparison: a list of {path, reason} records."""
    return compiled_input._differences(expected, actual)


def free_parameters(conf):
    return sum(1 for table in conf["params"].values() for d in table.values() if d.get("fixed") is False)


def current_vs_final(targets=None):
    """How JAM24.current differs from JAM24.final by construction (the SIDIS
    workbook substitutions, the reaction filters, the dropped native settings),
    embedded in every grade report so that a card built for one target is not
    marked wrong against the other without saying why."""
    targets = targets or all_targets()
    return differences(targets["JAM24.final"]["literal"], targets["JAM24.current"]["literal"])
