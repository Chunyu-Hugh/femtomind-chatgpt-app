"""Identity-mapped starting values, never an objective-function prior."""
from __future__ import annotations
import hashlib,json,math,re
from pathlib import Path

SCHEMA='femtomind.prior-replicas.v1'
def identity(row):
    if not isinstance(row,(list,tuple)) or len(row)!=3 or type(row[0]) is not int or row[0] not in (1,2):
        raise ValueError('Expected FitPack identity [1, object, coefficient] or [2, reaction, dataset]')
    return (row[0],str(row[1]),str(row[2]))

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def bundle(order,values,ids,provenance):
    keys=[identity(x) for x in order]
    if len(set(keys))!=len(keys) or len(set(ids))!=len(ids) or not ids:raise ValueError('Duplicate identities or invalid replica IDs')
    if len(values)!=len(ids):raise ValueError('Replica count mismatch')
    rows=[]
    for rid,row in zip(ids,values):
        if len(row)!=len(keys) or any(type(v) not in (float,int) or not math.isfinite(v) for v in row):raise ValueError('Nonfinite or misaligned parameter values')
        rows.append({'id':str(rid),'values':list(row)})
    data={'schema':SCHEMA,'order':[list(x) for x in keys],'replicas':rows,'provenance':provenance}
    return {**data,'sha256':digest(data)}

def verify(data):
    if data.get('schema')!=SCHEMA or data.get('sha256')!=digest({k:v for k,v in data.items() if k!='sha256'}):raise ValueError('Prior snapshot integrity failure')
    rebuilt=bundle(data['order'],[r['values'] for r in data['replicas']],[r['id'] for r in data['replicas']],data['provenance'])
    if rebuilt!=data:raise ValueError('Noncanonical prior snapshot')
    return data

def select(data,n,replica_ids=None):
    verify(data)
    if replica_ids is not None and not isinstance(replica_ids,list):raise ValueError('replica_ids must be an explicit list')
    rows={r['id']:r for r in data['replicas']}
    ids=list(rows) if replica_ids is None else [str(x) for x in replica_ids]
    if len(ids)!=n or len(set(ids))!=n or any(x not in rows for x in ids):
        raise ValueError('Explicit unique replica_ids of exactly the requested job count required; no implicit truncation/repetition')
    chosen=bundle(data['order'],[rows[x]['values'] for x in ids],ids,data['provenance'])
    chosen['provenance']={**chosen['provenance'],'source_replica_count':len(rows),'selected_ids':ids}
    chosen['sha256']=digest({k:v for k,v in chosen.items() if k!='sha256'})
    return chosen

def _mapping(data,order,definitions):
    """Build the target-first identity map once, independent of replica values."""
    source={identity(raw):i for i,raw in enumerate(data['order'])}
    keys=list(map(identity,order));matched=[];missing=[];entries=[]
    for i,key in enumerate(keys):
        definition=definitions(key)
        if definition.get('fixed') is not False:raise ValueError('Runtime free order contains fixed/linked parameter')
        if key not in source:missing.append(list(key));continue
        entries.append((i,source[key],key,definition.get('min'),definition.get('max')))
        matched.append(list(key))
    target=set(keys)
    return entries,{'matched':matched,'newly_initialized':missing,
                    'unused_source':[list(k) for k in source if k not in target],'snapshot_sha256':data['sha256']}

def _values(row,entries):
    incompatible=[];values=[]
    for target,source,key,lo,hi in entries:
        value=row['values'][source]
        if lo is None or hi is None or not lo<=value<=hi:
            incompatible.append({'identity':list(key),'value':value,'bounds':[lo,hi]})
        values.append((target,value))
    if incompatible:raise ValueError('Incompatible prior bounds: '+json.dumps(incompatible)+'; source replica '+row['id'])
    return values

def preview(data,order,definitions):
    """Validate the full ensemble once, then check every assigned value in O(N*P).

    Reports share one identity map; scientific value arrays stay server-side.
    This is deliberately not a cache: each new request verifies its source.
    """
    verify(data)
    entries,report=_mapping(data,order,definitions)
    assignments=[]
    for i,row in enumerate(data['replicas']):
        _values(row,entries)
        assignments.append({'job_index_zero_based':i,'slurm_replica':i+1,'source_replica_id':row['id']})
    return {**report,'assignments':assignments}

