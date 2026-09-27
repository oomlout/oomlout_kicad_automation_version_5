"""Ground-first plan and conservative raster access diagnostics, NOT native fill."""
from collections import deque
import numpy as np
from .layout_model import placed_pads
from .layout_zones import propose_pours


def ground_plan(*, model, placements, settings):
    pads=placed_pads(model=model,placements=placements)
    nets=settings.get('ground_pour_nets',[])
    return {'order':'ground_before_signals','copper_pours':propose_pours(model=model,placements=placements,settings=settings),
        'pads':[{'reference':p['reference'],'pin_number':p['number'],'pad_index':p['index'],
                 'xy_mm':p['xy'],'layers':p['layers'],'net':p['net']} for p in pads if p['net'] in nets],
        'native_fill_status':'PENDING_NATIVE_FILL_AND_AUDIT' if nets else 'NOT_REQUESTED'}


def _regions(*, free):
    labels=np.zeros(free.shape,dtype=np.int32); count=0; sizes=[]
    height,width=free.shape
    for y,x in np.argwhere(free):
        if labels[y,x]: continue
        count+=1; labels[y,x]=count; queue=deque([(int(y),int(x))]); size=0
        while queue:
            yy,xx=queue.popleft(); size+=1
            for ny,nx in ((yy-1,xx),(yy+1,xx),(yy,xx-1),(yy,xx+1)):
                if 0<=ny<height and 0<=nx<width and free[ny,nx] and not labels[ny,nx]:
                    labels[ny,nx]=count; queue.append((ny,nx))
        sizes.append(size)
    return labels,sizes


def ground_access(*, model, placements, settings, routes):
    """Estimate loss of ground access after signals; do not infer thermal spokes.

    Each layer is deliberately separate: no fabricated interlayer connections.
    Conservative free-space regions are diagnostic only. Native copper polygons
    and actual pad/via connectivity must replace these estimates at acceptance.
    """
    from .layout_search import RouteGrid
    pads=placed_pads(model=model,placements=placements); results=[]
    for net in settings.get('ground_pour_nets',[]):
        grid=RouteGrid(model=model,pads=pads,net=net,settings={**settings,
            'routing_grid_mm':max(.2,settings['routing_grid_mm'])},routes=routes)
        regions=[_regions(free=~grid.blocked[l]) for l in (0,1)]
        access=[]
        for pad in pads:
            if pad['net']!=net: continue
            nodes=grid.escape_nodes(pad=pad)
            ids=sorted({(('F.Cu','B.Cu')[l],int(regions[l][0][y,x])) for l,y,x in nodes if regions[l][0][y,x]})
            access.append({'reference':pad['reference'],'pin_number':pad['number'],'pad_index':pad['index'],
                           'estimated_regions':ids,'has_estimated_access':bool(ids)})
        results.append({'net':net,'physical_pads':len(access),'pads_without_estimated_access':sum(not p['has_estimated_access'] for p in access),
            'layers':{layer:{'free_regions':len(regions[l][1]),'largest_region_cells':max(regions[l][1],default=0)}
                      for l,layer in enumerate(('F.Cu','B.Cu'))},'pad_access':access})
    return {'scope':'Conservative free-space estimate, not filled copper or connectivity proof',
            'native_fill_evaluated':False,'nets':results}
