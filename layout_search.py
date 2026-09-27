"""Bounded deterministic placement and two-layer route *proposals*, offline.

No global-optimum claim and no KiCad/DRC acceptance. Conservative pad boxes
and raster guards deliberately prefer an unresolved connection over a short.
"""
from __future__ import annotations
import copy
import heapq
import math
import hashlib

import numpy as np

from .layout_model import bounds, placed_pads, spanning_edges
from .routing import validate_route
from .route_geometry import octilinear_fanouts


def intersects(*, a, b, gap=0):
    return a[0] < b[1]+gap and b[0] < a[1]+gap and a[2] < b[3]+gap and b[2] < a[3]+gap


def net_groups(*, pads):
    result={}
    for pad in pads:
        if pad['net'] and pad['layers']:
            result.setdefault(pad['net'],[]).append(pad)
    return result


def metrics(*, model, placements):
    groups=net_groups(pads=placed_pads(model=model,placements=placements))
    length=sum(math.dist(a['xy'],b['xy']) for pads in groups.values() for a,b in spanning_edges(pads=pads))
    blocks={}
    for component in model['components']:
        if not component['mechanical_only']:
            blocks.setdefault(component['block'],[]).append(bounds(component=component,placement=placements[component['reference']]))
    area=sum((max(b[1] for b in boxes)-min(b[0] for b in boxes)) *
             (max(b[3] for b in boxes)-min(b[2] for b in boxes)) for boxes in blocks.values())
    return {'airwire_mst_length_mm':round(length,6),'sum_block_envelope_area_mm2':round(area,6),
            'board_area_mm2':model['board']['width_mm']*model['board']['height_mm']}


