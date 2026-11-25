#!/usr/bin/env python3
"""Offline helpers for reading the GR IIIx RTOS image (requires: pip install capstone).

Usage (interactive):  import sys; sys.path.insert(0, "tools/firmware"); from rtos_analysis import *
Set GR_FW to the decoded payload path (default work/fwdc243b-v160.decoded.bin).
RTOS load base 0x53000000 at file offset 0x10870 (GR IIIx 1.60; GR IV uses 0x17D10).
Read-only: nothing here writes to the firmware or talks to a camera.
"""
import os
import array, re, pickle
_WORK=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","..","work")  # repo work/, from any folder
FW=os.environ.get("GR_FW",os.path.join(_WORK,"fwdc243b-v160.decoded.bin"))
BASE,OFF=0x53000000,0x10870
CODE_END=0xc1cb78
d=open(FW,"rb").read()
def va(o): return o-OFF+BASE
def fo(v): return v-BASE+OFF
def cstr(v,n=120):
    o=fo(v); e=d.find(b"\0",o,o+n); return d[o:e if e>0 else o+n].decode("latin1")
def arm_imm(x): return ((x>>4)&0xf000)|(x&0xfff)
def build_xrefs():
    # movw/movt pairs up to 16 instructions apart (8 missed e.g. 0x536e9bdc/0x536e9c08).
    cache=os.path.join(_WORK,"xrefs-w16.pkl") if FW.endswith("fwdc243b-v160.decoded.bin") else None
    if cache is None: raise SystemExit("build_xrefs: no cache path for this firmware; set one first")
    if os.path.exists(cache): return pickle.load(open(cache,"rb"))
    w=array.array("I",d[OFF:CODE_END&~3]); last={}; xr={}
    for i,x in enumerate(w):
        if (x&0x0ff00000)==0x03000000: last[(x>>12)&0xf]=(i,arm_imm(x))
        elif (x&0x0ff00000)==0x03400000:
            rd=(x>>12)&0xf
            if rd in last and i-last[rd][0]<=16:
                val=(arm_imm(x)<<16)|last[rd][1]
                xr.setdefault(val,[]).append(BASE+4*i)
    pickle.dump(xr,open(cache,"wb")); return xr
def func_start(v, maxback=0x4000):
    # walk back to a STMDB sp!,{...,lr} (push with lr)
    o=fo(v)&~3
    for k in range(0,maxback,4):
        x=int.from_bytes(d[o-k:o-k+4],"little")
        if (x&0xffff4000)==0xe92d4000: return v-k
    return None

import struct as _st
def u32(v): return _st.unpack("<I",d[fo(v):fo(v)+4])[0]
def find_ptr(v):
    p=_st.pack("<I",v); out=[]; i=d.find(p)
    while i>=0:
        if i%4==0: out.append(va(i))
        i=d.find(p,i+1)
    return out
def is_code(v): return BASE<=v<va(CODE_END)
def vtables_for(cls):
    import re
    m=re.search(rb"\d+"+cls.encode()+rb"\0",d)
    if not m: return []
    name_va=va(m.start())
    res=[]
    for ti in [p-4 for p in find_ptr(name_va)]:
        for vt in find_ptr(ti):
            if u32(vt-4)==0:  # offset_to_top
                meths=[]; a=vt+4
                while is_code(u32(a)) and len(meths)<80: meths.append(u32(a)); a+=4
                res.append((vt,meths))
    return res

import capstone as _cs
_md=_cs.Cs(_cs.CS_ARCH_ARM,_cs.CS_MODE_ARM); _md.detail=False
def func_end(start,maxlen=0x3000):
    o=fo(start)
    for k in range(4,maxlen,4):
        x=int.from_bytes(d[o+k:o+k+4],"little")
        if (x&0xffff4000)==0xe92d4000: return start+k
    return start+maxlen
def dis(start,end=None):
    end=end or func_end(start)
    return list(_md.disasm(d[fo(start):fo(end)],start))
def calls(start):
    out=[]
    for i in dis(start):
        if i.mnemonic in("bl","blx") and i.op_str.startswith("#"): out.append(int(i.op_str[1:],16))
    return out
def strrefs(start):
    ins=dis(start); out=[]
    regs={}
    for i in ins:
        if i.mnemonic.startswith("movw"):
            r,imm=i.op_str.split(", #"); regs[r]=int(imm,16)
        elif i.mnemonic.startswith("movt"):
            r,imm=i.op_str.split(", #")
            if r in regs:
                v=(int(imm,16)<<16)|regs[r]
                if not is_code(v) and 0x53000000<=v<0x56000000:
                    s=cstr(v,80)
                    if len(s)>=4 and s.isprintable(): out.append(s)
    return out
def show(start,n=400):
    for i in dis(start)[:n]: print(f"  {i.address:08x}: {i.mnemonic:8s} {i.op_str}")

def func_end(start,maxlen=0x6000):
    o=fo(start)
    for k in range(4,maxlen,4):
        x=int.from_bytes(d[o+k:o+k+4],"little")
        if x==0xe1a0c00d: return start+k   # next "mov ip, sp"
    return start+maxlen
def report(f,depth=2,seen=None,indent=0,maxcalls=25):
    seen=seen if seen is not None else set()
    if f in seen: return
    seen.add(f); ins=dis(f)
    fl=sum(1 for i in ins if i.mnemonic.startswith("v"))
    print(" "*indent+f"{f:08x} n={len(ins)} vfp={fl} strs={strrefs(f)[:6]}")
    if depth>0:
        for c in list(dict.fromkeys(calls(f)))[:maxcalls]: report(c,depth-1,seen,indent+2)

def all_funcs():
    import array
    w=array.array("I",d[OFF:CODE_END&~3])
    return [BASE+4*i for i,x in enumerate(w) if x==0xe1a0c00d and (w[i+1]&0xffff4000)==0xe92d4000]
def unit_ranges():
    import re,bisect
    xr=build_xrefs(); fs=all_funcs()
    anchors=[]
    for m in re.finditer(rb"src/[A-Za-z0-9_/]+\.cpp(?=\0)",d):
        for r in xr.get(va(m.start()),[]):
            i=bisect.bisect_right(fs,r)-1
            if i>=0: anchors.append((fs[i],m.group().decode()))
    return fs,sorted(set(anchors))
