"""Reserve legal fine-pitch exit corridors before greedy long-distance routing.

Reservations are routing constraints, not fabricated copper or proven ground
connections. Every emitted route must still pass independent geometry checks.
"""
import math
from .layout_model import placed_pads


def reserve_fanouts(*, model, placements, settings):
    from .layout_search import RouteGrid
    from .layout_escape_solver import select_escapes
    if not settings.get('reserve_dense_fanouts',True): return [],[]
    pads=placed_pads(model=model,placements=placements)
    dense={c['reference'] for c in model['components']
           if len({p['number'] for p in c['pads'] if p['number'] and p['layers']})>=8}
    selected=[p for p in pads if p['reference'] in dense and p['net'] and
              p['pad_type']=='smd' and p['layers'] and min(p['size'])<=.6]
    choices={}; pin_records={}; pending=[]
    for pad in sorted(selected,key=lambda p:(p['reference'],p['index'])):
        center=placements[pad['reference']]['anchor_mm']
        angle=math.radians(-pad['rotation']); axis=[math.cos(angle),math.sin(angle)]
        if pad['size'][1]>pad['size'][0]: axis=[-math.sin(angle),math.cos(angle)]
        outward=[pad['xy'][i]-center[i] for i in (0,1)]
        if sum(axis[i]*outward[i] for i in (0,1))<0: axis=[-v for v in axis]
        grid=RouteGrid(model=model,pads=pads,net=pad['net'],settings=settings,routes=[])
        candidates=[]
        for (layer,_,_),escape in grid.escape_nodes(pad=pad).items():
            delta=[escape['points'][-1][i]-pad['xy'][i] for i in (0,1)]
            reach=sum(delta[i]*axis[i] for i in (0,1))
            lateral=abs(delta[0]*axis[1]-delta[1]*axis[0])
            if reach<settings.get('reserve_escape_min_mm',.6)-1e-6 or lateral>.15: continue
            shortfall=max(0,settings.get('reserve_escape_length_mm',1.2)-reach)
            candidates.append((shortfall*10+escape['length_mm']+lateral*10,layer,escape['points']))
        if not candidates:
            pending.append({'reference':pad['reference'],'pin_number':pad['number'],'pad_index':pad['index'],
                            'net':pad['net'],'reason':'No collision-free outward reservation at configured length'})
            continue
        key=f'escape_{pad["reference"]}_{pad["index"]}'
        pin_records[key]={'reference':pad['reference'],'pin_number':pad['number'],'pad_index':pad['index'],'net':pad['net']}
        choices[key]=[{'name':key,'net':pad['net'],'width_mm':settings['track_width_mm'],
            'points_mm':[dict(zip(('x','y'),p)) for p in points],
            'segment_layers':[['F.Cu','B.Cu'][layer]]*(len(points)-1),'vias':[],
            'scope':'Jointly reserved access only; NOT copper or ground connectivity proof'}
            for _,layer,points in sorted(candidates)[:settings.get('fanout_candidates_per_pin',6)]]
    result=select_escapes(choices=choices,ground_nets=settings.get('ground_pour_nets',[]),
                         clearance_mm=settings['clearance_mm'],node_budget=settings.get('fanout_search_nodes',5000))
    for key in sorted(set(choices)-set(result['selected'])):
        pending.append({**pin_records[key],'reason':'No jointly selected escape within finite candidate/node bounds',
                        'joint_search':result['search']})
    return list(result['selected'].values()),pending
