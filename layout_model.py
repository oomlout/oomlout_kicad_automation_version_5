"""Read-only, millimetre-space inputs for the SVG layout design action.

Assembly SVGs are artwork, never a replacement for copper/pin geometry.
The frozen source libraries and electrical table are the numerical authority.
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import yaml

from .connectivity import _children, _parse_sexpression
from .footprints import footprint_bounds_text
from .sources import SOURCE_ID_PATTERN


def sha256(*, path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def child(*, node, tag, default=None):
    values = _children(node, tag)
    return values[0][1:] if values else default


def transform(*, xy, placement):
    """KiCad positive degrees rotate counterclockwise on the Y-down canvas."""
    if placement.get('side', 'front') != 'front':
        raise ValueError('Layout planner currently requires front-side components; no implicit flip')
    angle = math.radians(-placement['rotation_degrees'])
    x, y = xy
    ox, oy = placement['anchor_mm']
    return [round(ox + x*math.cos(angle)-y*math.sin(angle), 6),
            round(oy + x*math.sin(angle)+y*math.cos(angle), 6)]


def bounds(*, component, placement):
    x0, x1, y0, y1 = component['bounds_mm']
    points = [transform(xy=p, placement=placement) for p in ((x0,y0),(x0,y1),(x1,y0),(x1,y1))]
    return [min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points)]


def read_footprint(*, path):
    text = Path(path).read_text(encoding='utf-8')
    root = _parse_sexpression(text)
    if root[0] not in ('footprint', 'module'):
        raise ValueError('Expected footprint source')
    pads = []
    for index, pad in enumerate(_children(root, 'pad')):
        at = list(map(float, child(node=pad, tag='at', default=[0,0])))
        size = list(map(float, child(node=pad, tag='size')))
        if len(size)!=2 or min(size)<=0 or pad[3] == 'custom':
            raise ValueError(f'Unsupported/invalid pad geometry in {path}: {pad[:4]}')
        drill = child(node=pad, tag='drill', default=[])
        if any(isinstance(v,list) for v in drill):
            raise ValueError('Offset drills require explicit support, not guessed centres')
        drill_size = [float(v) for v in drill if v != 'oval']
        if len(drill_size)==1: drill_size *= 2
        layers = child(node=pad, tag='layers', default=[])
        copper = [l for l in ('F.Cu','B.Cu') if l in layers or '*.Cu' in layers]
        pads.append({'index':index, 'number':str(pad[1]), 'pad_type':pad[2], 'shape':pad[3],
                     'xy':at[:2], 'rotation':at[2] if len(at)>2 else 0,
                     'size':size, 'drill':drill_size, 'layers':copper})
    if not pads:
        raise ValueError(f'No pads in {path}')
    # Courtyard arcs are not silently reduced to three-point approximate bounds.
    for arc in _children(root,'fp_arc'):
        if child(node=arc,tag='layer') in (['F.CrtYd'],['B.CrtYd']):
            raise ValueError('Arced courtyard needs an exact-extrema implementation')
    envelope = footprint_bounds_text(text)
    # Include copper even when a defective courtyard excludes a pad.
    for pad in pads:
        angle=math.radians(-pad['rotation']); c,s=abs(math.cos(angle)),abs(math.sin(angle))
        rx=(pad['size'][0]*c+pad['size'][1]*s)/2
        ry=(pad['size'][0]*s+pad['size'][1]*c)/2
        x,y=pad['xy']; a,b,d,e=envelope
        envelope=(min(a,x-rx),max(b,x+rx),min(d,y-ry),max(e,y+ry))
    return {'pads':pads,'bounds_mm':list(envelope)}


def read_svg(*, path, pads):
    """Import safe self-contained SVG artwork and fit its pin orientation only.

    No stretching to make a diagram pretend to be a land pattern. Residuals
    are reported because physical terminal markers differ from solder lands.
    """
    raw=Path(path).read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('SVG DTD/entities are not accepted')
    root=ET.fromstring(raw)
    allowed={'svg','g','rect','circle','ellipse','line','polyline','polygon','path','text','tspan','title','desc'}
    for node in root.iter():
        if node.tag.split('}')[-1] not in allowed:
            raise ValueError(f'Unsupported active/external SVG element: {node.tag}')
        for key,value in node.attrib.items():
            if key.lower().startswith('on') or 'href' in key.lower() or 'url(' in value.lower() or key=='style':
                raise ValueError('SVG scripts, styles and external resources are not accepted')
    def mm(key):
        value=root.attrib.get(key,'')
        if not value.endswith('mm'): raise ValueError('SVG must declare physical millimetres')
        number=float(value[:-2])
        if not math.isfinite(number) or number<=0: raise ValueError('Invalid SVG dimensions')
        return number
    width,height=mm('width'),mm('height')
    view=list(map(float,root.attrib['viewBox'].split()))
    if len(view)!=4 or not all(math.isfinite(v) for v in view) or min(view[2:])<=0:
        raise ValueError('Invalid SVG viewBox')
    markers={}
    for item in root.attrib.get('data-pin-positions','').split('|'):
        if not item: continue
        number,position=item.rsplit('@',1); x,y=map(float,position.split(','))
        if not all(math.isfinite(v) for v in (x,y)): raise ValueError('Invalid SVG pin marker')
        markers.setdefault(number,[]).append([(x-view[0])*width/view[2],(y-view[1])*height/view[3]])
    pairs=[]
    for number,points in sorted(markers.items()):
        matches=[p for p in pads if p['number']==number and p['layers']]
        if len(points)==1 and len(matches)==1: pairs.append((points[0],matches[0]['xy']))
    candidates=[]
    if len(pairs)>=2:
        source=np.array([p[0] for p in pairs]); target=np.array([p[1] for p in pairs])
        for mirror in (1,-1):
            for degrees in (0,90,180,270):
                a=math.radians(degrees); c,s=round(math.cos(a)),round(math.sin(a))
                matrix=np.array([[c*mirror,-s],[s*mirror,c]])
                shifted=source@matrix.T
                translation=np.mean(target-shifted,axis=0)
                residual=float(np.sqrt(np.mean(np.sum((shifted+translation-target)**2,axis=1))))
                candidates.append((round(residual,9),mirror==-1,degrees,matrix,translation))
    if candidates:
        candidates.sort(key=lambda p:p[:3]); error,mirror,degrees,matrix,translation=candidates[0]
        fit={'status':'artwork_orientation_fit_not_copper','rms_terminal_to_land_mm':error,
             'matched_pin_numbers':len(pairs),'mirror':mirror,'degrees':degrees,
             'ambiguous_equivalent_fits':sum(abs(p[0]-error)<1e-8 for p in candidates),
             'matrix':matrix.tolist(),'translation_mm':translation.tolist()}
    else:
        fit={'status':'unregistered_artwork_gallery_only','matched_pin_numbers':len(pairs)}
    return {'width_mm':width,'height_mm':height,'view_box':view,'fit':fit,
            'sha256':hashlib.sha256(raw).hexdigest(),'xml':ET.tostring(root,encoding='unicode')}


def load_model(*, bundle, svg_roots):
    bundle=Path(bundle).resolve(); hashes={}
    def read(relative):
        path=bundle/relative; hashes[relative]=sha256(path=path)
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    board=read('pcb/board.yaml')
    if board.get('copper_layers')!=['F.Cu','B.Cu']:
        raise ValueError('SVG prototype planner requires the explicit two-layer board')
    w,h=board['width_mm'],board['height_mm']
    if board['outline_vertices_mm']!=[[0,0],[w,0],[w,h],[0,h]]:
        raise ValueError('Planner v1 requires a rectangular outline; cannot ignore cutouts')
    placements=read('pcb/placement.yaml')['placements']
    electrical=read('schematic/nets.yaml')
    manifest=read('sources/manifest.yaml')
    packaged_manifest=read('schematic/manifest.yaml')['files']
    plan=read('mcp_plan.yaml')
    design_rules={}
    for operation in plan['operations']:
        if operation['action']=='design_rules_configure': design_rules.update(operation['inputs'])
    sources={x['oomp_id']:x for x in manifest['sources']}
    net_by_endpoint={}
    for net in electrical['nets']:
        for end in net['endpoints']:
            key=(end['reference'],str(end['pin_number']))
            if key in net_by_endpoint and net_by_endpoint[key]!=net['name']:
                raise ValueError(f'Endpoint belongs to conflicting nets: {key}')
            net_by_endpoint[key]=net['name']
    components=[]; assets=[]
    for item in placements:
        if item['resolution_status']!='ready': raise ValueError('Unresolved placement input')
        source=item['source_id']; entry=sources[source]; kind=entry['selected_footprint_kind']
        if not SOURCE_ID_PATTERN.fullmatch(source) or kind not in ('machine_solder','hand_solder'):
            raise ValueError('Invalid source id or unresolved selected footprint kind')
        library='OOMP_MachineSolder.pretty' if kind=='machine_solder' else 'OOMP_HandSolder.pretty'
        relative=f'libraries/{library}/{source}.kicad_mod'; path=bundle/relative
        hashes[relative]=sha256(path=path)
        selected=[a for a in entry['assets'] if a['kind']==kind and a['valid']]
        if len(selected)!=1 or packaged_manifest.get(relative)!=hashes[relative]:
            raise ValueError(f'Frozen footprint differs from selected source manifest: {source}')
        component={**copy.deepcopy(item),**read_footprint(path=path)}
        # Validate side even if the optimizer never moves this component.
        transform(xy=[0,0],placement=item['pcb'])
        for pad in component['pads']:
            pad['net']=net_by_endpoint.get((item['reference'],pad['number']))
        svg=next((Path(root)/source/'data/working_svg_assembly.svg' for root in svg_roots
                  if (Path(root)/source/'data/working_svg_assembly.svg').is_file()),None)
        component['artwork']=read_svg(path=svg,pads=component['pads']) if svg else None
        assets.append({'source_id':source,'reference':item['reference'],
                       'svg_path':str(svg.resolve()) if svg else None,
                       'svg_sha256':component['artwork']['sha256'] if svg else None,
                       'status':'imported' if svg else 'exact_footprint_preview_fallback',
                       'fit':component['artwork']['fit'] if svg else None})
        components.append(component)
    refs={c['reference']:c for c in components}
    for ref,pin in net_by_endpoint:
        if ref not in refs or not any(p['number']==pin and p['layers'] for p in refs[ref]['pads']):
            raise ValueError(f'Electrical endpoint has no source copper pad: {ref}.{pin}')
    nc={(r['endpoint']['reference'],str(r['endpoint']['pin_number'])) for r in electrical['no_connects']}
    if nc & set(net_by_endpoint): raise ValueError('Explicit no-connect appears on a connected net')
    return {'board':board,'components':components,'electrical':electrical,'input_hashes':hashes,'assets':assets,
            'design_rules':design_rules}


def placed_pads(*, model, placements):
    output=[]
    for component in model['components']:
        ref=component['reference']; placement=placements[ref]
        for pad in component['pads']:
            output.append({**pad,'reference':ref,'instance_id':component['instance_id'],
                           'xy':transform(xy=pad['xy'],placement=placement),
                           'rotation':pad['rotation']+placement['rotation_degrees']})
    return output


def spanning_edges(*, pads):
    """Stable physical-pad MST: duplicate-number lands remain separate vertices."""
    ordered=sorted(pads,key=lambda p:(p['reference'],p['number'],p['index']))
    if len(ordered)<2: return []
    reached={0}; edges=[]
    while len(reached)<len(ordered):
        _,a,b=min((math.dist(ordered[i]['xy'],ordered[j]['xy']),i,j)
                  for i in sorted(reached) for j in range(len(ordered)) if j not in reached)
        reached.add(b)
        # Coincident same-net copper on a common layer already touches. Retain
        # both lands in the pad model, but don't invent a zero-length track.
        if ordered[a]['xy']==ordered[b]['xy'] and set(ordered[a].get('layers',[])) & set(ordered[b].get('layers',[])):
            continue
        edges.append((ordered[a],ordered[b]))
    return edges
