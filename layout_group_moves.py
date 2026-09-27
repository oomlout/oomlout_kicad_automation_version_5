"""Coordinated legal translations keep nearby support circuits with their IC."""
import copy


def move_groups(*, groups, current, step, score, legal, best):
    moves=[]
    for group in groups:
        before={ref:copy.deepcopy(current[ref]) for ref in group}
        chosen=before; chosen_score=best
        for dx,dy in ((-1,0),(0,-1),(0,1),(1,0),(-1,-1),(-1,1),(1,-1),(1,1)):
            for ref,p in before.items():
                current[ref]={**p,'anchor_mm':[round(p['anchor_mm'][0]+dx*step,6),round(p['anchor_mm'][1]+dy*step,6)]}
            if all(legal(ref) for ref in group):
                value=score()
                if value<chosen_score-1e-8:
                    chosen={ref:copy.deepcopy(current[ref]) for ref in group}; chosen_score=value
        current.update(chosen)
        for ref in group:
            if chosen[ref]!=before[ref]:
                moves.append({'reference':ref,'from':before[ref],'to':chosen[ref],
                              'score':round(chosen_score,6),'coordinated_group':list(group)})
        best=chosen_score
    return best,moves
