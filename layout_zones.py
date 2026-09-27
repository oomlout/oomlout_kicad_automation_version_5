"""Unfilled ground-zone intent; never equate polygon coverage with connectivity."""
from .layout_model import bounds


def subtract_box(*, rectangle, exclusion):
    a,b,c,d=rectangle; x0,x1,y0,y1=exclusion
    x0,x1,y0,y1=max(a,x0),min(b,x1),max(c,y0),min(d,y1)
    if x0>=x1 or y0>=y1: return [rectangle]
    return [r for r in ([a,x0,c,d],[x1,b,c,d],[x0,x1,c,y0],[x0,x1,y1,d])
            if r[1]-r[0]>1e-8 and r[3]-r[2]>1e-8]


def propose_pours(*, model, placements, settings):
    """Tile the allowed region without holes unsupported by add_zone's schema.

    Tiles share boundaries but do not certify filled continuity. KiCad may
    remove islands or leave thermal/neck disconnects; these remain native gates.
    Both copper layers are declared so front SMD ground pads can reach copper.
    Through-hole ground pads can join layers; any missing stitching is reported
    by the native connectivity audit, never replaced by pairwise GND airwires.
    """
    result=[]; edge=settings['edge_margin_mm']
    for net in settings.get('ground_pour_nets',[]):
        rectangles=[[edge,model['board']['width_mm']-edge,edge,model['board']['height_mm']-edge]]
        exclusions=[k['bounds_mm'] for k in settings['keepouts'] if net not in k.get('allowed_nets',[])]
        for c in model['components']:
            if c['mechanical_only']:
                a,b,d,e=bounds(component=c,placement=placements[c['reference']]); gap=settings['component_gap_mm']
                exclusions.append([a-gap,b+gap,d-gap,e+gap])
        for exclusion in exclusions:
            rectangles=[p for r in rectangles for p in subtract_box(rectangle=r,exclusion=exclusion)]
        for layer in ('F.Cu','B.Cu'):
            for index,(a,b,c,d) in enumerate(sorted(rectangles)):
                if min(b-a,d-c)<settings['track_width_mm']: continue
                result.append({'name':f'layout_{net}_{layer[0]}_{index:02d}','net':net,'layer':layer,
                    'points_mm':[{'x':a,'y':c},{'x':b,'y':c},{'x':b,'y':d},{'x':a,'y':d}],
                    'clearance_mm':settings['clearance_mm'],'min_width_mm':settings['track_width_mm'],
                    'pad_connection':'thermal','priority':0,
                    'reason':'UNACCEPTED ground pour; native refill, saved-zone verification and electrical audit required'})
    return result
