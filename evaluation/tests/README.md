# Tests for the evaluation toolchain

These four files are the tests this branch added. They live here so the branch
is testable on its own; the full suite (gateway + evaluation, ~160 tests) lives
in the sibling checkout `../femtomind-tests/tests`, which is not under version
control — a gap worth closing.

    conda run -n base python -m unittest discover -s evaluation/tests -v
    # or: FEMTOMIND_APP_ROOT=$PWD python3 -m unittest discover -s evaluation/tests -v

They need numpy and pandas (the L1 grader and the catalog read raw workbooks);
the stdlib alone is not enough.
