"""Option bundles that carry logic."""

from tai.options import ImageOptions


def test_image_request_drops_unset_fields_and_maps_names():
    opts = ImageOptions(size="512x512", steps=8, negative="blur")
    assert opts.request(42) == {"size": "512x512", "seed": 42, "steps": 8, "negative_prompt": "blur"}
