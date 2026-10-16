from pedit.model import Effect, Layer, Mask, Project, TextStyle


def test_roundtrip():
    p = Project(name="x", width=10, height=20)
    p.layers.append(Layer(id="L1", type="text", text="ciao", style=TextStyle(size=12),
                          effects=[Effect(type="blur", params={"radius": 2})], mask=Mask(kind="rect", box=[0, 0, 5, 5])))
    q = Project.from_obj(p.to_dict())
    assert q.to_dict() == p.to_dict()
    assert q.layers[0].style.size == 12
    assert q.layers[0].mask.box == [0, 0, 5, 5]


def test_unknown_keys_ignored():
    q = Project.from_obj({"name": "x", "futuro": 1, "layers": [{"id": "L", "nuovo": True}]})
    assert q.layers[0].id == "L"


def test_layer_lookup_by_name():
    p = Project(layers=[Layer(id="L1", name="Title")])
    assert p.layer("Title").id == "L1"
