"""External-test driver; source travels over SSH stdin, never by synchronization.

The tests mode does not create remote directories or rebuild an archive. Building
a new immutable archive is a separate, explicit opt-in operation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
REMOTE_APP = '/w/ceph24/JAM/treyand/femtomind-chatgpt-app'
REMOTE_DATA = REMOTE_APP + '/analyses/JAM24/results/data'
SOURCE = '/w/ceph24/JAM/treyand/pdf_plotting/strange_analysis/combined4/2-0'
FITPACK = '/w/ceph24/JAM/treyand/fitpack_legacy_mcp'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ProxyCommand=none',
       '-o', 'ProxyJump=none', '-p', '2201', '-i', 'C:/Users/treya/.ssh/id_ed25519_codex_ifarm', 'treyand@127.0.0.1']


def remote(code, command, filename, reports):
    result = subprocess.run(SSH + [command], input=code, text=True, encoding='utf-8', capture_output=True, timeout=300)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / (filename + '.stdout')).write_text(result.stdout, encoding='utf-8')
    (reports / (filename + '.stderr')).write_text(result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(result.stdout[-3000:] + '\n' + result.stderr[-5000:])
    return result.stdout


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['tests', 'build'])
    parser.add_argument('--tests-root', type=Path, default=Path(os.environ.get('FEMTOMIND_TEST_ROOT', ROOT.parent / 'femtomind-tests')))
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--allow-new-archive', action='store_true', help='Explicit permission to build a NEW archive; never use to migrate an existing one.')
    parser.add_argument('--expected-remote-head', help='Required exact clean source revision for a new archive build.')
    args = parser.parse_args()
    tests_root = args.tests_root.resolve()
    reports = (args.report_dir or tests_root / 'artifacts').resolve()
    if any(p == ROOT or ROOT in p.parents for p in (tests_root, reports)):
        parser.error('Tests and test reports must be outside the application checkout')
    if args.mode == 'build' and (not args.allow_new_archive or not args.expected_remote_head):
        parser.error('Build requires --allow-new-archive and --expected-remote-head; migration never requires rebuilding')
    builder = (HERE / 'build_archive.py').read_text(encoding='utf-8')
    prefix = "import types,sys,os\nmodule=types.ModuleType('jam24_reference')\nsys.modules['jam24_reference']=module\nexec(compile(" + repr(builder) + ", '<reviewed-archive-builder>', 'exec'),module.__dict__)\n"
    if args.mode == 'tests':
        tests = (tests_root / 'tests/test_jam24_reference_results.py').read_text(encoding='utf-8')
        program = prefix + "tests=types.ModuleType('archive_tests')\nexec(compile(" + repr(tests) + ",'<archive-tests>','exec'),tests.__dict__)\nimport unittest\nr=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(tests))\nraise SystemExit(not r.wasSuccessful())\n"
        print(remote(program, 'python3 -B -', 'tests', reports), flush=True)
        print((reports / 'tests.stderr').read_text(encoding='utf-8'))
        raise SystemExit(0)
    ignore_policy = '*\n!.gitignore\n'
    preflight = f'''
from pathlib import Path
import subprocess
app = Path({REMOTE_APP!r}).resolve()
assert str(app) == {REMOTE_APP!r}
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=app,text=True).strip() == {args.expected_remote_head!r}
assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=app,text=True).strip()
data = app / 'analyses/JAM24/results/data'
assert all(not p.is_symlink() for p in [app/'analyses',app/'analyses/JAM24',app/'analyses/JAM24/results',data])
data.mkdir(parents=True,exist_ok=True)
ignore = data / '.gitignore'
expected = {ignore_policy!r}
assert not ignore.exists() or ignore.read_text() == expected
if not ignore.exists(): ignore.write_text(expected)
assert subprocess.run(['git','check-ignore','-q',str(data/'probe.npz')],cwd=app).returncode == 0
print('Archive directory is within the application checkout and Git-ignored')
'''
    print(remote(preflight, 'python3 -B -', 'preflight', reports), flush=True)
    builder_hash = hashlib.sha256((HERE / 'build_archive.py').read_bytes()).hexdigest()
    command = ('apptainer exec --no-home --cleanenv --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 '
        '--env TMPDIR=' + REMOTE_DATA + ' --env JAM24_ARCHIVE_BUILDER_SHA256=' + builder_hash +
        ' --bind /w/ceph24:/w/ceph24:ro --bind ' + REMOTE_DATA + ':' + REMOTE_DATA + ':rw '
        '/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif python3 -B -')
    program = prefix + 'module.build(module.Path(' + repr(SOURCE) + '),module.Path(' + repr(FITPACK) + '),module.Path(' + repr(REMOTE_APP) + '))\n'
    print(remote(program, command, args.mode, reports), flush=True)
