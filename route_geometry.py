"""Exact horizontal/vertical/45-degree routing geometry in board-local mm."""
import math


def is_octilinear(*, a, b, tolerance_mm=0.000001):
    dx,dy=abs(a[0]-b[0]),abs(a[1]-b[1])
    return min(dx,dy)<=tolerance_mm or abs(dx-dy)<=tolerance_mm


def octilinear_fanouts(*, start, end):
    """Stable two-segment candidates, never an arbitrary-angle snap."""
    dx,dy=end[0]-start[0],end[1]-start[1]
    sx,sy=1 if dx>=0 else -1,1 if dy>=0 else -1
    diagonal=min(abs(dx),abs(dy))
    mids=([start[0]+sx*diagonal,start[1]+sy*diagonal],
          [end[0]-sx*diagonal,end[1]-sy*diagonal],
          [start[0],end[1]],[end[0],start[1]])
    candidates=[]
    for middle in mids:
        path=[]
        for p in (start,middle,end):
            point=[round(float(v),6) for v in p]
            if not path or point!=path[-1]: path.append(point)
        if path not in candidates: candidates.append(path)
    return sorted(candidates,key=lambda p:(sum(math.dist(a,b) for a,b in zip(p,p[1:])),len(p),p))
