#!/usr/bin/env python
"""
maxlike_jam.py — copy of maxlike.py with progress.json output for FemtoMind monitoring.

Changes vs maxlike.py:
  - Writes progress.json after each status print (for FittingMonitor dashboard).
  - Grid path auto-detection (conf['grid path']) already inherited from maxlike.py.
"""
try:
    import setproctitle
    setproctitle.setproctitle("jam")
except:
    pass

import argparse
import json
import os, sys
import copy
from pathlib import Path
import numpy as np
import time

# Keep imports working when running outside the lab HPC by resolving the repo
# root via env variables or sensible defaults.
from tools.env import bootstrap_fitpack_path

FITPACK_ROOT = bootstrap_fitpack_path()
print(f"INFO: using fitpack path: {FITPACK_ROOT}")

# Mellin grid base path candidates — checked in priority order, first existing wins.
# resman.py reads conf['grid path'] and builds sub-paths:
#   conf['path2dytab']  = conf['grid path'] + '/dy'
#   conf['path2zraptab']= conf['grid path'] + '/zrap'   etc.
# So candidates should point to the BASE directory (parent of dy/, zrap/, wasym/, ...).
grid_candidates = [
    os.environ.get("FITPACK_GRID_PATH", ""),          # env var override (highest priority)
    "/ceph24/JAM/share/fitpack_grids",                # ifarm JLab path
    Path(__file__).resolve().parent.parent / "grids", # local repo fallback
]

#--from scipy
from collections import deque
from scipy.optimize import least_squares

#--from tools
from tools.tools     import save, load, checkdir
from tools.config    import load_config, conf
from tools.inputmod  import INPUTMOD
from tools.randomstr import id_generator

#--from fitlib
from fitlib.resman import RESMAN

