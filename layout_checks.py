"""Independent analytic checks on draft segments (not KiCad DRC)."""
import math

from .layout_model import bounds, placed_pads
from .layout_search import intersects


def point_distance(*, p, a, b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)


def segment_distance(*, a, b, c, d):
    def cross(p,q,r): return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    ab_c,ab_d,cd_a,cd_b=cross(a,b,c),cross(a,b,d),cross(c,d,a),cross(c,d,b)
    if ab_c*ab_d<=0 and cd_a*cd_b<=0:
        if max(min(a[0],b[0]),min(c[0],d[0]))<=min(max(a[0],b[0]),max(c[0],d[0])) and max(min(a[1],b[1]),min(c[1],d[1]))<=min(max(a[1],b[1]),max(c[1],d[1])):
            return 0.0
    return min(point_distance(p=a,a=c,b=d),point_distance(p=b,a=c,b=d),
               point_distance(p=c,a=a,b=b),point_distance(p=d,a=a,b=b))


def segment_box(*, a, b, xy, size, rotation, margin):
    angle=math.radians(rotation); c,s=math.cos(angle),math.sin(angle)
    def local(p):
        x,y=p[0]-xy[0],p[1]-xy[1]
        return [x*c-y*s,x*s+y*c]
    p,q=local(a),local(b); lo,hi=0.,1.
    for axis in (0,1):
        half=size[axis]/2+margin; delta=q[axis]-p[axis]
        if abs(delta)<1e-12:
            if abs(p[axis])>half: return False
        else:
            left,right=sorted(((-half-p[axis])/delta,(half-p[axis])/delta))
            lo=max(lo,left); hi=min(hi,right)
            if lo>hi: return False
    return True


def check_draft(*, model, placements, routing, settings):
    pads=placed_pads(model=model,placements=placements); findings=[]; segments=[]; vias=[]
    for route in routing['routes']:
        for endpoint,point,layer in ((route['start'],route['points_mm'][0],route['segment_layers'][0]),
                                     (route['end'],route['points_mm'][-1],route['segment_layers'][-1])):
            choices=[p for p in pads if p['reference']==endpoint['reference'] and p['number']==endpoint['pin_number']
                     and math.dist(p['xy'],[point['x'],point['y']])<1e-7 and p['net']==route['net'] and layer in p['layers']]
            if 'pad_selector' in endpoint:
                selector=endpoint['pad_selector']
                choices=[p for p in choices if p['shape']==selector['shape'] and p['pad_type']==selector['pad_type']
                         and p['size']==[selector['size_mm']['x'],selector['size_mm']['y']]]
            if len(choices)!=1: findings.append({'kind':'endpoint_mismatch','route':route['name'],'endpoint':endpoint})
        for first,last,layer in zip(route['points_mm'],route['points_mm'][1:],route['segment_layers']):
            a,b=[first['x'],first['y']],[last['x'],last['y']]
            dx,dy=abs(a[0]-b[0]),abs(a[1]-b[1])
            if min(dx,dy)>1e-6 and abs(dx-dy)>1e-6:
                findings.append({'kind':'non_octilinear_segment','route':route['name'],'start':a,'end':b})
            segments.append((a,b,layer,route['net'],route['width_mm'],route['name']))
            margin=route['width_mm']/2+settings['clearance_mm']
            for pad in pads:
                if pad['net']==route['net'] or layer not in pad['layers']: continue
                if segment_box(a=a,b=b,xy=pad['xy'],size=pad['size'],rotation=pad['rotation'],margin=margin-1e-8):
                    findings.append({'kind':'segment_pad_clearance','route':route['name'],'pad':f'{pad["reference"]}.{pad["number"]}'})
        for via in route['vias']: vias.append((via,route['net'],route['name']))
    for i,(a,b,layer,net,width,name) in enumerate(segments):
        for c,d,other_layer,other_net,other_width,other_name in segments[:i]:
            if net!=other_net and layer==other_layer and segment_distance(a=a,b=b,c=c,d=d)<(width+other_width)/2+settings['clearance_mm']-1e-8:
                findings.append({'kind':'segment_segment_clearance','routes':[name,other_name]})
    for i,(via,net,name) in enumerate(vias):
        xy=[via['x'],via['y']]
        for pad in pads:
            if pad['net']!=net and pad['layers'] and segment_box(a=xy,b=xy,xy=pad['xy'],size=pad['size'],rotation=pad['rotation'],margin=via['diameter_mm']/2+settings['clearance_mm']-1e-8):
                findings.append({'kind':'via_pad_clearance','route':name,'pad':f'{pad["reference"]}.{pad["number"]}'})
            if pad['drill'] and segment_box(a=xy,b=xy,xy=pad['xy'],size=pad['drill'],rotation=pad['rotation'],margin=via['drill_mm']/2+settings['drill_gap_mm']-1e-8):
                findings.append({'kind':'via_pad_drill_clearance','route':name})
        for a,b,layer,other_net,width,other_name in segments:
            if net!=other_net and point_distance(p=xy,a=a,b=b)<(via['diameter_mm']+width)/2+settings['clearance_mm']-1e-8:
                findings.append({'kind':'via_segment_clearance','routes':[name,other_name]})
        for other,other_net,other_name in vias[:i]:
            limit=(via['drill_mm']+other['drill_mm'])/2+settings['drill_gap_mm']
            if net!=other_net: limit=max(limit,(via['diameter_mm']+other['diameter_mm'])/2+settings['clearance_mm'])
            if math.dist(xy,[other['x'],other['y']])<limit-1e-8:
                findings.append({'kind':'via_via_clearance','routes':[name,other_name]})
    overlaps=[]
    components=model['components']
    for i,component in enumerate(components):
        a=bounds(component=component,placement=placements[component['reference']])
        for other in components[:i]:
            b=bounds(component=other,placement=placements[other['reference']])
            if intersects(a=a,b=b): overlaps.append([component['reference'],other['reference']])
    return {'scope':'Offline conservative pad-box/segment/via math only; NOT native DRC',
            'copper_findings':findings,'courtyard_envelope_overlaps':overlaps,
            'passed':not findings and not overlaps}