def apply(data,replica,order,guess,definitions):
    verify(data)
    if type(replica) is not int or not 1<=replica<=len(data['replicas']):raise ValueError('Slurm job has no assigned prior replica')
    row=data['replicas'][replica-1]
    entries,report=_mapping(data,order,definitions)
    result=guess.copy()
    for i,value in _values(row,entries):result[i]=value
    return result,{**report,'job_index_zero_based':replica-1,'slurm_replica':replica,'source_replica_id':row['id']}

def load_source(spec, native_read=None):
    import numpy as np
    if native_read is None:
        def native_read(path):
            from .replica_formats import read
            return read(path)
    kind=spec['kind']
    if kind=='archive':
        root=Path(spec['directory']);manifest=json.loads((root/'manifest.json').read_text())
        if hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()!=spec['manifest_sha256']:raise ValueError('Archive manifest changed')
        for name in ('parameter_metadata.json','parameters.npz'):
            if hashlib.sha256((root/name).read_bytes()).hexdigest()!=manifest['files'][name]['sha256']:raise ValueError('Archive parameter hash mismatch')
        order=json.loads((root/'parameter_metadata.json').read_text())['order']
        with np.load(root/'parameters.npz',allow_pickle=False) as a:
            values=a['values'].tolist();ids=a['source_ordinal'].astype(str).tolist()
        return bundle(order,values,ids,{'kind':kind,'manifest_sha256':spec['manifest_sha256'],'scientific_settings':spec.get('scientific_settings',{})})
    if kind=='campaign':
        root=Path(spec['directory'])
        paths=sorted(root.glob('**/fitted_parameters.json'))
        if paths:
            snapshots=[verify(json.loads(p.read_text())) for p in paths]
            # Mixing partial canonical and legacy exports would omit/repeat replicas.
            msrs=list(root.glob('**/*.msr'))
            if msrs and len(msrs)!=len(paths):raise ValueError('Incomplete canonical campaign export; explicit file selection required')
        else:
            paths=sorted(root.glob('**/*.msr'),key=lambda p:tuple(int(x) if x.isdigit() else x for x in re.split(r'(\d+)',str(p.relative_to(root)))))
            if not paths:raise ValueError('Completed campaign has no supported replica files')
            snapshots=[normalize_native(native_read(p),p,{**spec,'source_id':str(p.relative_to(root))}) for p in paths]
        keys=[identity(x) for x in snapshots[0]['order']]
        rows=[]
        for snapshot in snapshots:
            positions={identity(k):i for i,k in enumerate(snapshot['order'])}
            if set(positions)!=set(keys):raise ValueError('Campaign replicas have differing scientific identities')
            for row in snapshot['replicas']:rows.append({'id':row['id'],'values':[row['values'][positions[k]] for k in keys]})
        rows=sorted(rows,key=lambda x:(0,int(x['id'])) if x['id'].isdigit() else (1,tuple(int(v) if v.isdigit() else v for v in re.split(r'(\d+)',x['id']))))
        if spec.get('expected_count') is not None and len(rows)!=spec['expected_count'] and not spec.get('explicit_selection'):
            raise ValueError('Campaign has missing fitted outputs; select available replica IDs explicitly, never omit them implicitly')
        return bundle(keys,[x['values'] for x in rows],[x['id'] for x in rows],{'kind':'campaign','source_hashes':[x['sha256'] for x in snapshots],'original_requested_jobs':spec.get('expected_count'),'available_fitted_replicas':len(rows),'scientific_settings':spec.get('scientific_settings') or snapshots[0]['provenance'].get('scientific_settings',{})})
    p=Path(spec['path']);raw=p.read_bytes()
    if p.suffix=='.npy':
        try:a=np.load(p,allow_pickle=False)
        except ValueError:return normalize_native(native_read(p),p,spec)
        required={'replica','kind','object','parameter','value'}
        if a.dtype.names is None or set(a.dtype.names)!=required:raise ValueError('NPY requires named replica/kind/object/parameter/value records; arbitrary arrays/pickled objects unsupported')
        groups={}
        for row in a:
            rid=str(row['replica']);key=identity([int(row['kind']),str(row['object']),str(row['parameter'])]);g=groups.setdefault(rid,{})
            if key in g:raise ValueError('Duplicate source parameter')
            g[key]=float(row['value'])
        order=list(next(iter(groups.values())))
        if any(set(g)!=set(order) for g in groups.values()):raise ValueError('Per-replica scientific identities differ')
        return bundle(order,[[g[k] for k in order] for g in groups.values()],list(groups),{'kind':'npy','sha256':hashlib.sha256(raw).hexdigest(),'scientific_settings':spec.get('scientific_settings',{})})
    # Native .msr and object NPY are executable pickle formats, not arbitrary numeric data.
    if p.suffix=='.msr':return normalize_native(native_read(p),p,spec)
    if p.suffix!='.json':raise ValueError('Supported sources are native NPY/MSR or canonical JSON')
    return verify(json.loads(raw))

