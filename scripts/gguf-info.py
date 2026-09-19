import argparse
import json
import struct
from pathlib import Path

TYPE_SIZES = {0:1,1:1,2:2,3:2,4:4,5:4,6:4,7:1,10:8,11:8,12:8}

def u32(f): return struct.unpack("<I", f.read(4))[0]
def u64(f): return struct.unpack("<Q", f.read(8))[0]

def read_string(f):
    n=u64(f)
    return f.read(n).decode("utf-8", errors="replace")

def skip_value(f, t):
    if t in TYPE_SIZES:
        f.seek(TYPE_SIZES[t], 1); return
    if t == 8:
        f.seek(u64(f), 1); return
    if t == 9:
        et=u32(f); n=u64(f)
        if et in TYPE_SIZES:
            f.seek(TYPE_SIZES[et]*n, 1); return
        for _ in range(n): skip_value(f, et)
        return
    raise ValueError(f"unsupported GGUF value type {t}")

def read_scalar(f, t):
    fmts={0:"<B",1:"<b",2:"<H",3:"<h",4:"<I",5:"<i",6:"<f",7:"<?",10:"<Q",11:"<q",12:"<d"}
    if t == 8: return read_string(f)
    if t in fmts:
        size=struct.calcsize(fmts[t])
        return struct.unpack(fmts[t], f.read(size))[0]
    skip_value(f,t)
    return None

def inspect(path):
    path=Path(path)
    with path.open("rb") as f:
        if f.read(4) != b"GGUF": raise ValueError("not a GGUF file")
        version=u32(f); tensors=u64(f); kv_count=u64(f)
        wanted={}
        for _ in range(kv_count):
            key=read_string(f); t=u32(f)
            capture=(key in {"general.name","general.architecture","general.file_type","general.quantization_version"} or
                     key.endswith(".context_length") or key.endswith(".embedding_length") or key.endswith(".block_count"))
            if capture and t != 9:
                wanted[key]=read_scalar(f,t)
            else:
                skip_value(f,t)
            if {"general.name","general.architecture","general.file_type"}.issubset(wanted) and any(k.endswith(".context_length") for k in wanted):
                break
    return {
        "path": str(path),
        "size_gib": round(path.stat().st_size / (1024**3), 3),
        "gguf_version": version,
        "tensor_count": tensors,
        "metadata": wanted,
    }

if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    args=ap.parse_args()
    print(json.dumps([inspect(p) for p in args.paths], indent=2))
