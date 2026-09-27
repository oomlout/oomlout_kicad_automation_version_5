"""Explain occupied exits and repair only their competing draft net groups."""
import copy
import math
from .layout_escape_solver import conflicting


def escape_blockers(*, pad, baseline_grid, routes, reservations):
    options=[]
    for (layer,_,_),escape in baseline_grid.escape_nodes(pad=pad).items():
        path={'net':baseline_grid.net,'width_mm':baseline_grid.width,
              'points_mm':[dict(zip(('x','y'),p)) for p in escape['points']],
              'segment_layers':[['F.Cu','B.Cu'][layer]]*(len(escape['points'])-1),'vias':[]}
        if len(path['points_mm'])==1:
            path['points_mm']*=2; path['segment_layers']=[['F.Cu','B.Cu'][layer]]
        actual=sorted({r['net'] for r in routes if conflicting(left=path,right=r,clearance_mm=baseline_grid.settings['clearance_mm'])})
        reserved=sorted({r['net'] for r in reservations if conflicting(left=path,right=r,clearance_mm=baseline_grid.settings['clearance_mm'])})
        options.append((len(reserved),len(actual),escape['length_mm'],actual,reserved))
    if not options: return {'routes':[],'reservations':[]}
    _,_,_,actual,reserved=min(options)
    return {'routes':actual,'reservations':reserved}


def history_regions(*, failures, previous, settings):
    regions=copy.deepcopy(previous)
    for failure in failures:
        for key,count in zip(('start_mm','end_mm'),failure.get('current_escape_options',[1,1])):
            if count or key not in failure: continue
            xy=failure[key]
            found=next((r for r in regions if r['xy_mm']==xy and r['exempt_net']==failure['net']),None)
            if found: found['cost']+=settings['congestion_history_cost']
            else: regions.append({'xy_mm':xy,'radius_mm':settings['congestion_radius_mm'],
                                  'exempt_net':failure['net'],'cost':settings['congestion_history_cost']})
    return regions


def repair_neighborhoods(*, model, placements, settings, best, units, reservations, history, progress=None):
    from .layout_search import _route_pass
    result=best; records=[]
    tried=set()
    for attempt in range(settings.get('local_repair_rounds',2)):
        choices=[u for u in result['unresolved'] if u.get('blocking_nets') and
                 (u['net'],tuple(u['blocking_nets'])) not in tried]
        if not choices: break
        failure=min(choices,key=lambda u:(len(u['blocking_nets']),u['net'],str(u['start']),str(u['end'])))
        tried.add((failure['net'],tuple(failure['blocking_nets'])))
        involved={failure['net'],*failure['blocking_nets']}
        affected=[unit for unit in units if set(unit)&involved]
        affected.sort(key=lambda unit:(failure['net'] not in unit,units.index(unit)))
        order=[net for unit in affected for net in unit]
        preserved=[r for r in result['routes'] if r['net'] not in order]
        if progress: progress(f'Local repair {attempt+1}: {order}; preserving {len(preserved)} unrelated routes')
        trial=_route_pass(model=model,placements=placements,settings=settings,ordered=order,
                          reservations=reservations,initial_routes=preserved,history=history,progress=progress)
        trial['unresolved']=[u for u in result['unresolved'] if u['net'] not in order]+trial['unresolved']
        def score(r): return (len(r['unresolved']),r['via_count'],r['proposed_track_length_mm'])
        accepted=score(trial)<score(result)
        records.append({'attempt':attempt+1,'nets':order,'preserved_routes':len(preserved),
            'before_unresolved':len(result['unresolved']),'after_unresolved':len(trial['unresolved']),
            'accepted':accepted,'failure_details':trial['unresolved']})
        if accepted: result=trial
    return result,records