def optimize(*, model, settings, initial_placements=None, routing_feedback=()):
    from .layout_relationships import relationship_metrics, validate_relationships
    initial={c['reference']:copy.deepcopy(c['pcb']) for c in model['components']}
    current=copy.deepcopy(initial); by_ref={c['reference']:c for c in model['components']}
    if initial_placements is not None:
        if set(initial_placements)!=set(initial): raise ValueError('Feedback placement membership changed')
        current=copy.deepcopy(initial_placements)
    validate_relationships(settings=settings,references=by_ref)
    relationships_before=relationship_metrics(model=model,placements=initial,settings=settings)
    fixed=set(settings['fixed_references'])|{c['reference'] for c in model['components'] if c['mechanical_only']}
    unknown=fixed-set(by_ref)
    if unknown: raise ValueError(f'Unknown fixed references: {sorted(unknown)}')
    for group in settings.get('placement_groups',[]):
        if set(group)-set(by_ref) or set(group)&fixed: raise ValueError('Placement groups must contain known movable references only')
    for pair in settings.get('proximity_limits',[]):
        if set(pair['references'])-set(by_ref): raise ValueError('Unknown proximity reference')
    w,h=model['board']['width_mm'],model['board']['height_mm']
    gap=settings['component_gap_mm']; keepouts=settings['keepouts']
    def apron(*, component):
        # Pad-rich packages need room outside their courtyard for escapes/vias.
        numbers={p['number'] for p in component['pads'] if p['number'] and p['layers']}
        return settings.get('dense_package_apron_mm',.8) if len(numbers)>=8 else 0
    def score():
        # HPWL is a fast wirelength proxy, not actual routed length.
        groups=net_groups(pads=placed_pads(model=model,placements=current))
        wire=0
        for name,pads in groups.items():
            if name in settings.get('ground_pour_nets',[]): continue
            xs=[p['xy'][0] for p in pads]; ys=[p['xy'][1] for p in pads]
            wire+=settings['net_weights'].get(name,1)*(max(xs)-min(xs)+max(ys)-min(ys))
        centers=[]
        displacement=0
        for ref,component in by_ref.items():
            if not component['mechanical_only']:
                if ref not in fixed: centers.append(current[ref]['anchor_mm'])
            displacement+=math.dist(initial[ref]['anchor_mm'],current[ref]['anchor_mm'])**2
        # Logical block membership deliberately does not enter this objective.
        # A single global spread term allows blocks to interleave naturally.
        center=[sum(p[i] for p in centers)/len(centers) for i in (0,1)] if centers else [0,0]
        spread=sum(math.dist(p,center)**2 for p in centers)
        relationships=relationship_metrics(model=model,placements=current,settings=settings)
        feedback=0.
        for hotspot in routing_feedback:
            for ref,component in by_ref.items():
                if ref in fixed or ref==hotspot['reference']: continue
                box=bounds(component=component,placement=current[ref])
                radius=math.hypot(box[1]-box[0],box[3]-box[2])/2+settings['congestion_radius_mm']
                distance=math.dist([(box[0]+box[1])/2,(box[2]+box[3])/2],hotspot['xy_mm'])
                feedback+=settings['placement_feedback_cost']*max(0,radius-distance)**2
        return wire+settings['compactness_weight']*spread+settings['movement_weight']*displacement+relationships['cost']+feedback
    def legal(ref):
        candidate=bounds(component=by_ref[ref],placement=current[ref])
        margin=settings['edge_margin_mm']
        if candidate[0]<margin or candidate[1]>w-margin or candidate[2]<margin or candidate[3]>h-margin: return False
        if math.dist(current[ref]['anchor_mm'],initial[ref]['anchor_mm'])>settings['max_move_mm']+1e-8: return False
        for pair in settings.get('proximity_limits',[]):
            if ref in pair['references']:
                left,right=pair['references']
                if math.dist(current[left]['anchor_mm'],current[right]['anchor_mm'])>pair['max_distance_mm']: return False
        if any(intersects(a=candidate,b=k['bounds_mm']) for k in keepouts): return False
        return not any(intersects(a=candidate,b=bounds(component=c,placement=current[r]),gap=gap+max(apron(component=by_ref[ref]),apron(component=c)))
                       for r,c in by_ref.items() if r!=ref)
    start_score=score(); best=start_score; moves=[]
    for step in settings['placement_steps_mm']:
        for sweep in range(settings['placement_sweeps']):
            changed=False
            # Give authored IC/support groups first choice before individual
            # compactness moves pack unrelated parts into their travel space.
            from .layout_group_moves import move_groups
            best,joint_moves=move_groups(groups=settings.get('placement_groups',[]),current=current,
                step=step,score=score,legal=legal,best=best)
            moves.extend(joint_moves); changed=bool(joint_moves)
            for ref in sorted(set(by_ref)-fixed):
                previous=copy.deepcopy(current[ref]); chosen=previous; chosen_score=best
                if not legal(ref): chosen_score=float('inf')
                x,y=previous['anchor_mm']
                candidates=[(round(x+dx*step,6),round(y+dy*step,6),previous['rotation_degrees'])
                            for dx,dy in ((-1,0),(0,-1),(0,1),(1,0),(-1,-1),(-1,1),(1,-1),(1,1))]
                if settings['allow_rotation']:
                    candidates.extend((x,y,(previous['rotation_degrees']+a)%360) for a in (90,180,270))
                for xx,yy,angle in candidates:
                    current[ref]={**previous,'anchor_mm':[xx,yy],'rotation_degrees':angle}
                    if not legal(ref): continue
                    value=score()
                    if value<chosen_score-1e-8:
                        chosen=copy.deepcopy(current[ref]); chosen_score=value
                current[ref]=chosen
                if chosen!=previous:
                    moves.append({'reference':ref,'from':previous,'to':chosen,'score':round(chosen_score,6)})
                    best=chosen_score; changed=True
            best,joint_moves=move_groups(groups=settings.get('placement_groups',[]),current=current,
                step=step,score=score,legal=legal,best=best)
            moves.extend(joint_moves); changed=changed or bool(joint_moves)
            if not changed: break
    invalid=[ref for ref in sorted(set(by_ref)-fixed) if not legal(ref)]
    if invalid: raise ValueError(f'No legal placement within explicit margins/proximity limits: {invalid}')
    return {'before':initial,'after':current,'moves':moves,'fixed_references':sorted(fixed),
            'score_before':round(start_score,6),'score_after':round(best,6),
            'relationships_before':relationships_before,
            'relationships_after':relationship_metrics(model=model,placements=current,settings=settings),
            'metrics_before':metrics(model=model,placements=initial),
            'metrics_after':metrics(model=model,placements=current)}


