"""Bounded route -> congestion -> placement -> route feedback; best legal wins."""
from .layout_search import optimize, route_all
from .layout_checks import check_draft


def refine_placement(*, model, settings, optimization, routing, progress=None):
    trials=[]; best_placement=optimization; best_routing=routing
    def score(placement,routes):
        obstruction=sum(len(r['obstructing_references']) for r in placement['relationships_after']['relationships'])
        return (obstruction,len(routes['unresolved']),routes['via_count'],routes['proposed_track_length_mm'])
    for index in range(settings.get('placement_feedback_rounds',1)):
        if not best_routing['unresolved']: break
        hotspots=[]
        for failure in best_routing['unresolved']:
            for endpoint,key in (('start','start_mm'),('end','end_mm')):
                record={'reference':failure[endpoint]['reference'],'xy_mm':failure[key]}
                if record not in hotspots: hotspots.append(record)
        if progress: progress(f'Placement feedback {index+1}: {len(hotspots)} failed-endpoint neighborhoods; immutable electrical intent')
        candidate=optimize(model=model,settings=settings,initial_placements=best_placement['after'],routing_feedback=hotspots)
        if candidate['after']==best_placement['after']:
            trials.append({'trial':index+1,'accepted':False,'reason':'No legal improving placement change','hotspots':hotspots}); break
        routed=route_all(model=model,placements=candidate['after'],settings=settings,progress=progress)
        checks=check_draft(model=model,placements=candidate['after'],routing=routed,settings=settings)
        accepted=checks['passed'] and score(candidate,routed)<score(best_placement,best_routing)
        trials.append({'trial':index+1,'accepted':accepted,'hotspots':hotspots,'checks':checks,
            'before_score':score(best_placement,best_routing),'after_score':score(candidate,routed),
            'placement':candidate,'routes':routed['routes'],'unresolved':routed['unresolved']})
        if accepted:
            # Preserve movement provenance from the original placement too.
            candidate['moves']=best_placement['moves']+candidate['moves']
            best_placement,best_routing=candidate,routed
    return best_placement,best_routing,trials
