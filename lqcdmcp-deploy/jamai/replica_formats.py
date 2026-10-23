"""Data-only reader for the observed FitPack NumPy/MSR serialization.

Pickle opcodes are interpreted as containers/numeric arrays. No pickle loader,
serialized callable, class import, or object method is ever executed.
"""
import ast,math,pickletools,zlib
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Symbol:
    module:str
    name:str
@dataclass
class DType:
    code:str
    endian:str='='
@dataclass
class Array:
    value:object=None

def decode(payload):
    import numpy as np
    stack=[];memo={};mark=object()
    def marked():
        i=next(i for i in range(len(stack)-1,-1,-1) if stack[i] is mark)
        values=stack[i+1:];del stack[i:];return values
    def numeric(dtype,raw,shape=(),fortran=False):
        if not isinstance(dtype,DType) or not isinstance(raw,bytes):raise ValueError('Unsupported numeric storage')
        dt=np.dtype(dtype.code)
        if dt.kind not in 'fiub' or dt.itemsize>8:raise ValueError('Unsupported scientific dtype')
        dt=dt.newbyteorder(dtype.endian)
        count=math.prod(shape) if shape else 1
        if count>10000000 or len(raw)!=count*dt.itemsize:raise ValueError('Numeric array size mismatch')
        a=np.frombuffer(raw,dtype=dt).reshape(shape,order='F' if fortran else 'C')
        return a.item() if not shape else a.tolist()
    for opcode,arg,pos in pickletools.genops(payload):
        op=opcode.name
        if op in ('PROTO','FRAME'):continue
        if op=='MARK':stack.append(mark)
        elif op in ('NONE','NEWTRUE','NEWFALSE'):stack.append({'NONE':None,'NEWTRUE':True,'NEWFALSE':False}[op])
        elif op in ('BININT','BININT1','BININT2','LONG','LONG1','LONG4','INT','BINFLOAT','FLOAT','BINUNICODE','SHORT_BINUNICODE','BINUNICODE8','UNICODE','BINBYTES','SHORT_BINBYTES','BINBYTES8'):stack.append(arg)
        elif op in ('EMPTY_LIST','EMPTY_DICT','EMPTY_TUPLE'):stack.append({'EMPTY_LIST':list,'EMPTY_DICT':dict,'EMPTY_TUPLE':tuple}[op]())
        elif op in ('BINPUT','LONG_BINPUT','PUT'):memo[int(arg)]=stack[-1]
        elif op=='MEMOIZE':memo[len(memo)]=stack[-1]
        elif op in ('BINGET','LONG_BINGET','GET'):stack.append(memo[int(arg)])
        elif op=='APPEND':value=stack.pop();stack[-1].append(value)
        elif op=='APPENDS':values=marked();stack[-1].extend(values)
        elif op=='SETITEM':value=stack.pop();key=stack.pop();stack[-1][key]=value
        elif op=='SETITEMS':
            values=marked()
            for i in range(0,len(values),2):stack[-1][values[i]]=values[i+1]
        elif op in ('TUPLE','LIST','DICT'):
            values=marked()
            stack.append(tuple(values) if op=='TUPLE' else values if op=='LIST' else dict(zip(values[::2],values[1::2])))
        elif op in ('TUPLE1','TUPLE2','TUPLE3'):
            n=int(op[-1]);values=tuple(stack[-n:]);del stack[-n:];stack.append(values)
        elif op=='GLOBAL':stack.append(Symbol(*arg.split(' ',1)))
        elif op=='STACK_GLOBAL':name=stack.pop();module=stack.pop();stack.append(Symbol(module,name))
        elif op=='REDUCE':
            args=stack.pop();symbol=stack.pop()
            if not isinstance(symbol,Symbol) or not isinstance(args,tuple):raise ValueError('Unsupported serialized reduction')
            name=(symbol.module,symbol.name)
            if name==('numpy','dtype'):
                if not args or not isinstance(args[0],str):raise ValueError('Unsupported dtype declaration')
                stack.append(DType(args[0]))
            elif name in (('numpy.core.multiarray','_reconstruct'),('numpy._core.multiarray','_reconstruct')):
                if not args or args[0]!=Symbol('numpy','ndarray'):raise ValueError('Unsupported array class')
                stack.append(Array())
            elif name in (('numpy.core.multiarray','scalar'),('numpy._core.multiarray','scalar')):
                stack.append(numeric(*args))
            else:raise ValueError('Unsupported serialized constructor: '+str(name))
        elif op=='BUILD':
            state=stack.pop();obj=stack[-1]
            if isinstance(obj,DType):
                if len(state)!=8 or state[2:5]!=(None,None,None):raise ValueError('Structured dtype is unsupported')
                obj.endian=state[1]
            elif isinstance(obj,Array):
                version,shape,dtype,fortran,raw=state
                if not isinstance(dtype,DType) or any(type(n) is not int or n<0 for n in shape) or math.prod(shape)>10000000:raise ValueError('Invalid array state')
                if np.dtype(dtype.code).kind=='O':
                    if not isinstance(raw,list) or len(raw)!=(math.prod(shape) if shape else 1):raise ValueError('Invalid object-container state')
                    obj.value=raw[0] if not shape else raw
                else:obj.value=numeric(dtype,raw,shape,fortran)
            else:raise ValueError('Unsupported serialized object state')
        elif op=='STOP':
            if len(stack)!=1 or pos+1!=len(payload):raise ValueError('Trailing or malformed replica payload')
            return unwrap(stack[0])
        else:raise ValueError('Unsupported replica opcode: '+op)
    raise ValueError('Incomplete replica serialization')

def unwrap(value):
    if isinstance(value,Array):return unwrap(value.value)
    if isinstance(value,(Symbol,DType)):raise ValueError('Unresolved serialized type')
    if isinstance(value,dict):return {k:unwrap(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [unwrap(v) for v in value]
    return value

def _read(path):
    path=Path(path);raw=path.read_bytes()
    if len(raw)>128*1024*1024:raise ValueError('Replica source exceeds supported size')
    if path.suffix=='.msr':
        decoder=zlib.decompressobj();payload=decoder.decompress(raw,128*1024*1024+1)
        if len(payload)>128*1024*1024 or not decoder.eof or decoder.unused_data:raise ValueError('Unsupported compressed replica size or trailing content')
        return decode(payload)
    if path.suffix=='.npy':
        if raw[:6]!=b'\x93NUMPY':raise ValueError('Invalid NPY header')
        version=tuple(raw[6:8]);size=2 if version==(1,0) else 4 if version in ((2,0),(3,0)) else 0
        if not size:raise ValueError('Unsupported NPY version')
        length=int.from_bytes(raw[8:8+size],'little');header=ast.literal_eval(raw[8+size:8+size+length].decode('utf-8'))
        if header['descr'] not in ('|O','|O8','O'):raise ValueError('Expected order-bearing object-container NPY')
        if header.get('shape')!=() or header.get('fortran_order') is not False:raise ValueError('Unsupported NPY container layout')
        return decode(raw[8+size+length:])
    raise ValueError('Expected NPY/MSR source')


def read(path):
    try:return _read(path)
    except (IndexError,KeyError,TypeError,zlib.error,UnicodeError,RecursionError,SyntaxError,OverflowError) as exc:
        raise ValueError('Malformed or unsupported replica serialization: '+type(exc).__name__) from None
