"""An unbake or recipe finding as text: its sentence and fix from knowledge/errors.toml, the reason, the next line."""

from unbake.contracts import Finding

from recipemaker import table


def render(finding: Finding) -> list[str]:
    row = table("knowledge/errors.toml").get("key", {}).get(finding.key, {})
    sentence = row.get("sentence", finding.reason)
    lines = [f"{'refused' if finding.blocking else 'debt'}: {sentence}"]
    if finding.reason and sentence != finding.reason:
        lines.append(f"because: {finding.reason}")
    lines += [f"why: {m}" for m in finding.missing]
    if finding.path:
        lines.append(f"in: {finding.path}" + (f":{finding.line}" if finding.line else ""))
    lines += [f"fix: {row['fix']}"] if "fix" in row else []
    return [*lines, f"next: {finding.action or 'recipe name next'}"]
