"""judge: unbake's command line as verdicts. The process is faked; each test checks the argv and the reading."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fakes import proof
from unbake.contracts import Refusal

from recipemaker import judge


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path))


@pytest.fixture
def ran(monkeypatch):
    """Every unbake argv `host.run` gets, answered from `ran.documents` in order."""
    calls = SimpleNamespace(argv=[], documents=[])

    def run(argv, cwd, config, **kw):
        calls.argv.append(list(argv))
        document = calls.documents.pop(0)
        return SimpleNamespace(
            exit=0 if document["ok"] else 1, stdout=(json.dumps(document) + "\n").encode(), stderr=b""
        )

    monkeypatch.setattr(judge.host, "run", run)
    return calls


def ok(result):
    return {"ok": True, "result": result, "findings": []}


def refused(key, reason):
    found = {"key": key, "reason": reason, "path": "", "line": 0, "unit": "", "versions": ["a"], "missing": []}
    return {"ok": False, "result": None, "findings": [{**found, "origin": None, "blocking": True, "action": ""}]}


def test_score_measures_every_version_with_the_header_edits_as_change_set_files(config, ran, tmp_path):
    proofs = [asdict(proof("f", "a", 0.5)), asdict(proof("f", "b", 1.0))]
    ran.documents.append(ok({"proofs": proofs, "exact": False, "diff": ["-a", "+b"]}))
    got = judge.score(config, tmp_path / "f.c", "f", {"include/a.h": b"int a;\n"}, version="b")
    argv = ran.argv[0]
    assert argv[1:4] == ["-m", "unbake", "score"] and "--function" in argv and argv[argv.index("--version") + 1] == "b"
    assert any(a.startswith("include/a.h=") for a in argv)  # the header edit, as a change-set file
    assert [p.score for p in got.proofs] == [0.5, 1.0] and not got.exact and got.diff == ("-a", "+b")


def test_a_candidate_that_does_not_compile_is_a_failed_verdict_and_another_refusal_raises(config, ran, tmp_path):
    ran.documents.append(refused("score.compile", "x.c:3: syntax error"))
    got = judge.score(config, tmp_path / "f.c", "f")
    assert not got.exact and got.proofs[0].missing == ("compile.error: x.c:3: syntax error",)
    ran.documents.append(refused("land.request", "f is not held by version z"))
    with pytest.raises(Refusal) as caught:
        judge.score(config, tmp_path / "f.c", "f")
    assert caught.value.findings[0].key == "land.request"


def test_score_many_screens_every_row_in_one_call(config, ran, tmp_path):
    body = {"unit": "src/a.c", "version": "a", "exact": False, "score": 0.75, "words": {}, "diff": []}
    ran.documents.append(
        ok(
            {
                "variants": [
                    {"ok": True, "result": body},
                    {"ok": False, "findings": refused("score.compile", "e")["findings"]},
                ]
            }
        )
    )
    rows = [(tmp_path / "1.c", "f", {}), (tmp_path / "2.c", "f", {"include/a.h": b"x"})]
    first, second = judge.score_many(config, rows)
    assert len(ran.argv) == 1 and "--variants" in ran.argv[0]
    assert (first.proofs[0].version, first.proofs[0].score) == ("a", 0.75)
    assert second.proofs[0].missing == ("compile.error: e",) and judge.score_many(config, []) == []


def test_submit_and_compare_read_their_results(config, ran, tmp_path):
    ran.documents.append(ok({"submissions": [{"member": "f", "id": "i1"}], "drain": {"landed": []}}))
    assert judge.submit(config, tmp_path / "f.c", "f")["submissions"][0]["id"] == "i1"
    ran.documents.append(ok({"exact": True, "proofs": []}))
    assert judge.compare(config, tmp_path / "f.c", "f")["exact"] and ran.argv[-1][3] == "compare"


def test_a_command_that_prints_no_json_refuses_with_its_last_error_line(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        judge.host,
        "run",
        lambda argv, cwd, config, **kw: SimpleNamespace(exit=2, stdout=b"", stderr=b"x\nusage: bad\n"),
    )
    with pytest.raises(Refusal) as caught:
        judge.compare(config, tmp_path / "f.c", "f")
    assert "usage: bad" in caught.value.findings[0].reason
