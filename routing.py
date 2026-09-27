"""Reviewed explicit route intent. No autorouter, AI or live queries during compile."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from .connectivity import _parse_sexpression, _children

ORIGIN_MM = 100.0


def validate_route(*, command: dict) -> dict:
    required = {'name','net','layer','width_mm','start','end','points_mm','reason'}
    optional = {'segment_layers', 'vias', 'angle_policy'}
    if not required <= set(command) or set(command) - required - optional:
        raise ValueError(f'Route requires {sorted(required)}; optional {sorted(optional)}')
    route = copy.deepcopy(command)
    for key in ('name','net','reason'):
        if not isinstance(route[key],str) or not route[key].strip(): raise ValueError(f'Missing route {key}')
    if route['layer'] not in ('F.Cu','B.Cu'): raise ValueError('Prototype routes require F.Cu or B.Cu')
    if type(route['width_mm']) not in (int,float) or not math.isfinite(route['width_mm']) or route['width_mm'] <= 0:
        raise ValueError('Route width must be positive finite mm')
    for key in ('start','end'):
        endpoint = route[key]
        if not isinstance(endpoint,dict) or not {'reference','pin_number'} <= set(endpoint) or set(endpoint)-{'reference','pin_number','pad_selector'}:
            raise ValueError('Endpoint requires reference and pin_number; optional explicit pad_selector')
        if not all(isinstance(endpoint[k],str) and endpoint[k] for k in ('reference','pin_number')): raise ValueError('Endpoint fields must be nonempty strings')
        if 'pad_selector' in endpoint:
            selector=endpoint['pad_selector']
            if not isinstance(selector,dict) or set(selector)!={'size_mm','shape','pad_type'}:
                raise ValueError('Pad selector requires size_mm, shape and pad_type; position comes from route endpoint')
            size=selector['size_mm']
            if not isinstance(size,dict) or set(size)!={'x','y'} or not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in size.values()):
                raise ValueError('Pad selector size must contain positive finite x/y')
            if not all(isinstance(selector[k],str) and selector[k] for k in ('shape','pad_type')):
                raise ValueError('Pad selector shape/type must be explicit strings')
    same_logical_pin=all(route['start'][k]==route['end'][k] for k in ('reference','pin_number'))
    if same_logical_pin and not all('pad_selector' in route[k] for k in ('start','end')):
        raise ValueError('Same-number physical lands require explicit selectors at both ends')
    points=route['points_mm']
    if not isinstance(points,list) or len(points)<2: raise ValueError('Route needs at least two points')
    for point in points:
        if not isinstance(point,dict) or set(point) != {'x','y'} or not all(type(v) in (int,float) and math.isfinite(v) for v in point.values()):
            raise ValueError('Route points require finite x/y mm')
    if any(a==b for a,b in zip(points,points[1:])): raise ValueError('Zero length route segment')
    if 'angle_policy' in route:
        from .route_geometry import is_octilinear
        if route['angle_policy']!='orthogonal_45': raise ValueError('Unknown route angle policy')
        if any(not is_octilinear(a=[a['x'],a['y']],b=[b['x'],b['y']]) for a,b in zip(points,points[1:])):
            raise ValueError('Route requires only horizontal, vertical or 45-degree segments, including escapes')
    if same_logical_pin and points[0]==points[-1]: raise ValueError('Same-number route must join distinct physical locations')
    layers = route.get('segment_layers', [route['layer']] * (len(points)-1))
    if not isinstance(layers,list) or len(layers)!=len(points)-1 or any(layer not in ('F.Cu','B.Cu') for layer in layers):
        raise ValueError('Each segment needs one supported copper layer')
    if layers[0] != route['layer']: raise ValueError('Initial segment must match route layer')
    vias = route.get('vias', [])
    if not isinstance(vias,list): raise ValueError('Vias must be an explicit list')
    for via in vias:
        if not isinstance(via,dict) or set(via)!={'x','y','diameter_mm','drill_mm'}:
            raise ValueError('Via requires x/y and explicit diameter_mm/drill_mm')
        if not all(type(v) in (int,float) and math.isfinite(v) for v in via.values()) or not 0 < via['drill_mm'] < via['diameter_mm']:
            raise ValueError('Via dimensions must be finite, positive, with copper diameter greater than drill')
    transitions = sorted((points[i]['x'],points[i]['y']) for i in range(1,len(layers)) if layers[i-1]!=layers[i])
    declared = [(v['x'],v['y']) for v in vias]
    if len(set(declared)) != len(declared) or sorted(declared)!=transitions:
        raise ValueError('Every layer transition requires exactly one via; extra/duplicate vias forbidden')
    return route


def backend_net(*, name: str) -> str:
    return name if name in {'GND','+3V3','+5V'} or name.startswith('/') else '/'+name


def validate_live_route(*, route: dict, start: dict, end: dict) -> None:
    """Refuse stale coordinates, wrong net, ambiguous pads and wrong-side routes."""
    layers=route.get('segment_layers',[route['layer']]*(len(route['points_mm'])-1))
    for endpoint, point, response, layer in ((route['start'],route['points_mm'][0],start,layers[0]),
                                           (route['end'],route['points_mm'][-1],end,layers[-1])):
        if response.get('source') != 'ipc' or response.get('reference') != endpoint['reference']:
            raise ValueError('Route requires exact live pad readback')
        pads=[p for p in response.get('pads',[]) if str(p['number']) == endpoint['pin_number']]
        if 'pad_selector' in endpoint:
            selector=endpoint['pad_selector']
            pads=[p for p in pads if p.get('shape')==selector['shape'] and p.get('pad_type')==selector['pad_type']
                  and all(abs(p.get('size',{}).get(axis,float('inf'))-selector['size_mm'][axis])<=0.000002
                          and abs(p.get(axis,float('inf'))-(ORIGIN_MM+point[axis]))<=0.000002 for axis in ('x','y'))]
        if len(pads)!=1: raise ValueError('Route endpoint pad is missing or ambiguous')
        pad=pads[0]
        if pad.get('net') != backend_net(name=route['net']): raise ValueError('Route endpoint net mismatch')
        if layer not in pad.get('layers',[]): raise ValueError('Route endpoint is not on selected copper layer')
        if any(abs(pad[axis]-(ORIGIN_MM+point[axis])) > 0.000002 for axis in ('x','y')):
            raise ValueError('Route endpoint moved; re-query MCP and revise Python explicitly')


SAVED_COPPER_TOLERANCE_NM = 1


def _match_saved_copper(*, expected: list, actual: list, kind: str) -> dict:
    """One-to-one semantic match at KiCad's one-internal-unit precision.

    Keys are exact; only explicit numeric fields may differ by one nanometre.
    A bipartite assignment preserves duplicate multiplicity and avoids greedy
    nearest-neighbour failures when two legal candidates overlap the bound.
    """
    def nm(value):
        return int(round(value * 1_000_000))
    def fields(item, reverse=False):
        if kind == 'segment':
            points=item[3][::-1] if reverse else item[3]
            return dict(zip(('width','start_x','start_y','end_x','end_y'),
                            (nm(item[2]),*(nm(v) for point in points for v in point))))
        return dict(zip(('x','y','diameter','drill'),(nm(item[1][0]),nm(item[1][1]),nm(item[2]),nm(item[3]))))
    def key(item):
        return (item[0],item[1]) if kind=='segment' else (item[0],item[4])
    candidates=[]
    differences={}
    for i,wanted in enumerate(expected):
        choices=[]
        wanted_fields=fields(wanted)
        for j,saved in enumerate(actual):
            if key(wanted)!=key(saved): continue
            options=[]
            for reverse in (False,True) if kind=='segment' else (False,):
                saved_fields=fields(saved,reverse)
                deltas={field:saved_fields[field]-value for field,value in wanted_fields.items()}
                if all(abs(delta)<=SAVED_COPPER_TOLERANCE_NM for delta in deltas.values()):
                    options.append((sum(abs(delta) for delta in deltas.values()),reverse,saved_fields,deltas))
            if options:
                cost,reverse,saved_fields,deltas=min(options,key=lambda option:(option[0],option[1]))
                differences[i,j]={'expected_index':i,'actual_index':j,'net':wanted[0],
                    'layers':[wanted[1]] if kind=='segment' else list(wanted[4]),
                    'reversed_endpoints':reverse,
                    'fields':{field:{'expected_nm':wanted_fields[field],'actual_nm':saved_fields[field],'delta_nm':delta}
                              for field,delta in deltas.items() if delta}}
                choices.append((cost,j))
        candidates.append([j for _,j in sorted(choices)])
    matched={}
    def assign(i,seen):
        for j in candidates[i]:
            if j in seen: continue
            seen.add(j)
            if j not in matched or assign(matched[j],seen):
                matched[j]=i
                return True
        return False
    for i in range(len(expected)): assign(i,set())
    changed=[differences[i,j] for j,i in sorted(matched.items(),key=lambda pair:pair[1]) if differences[i,j]['fields']]
    numeric_fields=('width','start_x','start_y','end_x','end_y') if kind=='segment' else ('x','y','diameter','drill')
    maxima={field:max((abs(item['fields'].get(field,{}).get('delta_nm',0)) for item in changed),default=0)
            for field in numeric_fields}
    return {'status':'ready' if len(expected)==len(actual)==len(matched) else 'failed',
            'expected_count':len(expected),'actual_count':len(actual),'matched_count':len(matched),
            'exact_match_count':len(matched)-len(changed),
            'tolerance_nm':SAVED_COPPER_TOLERANCE_NM,'quantization_differences':changed,
            'max_abs_delta_nm':max(maxima.values(),default=0),'max_abs_delta_nm_by_field':maxima}


def verify_saved_routes(*, bundle_root: Path, routes: list[dict], pours=None) -> dict:
    expected=[]
    expected_vias=[]
    for route in routes:
        layers=route.get('segment_layers',[route['layer']]*(len(route['points_mm'])-1))
        for a,b,layer in zip(route['points_mm'],route['points_mm'][1:],layers):
            expected.append((backend_net(name=route['net']),layer,round(route['width_mm'],6),
                             sorted([tuple(round(ORIGIN_MM+p[k],6) for k in ('x','y')) for p in (a,b)])))
        for via in route.get('vias',[]):
            expected_vias.append((backend_net(name=route['net']),tuple(round(ORIGIN_MM+via[k],6) for k in ('x','y')),
                                  round(via['diameter_mm'],6),round(via['drill_mm'],6),('B.Cu','F.Cu')))
    errors=[]
    raw=(bundle_root/'working.kicad_pcb').read_bytes()
    root=_parse_sexpression(raw.decode('utf-8'))
    actual=[]
    for seg in _children(root,'segment'):
        actual.append((_children(seg,'net')[0][-1],_children(seg,'layer')[0][1],
                       round(float(_children(seg,'width')[0][1]),6),
                       sorted([tuple(round(float(v),6) for v in _children(seg,k)[0][1:]) for k in ('start','end')])))
    segment_matching=_match_saved_copper(expected=expected,actual=actual,kind='segment')
    if segment_matching['status']!='ready': errors.append('Saved tracks differ from frozen geometry/net/width/layer intent')
    actual_vias=[]
    for via in _children(root,'via'):
        try:
            actual_vias.append((_children(via,'net')[0][-1],tuple(round(float(v),6) for v in _children(via,'at')[0][1:]),
                                round(float(_children(via,'size')[0][1]),6),round(float(_children(via,'drill')[0][1]),6),
                                tuple(sorted(_children(via,'layers')[0][1:]))))
        except (IndexError,TypeError,ValueError):
            errors.append('Unrecognized saved via geometry')
    via_matching=_match_saved_copper(expected=expected_vias,actual=actual_vias,kind='via')
    if via_matching['status']!='ready': errors.append('Saved vias differ from frozen geometry/net/dimensions/layers intent')
    from .copper_pours import verify_zones
    zone_matching=verify_zones(root=root,pours=pours or [])
    errors.extend(zone_matching['diagnostics'])
    if _children(root,'arc'): errors.append('Unexpected copper not declared by route_path')
    result={'status':'failed' if errors else 'ready','route_names':[r['name'] for r in routes],
            'selected_nets':sorted({backend_net(name=r['net']) for r in [*routes,*(pours or [])]}),
            'segment_count':len(actual),'via_count':len(actual_vias),'pcb_sha256':hashlib.sha256(raw).hexdigest(),'diagnostics':errors,
            'numeric_tolerance_nm':SAVED_COPPER_TOLERANCE_NM,
            'segment_matching':segment_matching,'via_matching':via_matching,'zone_matching':zone_matching}
    (bundle_root/'reports/routing_verification.json').write_text(json.dumps(result,indent=2))
    return result