class RouteGrid:
    def __init__(self, *, model, pads, net, settings, routes, reservations=(), history=(), origin_mm=(0,0)):
        routes=[*routes,*reservations]
        self.model,self.pads,self.routes=model,pads,routes
        self.step=settings['routing_grid_mm']; self.settings=settings; self.net=net
        self.origin_mm=origin_mm
        self.xs=np.arange(origin_mm[0],model['board']['width_mm']+self.step/2,self.step)
        self.ys=np.arange(origin_mm[1],model['board']['height_mm']+self.step/2,self.step)
        self.blocked=np.zeros((2,len(self.ys),len(self.xs)),dtype=bool)
        self.via_blocked=np.zeros(self.blocked.shape[1:],dtype=bool)
        self.history_cost=np.zeros(self.via_blocked.shape,dtype=np.float32)
        for region in history:
            if region['exempt_net']==net: continue
            x,y=region['xy_mm']; radius=region['radius_mm']
            dx=self.xs[None,:]-x; dy=self.ys[:,None]-y
            self.history_cost+=region['cost']*np.maximum(0,1-np.hypot(dx,dy)/radius)
        # Every grid edge is at most half a diagonal from a sampled endpoint.
        # Pad-to-grid snaps are separately checked analytically, not hidden in
        # a full-cell inflation that unnecessarily blocks adjacent fanouts.
        self.guard=self.step/math.sqrt(2)
        self.width=settings['track_width_mm']; clearance=settings['clearance_mm']
        self.bus_group=next((g for g in settings.get('bus_groups',[]) if net in g['nets']),None)
        self.via_radius=settings['via_diameter_mm']/2
        margin=settings['edge_margin_mm']+self.width/2+self.guard
        self.blocked[:,(self.ys<margin)|(self.ys>model['board']['height_mm']-margin),:]=True
        self.blocked[:,:,(self.xs<margin)|(self.xs>model['board']['width_mm']-margin)]=True
        via_margin=settings['edge_margin_mm']+self.via_radius+self.guard
        self.via_blocked[(self.ys<via_margin)|(self.ys>model['board']['height_mm']-via_margin),:]=True
        self.via_blocked[:,(self.xs<via_margin)|(self.xs>model['board']['width_mm']-via_margin)]=True
        for region in settings['keepouts']:
            if net not in region.get('allowed_nets',[]):
                a,b,c,d=region['bounds_mm']
                self.rectangle(xy=[(a+b)/2,(c+d)/2],size=[b-a,d-c],rotation=0,
                               margin=self.width/2+self.guard,target=self.blocked)
                self.rectangle(xy=[(a+b)/2,(c+d)/2],size=[b-a,d-c],rotation=0,
                               margin=self.via_radius+self.guard,target=self.via_blocked)
        for component in model['components']:
            if component['mechanical_only']:
                a,b,c,d=bounds(component=component,placement=component['pcb'])
                xy=[(a+b)/2,(c+d)/2]; size=[b-a,d-c]
                self.rectangle(xy=xy,size=size,rotation=0,margin=self.width/2+self.guard,target=self.blocked)
                self.rectangle(xy=xy,size=size,rotation=0,margin=self.via_radius+self.guard,target=self.via_blocked)
        for pad in pads:
            if pad['drill']:
                self.rectangle(xy=pad['xy'],size=pad['drill'],rotation=pad['rotation'],
                               margin=settings['via_drill_mm']/2+settings['drill_gap_mm']+self.guard,target=self.via_blocked)
                if pad['pad_type']=='np_thru_hole':
                    self.rectangle(xy=pad['xy'],size=pad['drill'],rotation=pad['rotation'],
                                   margin=self.width/2+clearance+self.guard,target=self.blocked)
            if pad['net']==net: continue
            for layer in pad['layers']:
                self.rectangle(xy=pad['xy'],size=pad['size'],rotation=pad['rotation'],
                               margin=self.width/2+clearance+self.guard,target=self.blocked[('F.Cu','B.Cu').index(layer)])
            if pad['layers']:
                self.rectangle(xy=pad['xy'],size=pad['size'],rotation=pad['rotation'],
                               margin=self.via_radius+clearance+self.guard,target=self.via_blocked)
        for route in routes:
            if route['net']!=net:
                for a,b,layer in zip(route['points_mm'],route['points_mm'][1:],route['segment_layers']):
                    aa=[a['x'],a['y']]; bb=[b['x'],b['y']]
                    self.segment(a=aa,b=bb,radius=(route['width_mm']+self.width)/2+clearance+self.guard,
                                 target=self.blocked[('F.Cu','B.Cu').index(layer)])
                    self.segment(a=aa,b=bb,radius=route['width_mm']/2+self.via_radius+clearance+self.guard,target=self.via_blocked)
            for via in route['vias']:
                xy=[via['x'],via['y']]
                # Drill-to-drill exclusion also applies to same-net vias.
                self.segment(a=xy,b=xy,radius=(via['drill_mm']+settings['via_drill_mm'])/2+settings['drill_gap_mm']+self.guard,target=self.via_blocked)
                if route['net']!=net:
                    self.segment(a=xy,b=xy,radius=via['diameter_mm']/2+self.width/2+clearance+self.guard,target=self.blocked)
                    self.segment(a=xy,b=xy,radius=via['diameter_mm']/2+self.via_radius+clearance+self.guard,target=self.via_blocked)

    def rectangle(self, *, xy, size, rotation, margin, target):
        angle=math.radians(-rotation); c,s=math.cos(angle),math.sin(angle)
        radius=math.hypot(size[0]/2+margin,size[1]/2+margin)
        ix,iy=self.window(xmin=xy[0]-radius,xmax=xy[0]+radius,ymin=xy[1]-radius,ymax=xy[1]+radius)
        xx=self.xs[None,ix]-xy[0]; yy=self.ys[iy,None]-xy[1]
        mask=(np.abs(xx*c+yy*s)<=size[0]/2+margin)&(np.abs(-xx*s+yy*c)<=size[1]/2+margin)
        target[...,iy,ix][...,mask]=True

    def window(self, *, xmin, xmax, ymin, ymax):
        xmin-=self.origin_mm[0]; xmax-=self.origin_mm[0]
        ymin-=self.origin_mm[1]; ymax-=self.origin_mm[1]
        return (slice(max(0,int(math.floor(xmin/self.step))),min(len(self.xs),int(math.ceil(xmax/self.step))+1)),
                slice(max(0,int(math.floor(ymin/self.step))),min(len(self.ys),int(math.ceil(ymax/self.step))+1)))

    def segment(self, *, a, b, radius, target):
        ix,iy=self.window(xmin=min(a[0],b[0])-radius,xmax=max(a[0],b[0])+radius,
                          ymin=min(a[1],b[1])-radius,ymax=max(a[1],b[1])+radius)
        xx=self.xs[None,ix]-a[0]; yy=self.ys[iy,None]-a[1]; dx=b[0]-a[0]; dy=b[1]-a[1]
        t=np.clip((xx*dx+yy*dy)/(dx*dx+dy*dy),0,1) if dx or dy else 0
        target[...,iy,ix][...,(xx-t*dx)**2+(yy-t*dy)**2<=radius**2]=True

    def escape_clear(self, *, a, b, layer):
        """Exact short fanout segments; never clear raster obstacle cells."""
        from .layout_checks import segment_box, segment_distance, point_distance
        margin=self.width/2+self.settings['clearance_mm']
        w,h=self.model['board']['width_mm'],self.model['board']['height_mm']
        edge=self.width/2+self.settings['edge_margin_mm']
        if any(not(edge<=p[0]<=w-edge and edge<=p[1]<=h-edge) for p in (a,b)): return False
        for pad in self.pads:
            if layer in pad['layers'] and pad['net']!=self.net:
                if segment_box(a=a,b=b,xy=pad['xy'],size=pad['size'],rotation=pad['rotation'],margin=margin-1e-9): return False
            if pad['pad_type']=='np_thru_hole' and pad['drill']:
                if segment_box(a=a,b=b,xy=pad['xy'],size=pad['drill'],rotation=pad['rotation'],margin=margin): return False
        for component in self.model['components']:
            if component['mechanical_only']:
                x0,x1,y0,y1=bounds(component=component,placement=component['pcb'])
                if segment_box(a=a,b=b,xy=[(x0+x1)/2,(y0+y1)/2],size=[x1-x0,y1-y0],rotation=0,margin=self.width/2): return False
        for region in self.settings['keepouts']:
            if self.net not in region.get('allowed_nets',[]):
                x0,x1,y0,y1=region['bounds_mm']
                if segment_box(a=a,b=b,xy=[(x0+x1)/2,(y0+y1)/2],size=[x1-x0,y1-y0],rotation=0,margin=self.width/2): return False
        for route in self.routes:
            if route['net']==self.net: continue
            for first,last,l in zip(route['points_mm'],route['points_mm'][1:],route['segment_layers']):
                if l==layer and segment_distance(a=a,b=b,c=[first['x'],first['y']],d=[last['x'],last['y']])<(self.width+route['width_mm'])/2+self.settings['clearance_mm']-1e-9: return False
            for via in route['vias']:
                if point_distance(p=[via['x'],via['y']],a=a,b=b)<(self.width+via['diameter_mm'])/2+self.settings['clearance_mm']-1e-9: return False
        return True

    def escape_nodes(self, *, pad):
        h,w=self.blocked.shape[1:]; candidates={}
        # Free snapped endpoints are retained. For blocked fine-pitch centres,
        # analytically test short cardinal/diagonal fanouts to free grid cells.
        for distance in self.settings.get('escape_distances_mm',[0,.3,.6,.9,1.2,1.8,2.4,3.6]):
            for dx,dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                scale=math.hypot(dx,dy)
                x=int(round((pad['xy'][0]+distance*dx/scale-self.origin_mm[0])/self.step))
                y=int(round((pad['xy'][1]+distance*dy/scale-self.origin_mm[1])/self.step))
                if not(0<=x<w and 0<=y<h): continue
                xy=[round(float(self.xs[x]),6),round(float(self.ys[y]),6)]
                for l,layer in enumerate(('F.Cu','B.Cu')):
                    if layer not in pad['layers'] or self.blocked[l,y,x]: continue
                    node=(l,y,x)
                    if node in candidates: continue
                    for path in octilinear_fanouts(start=pad['xy'],end=xy):
                        if not all(self.escape_clear(a=a,b=b,layer=layer) for a,b in zip(path,path[1:])): continue
                        candidates[node]={'points':path,'length_mm':sum(math.dist(a,b) for a,b in zip(path,path[1:]))}
                        break
        return candidates

    def propose(self, *, start, end):
        h,w=self.blocked.shape[1:]; size=h*w
        layers=('F.Cu','B.Cu')
        starts=self.escape_nodes(pad=start); ends=self.escape_nodes(pad=end)
        if not starts or not ends: raise ValueError('Endpoint escape blocked at configured grid/width/clearance; no obstacle carving')
        def index(l,y,x): return l*size+y*w+x
        def heuristic(y,x):
            dx=abs(float(self.xs[x])-end['xy'][0]); dy=abs(float(self.ys[y])-end['xy'][1])
            return max(dx,dy)+(math.sqrt(2)-1)*min(dx,dy)
        goals={index(*node) for node in ends}; sources={index(*node) for node in starts}
        costs={}; previous={}; heap=[]
        for (l,y,x),escape in sorted(starts.items()):
            distance=escape['length_mm']
            source=index(l,y,x); costs[source]=distance; previous[source]=None
            heapq.heappush(heap,(distance+heuristic(y,x),distance,source))
        expansions=0
        while heap:
            _,cost,current=heapq.heappop(heap)
            if cost>costs[current]+1e-9: continue
            if current in goals: break
            expansions+=1
            if expansions>self.settings['max_expansions_per_route']:
                raise ValueError('Bounded route search exhausted; connection remains unresolved')
            l,rem=divmod(current,size); y,x=divmod(rem,w)
            choices=[]
            for dy,dx in ((-1,0),(0,-1),(0,1),(1,0),(-1,-1),(-1,1),(1,-1),(1,1)):
                ny,nx=y+dy,x+dx
                if not(0<=ny<h and 0<=nx<w) or self.blocked[l,ny,nx]: continue
                if dx and dy and (self.blocked[l,y,nx] or self.blocked[l,ny,x]): continue
                distance=self.step*(math.sqrt(2) if dx and dy else 1)
                # Preserve a ground reference by preferring front-side signals.
                distance*=self.settings.get('bottom_route_cost',1.0) if l else 1.0
                axis=self.settings.get('layer_preferred_axes',{'F.Cu':'x','B.Cu':'y'})[layers[l]]
                # A soft preference leaves perpendicular escapes and 45-degree
                # detours available; it cannot declare a legal route impossible.
                cross=abs(dy) if axis=='x' else abs(dx)
                distance*=1+(self.settings.get('layer_cross_axis_cost',1.35)-1)*cross/math.hypot(dx,dy)
                distance+=self.step*float(self.history_cost[ny,nx])
                group=self.bus_group
                if group:
                    axis=group['preferred_axis']
                    if (axis=='x' and dy) or (axis=='y' and dx): distance*=self.settings.get('bus_cross_axis_cost',1.0)
                choices.append((l,ny,nx,distance))
            if current not in sources|goals and not self.via_blocked[y,x] and not self.blocked[1-l,y,x]:
                choices.append((1-l,y,x,self.settings['via_cost_mm']))
            for nl,ny,nx,delta in choices:
                nxt=index(nl,ny,nx); value=cost+delta
                if value<costs.get(nxt,float('inf'))-1e-9:
                    costs[nxt]=value; previous[nxt]=current
                    heapq.heappush(heap,(value+heuristic(ny,nx),value,nxt))
        else: raise ValueError('No route inside bounded two-layer search space')
        goal=current
        states=[]
        while current is not None:
            l,rem=divmod(current,size); y,x=divmod(rem,w)
            states.append((l,round(float(self.xs[x]),6),round(float(self.ys[y]),6)))
            current=previous[current]
        states.reverse()
        first_l,first_x,first_y=states[0]
        prefix=starts[(first_l,round((first_y-self.origin_mm[1])/self.step),round((first_x-self.origin_mm[0])/self.step))]['points']
        last_l,last_rem=divmod(goal,size); last_y,last_x=divmod(last_rem,w)
        suffix=list(reversed(ends[(last_l,last_y,last_x)]['points']))
        states=[(first_l,*p) for p in prefix[:-1]]+states+[(last_l,*p) for p in suffix[1:]]
        # Only remove exactly collinear grid points; no unsafe line-of-sight shortcut.
        simple=[]
        for state in states:
            while len(simple)>=2:
                a,b=simple[-2:]
                if a[0]!=b[0] or b[0]!=state[0]: break
                cross=(b[1]-a[1])*(state[2]-b[2])-(b[2]-a[2])*(state[1]-b[1])
                if abs(cross)>1e-10: break
                simple.pop()
            simple.append(state)
        points=[{'x':start['xy'][0],'y':start['xy'][1]}]; segment_layers=[]; vias=[]; layer=simple[0][0]
        for l,x,y in simple:
            point={'x':x,'y':y}
            if point!=points[-1]: points.append(point); segment_layers.append(layers[layer])
            if l!=layer:
                vias.append({**point,'diameter_mm':self.settings['via_diameter_mm'],'drill_mm':self.settings['via_drill_mm']})
                layer=l
        point={'x':end['xy'][0],'y':end['xy'][1]}
        if point!=points[-1]: points.append(point); segment_layers.append(layers[layer])
        if len(points)<2: raise ValueError('Coincident physical pads need independent connectivity review')
        return {'points_mm':points,'segment_layers':segment_layers,'vias':vias,'expanded_states':expansions}


