"""Run programs on the compute host over one authenticated SSH connection.

The evaluation never asks for a PIN: it reuses a ControlMaster socket the
person opened (ifarm_controlmaster.sh). The socket and host come from
FEMTOMIND_IFARM_SOCK / FEMTOMIND_IFARM_HOST; the default socket is the
evaluation's own (~/.ssh/controlmasters/ifarm-eval) so that a benchmark's
connection is never borrowed by accident.

Programs travel on standard input (`bash -s`, `python3 -B -`), never as
command-line text, so no quoting is involved and the size limit is the
pipe's. Remote shells here are tcsh, so every command is wrapped in bash.
"""
import os
import shlex
import subprocess

SOCK = os.path.expanduser(os.environ.get("FEMTOMIND_IFARM_SOCK", "~/.ssh/controlmasters/ifarm-eval"))
HOST = os.environ.get("FEMTOMIND_IFARM_HOST", "ifarm-tunnel")

# The site's container, as lqcdmcp-deploy/jam_gateway.py SITE["container"].
IMAGE = "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"
BINDS = ("/w/ceph24",)


class SSHError(RuntimeError):
    pass


def alive():
    """Is the ControlMaster behind SOCK up?"""
    r = subprocess.run(["ssh", "-S", SOCK, "-O", "check", HOST], capture_output=True, text=True)
    return r.returncode == 0


def run(command, stdin=None, timeout=300):
    """Run one bash command on the host; stdin is what it reads. Returns
    (returncode, stdout, stderr)."""
    argv = ["ssh", "-S", SOCK, "-o", "BatchMode=yes", HOST, "bash -lc " + shlex.quote(command)]
    r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    err = "\n".join(ln for ln in r.stderr.splitlines() if "syscshrc" not in ln)
    return r.returncode, r.stdout, err


def check(command, stdin=None, timeout=300):
    rc, out, err = run(command, stdin, timeout)
    if rc:
        raise SSHError("exit %d from %r: %s" % (rc, command[:120], (err or out)[-3000:]))
    return out


def container_python(fitpack_root=None, env=None, extra_binds=(), read_only=True):
    """The apptainer command that runs `python3 -B -` inside the site's image,
    with FitPack on the path. Default binds are read-only."""
    binds = []
    for b in BINDS:
        binds.append("--bind %s:%s:%s" % (b, b, "ro" if read_only else "rw"))
    for b in extra_binds:
        binds.append("--bind " + b)
    envs = ["--env OPENBLAS_NUM_THREADS=1", "--env OMP_NUM_THREADS=1"]
    if fitpack_root:
        envs.append("--env PYTHONPATH=" + shlex.quote(fitpack_root))
        envs.append("--env FITPACK=" + shlex.quote(fitpack_root))
    for k, v in (env or {}).items():
        envs.append("--env %s=%s" % (k, shlex.quote(str(v))))
    return "apptainer exec --no-home --cleanenv --containall --network none %s %s %s python3 -B -I -" % (
        " ".join(binds), " ".join(envs), IMAGE)


def python(program, container=True, fitpack_root=None, env=None, timeout=600, extra_binds=()):
    """Run a python program on the host and return its stdout."""
    cmd = container_python(fitpack_root, env, extra_binds) if container else "python3 -B -I -"
    return check(cmd, stdin=program, timeout=timeout)


def sha256(path):
    out = check("sha256sum " + shlex.quote(path))
    return out.split()[0]


def readable(path):
    rc, out, _ = run("test -r %s && echo yes || echo no" % shlex.quote(path))
    return rc == 0 and out.strip() == "yes"


def read_text(path, timeout=120):
    return check("cat " + shlex.quote(path), timeout=timeout)
