"""Editable placement relationships; no hard-coded MCU/header reference names."""
import math
from .layout_model import placed_pads, bounds


def relationship_metrics(*, model, placements, settings):
    from .layout_checks import segment_box, point_distance
    pads=placed_pads(model=model,placements=placements)
    components={c['reference']:c for c in model['components']}
    records=[]; score=0.
    for rule in settings.get('placement_relationships',[]):
        source=rule['source']
        for target in rule['targets']:
            # Use mean physical signal terminals, not the library anchor at one
            # end of a long header. Ground/power pours do not attract placement.
            left=[p for p in pads if p['reference']==source and p['net'] and p['net'] not in settings['ground_pour_nets']]
            right=[p for p in pads if p['reference']==target and p['net'] and p['net'] not in settings['ground_pour_nets']]
            shared={p['net'] for p in left}&{p['net'] for p in right}
            def center(ref,items):
                selected=[p['xy'] for p in items if p['net'] in shared]
                return [sum(p[i] for p in selected)/len(selected) for i in (0,1)] if selected else placements[ref]['anchor_mm']
            a,b=center(source,left),center(target,right)
            blocked=[]; obstruction=0.
            for ref in rule['avoid_between']:
                x0,x1,y0,y1=bounds(component=components[ref],placement=placements[ref])
                if segment_box(a=a,b=b,xy=[(x0+x1)/2,(y0+y1)/2],size=[x1-x0,y1-y0],rotation=0,margin=rule['corridor_width_mm']/2):
                    blocked.append(ref)
                    radius=math.hypot(x1-x0,y1-y0)/2+rule['corridor_width_mm']/2
                    # A graded cost permits small successive moves to clear a
                    # wide corridor; a binary penalty creates a local plateau.
                    distance_to_axis=point_distance(p=[(x0+x1)/2,(y0+y1)/2],a=a,b=b)
                    obstruction+=1+max(0,1-distance_to_axis/radius)
            distance=math.dist(a,b)
            score+=rule['distance_weight']*distance+rule['obstruction_cost']*obstruction
            records.append({'name':rule['name'],'source':source,'target':target,
                'source_mm':a,'target_mm':b,'distance_mm':round(distance,6),
                'shared_nets':sorted(shared),'obstructing_references':sorted(blocked)})
    return {'cost':score,'relationships':records}


def validate_relationships(*, settings, references=None):
    rules=settings['placement_relationships']
    if not isinstance(rules,list): raise ValueError('placement_relationships must be a list')
    names=set()
    for rule in rules:
        if not isinstance(rule,dict) or set(rule)!={'name','source','targets','avoid_between','distance_weight','obstruction_cost','corridor_width_mm'}:
            raise ValueError('Placement relationship needs explicit source, targets, avoid_between and costs/width')
        if not isinstance(rule['name'],str) or not rule['name'] or rule['name'] in names: raise ValueError('Relationship names must be unique')
        names.add(rule['name'])
        if not isinstance(rule['source'],str) or not rule['source']: raise ValueError('Relationship source must be a reference')
        for key in ('targets','avoid_between'):
            items=rule[key]
            if not isinstance(items,list) or any(not isinstance(r,str) or not r for r in items) or len(set(items))!=len(items):
                raise ValueError('Relationship reference lists must be unique names')
        if not rule['targets'] or rule['source'] in rule['targets'] or set([rule['source'],*rule['targets']])&set(rule['avoid_between']):
            raise ValueError('Relationship endpoints and obstacles must be different')
        for key in ('distance_weight','obstruction_cost','corridor_width_mm'):
            value=rule[key]
            if type(value) not in (float,int) or not math.isfinite(value) or value<=0: raise ValueError('Relationship dimensions/costs must be positive finite')
        if references is not None and set([rule['source'],*rule['targets'],*rule['avoid_between']])-set(references):
            raise ValueError('Unknown placement relationship reference')