def _route_pass(*, model, placements, settings, ordered, reservations, progress=None, initial_routes=(), history=()):
    pads=placed_pads(model=model,placements=placements); groups=net_groups(pads=pads)
    routes=copy.deepcopy(list(initial_routes)); unresolved=[]; counts={}; sequence=0; search_records=[]
    ground=set(settings.get('ground_pour_nets',[]))
    if ground-set(groups): raise ValueError('Ground pour net absent from frozen electrical model')
    empty_grids={}
    for net in ordered:
        edges=spanning_edges(pads=groups[net]); counts[net]={'physical_pads':len(groups[net]),'requested_links':len(edges),'proposed_links':0}
        if net in ground:
            counts[net].update(strategy='copper_pour',requested_links=0,connectivity_status='PENDING_NATIVE_FILL_AND_AUDIT')
            continue
        for start,end in edges:
            sequence+=1
            if progress: progress(f'Route proposal {sequence}: {net} {start["reference"]}.{start["number"]} -> {end["reference"]}.{end["number"]}')
            def endpoint(pad):
                result={'reference':pad['reference'],'pin_number':pad['number']}
                if sum(p['reference']==pad['reference'] and p['number']==pad['number'] for p in pads)>1:
                    result['pad_selector']={'size_mm':dict(zip(('x','y'),pad['size'])),'shape':pad['shape'],'pad_type':pad['pad_type']}
                return result
            # Invalid obstacle/model construction is a study error, not a lost
            # connection; only classify failures from an actual route attempt.
            grid=RouteGrid(model=model,pads=pads,net=net,settings=settings,routes=routes,reservations=reservations,history=history)
            try:
                # Try phase-aligned grids only when an endpoint has no raster
                # exit. Same pitch/guard/geometry: never carve or reduce margins.
                try:
                    candidate=grid.propose(start=start,end=end)
                except ValueError as initial_error:
                    if not settings.get('adaptive_grid_alignment',True) or 'escape blocked' not in str(initial_error): raise
                    origins=[]
                    for pad in sorted((start,end),key=lambda p:(min(p['size']),p['reference'],p['index'])):
                        origin=tuple(round(value%settings['routing_grid_mm'],6) for value in pad['xy'])
                        origin=tuple(0 if abs(v-settings['routing_grid_mm'])<1e-6 else v for v in origin)
                        if origin!=(0,0) and origin not in origins: origins.append(origin)
                    candidate=None; attempts=[]
                    for origin in origins:
                        trial_grid=RouteGrid(model=model,pads=pads,net=net,settings=settings,routes=routes,
                            reservations=reservations,history=history,origin_mm=origin)
                        try:
                            candidate=trial_grid.propose(start=start,end=end)
                        except ValueError as retry_error:
                            attempts.append({'origin_mm':origin,'outcome':str(retry_error)}); continue
                        attempts.append({'origin_mm':origin,'outcome':'proposed'}); grid=trial_grid; break
                    search_records.append({'net':net,'start':endpoint(start),'end':endpoint(end),'alignment_attempts':attempts})
                    if candidate is None: raise initial_error
                identity=f'{net}|{start["reference"]}|{start["index"]}|{end["reference"]}|{end["index"]}'
                route={'name':'svg_layout_'+hashlib.sha256(identity.encode()).hexdigest()[:12], 'net':net,'layer':candidate['segment_layers'][0],
                       'angle_policy':'orthogonal_45',
                       'width_mm':settings['track_width_mm'],'start':endpoint(start),'end':endpoint(end),
                       'points_mm':candidate['points_mm'],'segment_layers':candidate['segment_layers'],'vias':candidate['vias'],
                       'reason':'UNACCEPTED SVG/math layout proposal; exact live endpoint and native DRC validation required'}
                validate_route(command=route); routes.append(route); counts[net]['proposed_links']+=1
            except ValueError as exc:
                if net not in empty_grids:
                    empty_grids[net]=RouteGrid(model=model,pads=pads,net=net,settings=settings,routes=[])
                baseline=[len(empty_grids[net].escape_nodes(pad=p)) for p in (start,end)]
                current=[len(grid.escape_nodes(pad=p)) for p in (start,end)]
                category=('fixed_geometry_or_fanout_limit' if not all(baseline) else
                          'occupied_escape_corridor' if not all(current) else
                          'search_limit' if 'exhausted' in str(exc) else 'congested_search_space')
                from .layout_negotiation import escape_blockers
                blockers=[escape_blockers(pad=p,baseline_grid=empty_grids[net],routes=routes,reservations=reservations)
                          for p,count in zip((start,end),current) if not count]
                unresolved.append({'net':net,'start':endpoint(start),'end':endpoint(end),
                                   'start_mm':start['xy'],'end_mm':end['xy'],'reason':str(exc),
                                   'blocking_nets':sorted({n for item in blockers for n in item['routes']}),
                                   'blocking_reservation_nets':sorted({n for item in blockers for n in item['reservations']}),
                                   'category':category,'fixed_geometry_escape_options':baseline,'current_escape_options':current})
    from .layout_zones import propose_pours
    pours=propose_pours(model=model,placements=placements,settings=settings)
    for net,net_pads in groups.items():
        counts.setdefault(net,{'physical_pads':len(net_pads),'requested_links':len(spanning_edges(pads=net_pads))})
        counts[net]['proposed_links']=sum(r['net']==net for r in routes)
        if net in ground: counts[net].update(strategy='copper_pour',requested_links=0,connectivity_status='PENDING_NATIVE_FILL_AND_AUDIT')
    return {'routes':routes,'unresolved':unresolved,'net_summary':counts,'search_records':search_records,
            'copper_pours':pours,'pending_pour_nets':sorted(ground),
            'track_segments':sum(len(r['points_mm'])-1 for r in routes),
            'via_count':sum(len(r['vias']) for r in routes),
            'proposed_track_length_mm':round(sum(math.dist(list(a.values()),list(b.values())) for r in routes
                                               for a,b in zip(r['points_mm'],r['points_mm'][1:])),6)}


