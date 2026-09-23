"""Unit tests for the FemtoMind write-boundary policy (path_policy).

Run:  python3 path_policy_test.py
The 2026-09 grid1 incident is the canonical regression case.
"""

from path_policy import (
    PolicyError,
    assert_command_write_safe,
    check_write_path,
    check_write_paths,
    classify_path,
    scan_command_for_writes,
)

USER = "chunyu"
OWN = f"/w/ceph24/JAM/{USER}/fitpack_legacy"
SHARED_GRID1 = "/w/ceph24/JAM/share/fitpack_grids/dy/grid1"


def expect_raise(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except PolicyError as e:
        print(f"  OK denied: {str(e)[:110]}")
        return
    raise AssertionError(f"expected PolicyError from {getattr(fn, '__name__', fn)}")


def main():
    print("== classify_path ==")
    assert classify_path(OWN, USER).allowed
    assert classify_path(f"/w/ceph24/JAM/{USER}", USER).allowed
    assert classify_path("/tmp/scratch/abc", USER).allowed
    assert classify_path("/var/tmp/x", USER).allowed
    assert not classify_path(SHARED_GRID1, USER).allowed
    assert not classify_path("/w/ceph24/JAM/share/fitpack_grids", USER).allowed
    assert not classify_path("/w/ceph24/JAM/nsato/foo", USER).allowed
    assert not classify_path("/w/ceph24/JAM/apps/lhapdf", USER).allowed
    assert not classify_path("/u/scigroup/scicomp/x", USER).allowed
    assert not classify_path("/etc/passwd", USER).allowed
    assert not classify_path("relative/dir", USER).allowed
    print("  OK")

    print("== check_write_path(s) ==")
    check_write_path(OWN, USER)
    check_write_paths([OWN, "/tmp/x", f"/ceph24/JAM/{USER}/y"], USER)
    expect_raise(check_write_path, SHARED_GRID1, USER)
    expect_raise(check_write_paths, [OWN, "/w/ceph24/JAM/share/x"], USER)
    print("  OK")

    print("== the grid1 incident (must be DENIED) ==")
    incident = f"cp /w/ceph24/JAM/{USER}/fitpack_legacy/grids/grids-dy/grid1/*.melltab {SHARED_GRID1}/"
    expect_raise(assert_command_write_safe, incident, USER)
    print("  OK")

    print("== incident variants (must be DENIED) ==")
    for cmd in (
        f"mv {SHARED_GRID1} {SHARED_GRID1}_old50",
        f"rm -rf {SHARED_GRID1}",
        f"ln -sfn /x/y {SHARED_GRID1}",
        f"mkdir -p /w/ceph24/JAM/share/newdir",
        f"touch /w/ceph24/JAM/nsato/hi",
        f"sed -i 's/a/b/' /w/ceph24/JAM/share/fitpack_grids/dy/README.md",
        f"echo hi > /w/ceph24/JAM/share/f.txt",
        f"cat > {SHARED_GRID1}/0.melltab",
        "sbatch --output=/w/ceph24/JAM/share/out.txt script.sh",
    ):
        expect_raise(assert_command_write_safe, cmd, USER)
    print("  OK")

    print("== legitimate commands (must PASS) ==")
    for cmd in (
        f"cp /w/ceph24/JAM/share/fitpack_grids/dy/grid2/*.melltab {OWN}/grids/grids-dy/grid2/",
        f"mkdir -p {OWN}/runs/s2",
        f"ln -sfn {OWN}/grids/grids-dy {OWN}/grids_jam24/dy",
        f"ls /w/ceph24/JAM/share/fitpack_grids/dy",
        "squeue -u chunyu -h | wc -l",
        f"tail -20 {OWN}/logs/x.out 2>/dev/null",
        "sbatch --output=/w/ceph24/JAM/chunyu/runs/slurm_%j.out script.sh",
        f"mv $OLD $NEW  # variable paths: skipped, not denied",
        f"grep -n 'x' {OWN}/a.py > /tmp/grep.out",
        "scancel 12345",
        "sacct -j 1 --format=State -n",
    ):
        assert_command_write_safe(cmd, USER)
    print("  OK")

    print("== script-with-input scan ==")
    script = f"""#!/bin/bash
#SBATCH --output={OWN}/logs/slurm_%j.out
mkdir -p $OUT
cp $CEPH_GRIDS/dy/grid2/*.melltab $FP/grids/grids-dy/grid2/
ln -sfn $FP/grids/grids-dy $FP/grids_jam24/dy
echo done
"""
    assert_command_write_safe(script, USER)
    bad_script = f"""#!/bin/bash
cp {OWN}/x/*.melltab {SHARED_GRID1}/
"""
    expect_raise(assert_command_write_safe, bad_script, USER)
    print("  OK")

    print("\nALL POLICY TESTS PASSED")


if __name__ == "__main__":
    main()