def normalize_native(payload,path,spec):
    if not isinstance(payload,dict) or 'order' not in payload or 'params' not in payload:raise ValueError('Expected source order and params')
    order=payload['order'];values=payload['params'];step=spec.get('step')
    if step is not None and type(step) is not int:raise ValueError('Fit step must be an integer')
    if isinstance(order,dict):
        if not isinstance(values,dict) or set(values)!=set(order):raise ValueError('Fit-step order/params keys differ')
        if step is None:
            if len(order)!=1:raise ValueError('Multiple fit steps: select step explicitly')
            step=next(iter(order))
        if step not in order:raise ValueError('Selected fit step is absent')
        order=order[step];values=values[step]
    elif step is not None:raise ValueError('Source has no fit-step axis')
    if not isinstance(order,list) or not isinstance(values,list) or not values:raise ValueError('Unsupported order/value layout')
    if all(type(x) in (float,int) for x in values):values=[values]
    if any(not isinstance(row,list) for row in values):raise ValueError('Expected parameter vectors')
    ids=([spec.get('source_id',Path(path).stem)] if Path(path).suffix=='.msr' and len(values)==1 else [str(i) for i in range(len(values))])
    stored=payload.get('conf',{})
    science={k:v for k,v in stored.items() if k in ('Q20','beta version') or k.endswith(' parametrization')}
    declared=spec.get('scientific_settings',{})
    if any(k in science and science[k]!=v for k,v in declared.items()):raise ValueError('Declared source settings contradict stored conf')
    return bundle(order,values,ids,{'kind':Path(path).suffix[1:],'source_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),'step':step,'scientific_settings':{**declared,**science},'metadata_origin':'stored conf' if science else 'caller declaration' if declared else 'missing: source contains no conf'})


def install(replica):
    """Application-side hook after native random/Gaussian initialization, for audit AND fit."""
    from fitlib.maxlike import MAXLIKE
    from tools.config import conf
    original=MAXLIKE.get_guess
    def get_guess(self,*args,**kwargs):
        data=conf.get('jamai prior replicas')
        if data is None:return original(self,*args,**kwargs)
        runtime_keys=set(map(identity,self.parman.order))
        model_objects={key[1] for key in map(identity,data['order']) if key[0]==1 and key in runtime_keys}
        science=data['provenance'].get('scientific_settings',{})
        if model_objects and science.get('Q20') != conf.get('Q20'):raise ValueError('Prior input scale does not match runtime configuration')
        relevant={'Q20'} if model_objects else set()
        if 'pdf' in model_objects:relevant.add('beta version')
        relevant.update(obj+' parametrization' for obj in model_objects if obj.startswith('ff'))
        for key in relevant:
            if key in science and science[key]!=conf.get(key):raise ValueError('Prior parametrization changed at runtime: '+key)
        old=conf.get('flat par');conf['flat par']=True
        try:guess=original(self,*args,**kwargs)
        finally:
            if old is None:conf.pop('flat par',None)
            else:conf['flat par']=old
        def definition(key):return conf['params'][key[1]][key[2]] if key[0]==1 else conf['datasets'][key[1]]['norm'][int(key[2])]
        values,report=apply(data,replica,self.parman.order,guess,definition)
        self.parman.set_new_params(values,initial=True)
        output=Path(self.current);output.mkdir(parents=True,exist_ok=True)
        audit={**report,'order':[list(identity(k)) for k in self.parman.order],'effective_start':values.tolist()}
        (output/'prior_initialization.json').write_text(json.dumps(audit,allow_nan=False,indent=2))
        return values
    MAXLIKE.get_guess=get_guess
    original_get_par=MAXLIKE.get_par
    def get_par(self,*args,**kwargs):
        result=original_get_par(self,*args,**kwargs)
        values=result[0]
        snapshot=bundle(self.parman.order,[[float(v) for v in values]],[str(replica)],{'kind':'completed_campaign','scientific_settings':{k:v for k,v in conf.items() if k in ('Q20','beta version') or k.endswith(' parametrization')},'prior_snapshot':conf.get('jamai prior replicas',{}).get('sha256')})
        (Path(self.current)/'fitted_parameters.json').write_text(json.dumps(snapshot,allow_nan=False))
        return result
    MAXLIKE.get_par=get_par
