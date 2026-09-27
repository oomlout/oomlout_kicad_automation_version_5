"""SVG assembly artwork and Matplotlib engineering previews of a layout draft."""
from __future__ import annotations
import copy
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .layout_model import bounds, placed_pads, spanning_edges
from .layout_search import net_groups


def render(*, model, optimization, routing, settings, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Circle, Polygon
    from matplotlib.transforms import Affine2D
    plt.rcParams['svg.hashsalt']='oomp-layout-v1'
    plt.rcParams['font.family']='DejaVu Sans'
    w,h=model['board']['width_mm'],model['board']['height_mm']
    fig,axes=plt.subplots(1,2,figsize=(16,7.8),layout='constrained')
    for ax,key,title in zip(axes,('before','after'),('Authored baseline / airwires','Optimized candidate / proposed copper')):
        placements=optimization[key]; pads=placed_pads(model=model,placements=placements)
        ax.add_patch(Rectangle((0,0),w,h,facecolor='#f4f7f3',edgecolor='#314b47',linewidth=1.2))
        for region in settings['keepouts']:
            a,b,c,d=region['bounds_mm']
            ax.add_patch(Rectangle((a,c),b-a,d-c,facecolor='#f1e3cf',hatch='///',alpha=.5))
        if key=='before':
            for group in net_groups(pads=pads).values():
                for a,b in spanning_edges(pads=group):
                    ax.plot([a['xy'][0],b['xy'][0]],[a['xy'][1],b['xy'][1]],color='#96a8ad',alpha=.3,lw=.5,zorder=1)
        else:
            for pour in routing.get('copper_pours',[]):
                if pour['layer']=='B.Cu':
                    ax.add_patch(Polygon([[p['x'],p['y']] for p in pour['points_mm']],
                        facecolor='none',edgecolor='#b8cac3',hatch='..',lw=.3,zorder=1))
            for pending in routing['unresolved']:
                a,b=pending['start_mm'],pending['end_mm']
                ax.plot([a[0],b[0]],[a[1],b[1]],color='#b58f5e',alpha=.25,lw=.5,ls='--',zorder=1)
            for route in routing['routes']:
                for a,b,layer in zip(route['points_mm'],route['points_mm'][1:],route['segment_layers']):
                    ax.plot([a['x'],b['x']],[a['y'],b['y']],color='#c84842' if layer=='F.Cu' else '#356dab',lw=.8,zorder=2)
                for via in route['vias']:
                    ax.add_patch(Circle((via['x'],via['y']),via['diameter_mm']/2,facecolor='#c89a38',edgecolor='#4d493a',lw=.4))
        for component in model['components']:
            ref=component['reference']; a,b,c,d=bounds(component=component,placement=placements[ref])
            color='#46636a' if ref in optimization['fixed_references'] else '#589474'
            ax.add_patch(Rectangle((a,c),b-a,d-c,facecolor='white',alpha=.45,edgecolor=color,lw=.65,zorder=3))
            ax.text((a+b)/2,c-.35,ref,ha='center',va='bottom',fontsize=6,color='#263d41',zorder=6)
        for pad in pads:
            x,y=pad['xy']; sx,sy=pad['size']
            patch=Rectangle((x-sx/2,y-sy/2),sx,sy,facecolor='#dabf78',edgecolor='#6d6245',lw=.35,zorder=4)
            patch.set_transform(Affine2D().rotate_deg_around(x,y,-pad['rotation'])+ax.transData); ax.add_patch(patch)
            if pad['drill']: ax.add_patch(Circle((x,y),min(pad['drill'])/2,facecolor='white',edgecolor='#625e51',lw=.3,zorder=5))
        ax.set(xlim=(-3,w+3),ylim=(h+3,-4),aspect='equal',xlabel='mm',ylabel='mm',title=title)
        ax.grid(alpha=.12)
    axes[1].set_title(f'Candidate: {len(routing["routes"])} proposed / {len(routing["unresolved"])} unresolved links')
    axes[1].set_xlabel('mm | red: F.Cu   blue: B.Cu   dashed: unresolved\nDotted ground regions are UNFILLED INTENT, not connected copper')
    fig.suptitle('OOMP visual layout study — unaccepted proposals, not a routed-board sign-off',fontsize=14)
    fig.savefig(Path(output)/'layout_comparison.png',dpi=180)
    fig.savefig(Path(output)/'layout_comparison.svg',metadata={'Date':None})
    plt.close(fig)
    for key in ('before','after'):
        write_assembly_svg(model=model,placements=optimization[key],routes=routing['routes'] if key=='after' else [],
                           unresolved=routing['unresolved'] if key=='after' else [],settings=settings,
                           pours=routing.get('copper_pours',[]) if key=='after' else [],
                           output=Path(output)/f'assembly_{key}.svg')


def write_assembly_svg(*, model, placements, routes, unresolved, settings, output, pours=()):
    w,h=model['board']['width_mm'],model['board']['height_mm']
    root=ET.Element('svg',{'xmlns':'http://www.w3.org/2000/svg','width':f'{w+8}mm','height':f'{h+10}mm',
                           'viewBox':f'-4 -6 {w+8} {h+10}'})
    ET.SubElement(root,'rect',{'x':'-4','y':'-6','width':str(w+8),'height':str(h+10),'fill':'white'})
    ET.SubElement(root,'rect',{'x':'0','y':'0','width':str(w),'height':str(h),'fill':'#f4f7f3','stroke':'#314b47','stroke-width':'.2'})
    ET.SubElement(root,'text',{'x':'0','y':'-4','font-size':'1.5','font-family':'sans-serif'}).text=f'DRAFT — {len(routes)} proposed / {len(unresolved)} unresolved links; live DRC required'
    if pours:
        ET.SubElement(root,'text',{'x':'0','y':'-2','font-size':'1.2','font-family':'sans-serif'}).text='Dashed green: UNFILLED GND boundaries; connectivity pending native fill/audit'
        for pour in pours:
            if pour['layer']=='B.Cu':
                ET.SubElement(root,'polygon',{'points':' '.join(f"{p['x']},{p['y']}" for p in pour['points_mm']),
                    'fill':'none','stroke':'#9fb9aa','stroke-width':'.12','stroke-dasharray':'.5 .5'})
    for region in settings['keepouts']:
        a,b,c,d=region['bounds_mm']
        ET.SubElement(root,'rect',{'x':str(a),'y':str(c),'width':str(b-a),'height':str(d-c),
                                  'fill':'#ebd6b6','fill-opacity':'.35','stroke':'#b58f5e','stroke-width':'.1','stroke-dasharray':'.5 .5'})
    for pending in unresolved:
        a,b=pending['start_mm'],pending['end_mm']
        ET.SubElement(root,'line',{'x1':str(a[0]),'y1':str(a[1]),'x2':str(b[0]),'y2':str(b[1]),
                                  'stroke':'#b58f5e','opacity':'.3','stroke-width':'.07','stroke-dasharray':'.4 .3'})
    for route in routes:
        for a,b,layer in zip(route['points_mm'],route['points_mm'][1:],route['segment_layers']):
            ET.SubElement(root,'line',{'x1':str(a['x']),'y1':str(a['y']),'x2':str(b['x']),'y2':str(b['y']),
                                      'stroke':'#c84842' if layer=='F.Cu' else '#356dab','stroke-width':str(route['width_mm'])})
        for via in route['vias']:
            ET.SubElement(root,'circle',{'cx':str(via['x']),'cy':str(via['y']),'r':str(via['diameter_mm']/2),'fill':'#d5b66e'})
    for component in model['components']:
        ref=component['reference']; placement=placements[ref]; x,y=placement['anchor_mm']
        group=ET.SubElement(root,'g',{'id':ref,'transform':f'translate({x} {y}) rotate({-placement["rotation_degrees"]})'})
        a,b,c,d=component['bounds_mm']
        ET.SubElement(group,'rect',{'x':str(a),'y':str(c),'width':str(b-a),'height':str(d-c),
                                  'fill':'none','stroke':'#648b80','stroke-width':'.09','stroke-dasharray':'.3 .2'})
        art=component['artwork']
        if art and 'matrix' in art['fit']:
            fit=art['fit']; m=fit['matrix']; tx,ty=fit['translation_mm']
            art_group=ET.SubElement(group,'g',{'opacity':'.6','transform':f'matrix({m[0][0]} {m[1][0]} {m[0][1]} {m[1][1]} {tx} {ty})'})
            imported=ET.fromstring(art['xml'])
            imported.set('x','0'); imported.set('y','0')
            imported.set('width',str(art['width_mm'])); imported.set('height',str(art['height_mm']))
            # Namespaced source nodes remain self-contained; strip duplicate ids.
            for node in imported.iter(): node.attrib.pop('id',None)
            art_group.append(imported)
        for pad in component['pads']:
            xx,yy=pad['xy']; sx,sy=pad['size']
            pg=ET.SubElement(group,'g',{'transform':f'translate({xx} {yy}) rotate({-pad["rotation"]})'})
            ET.SubElement(pg,'rect',{'x':str(-sx/2),'y':str(-sy/2),'width':str(sx),'height':str(sy),
                                    'fill':'#d4af59','fill-opacity':'.55','stroke':'#675224','stroke-width':'.07'})
            if pad['drill']:
                ET.SubElement(pg,'ellipse',{'cx':'0','cy':'0','rx':str(pad['drill'][0]/2),'ry':str(pad['drill'][1]/2),'fill':'white'})
        a,b,c,d=bounds(component=component,placement=placement)
        ET.SubElement(root,'text',{'x':str((a+b)/2),'y':str(c-.4),'font-size':'1.05','text-anchor':'middle','font-family':'sans-serif'}).text=ref
    ET.ElementTree(root).write(output,encoding='utf-8',xml_declaration=True)
