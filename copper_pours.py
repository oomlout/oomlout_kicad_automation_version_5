"""Explicit rectangular copper zones, created/refilled only through Konnect."""
import copy
import math
from .connectivity import _children
from .routing import ORIGIN_MM, backend_net


def validate_pour(*, command):
    keys={'name','net','layer','points_mm','clearance_mm','min_width_mm','pad_connection','priority','reason'}
    if not isinstance(command,dict) or set(command)!=keys: raise ValueError('Copper pour requires explicit '+str(sorted(keys)))
    for k in ('name','net','reason'):
        if not isinstance(command[k],str) or not command[k].strip(): raise ValueError('Missing pour '+k)
    if command['layer'] not in ('F.Cu','B.Cu'): raise ValueError('Pour requires an explicit two-layer copper side')
    if command['pad_connection'] not in ('solid','thermal','none'): raise ValueError('Unknown pad connection mode')
    if type(command['priority'])!=int or command['priority']<0: raise ValueError('Priority must be a nonnegative integer')
    for k in ('clearance_mm','min_width_mm'):
        if type(command[k]) not in (int,float) or not math.isfinite(command[k]) or command[k]<=0:
            raise ValueError('Pour dimension must be positive finite')
    points=command['points_mm']
    if not isinstance(points,list) or len(points)!=4: raise ValueError('This action currently requires an axis-aligned rectangle')
    for p in points:
        if not isinstance(p,dict) or set(p)!={'x','y'} or not all(type(v) in (int,float) and math.isfinite(v) for v in p.values()):
            raise ValueError('Pour points require finite x/y')
    vertices=[(p['x'],p['y']) for p in points]
    if len(set(vertices))!=4 or len({p[0] for p in vertices})!=2 or len({p[1] for p in vertices})!=2:
        raise ValueError('Nondegenerate rectangle required')
    if any((a[0]!=b[0])==(a[1]!=b[1]) for a,b in zip(vertices,vertices[1:]+vertices[:1])):
        raise ValueError('Crossed/diagonal pour boundary forbidden')
    return copy.deepcopy(command)


def verify_zones(*, root, pours):
    """Read-only semantic intent check, including filled-copper presence.

    Presence does not prove connectivity; the independent native electrical
    audit remains mandatory and reports disconnected islands/thermal spokes.
    """
    zones=_children(root,'zone'); errors=[]; seen=set(); numeric_differences=[]
    expected={p['name']:validate_pour(command=p) for p in pours}
    if len(expected)!=len(pours): errors.append('Duplicate expected pour name')
    def scalar(node,key,default=None):
        values=_children(node,key)
        return values[0][1] if values else default
    for zone in zones:
        name=scalar(zone,'name'); wanted=expected.get(name)
        if wanted is None or name in seen:
            errors.append('Unexpected or duplicate saved zone: '+str(name)); continue
        seen.add(name)
        try:
            net=scalar(zone,'net_name',scalar(zone,'net'))
            layers=_children(zone,'layers')
            actual_layers=layers[0][1:] if layers else [scalar(zone,'layer')]
            if net!=backend_net(name=wanted['net']) or actual_layers!=[wanted['layer']]: raise ValueError('net/layer')
            if int(scalar(zone,'priority',0))!=wanted['priority']: raise ValueError('priority')
            connection=_children(zone,'connect_pads')[0]
            mode=next((v for v in connection[1:] if isinstance(v,str)),'thermal')
            if mode!={'solid':'yes','thermal':'thermal','none':'no'}[wanted['pad_connection']]: raise ValueError('pad connection')
            if abs(float(scalar(connection,'clearance'))-wanted['clearance_mm'])>1e-6: raise ValueError('clearance')
            if abs(float(scalar(zone,'min_thickness'))-wanted['min_width_mm'])>1e-6: raise ValueError('minimum thickness')
            polygons=_children(zone,'polygon')
            if len(polygons)!=1: raise ValueError('polygon count')
            pts=_children(polygons[0],'pts')[0]
            actual=[tuple(round(float(v),6) for v in p[1:]) for p in _children(pts,'xy')]
            if len(actual)>1 and actual[-1]==actual[0]: actual.pop()
            target=[tuple(round(ORIGIN_MM+p[k],6) for k in ('x','y')) for p in wanted['points_mm']]
            equivalent=[r[i:]+r[:i] for r in (target,list(reversed(target))) for i in range(len(target))]
            matches=[r for r in equivalent if len(actual)==len(r) and all(
                abs(round(a*1_000_000)-round(b*1_000_000))<=1
                for p,q in zip(actual,r) for a,b in zip(p,q))]
            if not matches: raise ValueError('boundary geometry')
            deltas=[[round(a*1_000_000)-round(b*1_000_000) for a,b in zip(p,q)] for p,q in zip(actual,matches[0])]
            if any(v for pair in deltas for v in pair): numeric_differences.append({'name':name,'vertex_delta_nm':deltas})
            if _children(zone,'keepout'): raise ValueError('unexpected rule area')
            fills=_children(zone,'filled_polygon')
            if not fills: raise ValueError('unfilled zone (no native filled_polygon)')
            if any(scalar(f,'layer')!=wanted['layer'] for f in fills): raise ValueError('fill layer')
            if any(not _children(f,'pts') or len(_children(_children(f,'pts')[0],'xy'))<3 for f in fills):
                raise ValueError('empty/malformed native fill polygon')
        except (IndexError,TypeError,ValueError) as exc:
            errors.append(f'Saved zone {name} differs: {exc}')
    if set(expected)!=seen: errors.append('Missing declared zones: '+str(sorted(set(expected)-seen)))
    return {'status':'failed' if errors else 'ready','zone_count':len(zones),'diagnostics':errors,
            'numeric_tolerance_nm':1,'boundary_quantization_differences':numeric_differences,
            'scope':'Declared geometry and native fill presence only; not electrical connectivity acceptance'}
