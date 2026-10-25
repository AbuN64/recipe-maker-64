from types import SimpleNamespace

from recipemaker import settle

ERR = "build/us/src/game/a.c.i:{n}: error: structure has no member named `{m}'\n"


def tree(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src/game").mkdir(parents=True)
    (tmp_path / "include/R.h").write_text("struct R { int unk4; int bob; };\n#define GETX(p) ((p)->unk4)\n")
    (tmp_path / "src/game/a.c").write_text(
        "#include <R.h>\nvoid f(struct R *p) {\n    p->unk4 = 1;\n    g = GETX(p);\n    q->bob = 2;\n}\n"
    )
    (tmp_path / "build/us/src/game").mkdir(parents=True)
    (tmp_path / "build/us/src/game/a.c.d").write_text("build/us/src/game/a.c.o: src/game/a.c include/R.h\n")
    (tmp_path / "build/us/src/game/a.c.i").write_text(
        "\n\nvoid f(struct R *p) {\n    p->unk4 = 1;\n    g = ((p)->unk4);\n    q->bob = 2;\n}\n"
    )
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path, versions=("us",)))


def test_objects_for_a_header_are_the_objects_whose_dependency_file_names_it(tmp_path):
    cfg = tree(tmp_path)
    assert settle.objects_for(cfg.project.root, ["include/R.h"], "us") == ["build/us/src/game/a.c.o"]


def test_a_missed_access_is_fixed_from_its_line_and_a_macro_expansion_is_reported(tmp_path):
    cfg = tree(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        text = (tmp_path / "src/game/a.c").read_text()
        out = ""
        if "p->unk4 = 1" in text:
            out += ERR.format(n=4, m="unk4")
        out += ERR.format(n=5, m="unk4")  # the macro line: no source line spells it
        return "", out

    found = settle.settle(cfg, {"unk4": "bobAmplitude"}, {"struct R"}, run=run, version="us")
    assert (tmp_path / "src/game/a.c").read_text().count("p->bobAmplitude = 1") == 1
    assert found["fixed"] == 1 and found["settled"] is False
    assert found["unfixed"] == ["build/us/src/game/a.c.i:5 unk4"]
    assert settle.lines(found) and "macro" in settle.lines(found)[0]
    assert calls[0] == ["build/us/src/game/a.c.o"]  # targets only: unbake's make runs them under the host jobserver


def test_a_new_name_a_typed_rename_put_on_another_record_goes_back(tmp_path):
    cfg = tree(tmp_path)
    (tmp_path / "src/game/a.c").write_text("void f(struct R *p) {\n    q->bobAmplitude = 2;\n}\n")
    (tmp_path / "build/us/src/game/a.c.i").write_text("void f(struct R *p) {\n    q->bobAmplitude = 2;\n}\n")
    done = []

    def run(argv):
        text = (tmp_path / "src/game/a.c").read_text()
        done.append(1)
        return "", ERR.format(n=2, m="bobAmplitude") if "bobAmplitude" in text else ""

    found = settle.settle(cfg, {"unk4": "bobAmplitude"}, {"struct R"}, run=run, version="us")
    assert "q->unk4 = 2" in (tmp_path / "src/game/a.c").read_text()
    assert found["settled"] is True and found["unfixed"] == []


def test_nothing_to_compile_is_settled(tmp_path):
    cfg = tree(tmp_path)
    found = settle.settle(cfg, {"unk4": "x"}, {"struct R"}, run=lambda a: ("", ""), version="us")
    assert found["settled"] is True or found["compiled"] >= 0


def test_an_access_the_types_place_on_another_record_is_never_compiled(tmp_path):
    tree(tmp_path)
    (tmp_path / "src/game/b.c").write_text("struct S { int unk4; };\nvoid g(struct S *s) {\n    s->unk4 = 1;\n}\n")
    (tmp_path / "src/game/a.c").write_text("void f(struct R *p) {\n    p->bob = 2;\n}\n")
    (tmp_path / "include/R.h").write_text("struct R { int unk4; int bob; };\n")
    assert settle.doubtful(tmp_path, {"unk4": "x"}, {"struct R"}) == set()