def route_all(*, model, placements, settings, progress=None):
    """Bounded deterministic restarts, retaining the best complete candidate.

    Only draft copper is rebuilt. No live copper is removed or modified, and
    clearances/widths/via dimensions never relax to get a better completion count.
    Related bus nets stay in their authored order as groups move in priority.
    """
    from .layout_fanouts import reserve_fanouts
    from .layout_ground import ground_plan, ground_access
    ground=ground_plan(model=model,placements=placements,settings=settings)
    if progress: progress(f'Ground first: {len(ground["copper_pours"])} pour intents; {len(ground["pads"])} physical pads; native fill remains required')
    ground_before=ground_access(model=model,placements=placements,settings=settings,routes=[])
    groups=net_groups(pads=placed_pads(model=model,placements=placements))
    priority=list(dict.fromkeys(settings['net_priority']+[n for g in settings.get('bus_groups',[]) for n in g['nets']]))
    ordered=sorted(groups,key=lambda n:(priority.index(n) if n in priority else len(priority),len(groups[n]),n))
    units=[]; used=set()
    for net in ordered:
        if net in used: continue
        bus=next((g for g in settings.get('bus_groups',[]) if net in g['nets']),None)
        unit=[n for n in bus['nets'] if n in groups] if bus else [net]
        units.append(unit); used.update(unit)
    reservations,reservation_failures=reserve_fanouts(model=model,placements=placements,settings=settings)
    if progress: progress(f'Reserved {len(reservations)} fine-pitch exits; {len(reservation_failures)} reservation attempts unavailable (not lost net endpoints)')
    from .layout_negotiation import history_regions, repair_neighborhoods
    passes=[]; best=None; best_key=None; seen_orders=set(); history=[]
    for attempt in range(settings.get('routing_passes',2)):
        order=[n for unit in units for n in unit]
        trial_settings={**settings,'via_cost_mm':settings['via_cost_mm']*(settings.get('retry_via_cost_factor',.6) if attempt else 1)}
        signature=(tuple(order),trial_settings['via_cost_mm'],str(history))
        if signature in seen_orders: break
        seen_orders.add(signature)
        if progress: progress(f'Routing pass {attempt+1}: fresh draft; via cost {trial_settings["via_cost_mm"]}; widths/clearances unchanged')
        result=_route_pass(model=model,placements=placements,settings=trial_settings,ordered=order,
                           reservations=reservations,progress=progress,history=history)
        key=(len(result['unresolved']),result['via_count'],result['proposed_track_length_mm'])
        passes.append({'pass':attempt+1,'net_order':order,'via_cost_mm':trial_settings['via_cost_mm'],
                       'unresolved_links':key[0],'via_count':key[1],'proposed_track_length_mm':key[2],
                       'failure_details':result['unresolved']})
        if best_key is None or key<best_key:
            best,best_key=result,key; selected=attempt+1
        if not result['unresolved']: break
        history=history_regions(failures=result['unresolved'],previous=history,settings=settings)
        failed={u['net'] for u in result['unresolved']}
        units=sorted(units,key=lambda unit:(not bool(set(unit)&failed),min(ordered.index(n) for n in unit)))
    best,repairs=repair_neighborhoods(model=model,placements=placements,settings=settings,best=best,
        units=units,reservations=reservations,history=history,progress=progress)
    best.update(routing_passes=passes,selected_pass=selected,fanout_reservations=reservations,
                local_repairs=repairs,congestion_history=history,
                ground_plan=ground,ground_access_before=ground_before,
                ground_access_after=ground_access(model=model,placements=placements,settings=settings,routes=best['routes']),
                unavailable_fanout_reservations=reservation_failures,
                ground_model={'mode':'unfilled_intent_with_access_reservations',
                    'signal_layer_preference':'F.Cu; B.Cu available with explicit vias',
                    'filled_plane_continuity_evaluated':False,'return_path_continuity_proven':False,
                    'required':'Native fill/save, thermal/island/interlayer connectivity audit; add stitching from those results'})
    return best
