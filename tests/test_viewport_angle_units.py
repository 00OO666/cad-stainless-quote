import math

import ezdxf
import pytest
from cadquote.models import CadEntity
from cadquote.panels import _PaperToModelTransform


@pytest.mark.parametrize("angle", [0, 360, -360, 720])
def test_whole_degree_turn_matches_native_transform(angle):
    doc = ezdxf.new("R2018")
    native = doc.layout("Layout1").add_viewport(
        center=(0, 0), size=(10, 10), view_center_point=(0, 0), view_height=100
    )
    native.dxf.view_twist_angle = angle
    vp = CadEntity(
        id="vp",
        source_file_id="synthetic",
        handle=native.dxf.handle,
        entity_type="VIEWPORT",
        space="paper:Layout1",
        bbox=(-5, -5, 5, 5),
        geometry={"view_twist_angle": angle, "view_direction_vector": [0, 0, 1]},
    )
    tr = _PaperToModelTransform.build(vp, (-50, -50, 50, 50))
    assert tr is not None
    inverse = native.get_transformation_matrix()
    inverse.inverse()
    expected = inverse.transform((2, 3, 0))
    assert tr.point((2, 3)) == pytest.approx([expected.x, expected.y])


@pytest.mark.parametrize("angle", [2 * math.pi, 90, float("nan")])
def test_nonzero_degree_rotation_is_not_silently_unrotated(angle):
    vp = CadEntity(
        id="vp",
        source_file_id="synthetic",
        entity_type="VIEWPORT",
        space="paper:Layout1",
        bbox=(-5, -5, 5, 5),
        geometry={"view_twist_angle": angle},
    )
    assert _PaperToModelTransform.build(vp, (-50, -50, 50, 50)) is None
