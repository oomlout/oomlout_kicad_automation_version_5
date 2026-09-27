"""Design-time planning only: SVG import -> placement search -> draft routes.

Normal board compilation never invokes this optimizer. No board file is edited,
no MCP connection is opened and no active Python board definition is rewritten.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import pprint

from .layout_model import load_model, sha256
from .layout_search import optimize, route_all
from .layout_preview import render
from .layout_checks import check_draft


DEFAULTS={
    'fixed_references':[], 'keepouts':[], 'net_weights':{'GND':.2,'+3V3':.4},
    'compactness_weight':.025, 'movement_weight':.03, 'component_gap_mm':.35,
    'edge_margin_mm':.5, 'max_move_mm':3.0, 'placement_steps_mm':[1.0,.5,.25],
    'placement_sweeps':2, 'allow_rotation':False, 'routing_grid_mm':.15,
    'track_width_mm':.25, 'clearance_mm':.2, 'via_diameter_mm':.6,
    'via_drill_mm':.3, 'drill_gap_mm':.25, 'via_cost_mm':4.0,
    'max_expansions_per_route':60000, 'net_priority':[],
    'proximity_limits':[], 'bus_groups':[], 'bus_cross_axis_cost':1.1,
    'bottom_route_cost':1.2, 'ground_pour_nets':[],
    'dense_package_apron_mm':.8,
    'reserve_dense_fanouts':True,'reserve_escape_length_mm':1.2,
    'routing_passes':2,'retry_via_cost_factor':.6,
    'layer_preferred_axes':{'F.Cu':'x','B.Cu':'y'},'layer_cross_axis_cost':1.35,
    'placement_relationships':[],
    'placement_groups':[],
    'reserve_escape_min_mm':.6,'fanout_candidates_per_pin':6,'fanout_search_nodes':5000,
    'local_repair_rounds':2,'congestion_history_cost':.5,'congestion_radius_mm':2.0,
    'escape_distances_mm':[0,.3,.6,.9,1.2,1.8,2.4,3.6],
    'placement_feedback_rounds':1,'placement_feedback_cost':4.0,
    'adaptive_grid_alignment':True,
}


def validated_settings(*, command):
    unexpected=set(command)-{'output','svg_roots','settings'}
    if unexpected: raise ValueError(f'Unknown layout command fields: {sorted(unexpected)}')
    supplied=command.get('settings',{})
    if set(supplied)-set(DEFAULTS): raise ValueError('Unknown layout settings')
    settings={**copy.deepcopy(DEFAULTS),**copy.deepcopy(supplied)}
    for key,value in settings.items():
        if key.endswith('_mm') or key in ('compactness_weight','movement_weight','bus_cross_axis_cost','bottom_route_cost','layer_cross_axis_cost','congestion_history_cost'):
            if key in ('placement_steps_mm','escape_distances_mm'): continue
            if type(value) not in (float,int) or not math.isfinite(value) or value<=0:
                raise ValueError(f'{key} must be positive finite')
    for key in ('placement_sweeps','max_expansions_per_route','routing_passes'):
        if type(settings[key])!=int or settings[key]<1: raise ValueError(f'{key} must be positive integer')
    if type(settings['allow_rotation'])!=bool: raise ValueError('allow_rotation must be a boolean')
    if type(settings['reserve_dense_fanouts'])!=bool: raise ValueError('reserve_dense_fanouts must be a boolean')
    if type(settings['adaptive_grid_alignment'])!=bool: raise ValueError('adaptive_grid_alignment must be a boolean')
    if settings['routing_passes']>5: raise ValueError('At most five bounded routing passes')
    for key,lo,hi in (('fanout_candidates_per_pin',1,16),('fanout_search_nodes',1,100000),('local_repair_rounds',0,8),('placement_feedback_rounds',0,3)):
        if type(settings[key])!=int or not lo<=settings[key]<=hi: raise ValueError(f'{key} outside bounded integer range')
    if settings['reserve_escape_min_mm']>settings['reserve_escape_length_mm']: raise ValueError('Minimum escape exceeds preferred length')
    distances=settings['escape_distances_mm']
    if not isinstance(distances,list) or not distances or len(distances)>20 or any(type(v) not in (float,int) or not math.isfinite(v) or v<0 or v>10 for v in distances) or sorted(set(distances))!=distances:
        raise ValueError('Escape distances require at most twenty ascending finite distances in 0..10 mm')
    if type(settings['placement_feedback_cost']) not in (float,int) or not math.isfinite(settings['placement_feedback_cost']) or settings['placement_feedback_cost']<=0:
        raise ValueError('Placement feedback cost must be positive finite')
    groups=settings['placement_groups']
    if not isinstance(groups,list): raise ValueError('Placement groups must be reference lists')
    for group in groups:
        if not isinstance(group,list) or len(group)<2 or any(not isinstance(r,str) or not r for r in group) or len(set(group))!=len(group):
            raise ValueError('Placement group needs at least two unique reference names')
    factor=settings['retry_via_cost_factor']
    if type(factor) not in (int,float) or not math.isfinite(factor) or not 0<factor<=1:
        raise ValueError('Retry via cost factor must be positive and at most one')
    for key in ('fixed_references','net_priority','ground_pour_nets'):
        if not isinstance(settings[key],list) or any(not isinstance(v,str) or not v for v in settings[key]):
            raise ValueError(f'{key} must be a list of names')
    if not isinstance(settings['net_weights'],dict) or any(type(v) not in (float,int) or not math.isfinite(v) or v<=0 for v in settings['net_weights'].values()):
        raise ValueError('Net weights must be positive finite')
    if not settings['placement_steps_mm'] or any(type(v) not in (float,int) or not math.isfinite(v) or v<=0 for v in settings['placement_steps_mm']):
        raise ValueError('Placement steps must be positive finite numbers')
    if settings['via_drill_mm']>=settings['via_diameter_mm']: raise ValueError('Via drill must be smaller than copper')
    if settings['routing_grid_mm']<.02: raise ValueError('Grid below .02 mm exceeds this bounded design-time planner')
    if min(settings['bottom_route_cost'],settings['bus_cross_axis_cost'],settings['layer_cross_axis_cost'])<1:
        raise ValueError('Routing cost multipliers must be at least one')
    axes=settings['layer_preferred_axes']
    if not isinstance(axes,dict) or set(axes)!={'F.Cu','B.Cu'} or set(axes.values())!={'x','y'}:
        raise ValueError('Two copper layers require opposite preferred axes x/y')
    from .layout_relationships import validate_relationships
    validate_relationships(settings=settings)
    if len(settings['ground_pour_nets'])>1: raise ValueError('This planner supports one ground pour net, not overlapping power planes')
    for pair in settings['proximity_limits']:
        if set(pair)!={'references','max_distance_mm'} or len(pair['references'])!=2 or len(set(pair['references']))!=2:
            raise ValueError('Proximity limit requires two different references and max_distance_mm')
        value=pair['max_distance_mm']
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError('Proximity distance must be positive finite')
    seen=set()
    for group in settings['bus_groups']:
        if set(group)!={'name','nets','preferred_axis'} or not group['name'] or group['preferred_axis'] not in ('x','y') or not group['nets']:
            raise ValueError('Bus group requires name, ordered nets and preferred_axis x/y')
        for net in group['nets']:
            if not isinstance(net,str) or not net or net in seen: raise ValueError('Bus nets must be unique nonempty names')
            seen.add(net)
    for region in settings['keepouts']:
        if set(region)-{'name','bounds_mm','allowed_nets'} or 'bounds_mm' not in region: raise ValueError('Invalid keepout dictionary')
        box=region['bounds_mm']
        if len(box)!=4 or not all(type(v) in (int,float) and math.isfinite(v) for v in box) or box[0]>=box[1] or box[2]>=box[3]:
            raise ValueError('Keepout requires [xmin,xmax,ymin,ymax]')
    return settings


def plan_layout(*, board, command):
    """Public dictionary action. ``board`` names a frozen input bundle."""
    if not isinstance(command,dict): raise TypeError('command must be a dictionary')
    if not isinstance(board,dict) or set(board)!={'bundle_path'}:
        raise ValueError('Design-time board requires only bundle_path, not mutable authoring state')
    settings=validated_settings(command=command)
    bundle=Path(board['bundle_path']).resolve(); output=Path(command['output']).resolve()
    if output.exists(): raise FileExistsError('Layout output must be a new directory; previous work is preserved')
    if output==bundle or output.is_relative_to(bundle): raise ValueError('Layout proposals must be outside the compiled bundle')
    roots=[Path(p).resolve() for p in command['svg_roots']]
    if not roots or any(not p.is_dir() for p in roots): raise ValueError('Explicit existing SVG source roots required')
    if any(output==p or output.is_relative_to(p) for p in roots): raise ValueError('Never write into OOMP SVG source roots')
    print('Loading exact selected footprints, frozen nets and SVG artwork',flush=True)
    model=load_model(bundle=bundle,svg_roots=roots)
    nets={n['name'] for n in model['electrical']['nets']}
    if set(settings['net_priority']+[n for g in settings['bus_groups'] for n in g['nets']])-nets:
        raise ValueError('Priority/bus nets must exist in the frozen schematic')
    if (1+model['board']['width_mm']/settings['routing_grid_mm'])*(1+model['board']['height_mm']/settings['routing_grid_mm'])>2_000_000:
        raise ValueError('Requested board/grid exceeds the two-million-cell planner bound')
    minimum=max(model['design_rules'].get('minimum_clearance_mm',0),model['design_rules'].get('default_netclass_clearance_mm',0))
    if settings['clearance_mm']<minimum: raise ValueError('Planner clearance cannot relax the frozen project design rules')
    output.mkdir(parents=True,exist_ok=False)
    def log(message):
        print(message,flush=True)
        with (output/'actions.log').open('a',encoding='utf-8') as stream: stream.write(message+'\n')
    def write(name,data):
        (output/name).write_text(json.dumps(data,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    write('settings.json',settings)
    write('source_assets.json',model['assets'])
    log(f'Imported {sum(c["artwork"] is not None for c in model["components"])} SVGs for {len(model["components"])} instances')
    log('Optimizing bounded placements; fixed anchors, holes and board outline preserved')
    optimization=optimize(model=model,settings=settings)
    write('placements.json',optimization)
    log(f'Placement objective {optimization["score_before"]} -> {optimization["score_after"]}')
    routing=route_all(model=model,placements=optimization['after'],settings=settings,progress=log)
    from .layout_feedback import refine_placement
    optimization,routing,trials=refine_placement(model=model,settings=settings,optimization=optimization,routing=routing,progress=log)
    write('placement_feedback.json',trials)
    write('placements.json',optimization)
    checks=check_draft(model=model,placements=optimization['after'],routing=routing,settings=settings)
    write('draft_checks.json',checks)
    write('route_intents.json',routing['routes']); write('unresolved_connections.json',routing['unresolved'])
    write('copper_pour_intents.json',routing['copper_pours'])
    write('ground_plan.json',routing['ground_plan'])
    write('ground_access.json',{'before':routing['ground_access_before'],'after':routing['ground_access_after']})
    write('routing_passes.json',routing['routing_passes'])
    write('routing_repairs.json',routing['local_repairs'])
    write('congestion_history.json',routing['congestion_history'])
    write('routing_searches.json',routing['search_records'])
    write('fanout_reservations.json',{'reservations':routing['fanout_reservations'],
                                     'unavailable':routing['unavailable_fanout_reservations']})
    if not checks['passed']:
        write('report.json',{'status':'REJECTED_OFFLINE_GEOMETRY','checks':checks,'live_tests_run':False})
        raise ValueError('Independent draft geometry check rejected candidate; see draft_checks.json')
    # This is a draft dictionary module, never an automatic rewrite of accepted
    # source. Promotion requires review, endpoint verification and native DRC.
    placements=[]
    for component in model['components']:
        ref=component['reference']; p=optimization['after'][ref]
        placements.append({'reference':ref,'instance_id':component['instance_id'],
                           'command':{'pcb_x_mm':p['anchor_mm'][0],'pcb_y_mm':p['anchor_mm'][1],
                                      'pcb_rotation':p['rotation_degrees'],'pcb_side':p['side']}})
    draft='"""UNACCEPTED layout proposal. Review before copying into authoritative board Python.\nNever mix these placements with the old tracks; rebuild from an unrouted board.\n"""\n\n'
    draft+='PLACEMENT_COMMANDS = '+pprint.pformat(placements,sort_dicts=False,width=110)+'\n\n'
    draft+='ROUTE_COMMANDS = '+pprint.pformat(routing['routes'],sort_dicts=False,width=110)+'\n'
    draft+='\nCOPPER_POUR_COMMANDS = '+pprint.pformat(routing['copper_pours'],sort_dicts=False,width=110)+'\n'
    (output/'layout_candidate.py').write_text(draft,encoding='utf-8')
    log('Rendering SVG artwork and Matplotlib comparison')
    render(model=model,optimization=optimization,routing=routing,settings=settings,output=output)
    assets=output/'assets'; assets.mkdir()
    for component in model['components']:
        if component['artwork']:
            # Preserve the exact imported bytes once per source id.
            origin=next(a['svg_path'] for a in model['assets'] if a['source_id']==component['source_id'])
            (assets/(component['source_id']+'.svg')).write_bytes(Path(origin).read_bytes())
    changed=[p for p,digest in model['input_hashes'].items() if sha256(path=bundle/p)!=digest]
    changed.extend(a['svg_path'] for a in model['assets'] if a['svg_path'] and sha256(path=a['svg_path'])!=a['svg_sha256'])
    if changed: raise RuntimeError(f'Input changed while planning; reject proposal: {changed}')
    payload={'settings':settings,'input_hashes':model['input_hashes'],
             'svg_hashes':{a['source_id']:a['svg_sha256'] for a in model['assets']},
             'placements':optimization['after'],'routes':routing['routes'],'unresolved':routing['unresolved'],
             'copper_pours':routing['copper_pours']}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    report={'status':'PROPOSAL_ONLY','candidate_sha256':digest,'live_tests_run':False,
            'source_inputs_unchanged':True,'input_hashes':model['input_hashes'],
            'objective':'Non-pour HPWL + global spread + movement + explicit relationship distance/corridor costs; heuristic, not global optimum',
            'metrics_before':optimization['metrics_before'],'metrics_after':optimization['metrics_after'],
            'moved_references':sorted({m['reference'] for m in optimization['moves']}),
            'imported_svg_instances':sum(c['artwork'] is not None for c in model['components']),
            'footprint_fallback_instances':sum(c['artwork'] is None for c in model['components']),
            'route_summary':{k:v for k,v in routing.items() if k not in ('routes','unresolved','copper_pours','fanout_reservations','routing_passes','ground_plan','ground_access_before','ground_access_after','congestion_history','local_repairs','search_records')},
            'local_repair_summary':[{k:v for k,v in r.items() if k!='failure_details'} for r in routing['local_repairs']],
            'placement_relationships':optimization['relationships_after'],
            'placement_feedback_summary':[{k:v for k,v in t.items() if k not in ('placement','routes','unresolved','checks')} for t in trials],
            'layer_preferred_axes':settings['layer_preferred_axes'],
            'routing_angle_policy':'orthogonal_45 including pad escapes',
            'routing_pass_summary':[{k:v for k,v in p.items() if k!='failure_details'} for p in routing['routing_passes']],
            'ground_connectivity_status':'PENDING_NATIVE_FILL_AND_AUDIT' if routing['pending_pour_nets'] else 'NOT_REQUESTED',
            'copper_pour_count':len(routing['copper_pours']),
            'proposed_links':len(routing['routes']),'unresolved_links':len(routing['unresolved']),
            'required_before_promotion':['Visual/circuit placement review','Fresh Konnect schematic/NC parity and unrouted PCB',
                'Exact live pad endpoint gate','Native zone refill and saved pour geometry verification',
                'Saved footprint/placement/layer/route verification',
                'Native exhaustive electrical audit and retained DRC/ERC','Two clean accepted replays'],
            'limitations':['Assembly drawings are not copper geometry; registration residuals retained',
                'Front-side components and rectangular boards only','Conservative rectangular pad envelopes',
                'Fixed outline: global packing does not shrink OOBB mechanics',
                'Bus ordering/axis and via cost are preferences, not matched-pair or minimum-via guarantees',
                'Pour polygons are unfilled intent, not proven ground connectivity; native audit may require stitching',
                'Old copper deliberately excluded: no mixing moved components with stale tracks',
                'No manufacturing, RF, impedance or routed-board success claimed']}
    write('report.json',report)
    log(f'PROPOSAL_ONLY: {len(routing["routes"])} candidate links; {len(routing["unresolved"])} unresolved; no live acceptance')
    return report
