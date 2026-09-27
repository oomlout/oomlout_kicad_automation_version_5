"""Bounded joint escape selection on finite legal candidates; no external solver.

Deterministic branch-and-bound with most-constrained-first ordering, forward
filtering and an explicit node budget. Infeasibility is only for these candidates,
never proof that the physical PCB cannot route. Ground access is prioritized.
"""
from .layout_checks import segment_distance, point_distance


def conflicting(*, left, right, clearance_mm):
    if left['net']==right['net']: return False
    limit=(left['width_mm']+right['width_mm'])/2+clearance_mm
    for a,b,l in zip(left['points_mm'],left['points_mm'][1:],left['segment_layers']):
        for via in right.get('vias',[]):
            if point_distance(p=[via['x'],via['y']],a=[a['x'],a['y']],b=[b['x'],b['y']])<(left['width_mm']+via['diameter_mm'])/2+clearance_mm-1e-9:
                return True
        for c,d,r in zip(right['points_mm'],right['points_mm'][1:],right['segment_layers']):
            if l==r and segment_distance(a=[a['x'],a['y']],b=[b['x'],b['y']],
                                         c=[c['x'],c['y']],d=[d['x'],d['y']])<limit-1e-9:
                return True
    for via in left.get('vias',[]):
        for a,b in zip(right['points_mm'],right['points_mm'][1:]):
            if point_distance(p=[via['x'],via['y']],a=[a['x'],a['y']],b=[b['x'],b['y']])<(right['width_mm']+via['diameter_mm'])/2+clearance_mm-1e-9:
                return True
    return False


def select_escapes(*, choices, ground_nets, clearance_mm, node_budget):
    order=sorted(choices,key=lambda key:(not any(c['net'] in ground_nets for c in choices[key]),len(choices[key]),key))
    nodes=0; best={}; best_key=(-1,-1); exhausted=False; cache={}
    def clash(k,i,j,v):
        identity=(k,i,j,v)
        if identity not in cache:
            cache[identity]=conflicting(left=choices[k][i],right=choices[j][v],clearance_mm=clearance_mm)
        return cache[identity]
    def visit(index,selected,ground_count):
        nonlocal nodes,best,best_key,exhausted
        if nodes>=node_budget: exhausted=True; return
        nodes+=1
        key=(ground_count,len(selected))
        if key>best_key: best=selected.copy(); best_key=key
        if len(best)==len(order) or index==len(order): return
        remaining=order[index:]
        max_ground=ground_count+sum(any(c['net'] in ground_nets for c in choices[k]) for k in remaining)
        if (max_ground,len(selected)+len(remaining))<=best_key: return
        k=order[index]
        for i,candidate in enumerate(choices[k]):
            if any(clash(k,i,j,v) for j,v in selected.items()): continue
            selected[k]=i
            visit(index+1,selected,ground_count+int(candidate['net'] in ground_nets))
            selected.pop(k)
            if len(best)==len(order) or exhausted: return
        visit(index+1,selected,ground_count)
    visit(0,{},0)
    return {'selected':{k:choices[k][v] for k,v in sorted(best.items())},
            'search':{'method':'bounded_joint_fanout_branch_and_bound','nodes':nodes,'node_budget':node_budget,
                      'budget_exhausted':exhausted,'candidate_pins':len(order),
                      'selected_pins':len(best),'selected_ground_pins':best_key[0],
                      'scope':'Finite candidate reservations, not copper or global routability proof'}}
