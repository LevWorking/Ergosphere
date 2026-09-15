import struct, sys, io

class R:
    def __init__(self, d): self.d=d; self.p=0
    def seek(self,p): self.p=p
    def tell(self): return self.p
    def u8(self):  v=self.d[self.p]; self.p+=1; return v
    def i32(self): v=struct.unpack_from('<i',self.d,self.p)[0]; self.p+=4; return v
    def u32(self): v=struct.unpack_from('<I',self.d,self.p)[0]; self.p+=4; return v
    def i64(self): v=struct.unpack_from('<q',self.d,self.p)[0]; self.p+=8; return v
    def u16(self): v=struct.unpack_from('<H',self.d,self.p)[0]; self.p+=2; return v
    def f32(self): v=struct.unpack_from('<f',self.d,self.p)[0]; self.p+=4; return v
    def raw(self,n): v=self.d[self.p:self.p+n]; self.p+=n; return v
    def fstring(self):
        n=self.i32()
        if n==0: return ''
        if n<0:
            b=self.raw(-n*2); return b.decode('utf-16-le').rstrip('\0')
        b=self.raw(n); return b.decode('latin-1').rstrip('\0')
    def guid(self): return self.raw(16).hex()

class Pkg:
    def __init__(self, path):
        self.d=open(path,'rb').read()
        r=R(self.d)
        tag=r.u32()
        assert tag==0x9E2A83C1, hex(tag)
        self.legacy=r.i32()
        if self.legacy != -4: r.i32()      # LegacyUE3Version
        self.verUE4=r.i32()
        self.verUE5=r.i32() if self.legacy<=-8 else 0
        self.verLic=r.i32()
        ncv=r.i32()
        for _ in range(ncv): r.raw(16); r.i32()
        self.totalHeaderSize=r.i32()
        self.folder=r.fstring()
        self.packageFlags=r.u32()
        self.nameCount=r.i32(); self.nameOffset=r.i32()
        # UE5 ADD_SOFTOBJECTPATH_LIST = 1008
        if self.verUE5 >= 1008:
            self.softCount=r.i32(); self.softOffset=r.i32()
        self.filterEditorOnly = bool(self.packageFlags & 0x80000000)
        if not self.filterEditorOnly:
            self.localizationId=r.fstring()
        self.gatherCount=r.i32(); self.gatherOffset=r.i32()
        self.exportCount=r.i32(); self.exportOffset=r.i32()
        self.importCount=r.i32(); self.importOffset=r.i32()
        self.dependsOffset=r.i32()
        self.softPkgCount=r.i32(); self.softPkgOffset=r.i32()
        self.searchableNamesOffset=r.i32()
        self.thumbnailTableOffset=r.i32()
        self.guid=r.guid()
        self.names=[]; self.imports=[]; self.exports=[]
        self._readNames(); self._readImports(); self._readExports()

    def _readNames(self):
        r=R(self.d); r.seek(self.nameOffset)
        for _ in range(self.nameCount):
            s=r.fstring()
            if self.verUE4>=504: r.u16(); r.u16()   # NAME_HASHES
            self.names.append(s)

    def name(self, idx, num=0):
        s = self.names[idx] if 0<=idx<len(self.names) else f'<bad {idx}>'
        return s if num==0 else f'{s}_{num-1}'

    def fname(self, r): 
        i=r.i32(); n=r.i32(); return self.name(i,n)

    def _readImports(self):
        r=R(self.d); r.seek(self.importOffset)
        for _ in range(self.importCount):
            cp=self.fname(r); cn=self.fname(r); outer=r.i32(); on=self.fname(r)
            pkg=None
            if self.verUE4>=520 and not self.filterEditorOnly:  # NON_OUTER_PACKAGE_IMPORT
                pkg=self.fname(r)
            if self.verUE5>=1003:  # OPTIONAL_RESOURCES
                r.i32()
            self.imports.append(dict(classPkg=cp,className=cn,outer=outer,name=on,pkg=pkg))

    def _readExports(self):
        r=R(self.d); r.seek(self.exportOffset)
        for _ in range(self.exportCount):
            e={}
            e['classIndex']=r.i32(); e['superIndex']=r.i32(); e['templateIndex']=r.i32()
            e['outerIndex']=r.i32(); e['name']=self.fname(r); e['flags']=r.u32()
            e['serialSize']=r.i64(); e['serialOffset']=r.i64()
            e['bForcedExport']=r.i32(); e['bNotForClient']=r.i32(); e['bNotForServer']=r.i32()
            if self.verUE5 < 1005:   # REMOVE_OBJECT_EXPORT_PACKAGE_GUID
                r.raw(16)
            if self.verUE5 >= 1006:  # TRACK_OBJECT_EXPORT_IS_INHERITED
                r.i32()
            e['packageFlags']=r.u32()
            e['bNotAlwaysLoadedForEditorGame']=r.i32(); e['bIsAsset']=r.i32()
            if self.verUE5>=1003: r.i32()   # GeneratePublicHash
            for k in ('firstDep','serBeforeSer','createBeforeSer','serBeforeCreate','createBeforeCreate'):
                e[k]=r.i32()
            self.exports.append(e)

    def resolve(self, idx):
        if idx>0:  return self.exports[idx-1]['name']
        if idx<0:  return self.imports[-idx-1]['name']
        return None

    def cls(self, e): return self.resolve(e['classIndex'])

    def path(self, idx):
        parts=[]; seen=0
        while idx!=0 and seen<20:
            seen+=1
            if idx>0:
                e=self.exports[idx-1]; parts.append(e['name']); idx=e['outerIndex']
            else:
                im=self.imports[-idx-1]; parts.append(im['name']); idx=im['outer']
        return '.'.join(reversed(parts))

