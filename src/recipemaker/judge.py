"""unbake's verdicts through its command line: score a candidate, score many, compare and submit. Each call runs
`unbake` in the project with the host config and reads its one JSON line; a refusal comes back as a `Refusal`."""

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake.contracts import Config, Finding, Json, Proof, Refusal

from recipemaker import host


@dataclass(frozen=True)
class Verdict:
    """One measured candidate: its proofs per holding version (only the scored one for a screened variant), whether
    every version is exact, and the instruction diff of the scored version."""

    proofs: tuple[Proof, ...]
    exact: bool
    diff: tuple[str, ...]
    findings: tuple[Finding, ...] = ()  # why it was not measured (a compile error), empty when it was


def _argv(config: Config, command: str, *args: str) -> list[str]:
    return [sys.executable, "-m", "unbake", command, *args, "--project", str(config.project.root)]


def call(config: Config, command: str, *args: str, timeout: float | None = None) -> Json:
    """The result of one unbake command (host config from UNBAKE_HOST, which `recipe --host` sets); its findings
    refuse when it did not succeed."""
    done = host.run(_argv(config, command, *args), config.project.root, config, timeout=timeout)
    lines = [ln for ln in done.stdout.decode("utf-8", errors="replace").splitlines() if ln.startswith("{")]
    if not lines:
        tail = done.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise Refusal(Finding("native.exit", reason=f"unbake {command} printed no result: {tail[0]}"))
    document = json.loads(lines[-1])
    if not document.get("ok"):
        raise Refusal(*([_finding(f) for f in document.get("findings") or ()] or _no_cause(command)))
    result: Json = document.get("result") or {}
    return result


def _no_cause(command: str) -> tuple[Finding]:
    return (Finding("native.exit", reason=f"unbake {command} failed without a finding"),)


def _finding(row: Mapping[str, Any]) -> Finding:
    known = set(Finding.__dataclass_fields__)
    kept: dict[str, Any] = {k: tuple(v) if isinstance(v, list) else v for k, v in row.items() if k in known}
    kept["origin"] = None
    return Finding(**kept)


def proof(row: Mapping[str, Any]) -> Proof:
    return Proof(**{**row, "missing": tuple(row["missing"])})


def _extras(work: Path, extras: Mapping[str, bytes]) -> list[str]:
    """The PATH=FILE change-set arguments of the header edits a candidate is measured with."""
    out = []
    for n, (path, text) in enumerate(sorted(extras.items())):
        file = work / f"extra-{n}{Path(path).suffix}"
        host.write(file, text)
        out.append(f"{path}={file}")
    return out


def _unbuilt(findings: Sequence[Finding]) -> bool:
    return bool(findings) and all(f.key in ("score.compile",) for f in findings)


def proof_failed(member: str, reason: str, key: str = "compile.error") -> Proof:
    """A failed proof carrying why unbake could not measure the candidate, or took nothing of it."""
    return Proof("", member, "", "", "", "", "", "", False, (f"{key}: {reason}",), 0.0, {"compile_failed": True})


def _failed(member: str, findings: tuple[Finding, ...]) -> Verdict:
    """A candidate that did not build: one failed proof carrying the compile error, so the ladder records why."""
    missing = tuple(f"compile.error: {f.reason}" for f in findings) or ("compile.error: no instructions were built",)
    failed = Proof("", member, "", "", "", "", "", "", False, missing, 0.0, {"compile_failed": True})
    return Verdict((failed,), False, (), findings)


def score(
    config: Config, file: Path, member: str, extras: Mapping[str, bytes] | None = None, *, version: str = ""
) -> Verdict:
    """Measure one candidate source in every version that holds `member`, with the instruction diff of `version`
    (default the names-from version); nothing is written to the project."""
    with host.work(config) as work:
        args = [str(file), *_extras(work, extras or {}), "--function", member, "--lines", "200"]
        args += ["--version", version] if version else []
        try:
            result = call(config, "score", *args)
        except Refusal as refusal:
            if _unbuilt(refusal.findings):
                return _failed(member, tuple(refusal.findings))
            raise
    proofs = tuple(proof(p) for p in result["proofs"])
    return Verdict(proofs, bool(result["exact"]), tuple(result["diff"]))


def score_many(config: Config, rows: Sequence[tuple[Path, str, Mapping[str, bytes]]]) -> list[Verdict]:
    """Screen many candidates in one unbake process on every core: each in the reference version only, so a screened
    verdict holds that version's proof; the one that wins is scored again with `score` in every version."""
    if not rows:
        return []
    with host.work(config) as work:
        manifest = []
        for n, (file, member, extras) in enumerate(rows):
            overlays = _extras(work / f"v{n}", extras) if extras else []
            manifest.append({"file": str(file), "function": member, "overlays": overlays or None})
        listing = work / "variants.json"
        host.write(listing, json.dumps(manifest).encode())
        result = call(config, "score", "--variants", str(listing), "--lines", "200")
    out = []
    for (_, member, _), row in zip(rows, result["variants"], strict=True):
        if not row["ok"]:
            found = tuple(_finding(f) for f in row["findings"])
            if not _unbuilt(found):
                raise Refusal(*found)
            out.append(_failed(member, found))
            continue
        body = row["result"]
        symptoms: Json = {} if body["exact"] else {"words": body["words"]}
        missing = () if body["exact"] else (f"version {body['version']}: {body['score']:.1%} of the words match",)
        one = Proof(
            body["unit"], member, body["version"], "", "", "", "", "", bool(body["exact"]), missing,
            float(body["score"]), symptoms,
        )  # fmt: skip
        out.append(Verdict((one,), bool(body["exact"]), tuple(body["diff"])))
    return out


def submit(config: Config, file: Path, member: str, extras: Mapping[str, bytes] | None = None) -> Json:
    """Put a candidate in unbake's landing inbox (unbake proves it again and lands it when it is exact or a fuzzy
    gain); the result holds the submission and the drain."""
    with host.work(config) as work:
        args = [str(file), *_extras(work, extras or {}), "--function", member, "--note", "recipe"]
        return call(config, "submit", *args)


def compare(config: Config, file: Path, member: str) -> Json:
    """Measure a candidate in every version and submit it when it is exact (unbake `compare`): the proofs, the score
    per version, the gaps and the drain."""
    return call(config, "compare", str(file), "--function", member, "--note", "recipe")
