import numpy as np
import pytest

from src.core.local_color import render
from src.core.photo_engine import RenderSource
from src.core.mask_components import (POINT_LIMIT, brush_strokes, component_weight, composite_weight,
                                      prepare_components)


def _component(identity, **values):
    return {"id": identity, "name": "x", "enabled": True, "op": "add", **values}


def _stroke(points, radius=.05, hardness=.5, opacity=1.0):
    return {"points": points, "radius": radius, "hardness": hardness, "opacity": opacity}


def _mask(*components, params=None):
    return {"id": "00000000-0000-4000-8000-000000000001", "name": "m", "enabled": True,
            "params": params or {"exposure": 1}, "components": list(components)}


IDS = ["00000000-0000-4000-8000-00000000001%d" % index for index in range(10)]
SHAPE, REFERENCE = (60, 90), (900, 600)


def _weights(component):
    return component_weight(component, SHAPE, 0, SHAPE[0], REFERENCE, {}, lambda: False)


def test_brush_strokes_combine_by_maximum():
    first, second = _stroke([[.2, .3], [.5, .3]]), _stroke([[.4, .7]], radius=.1, hardness=0, opacity=.6)
    combined = _weights(_component(IDS[0], type="brush", strokes=[first, second]))
    separate = np.maximum(_weights(_component(IDS[1], type="brush", strokes=[first])),
                          _weights(_component(IDS[2], type="brush", strokes=[second])))
    np.testing.assert_array_equal(combined, separate)
    assert combined.max() == 1 and combined.min() == 0


def test_single_stroke_layout_reads_as_one_stroke():
    stroke = _stroke([[.1, .1], [.8, .9]])
    legacy = _component(IDS[0], type="brush", **stroke)
    assert brush_strokes(legacy) == [stroke]
    prepare_components([_mask(legacy)], active_only=False)
    np.testing.assert_array_equal(_weights(legacy), _weights(_component(IDS[1], type="brush", strokes=[stroke])))


def test_whole_photo_area_minus_brush_leaves_the_rest():
    brush = {**_component(IDS[1], type="brush", strokes=[_stroke([[.5, .5]], radius=.2, hardness=1)]), "op": "subtract"}
    mask = prepare_components([_mask(_component(IDS[0], type="all"), brush)])[0]
    weights = composite_weight(mask, SHAPE, 0, SHAPE[0], REFERENCE, {}, lambda: False)
    np.testing.assert_array_equal(weights, 1 - _weights({**brush, "op": "add"}))
    assert weights[0, 0] == 1 and weights[SHAPE[0] // 2, SHAPE[1] // 2] == 0


@pytest.mark.parametrize("component, error", [
    (_component(IDS[0], type="all", points=[]), "error.mask.parameter"),
    (_component(IDS[0], type="brush", strokes=[]), "error.mask.geometry"),
    (_component(IDS[0], type="brush", strokes=[{"points": [[.1, .1]]}]), "error.mask.parameter"),
    (_component(IDS[0], type="brush", strokes=[_stroke([[.1, .1]], radius=.9)]), "error.mask.parameter"),
    (_component(IDS[0], type="brush", strokes=[_stroke([[.1, .1]])], radius=.1), "error.mask.parameter"),
])
def test_invalid_components_are_rejected(component, error):
    with pytest.raises(ValueError, match=error):
        prepare_components([_mask(component)], active_only=False)


def test_point_limit_counts_every_stroke():
    half = POINT_LIMIT // 2
    strokes = [_stroke([[.5, .5]] * half), _stroke([[.5, .5]] * half)]
    prepare_components([_mask(_component(IDS[0], type="brush", strokes=strokes))], active_only=False)
    with pytest.raises(ValueError, match="error.mask.point_limit"):
        prepare_components([_mask(_component(IDS[0], type="brush", strokes=[*strokes, _stroke([[.5, .5]])]))],
                           active_only=False)


def test_whole_photo_and_strokes_render_the_same_on_both_backends():
    pytest.importorskip("ops_accel")
    rng = np.random.default_rng(3)
    source = RenderSource(rng.random((48, 64, 3), dtype=np.float32) * 0.9, None, "srgb8", "", False, (64, 48), "srgb-direct")
    brush = {**_component(IDS[1], type="brush", strokes=[_stroke([[.2, .2], [.7, .6]], radius=.15),
                                                          _stroke([[.8, .2]], radius=.1, hardness=1)]), "op": "subtract"}
    params = {"process_version": 4, "masks": [_mask(_component(IDS[0], type="all"), brush,
                                                    params={"exposure": .8, "saturation": .3})]}
    from src.core.tools.color import COLOR_PARAMETERS
    settings = {**COLOR_PARAMETERS.defaults(COLOR_PARAMETERS.version), **params}
    masks = prepare_components(settings["masks"])
    from dataclasses import replace
    prepared = tuple(replace(mask, raster=composite_weight(mask, (48, 64), 0, 48, (64, 48), {}, lambda: False))
                     for mask in masks)
    python = render(source, settings, backend="python", prepared_masks=prepared).pixels
    native = render(source, settings, backend="native", prepared_masks=prepared).pixels
    plain = render(source, {**settings, "masks": None}, backend="python").pixels
    assert np.abs(python - native).max() < 2e-4
    assert np.abs(python - plain).max() > 0.05
