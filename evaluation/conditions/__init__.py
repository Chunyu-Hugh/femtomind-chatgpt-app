"""The knowledge conditions of the evaluation: what an agent is given.

A  the paper (and, without MCP, FitPack's own examples and data);
B  A plus the official JAM24 input as the authors ran it;
C  B plus the authors' written clarifications of 2026-10-19.

materials.py writes a run's box (prompt, paper, reference files) with a
manifest of hashes; derive_catalog.py makes the catalog variant the gateway
serves under B (the author layer removed, provably); deny_lists.py says which
gateway tools each condition hides; leak_scan.py checks a finished run for
knowledge it should not have had.
"""
