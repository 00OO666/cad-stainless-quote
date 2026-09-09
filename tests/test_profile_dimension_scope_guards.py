"""Synthetic guards against borrowing a substrate dimension for a metal profile."""

import copy

import ezdxf
import pytest
from cadquote.group_materials import native_group_dimension_record
from cadquote.profile_dimensions import bind_boundary_dimensions
from ezdxf import bbox

TRANSFORMS = [
    pytest.param(1, 1, (0, 0), id="identity"),
    pytest.param(1, 1, (137, -211), id="translated"),
    pytest.param(-1, 1, (0, 0), id="mirror-x"),
    pytest.param(1, -1, (0, 0), id="mirror-y"),
    pytest.param(-1, -1, (137, -211), id="mirrored-and-translated"),
]


def _adjacent_profiles(sx, sy, offset, reverse_profile=False, reverse_dimension=False):
    # Invented raw-unit coordinates: the recess and the metal have a common
    # horizontal edge, but their vertical legs occupy opposite intervals.
    substrate = [(17, -23), (17, -10), (91, -10), (91, -23)]
    metal = [(17, -3), (17, -10), (91, -10), (91, -3)]

    def transform(point):
        return (sx * point[0] + offset[0], sy * point[1] + offset[1])

    doc = ezdxf.new()
    dimensions = {}

    def dimension(name, first, second, angle):
        first, second = transform(first), transform(second)
        if reverse_dimension:
            first, second = second, first
        native = doc.modelspace().add_linear_dim(
            base=transform((111, 29)), p1=first, p2=second, angle=angle
        )
        native.render()
        entity = native_group_dimension_record(
            native.dimension, "synthetic-source", "synthetic-section", "model", bbox.Cache()
        )
        dimensions[name] = {"entity": entity.model_dump(mode="json"), "coordinate_space": "model"}

    dimension("shared_width", substrate[1], substrate[2], 0)
    dimension("substrate_depth", substrate[0], substrate[1], 90)
    dimension("metal_leg", metal[0], metal[1], 90)
    # Same length as the metal leg, but neither origin belongs to that profile.
    dimension("foreign_equal_length", (17, -103), (17, -110), 90)
    substrate, metal = [transform(p) for p in substrate], [transform(p) for p in metal]
    if reverse_profile:
        substrate.reverse()
        metal.reverse()
    return substrate, metal, dimensions


def _bound_names(result, dimensions):
    names = {record["entity"]["handle"]: name for name, record in dimensions.items()}
    return {names[proof["dimension_handle"]] for proof in result["bindings"]}


@pytest.mark.parametrize("sx,sy,offset", TRANSFORMS)
@pytest.mark.parametrize(
    "reverse_profile", [False, True], ids=["forward-profile", "reverse-profile"]
)
@pytest.mark.parametrize("reverse_dimension", [False, True], ids=["forward-dim", "reverse-dim"])
def test_shared_horizontal_width_does_not_lend_substrate_depth_to_metal(
    sx, sy, offset, reverse_profile, reverse_dimension
):
    substrate, metal, dimensions = _adjacent_profiles(
        sx, sy, offset, reverse_profile, reverse_dimension
    )
    selected = [dimensions["shared_width"], dimensions["substrate_depth"]]
    frozen = copy.deepcopy((substrate, metal, selected))

    substrate_result = bind_boundary_dimensions(substrate, selected)
    metal_result = bind_boundary_dimensions(metal, selected)

    assert _bound_names(substrate_result, dimensions) == {"shared_width", "substrate_depth"}
    assert _bound_names(metal_result, dimensions) == {"shared_width"}
    assert len(metal_result["bindings"]) == 1
    assert metal_result["bindings"][0]["segment_length_raw"] == pytest.approx(74)
    assert metal_result["conflicts"] == []
    assert metal_result["ambiguous_segment_bindings"] == []
    assert (substrate, metal, selected) == frozen


@pytest.mark.parametrize("sx,sy,offset", TRANSFORMS)
@pytest.mark.parametrize(
    "reverse_profile", [False, True], ids=["forward-profile", "reverse-profile"]
)
def test_each_vertical_dimension_remains_with_its_own_boundary(sx, sy, offset, reverse_profile):
    substrate, metal, dimensions = _adjacent_profiles(sx, sy, offset, reverse_profile)
    selected = list(dimensions.values())

    substrate_result = bind_boundary_dimensions(substrate, selected)
    metal_result = bind_boundary_dimensions(metal, selected)

    assert _bound_names(substrate_result, dimensions) == {"shared_width", "substrate_depth"}
    assert _bound_names(metal_result, dimensions) == {"shared_width", "metal_leg"}
    assert sorted(b["segment_length_raw"] for b in substrate_result["bindings"]) == pytest.approx(
        [13, 74]
    )
    assert sorted(b["segment_length_raw"] for b in metal_result["bindings"]) == pytest.approx(
        [7, 74]
    )
    for result in (substrate_result, metal_result):
        assert result["conflicts"] == []
        assert result["ambiguous_segment_bindings"] == []


@pytest.mark.parametrize("sx,sy,offset", TRANSFORMS)
def test_equal_numeric_length_without_boundary_origins_is_not_a_binding(sx, sy, offset):
    _, metal, dimensions = _adjacent_profiles(sx, sy, offset)
    foreign = dimensions["foreign_equal_length"]
    assert foreign["entity"]["value"] == pytest.approx(dimensions["metal_leg"]["entity"]["value"])

    result = bind_boundary_dimensions(metal, [foreign])

    assert result == {"bindings": [], "conflicts": [], "ambiguous_segment_bindings": []}