# ---------- tagged property parsing ----------
def read_props(pkg, r, end):
    props=[]
    while r.tell() < end-8:
        nm = pkg.fname(r)
        if nm=='None': break
        ty = pkg.fname(r)
        size=r.i32(); arr=r.i32()
        extra={}
        if ty=='StructProperty':
            extra['struct']=pkg.fname(r); r.raw(16)
        elif ty=='BoolProperty': extra['bool']=r.u8()
        elif ty in ('ByteProperty','EnumProperty'): extra['enum']=pkg.fname(r)
        elif ty=='ArrayProperty': extra['inner']=pkg.fname(r)
        elif ty=='SetProperty': extra['inner']=pkg.fname(r)
        elif ty=='MapProperty': extra['key']=pkg.fname(r); extra['val']=pkg.fname(r)
        elif ty=='OptionalProperty': extra['inner']=pkg.fname(r)
        hasGuid=r.u8()
        if hasGuid: r.raw(16)
        start=r.tell(); data=r.raw(size); r.seek(start+size)
        props.append(dict(name=nm,type=ty,size=size,arrayIndex=arr,data=data,**extra))
    return props

def prop_val(pkg, p):
    """Decode simple property values."""
    d=p['data']; t=p['type']
    try:
        if t=='IntProperty':   return struct.unpack_from('<i',d,0)[0]
        if t=='FloatProperty': return struct.unpack_from('<f',d,0)[0]
        if t=='DoubleProperty':return struct.unpack_from('<d',d,0)[0]
        if t=='BoolProperty':  return bool(p.get('bool'))
        if t=='NameProperty':  return pkg.fname(R(d))
        if t=='StrProperty':   return R(d).fstring()
        if t=='ObjectProperty':
            i=struct.unpack_from('<i',d,0)[0]; return pkg.path(i) or 'None'
        if t=='ByteProperty':
            if len(d)==8: return pkg.fname(R(d))
            return d[0]
        if t=='EnumProperty':  return pkg.fname(R(d))
        if t=='TextProperty':
            rr=R(d); rr.u32(); rr.u8()
            return rr.fstring()
    except Exception as ex:
        return f'<err {ex}>'
    return None

def struct_props(pkg, p):
    """Parse a StructProperty's inner tagged properties."""
    rr=R(p['data'])
    return read_props(pkg, rr, len(p['data']))
