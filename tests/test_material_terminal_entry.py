"""Synthetic terminal-entry cases; no customer examples or dimensions."""
import copy

import pytest
from cadquote.material_branches import discover_material_leader_branches
from test_material_branches import entity, fixture


def setup():
    entities, occurrences = fixture()
    entities = entities[:3]
    entities[-1].geometry['vertices'][-1] = [3, 6]
    return entities, occurrences


def branch_result(entities, occurrences, **kwargs):
    return discover_material_leader_branches(entities, occurrences, allow_terminal_entry=True,
                                            **kwargs)


def test_terminal_entry_is_opt_in_and_does_not_mutate_or_confirm():
    entities, occurrences = setup()
    before = copy.deepcopy((entities, occurrences))
    assert not discover_material_leader_branches(entities, occurrences)['records'][0]['branches']
    result = branch_result(entities, occurrences)
    branch = result['records'][0]['branches'][0]
    assert branch['geometry_routing_eligible']
    assert branch['connection_basis'] == 'NATIVE_TERMINAL_SEGMENT_ENTERS_ANNOTATION'
    assert branch['terminal_border_intersection'] == [0, 6]
    assert branch['landing_point'] == [3, 6]
    assert branch['state'] == 'REVIEW' and branch['physical_quantity'] is None
    assert branch['measurement_role'] is None and not result['mutates_takeoff']
    assert before == (entities, occurrences)


@pytest.mark.parametrize('vertices', [
    [[-20, 6], [-0.01, 6]],   # Near but outside: no extension.
    [[2, 6], [3, 6]],         # Both inside: no border crossing.
    [[0, 6], [3, 6]],         # Starts on border, not a directed entry from outside.
    [[2, 6], [-20, 6], [3, 6]],  # Earlier path exits and re-enters.
    [[-20, 6], [45, 6]],      # Passes through and ends outside.
    [[-20, 20], [3, 20]],     # Entire segment misses the label.
    [[-20, 6], [3, 6, 1]],    # Non-planar indexed coordinates.
])
def test_invalid_entry_never_connects(vertices):
    entities, occurrences = setup()
    entities[-1].geometry['vertices'] = vertices
    assert not branch_result(entities, occurrences)['records'][0]['branches']


@pytest.mark.parametrize('mode', ['overlap', 'terminal_crossing', 'attachment', 'target'])
def test_competing_or_conflicting_entry_is_not_eligible(mode):
    entities, occurrences = setup()
    if mode in ('overlap', 'terminal_crossing'):
        points = ([[0, 0], [40, 0], [40, 12], [0, 12]] if mode == 'overlap' else
                  [[-12, 0], [-8, 0], [-8, 12], [-12, 12]])
        entities.append(entity('other', 'INSERT', geometry={'annotation_boundary': points}))
    elif mode == 'attachment':
        entities[-1].geometry['annotation_handle'] = 'other'
    else:
        entities[-1].geometry['leader_target'] = [-99, 6]
    branches = branch_result(entities, occurrences)['records'][0]['branches']
    assert branches and not any(b['geometry_routing_eligible'] for b in branches)


@pytest.mark.parametrize('field,value', [('source_file_id', 'other'), ('sheet_id', 'other'),
                                        ('space', 'paper:other')])
def test_entry_keeps_native_scope(field, value):
    entities, occurrences = setup()
    entities[-1] = entities[-1].model_copy(update={field: value})
    assert not branch_result(entities, occurrences)['records'][0]['branches']


@pytest.mark.parametrize('transform', ['translate', 'mirror', 'rotate'])
def test_entry_is_coordinate_invariant(transform):
    entities, occurrences = setup()
    def move(p):
        x, y = p
        return [x + 12345, y - 54321] if transform == 'translate' else (
            [-x, y] if transform == 'mirror' else [-y, x])
    entities[0].geometry['annotation_boundary'] = [move(p) for p in
                                                   entities[0].geometry['annotation_boundary']]
    entities[-1].geometry['vertices'] = [move(p) for p in entities[-1].geometry['vertices']]
    result = branch_result(entities, occurrences)
    branch = result['records'][0]['branches'][0]
    assert branch['geometry_routing_eligible']
    assert branch['terminal_border_intersection'] == move([0, 6])
    assert result == branch_result(list(reversed(entities)), occurrences)


def test_entry_caps_retain_incomplete_result():
    entities, occurrences = setup()
    entities.append(entity('other', 'INSERT', geometry={
        'annotation_boundary': [[80, 0], [90, 0], [90, 12], [80, 12]]}))
    result = branch_result(entities, occurrences, max_contact_checks=1)
    assert result['truncated']
    assert not any(b['geometry_routing_eligible'] for r in result['records'] for b in r['branches'])


@pytest.mark.parametrize('value', ['true', 1, None])
def test_entry_flag_requires_boolean(value):
    entities, occurrences = setup()
    with pytest.raises(ValueError, match='boolean'):
        discover_material_leader_branches(entities, occurrences, allow_terminal_entry=value)