class MAXLIKE:

    def __init__(self, input_name, current, nworkers = 2, verbose = False, prior = None, seed = None, fitpack_path = None):

        self.input_name = input_name
        self.current    = current
        self.nworkers   = nworkers
        self.verbose    = verbose
        self.prior      = prior
        self.seed       = seed
        # Progress tracking for FemtoMind FittingMonitor
        self.progress_file = Path(self.current) / "progress.json"
        self.progress_data = []
        # Allow callers to override the resolved fitpack root.
        if fitpack_path is not None:
            bootstrap_fitpack_path(fitpack_path)

    def set_counters(self):
        self.chi2tot=1e1000
        self.dchi2=0
        self.t0 = time.time()
        self.cnt=0
        self.progress_data = []

    def print_status(self,res,rres,nres):

        #--update status parameters
        shifts  = self.parman.shifts
        etime   = (time.time() - self.t0) / 60.0
        npts    = res.size
        chi2    = np.sum(res  ** 2.0)
        if len(rres) > 0: rchi2   = np.sum(rres ** 2.0)
        else:             rchi2   = 0.0
        if len(nres) > 0: nchi2   = np.sum(nres ** 2.0)
        else:             nchi2   = 0.0
        chi2tot = chi2 + rchi2 + nchi2
        dchi2   = chi2tot - self.chi2tot
        if shifts > 2:
            if chi2tot < self.chi2tot:
                self.dchi2 = self.chi2tot - chi2tot
                self.chi2tot = chi2tot

        #--build header
        status=[]
        status.append('JAM FITTER')
        status.append('count: {:d}'.format(self.cnt))
        status.append('elapsed time: {:.3f} minutes'.format(etime))
        status.append('shifts  = {:d}'.format(shifts))
        status.append('npts    = {:d}'.format(npts))
        status.append('chi2    = {:f}'.format(chi2))
        status.append('rchi2   = {:f}'.format(rchi2))
        status.append('nchi2   = {:f}'.format(nchi2))
        status.append('chi2tot = {:f}'.format(chi2tot))
        status.append('dchi2(iter)  = {:f}'.format(self.dchi2))
        status.append('dchi2(local) = {:f}'.format(dchi2))

        #--special output for PDFs
        if 'pdf' in conf['params']:
              for _ in conf['pdf'].sr:
                  status.append('pdf {:s}:{:f}'.format(_, conf['pdf'].sr[_]))

        #--special output for pion PDFs
        if 'pdf-pion' in conf['params']:
            for _ in conf['pdf-pion'].sr:
                status.append('pdf(pi) {:s}:{:f}'.format(_, conf['pdf-pion'].sr[_]))

        #--report from resman
        status.append('')
        status.extend(self.resman.gen_report())

        #--report from parman
        parstatus = self.parman.gen_report()

        #--print into screen
        nstatus=len(status)
        nparstatus=len(parstatus)
        os.system('clear')
        for i in range(max([nstatus,nparstatus])):
            data=[]
            if i<nstatus: data.append(status[i])
            else: data.append('')
            if i<nparstatus: data.append(parstatus[i])
            else: data.append('')
            print ('%-120s  | %s'%tuple(data))

        # Write progress.json for FemtoMind FittingMonitor
        try:
            chi2_per = self.resman.get_chi2()
            # Flatten all reactions into a single dict: str(idx) -> {chi2, npts}
            datasets_flat = {}
            for reaction, idxs in chi2_per.items():
                for idx, info in idxs.items():
                    datasets_flat[str(idx)] = {"chi2": info.get("chi2", 0), "npts": info.get("npts", 0)}
            params_flat = {}
            cur = getattr(self, "_cur_par", None)
            if cur is not None:
                for _o, _v in zip(self.parman.order, cur):
                    try:
                        _k = " ".join(str(x) for x in _o[1:]) if isinstance(_o, (list, tuple)) else str(_o)
                    except Exception:
                        _k = str(_o)
                    params_flat[_k] = float(_v)
            entry = {
                "iter": self.cnt,
                "chi2": float(chi2tot),
                "elapsed_min": etime,
                "datasets": datasets_flat,
                "params": params_flat,
            }
            self.progress_data.append(entry)
            self.progress_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.progress_file, "w") as _f:
                json.dump(self.progress_data, _f)
        except Exception as _e:
            print(f"WARNING: could not write progress.json: {_e}")

        return status,parstatus

    def get_residuals(self,par):
        res, rres, nres = self.resman.get_residuals(par)
        self.cnt += 1
        if (self.cnt % conf['verbose']) == 0:
            # Hand the live parameter vector to print_status so progress.json can
            # carry the fit's CURRENT state, not just its chi2. Without this the
            # only parameters on disk mid-run are the randomized starting point.
            try:
                self._cur_par = np.array(par).tolist()
            except Exception:
                self._cur_par = None
            self.print_status(res, rres, nres)
        if len(rres) != 0:
            res = np.append(res, rres)
        if len(nres) != 0:
            res = np.append(res, nres)

        ## save the parameters when they change (in case the fit is killed)
        self.history.append(np.array(par))
        diff = self.history[1] - self.history[0]
        if (len([_ for _ in diff if _ != 0]) > 2) and (self.prior != None):
            name = self.current + '/msr-buffer/' + self.prior.rsplit('/', 1)[-1]
            checkdir(name[: name.rindex('/') + 1])
            data = {}
            data['order'] = copy.deepcopy(self.order)
            data['order'][self.current_step] = copy.deepcopy(self.parman.order)
            data['params'] = copy.deepcopy(self.params)
            data['params'][self.current_step] = copy.deepcopy(par)
            data['deviation'] = copy.deepcopy(conf['deviation-buffer'])
            ## chi^2 is not saved for buffer because they will not be inspected
            save(data, name)

        return res

    def checklimits(self):

        for k in conf['params']:
            for kk in conf['params'][k]:
                if conf['params'][k][kk]['fixed']!=False: continue
                p=conf['params'][k][kk]['value']
                pmin=conf['params'][k][kk]['min']
                pmax=conf['params'][k][kk]['max']
                if  p<pmin or p>pmax:
                    print ('%s-%s out of limits. '%(k,kk))
                    # Non-zero exit: an out-of-bounds start must fail the
                    # SLURM job, not be recorded as a completed replica.
                    sys.exit(1)

        for k in conf['datasets']:
            for kk in conf['datasets'][k]['norm']:
                p=conf['datasets'][k]['norm'][kk]['value']
                pmin=conf['datasets'][k]['norm'][kk]['min']
                pmax=conf['datasets'][k]['norm'][kk]['max']
                if  p<pmin or p>pmax:
                    print ('%s-%s out of limits. '%(k,kk))
                    sys.exit(1)

    def get_conf(self,_conf,step):

        conf=copy.deepcopy(_conf)

        #--remove pdf/ff that is not in the step
        distributions=list(conf['params'])  #--pdf,ppdf,ffpion,ffkaon,...
        for dist in distributions:
            if  dist in step['active distributions']:
                print('active',dist)
                continue
            elif 'passive distributions' in step and dist in step['passive distributions']:
                print('passive',dist)
                continue
            else:
                print('remove',dist)
                if '%s parametrization'%dist in conf:
                    del conf['%s parametrization'%dist]
                del conf['params'][dist]

        #--set fixed==True for passive distributions
        if 'passive distributions' in step:
            for dist in step['passive distributions']:
                for par in conf['params'][dist]:

                    if conf['params'][dist][par]['fixed']==False:
                        conf['params'][dist][par]['fixed']=True

                    #--set prior parameters values for passive distributions
                    for istep in step['dep']:
                        prior_order=self.order[istep]
                        prior_params=self.params[istep]
                        for i in range(len(prior_order)):
                            _,_dist,_par = prior_order[i]
                            if  dist==_dist and par==_par:
                                conf['params'][dist][par]['value']=prior_params[i]

        #--another version for fixed parameters
        if 'fix parameters' in step:
            for dist in step['fix parameters']:
                for par in step['fix parameters'][dist]:
                    conf['params'][dist][par]['fixed']=True
                    #--set prior parameters values for passive distributions
                    for istep in step['dep']:
                        prior_order=self.order[istep]
                        prior_params=self.params[istep]
                        for i in range(len(prior_order)):
                            _,_dist,_par = prior_order[i]
                            if  dist==_dist and par==_par:
                                conf['params'][dist][par]['value']=prior_params[i]

        #--remove datasets not in the step
        datasets=list(conf['datasets']) #--idis,dy,....
        for dataset in datasets:
            if  dataset in step['datasets']:

                #--remove entry from xlsx
                xlsx=list(conf['datasets'][dataset]['xlsx'])
                for idx in xlsx:
                    if  idx in step['datasets'][dataset]:
                        continue
                    else:
                        del conf['datasets'][dataset]['xlsx'][idx]

                #--remove entry from norm
                norm=list(conf['datasets'][dataset]['norm'])
                for idx in norm:
                    if  idx in step['datasets'][dataset]:
                        continue
                    else:
                        del conf['datasets'][dataset]['norm'][idx]
            else:
                del conf['datasets'][dataset]

        return conf

    def get_bounds(self):
        order=self.parman.order

        bounds_min=[]
        bounds_max=[]
        for entry in order:
            i,k,kk=entry
            if  i==1:
                p=conf['params'][k][kk]['value']
                pmin=conf['params'][k][kk]['min']
                pmax=conf['params'][k][kk]['max']
                if p<pmin or p>pmax:
                    msg='%s/%s outsize the limits %f %f %f'%(k,kk,p,pmin,pmax)
                    raise ValueError(msg)
                bounds_min.append(conf['params'][k][kk]['min'])
                bounds_max.append(conf['params'][k][kk]['max'])
            elif i==2:
                p=conf['datasets'][k]['norm'][kk]['value']
                pmin=conf['datasets'][k]['norm'][kk]['min']
                pmax=conf['datasets'][k]['norm'][kk]['max']
                if p<pmin or p>pmax:
                    msg='%s/%s outsize the limits %f %f %f'%(k,kk,p,pmin,pmax)
                    raise ValueError(msg)
                bounds_min.append(conf['datasets'][k]['norm'][kk]['min'])
                bounds_max.append(conf['datasets'][k]['norm'][kk]['max'])

        return (bounds_min, bounds_max)

    def get_guess(self,dep=[]):

        order = self.parman.order
        if conf['flat par']:
            if self.seed is not None:
                np.random.seed(self.seed)
            guess = self.parman.gen_flat()
        else:
            guess = self.parman.par

        #--retrieve priors from previous steps according to dep
        if len(dep) > 0:
            for istep in dep:
                prior_order  = self.order [istep]
                prior_params = self.params[istep]
                #--compare and match prior order to current order
                for i in range(len(prior_order)):
                    for j in range(len(order)):
                        if order[j] == prior_order[i]:
                            guess[j] = prior_params[i]

            #--initiate parameters with 'zero' to 0
            for i in range(len(order)):
                kind = order[i]
                if kind[0] == 2:
                    continue
                flav = kind[2]
                if 'zero' in conf['params'][kind[1]][flav]:
                    if conf['params'][kind[1]][flav]['zero']:
                        guess[i] = 0.0
                        print('initializing %s %s to start at zero'%(kind[1],flav))

            #--initiate parameters with 'start' to given value
            for i in range(len(order)):
                kind = order[i]
                if kind[0] == 2:
                    continue
                flav = kind[2]
                if 'start' in conf['params'][kind[1]][flav]:
                    value = conf['params'][kind[1]][flav]['start']
                    guess[i] = value
                    print('initializing %s %s to start at %s'%(kind[1],flav,value))

            #--randomize parameters within new bounds
            for i in range(len(order)):
                kind = order[i]
                if kind[0] == 2:
                    continue
                flav = kind[2]
                if 'reset' in conf['params'][kind[1]][flav]:
                    if conf['params'][kind[1]][flav]['reset']:
                        if conf['flat par']:
                            pmin = conf['params'][kind[1]][flav]['min']
                            pmax = conf['params'][kind[1]][flav]['max']
                            value = np.random.uniform(low=pmin,high=pmax,size=1)
                            guess[i] = value
                            print('randomizing %s %s between %s and %s'%(kind[1],flav,pmin, pmax))
                        else:
                            value = conf['params'][kind[1]][flav]['value']
                            guess[i] = value
                            print('setting %s %s to %s'%(kind[1],flav,value))

            #--updating the guesses to be priors
            for i in range(len(order)):
                kind = order[i]
                if kind[0] == 2:
                    continue
                flav = kind[2]
                if 'prior' in conf['params'][kind[1]][flav]:
                    priorflav = conf['params'][kind[1]][flav]['prior']
                    for j in range(len(prior_order)):
                        if prior_order[j][2] == priorflav and prior_order[j][1] == kind[1]:
                            guess[i] = prior_params[j]

        ## set parameters to buffer (when applicable)
        if (self.prior != None) and ('msr-buffer' in self.prior):
            for i in range(len(self.buffer_order)):
                for j in range(len(order)):
                    if order[j] == self.buffer_order[i]:
                        guess[j] = self.buffer_params[i]

        return guess

    def get_par(self,step,dep=[]):
        #--current value must be within min and max
        self.checklimits()

        #--initialize resman
        self.resman=RESMAN(self.nworkers)
        self.parman=self.resman.parman

        #--setups
        guess=self.get_guess(dep)
        bounds=self.get_bounds()
        self.set_counters()
        self.parman.set_new_params(guess,initial=True)

        ## print the 'guess' parameter when it is out of bound
        ## the 'guess' can be different from the parameters in 'conf', and 'guess' is not checked by 'self.checklimits()'
        if 'enforce limits' not in conf: enforce_limits = True
        else:                            enforce_limits = conf['enforce limits']
        if enforce_limits:
            for i in range(len(guess)):
                if (guess[i] < bounds[0][i]) or (guess[i] > bounds[1][i]):
                    message = '%s-%s has a value of %f that is outside of its limit [%f, %f]' % \
                              (self.parman.order[i][1], self.parman.order[i][2], guess[i], bounds[0][i], bounds[1][i])
                    self.resman.shutdown()
                    sys.exit(message)
        else:
            for i in range(len(guess)):
                if guess[i] < bounds[0][i]: guess[i] = bounds[0][i]
                if guess[i] > bounds[1][i]: guess[i] = bounds[1][i]


        #--run fit
        self.history = deque(maxlen = 2)
        self.history.append(copy.deepcopy(guess))
        # The UI and the schema advertise maxiter and optimizer; honour them
        # instead of silently ignoring them. least_squares bounds support
        # limits the method to trf/dogbox.
        ls_kwargs = dict(bounds=bounds, ftol=conf['ftol'])
        method = str(conf.get('method') or 'trf').lower()
        if method not in ('trf', 'dogbox'):
            method = 'trf'
        ls_kwargs['method'] = method
        try:
            max_nfev = int(conf.get('maxiter') or 0)
        except (TypeError, ValueError):
            max_nfev = 0
        if max_nfev > 0:
            ls_kwargs['max_nfev'] = max_nfev
        fit = least_squares(self.get_residuals, guess, **ls_kwargs)
        sol = fit.x

        self.parman.set_new_params(sol,initial=True)
        chi2=self.resman.get_chi2()

        #--close resman
        self.resman.shutdown()
        return sol, chi2

    def gen_output(self, step):
        inputmod=INPUTMOD(self.input_name)

        for kind in conf['params']:
            for par in conf['params'][kind]:
                value=conf['params'][kind][par]['value']
                inputmod.mod_par(kind,par,'value',value)

        for reaction in conf['datasets']:
            for idx in conf['datasets'][reaction]['norm']:
                value=conf['datasets'][reaction]['norm'][idx]['value']
                inputmod.mod_norm(reaction,idx,'value',value)

        fname = 'output-%d.py' % step
        inputmod.gen_input(fname)

    def run(self):
        load_config(self.input_name)

        # Inject conf['grid path'] from candidates if not already set in the YAML.
        # resman.py uses this to locate Mellin/interpolation tables for DY, zrap, etc.
        if 'grid path' not in conf:
            for _candidate in grid_candidates:
                _p = Path(str(_candidate))
                if _candidate and _p.exists():
                    conf['grid path'] = str(_p)
                    print(f"INFO: conf['grid path'] set to {conf['grid path']}")
                    break

        #--modify confs
        if self.verbose == False:
            conf['verbose'] = 1
        else:
            conf['verbose']=self.verbose

        #--backup conf(after mods)
        conf_bkp = copy.deepcopy(conf)

        #--decide if the input should be modified
        output = True
        if 'bootstrap' in conf and conf['bootstrap'] == True:
            output = False

        if 'steps' in conf:
            isteps = sorted(conf['steps'])
            self.order  = {}
            self.params = {}

            if self.prior != None:
                prior = load(self.prior)
                self.order  = prior['order']
                self.params = prior['params']
                prior_steps = sorted(list(self.order))
                self.prior_step = max(prior_steps)
                isteps = [i for i in isteps if i not in prior_steps]
                if len(isteps) > 1:
                    sys.exit('Process one step at a time!')
                elif len(isteps) == 1:
                    self.current_step = isteps[0]
                elif (len(isteps) == 0) and ('msr-buffer' in self.prior):
                    self.current_step = max(prior_steps)
                    prior_steps.pop()
                    self.prior_step = max(prior_steps)
                if 'msr-buffer' in self.prior:
                    self.buffer_order  = copy.deepcopy(self.order [self.current_step])
                    self.buffer_params = copy.deepcopy(self.params[self.current_step])
                    conf['deviation-buffer'] = copy.deepcopy(prior['deviation'])
                    del self.order [self.current_step]
                    del self.params[self.current_step]
                    del prior['deviation']
                else:
                    # plain posterior->prior chain (no msr-buffer): get_residuals
                    # may snapshot a buffer msr and needs this key to exist
                    conf['deviation-buffer'] = []
                print('prior   step(s):', prior_steps)
                print('current step   :', self.current_step)
            else:
                self.current_step = isteps[0]

            step = conf['steps'][self.current_step]
            conf.update(copy.deepcopy(conf_bkp))
            conf.update(self.get_conf(conf_bkp,step))

            print()
            message  = '--step %d: ' % self.current_step
            message += 'npQCD objects = '
            for _ in conf['params'].keys():
                message += _ + '  '
            message += ', datasets = '
            for _ in conf['datasets'].keys():
                message += _ + ' '
            print(message)

            par, chi2 = self.get_par(self.current_step, step['dep'])
            self.order [self.current_step] = copy.deepcopy(self.parman.order)
            self.params[self.current_step] = copy.deepcopy(par)
            if output:
                self.gen_output(self.current_step)

            data = {}
            data['order' ] = copy.deepcopy(self.order)
            data['params'] = copy.deepcopy(self.params)
            data['chi2'  ] = chi2
            if self.prior == None:
                name  = self.current + '/msr/'
                name += '%s.msr' % id_generator(size = 12)
            else:
                name = self.current + '/msr/' + self.prior.rsplit('/', 1)[-1]
            checkdir(name[: name.rindex('/') + 1])
            save(data, name)
            os.system('rm -f %s' % name.replace('/msr/', '/msr-buffer/'))

        else:
            par = self.get_par(0)
            if output:
                self.gen_output(0)

        return

if __name__ == '__main__':

    ap = argparse.ArgumentParser()
    ap.add_argument(      'input'    , type = str,                 help = 'config file (e.g. input.py)')
    ap.add_argument(      'current'  , type = str,                 help = 'path to current fit')
    ap.add_argument('-n', '--ncores' , type = int, default = 20  , help = 'cpu cores')
    ap.add_argument('-v', '--verbose', type = int, default = 1   , help = 'verbose')
    ap.add_argument('-p', '--prior'  , type = str, default = None, help = 'path to prior')
    ap.add_argument('-s', '--seed'   , type = int, default = None, help = 'seed for flat parameter initialization')
    ap.add_argument('--fitpack-path' , type = str, default = None, help = 'override FITPACK path resolution')
    pas = ap.parse_args()

    MAXLIKE(  pas.input      \
            , pas.current    \
            , pas.ncores     \
            , pas.verbose    \
            , pas.prior      \
            , pas.seed       \
            , pas.fitpack_path).run()
