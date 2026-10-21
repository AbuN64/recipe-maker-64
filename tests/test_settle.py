"""settle: write a declaration set, prove every ROM with the host's build, commit it or put the old bytes back."""

from types import SimpleNamespace

import pytest

from recipemaker import settle


@pytest.fixture
def project(fakes, monkeypatch):
    root = fakes.root
    (root / "include").mkdir(exist_ok=True)
    (root / "include/a.h").write_bytes(b"old\n")
    calls = SimpleNamespace(run=[], git=[], build_exit=0, dirty="")

    def run(argv, cwd, config, **kwargs):
        calls.run.append(argv)
        return SimpleNamespace(exit=calls.build_exit, stdout=b"line 1\nbad sha1 for us\n", stderr=b"")

    def git(config, *args):
        calls.git.append(args)
        out = calls.dirty if args[0] == "status" else "abc1234" if args[0] == "rev-parse" else ""
        return SimpleNamespace(exit=0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(settle.host, "run", run)
    monkeypatch.setattr(settle.host, "git", git)
    return root, calls


def land(fakes, edits, old=None):
    return settle.land(fakes.config, {"include/a.h": b"old\n"} if old is None else old, edits, "Declare f once")


def test_a_set_whose_roms_match_is_written_staged_and_committed_as_the_hosts_author(fakes, project):
    root, calls = project
    got = land(fakes, {"include/a.h": b"new\n"})
    assert got == {"commit": "abc1234"} and (root / "include/a.h").read_bytes() == b"new\n"
    assert calls.run[0][-4:] == ["--", "make", "-j{j}", "check"] and calls.run[0][0].endswith("unbake-run")
    commit = next(a for a in calls.git if "commit" in a)
    assert (
        commit[:4] == ("-c", "user.name=test", "-c", "user.email=test@example.invalid")
        and commit[-1] == "Declare f once"
    )
    assert any(a[:2] == ("add", "--") for a in calls.git)


def test_roms_that_differ_put_the_old_bytes_back_and_commit_nothing(fakes, project):
    root, calls = project
    calls.build_exit = 2
    got = land(fakes, {"include/a.h": b"new\n"})
    assert "the ROMs differ or the build failed" in got["problem"] and "bad sha1 for us" in got["problem"]
    assert (root / "include/a.h").read_bytes() == b"old\n" and not any("commit" in a for a in calls.git)


def test_a_file_that_moved_or_holds_uncommitted_changes_refuses_the_set_before_writing(fakes, project):
    root, calls = project
    assert "moved" in land(fakes, {"include/a.h": b"new\n"}, old={"include/a.h": b"older\n"})["problem"]
    calls.dirty = " M include/a.h"
    assert "moved" in land(fakes, {"include/a.h": b"new\n"})["problem"]
    assert (root / "include/a.h").read_bytes() == b"old\n" and calls.run == []
