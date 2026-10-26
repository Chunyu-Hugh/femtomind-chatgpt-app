"""Evaluation tooling for the JAM gateway paper.

Nothing here runs a fit or changes the gateway. The modules grade what an
agent produced (l1: exact typed parity of a FitPack input against the
published configuration), build the knowledge conditions A/B/C (conditions),
drive repeated benchmark runs (matrix), recompute theory at archived
parameters (l2), compare a refit with the archive (l3) and summarize the audit
log (audit). Each claim of the paper points at the JSON a module writes; see
evaluation/README.md.
"""
