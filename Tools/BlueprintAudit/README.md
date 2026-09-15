# Blueprint Audit

Offline reader for uncooked UE 5.3 `.uasset` Blueprint packages. No engine install required.

`uasset.py`  - package header / name table / import + export tables / tagged properties.
`bpdump.py`  - reconstructs Blueprint graphs: nodes, node kinds, function & variable
               references, node positions, and pin-level connections.

Usage:

    python3 Tools/BlueprintAudit/bpdump.py <path/to/Asset.uasset> [graph name filter]

Example:

    python3 Tools/BlueprintAudit/bpdump.py Content/UI/CardUI/wCardZone.uasset OnDragOver

Handy one-liner to find variables that are read but never written:

    python3 - <<'PY'
    import sys; sys.path.insert(0,'Tools/BlueprintAudit')
    from bpdump import *
    b=BP('Content/UI/CardUI/wCardZone.uasset')
    sets,gets={},{}
    for n in b.nodes.values():
        vr=n['props'].get('VariableReference')
        if not vr or 'Variable' not in n['cls']: continue
        d={x['name']:prop_val(b.p,x) for x in struct_props(b.p,vr)}
        if not d.get('bSelfContext'): continue
        (sets if 'VariableSet' in n['cls'] else gets).setdefault(d['MemberName'],[]).append(n['outer'])
    for v in gets:
        if v not in sets: print('READ BUT NEVER WRITTEN:', v)
    PY

Caveat: pin *records* inside a node are not 4-byte aligned (they contain
variable-length strings), so the pin scanner walks the payload byte by byte and
accepts a pin only on the full 48-byte signature
`{selfExportIndex, PinGuid, selfExportIndex, same PinGuid, FName}`. Links are
resolved only against guids that are known pins, which keeps false positives out.
