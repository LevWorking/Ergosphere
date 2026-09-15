import sys, struct
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from uasset import *

NODE_CLASSES_HINT=('K2Node','EdGraphNode','MaterialGraphNode','AnimGraphNode')

class BP:
    def __init__(self, path):
        self.p=Pkg(path); self.nodes={}
        self._parse()

    def _parse(self):
        p=self.p
        for i,e in enumerate(p.exports):
            idx=i+1
            cls=p.cls(e) or ''
            if not any(h in cls for h in NODE_CLASSES_HINT): continue
            r=R(p.d); r.seek(e['serialOffset']); end=e['serialOffset']+e['serialSize']
            try: props=read_props(p,r,end)
            except Exception: continue
            tail=p.d[r.tell():end]
            pins=self._pins(idx,tail)
            self.nodes[idx]=dict(idx=idx,cls=cls,name=e['name'],
                                 outer=p.path(e['outerIndex']),
                                 props={pr['name']:pr for pr in props}, pins=pins, tail=tail)
        # global pin guid map
        self.pinmap={}
        for n in self.nodes.values():
            for pin in n['pins']:
                self.pinmap[pin['guid']]=(n['idx'],pin['name'])
        # resolve links
        for n in self.nodes.values():
            for pin in n['pins']:
                links=[]
                for (ni,g) in pin['refs']:
                    if g==pin['guid']: continue
                    if g not in self.pinmap: continue
                    tgt=self.pinmap.get(g)
                    if tgt and tgt[0]!=n['idx']:
                        links.append(tgt)
                seen=set(); pin['links']=[x for x in links if not (x in seen or seen.add(x))]

    def _pins(self, selfidx, tail):
        p=self.p; pins=[]; starts=[]
        i=0
        while i+48<=len(tail):
            a=struct.unpack_from('<i',tail,i)[0]
            if a==selfidx:
                g1=tail[i+4:i+20]
                b=struct.unpack_from('<i',tail,i+20)[0]
                g2=tail[i+24:i+40]
                if b==selfidx and g1==g2:
                    ni=struct.unpack_from('<i',tail,i+40)[0]
                    nn=struct.unpack_from('<i',tail,i+44)[0]
                    if 0<=ni<p.nameCount:
                        starts.append((i,g1,p.name(ni,nn)))
                        i+=48; continue
            i+=1
        for k,(off,g,nm) in enumerate(starts):
            nxt = starts[k+1][0] if k+1<len(starts) else len(tail)
            region=tail[off+48:nxt]
            refs=[]
            j=0
            while j+20<=len(region):
                v=struct.unpack_from('<i',region,j)[0]
                if 0<v<=p.exportCount:
                    refs.append((v,region[j+4:j+20]))
                j+=1
            pins.append(dict(name=nm,guid=g,off=off,refs=refs))
        return pins

    def label(self, n):
        p=self.p; pr=n['props']; cls=n['cls']
        def member(key):
            if key in pr:
                sp=struct_props(p,pr[key])
                d={x['name']:prop_val(p,x) for x in sp}
                mn=d.get('MemberName'); par=d.get('MemberParent')
                if par and isinstance(par,str): par=par.split('.')[-1]
                return mn, par, d.get('bSelfContext')
            return None,None,None
        if 'CallFunction' in cls or 'CallArrayFunction' in cls or 'CallParentFunction' in cls:
            mn,par,slf=member('FunctionReference')
            return f"Call {mn}" + (f"  [on {par}]" if par and not slf else "")
        if 'VariableGet' in cls:
            mn,par,slf=member('VariableReference'); return f"GET {mn}" + (f" [{par}]" if par and not slf else "")
        if 'VariableSet' in cls:
            mn,par,slf=member('VariableReference'); return f"SET {mn}" + (f" [{par}]" if par and not slf else "")
        if 'CustomEvent' in cls:
            v=prop_val(p,pr['CustomFunctionName']) if 'CustomFunctionName' in pr else '?'
            return f"CustomEvent {v}"
        if cls=='K2Node_Event':
            mn,par,slf=member('EventReference'); return f"Event {mn}"
        if 'FunctionEntry' in cls:
            mn,par,slf=member('FunctionReference'); return f"ENTRY {mn or n['outer'].split('.')[-1]}"
        if 'FunctionResult' in cls: return "RETURN"
        if 'Comment' in cls:
            v=prop_val(p,pr['NodeComment']) if 'NodeComment' in pr else ''
            return f"/* COMMENT: {v} */"
        if 'MacroInstance' in cls:
            if 'MacroGraphReference' in pr:
                d={x['name']:prop_val(p,x) for x in struct_props(p,pr['MacroGraphReference'])}
                return f"Macro {d.get('MacroGraph','?')}"
            return "Macro ?"
        if 'DynamicCast' in cls:
            t=prop_val(p,pr['TargetType']) if 'TargetType' in pr else '?'
            return f"CastTo {t.split('.')[-1] if isinstance(t,str) else t}"
        if 'CreateWidget' in cls or 'ConstructObject' in cls:
            return f"{cls.replace('K2Node_','')}"
        return cls.replace('K2Node_','')

    def dump(self, graph_filter=None, show_links=True):
        out=[]
        graphs={}
        for n in self.nodes.values():
            graphs.setdefault(n['outer'],[]).append(n)
        for g,ns in graphs.items():
            if graph_filter and graph_filter.lower() not in g.lower(): continue
            out.append(f"\n===== GRAPH: {g} =====")
            def pos(n):
                px=prop_val(self.p,n['props']['NodePosX']) if 'NodePosX' in n['props'] else 0
                py=prop_val(self.p,n['props']['NodePosY']) if 'NodePosY' in n['props'] else 0
                return (px or 0,py or 0)
            for n in sorted(ns,key=lambda n:(pos(n)[0],pos(n)[1])):
                st=''
                if 'EnabledState' in n['props']:
                    v=prop_val(self.p,n['props']['EnabledState'])
                    if v and 'Disabled' in str(v): st=' (DISABLED)'
                x,y=pos(n)
                out.append(f"[{n['idx']:>3}] ({x:>6},{y:>6}) {self.label(n)}{st}")
                if show_links:
                    for pin in n['pins']:
                        if not pin['links']: continue
                        tgts=', '.join(f"{self.label(self.nodes[t])}#{t}.{pn}" for t,pn in pin['links'] if t in self.nodes)
                        if tgts: out.append(f"          .{pin['name']} -> {tgts}")
        return '\n'.join(out)

if __name__=='__main__':
    b=BP(sys.argv[1])
    print(b.dump(sys.argv[2] if len(sys.argv)>2 else None))
