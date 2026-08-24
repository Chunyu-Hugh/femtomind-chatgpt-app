import sys
import numpy as np
from pathlib import Path
from typing import Any

conf = {}
options = {}


def _load_python(fname: str) -> Any:
    code = Path(fname).read_text()
    namespace = {"__file__": fname}
    exec(compile(code, fname, "exec"), namespace)
    return namespace["conf"]


def _load_yaml(fname: str) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise ImportError("PyYAML is required to load YAML config files") from exc
    data = yaml.safe_load(Path(fname).read_text())
    if isinstance(data, dict) and "conf" in data:
        return data["conf"]
    if isinstance(data, dict):
        return data
    raise ValueError("YAML config must be a mapping with a top-level 'conf' key or a mapping directly representing conf")


def load_config(fname):
    try:
        if fname.endswith((".yaml", ".yml")):
            loaded = _load_yaml(fname)
        else:
            loaded = _load_python(fname)
    except Exception as exc:
        print(f'ERR parsing the input card "{fname}": {exc}')
        # Exit NON-zero: a bad input card must fail the SLURM job. A bare
        # sys.exit() exits 0 and the scheduler reports success, which is how
        # broken cards silently produced "completed" replicas.
        sys.exit(1)

    conf.update(loaded)


def get_bounds(resman):
    order=resman.parman.order

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
            if (k in conf['datasets']) and (kk in conf['datasets'][k]):
                
                p=conf['datasets'][k]['norm'][kk]['value']
                pmin=conf['datasets'][k]['norm'][kk]['min']
                pmax=conf['datasets'][k]['norm'][kk]['max']
                if p<pmin or p>pmax: 
                    msg='%s/%s outsize the limits %f %f %f'%(k,kk,p,pmin,pmax)
                    raise ValueError(msg)
                bounds_min.append(conf['datasets'][k]['norm'][kk]['min'])
                bounds_max.append(conf['datasets'][k]['norm'][kk]['max'])

            else:
                bounds_min.append(0)
                bounds_max.append(0)

                
    return (np.array(bounds_min), np.array(bounds_max))
