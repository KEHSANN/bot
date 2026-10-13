"""Fail on new secret findings. Baseline changes require explicit manual review."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUDE = r"(^backend/uv\.lock$)|(^\.secrets\.baseline$)"


def main() -> int:
    baseline = json.loads((ROOT / ".secrets.baseline").read_text())
    allowed = {
        (filename, finding["hashed_secret"])
        for filename, findings in baseline["results"].items()
        for finding in findings
        if finding.get("is_secret") is False
    }
    scan = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-m",
                "detect_secrets",
                "scan",
                "--no-verify",
                "--exclude-files",
                EXCLUDE,
            ],
            cwd=ROOT,
            text=True,
        )
    )
    new_findings = [
        (filename, finding["line_number"], finding["type"])
        for filename, findings in scan["results"].items()
        for finding in findings
        if (filename, finding["hashed_secret"]) not in allowed
    ]
    for filename, line, kind in new_findings:
        print(
            f"New potential secret: {filename}:{line} ({kind}); review without printing its value"
        )
    print(f"Secret scan: {len(new_findings)} new findings")
    return 1 if new_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
