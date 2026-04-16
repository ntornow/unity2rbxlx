"""
asset_moderator.py -- Pre-upload asset safety screening.

Screens assets against Roblox's published safety standards before upload
to prevent account moderation. This is an automated heuristic check --
it catches obvious violations but is not a substitute for human review.

Standards enforced:
  - Roblox Community Standards (Safety, Civility, Integrity, Security)
  - Restricted Content Policy
  - Terms of Use / DMCA
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ModerationFinding:
    relative_path: str
    kind: str  # texture, mesh, audio, script
    classification: str  # OK, WARNING, VIOLATION
    standards: list[str]  # e.g. ["Safety"], ["Integrity", "ToU-DMCA"]
    evidence: str
    source_document: str  # e.g. "#1" for Community Standards


@dataclass
class ModerationReport:
    project: str
    checked: int = 0
    ok: int = 0
    warnings: int = 0
    violations: int = 0
    findings: list[ModerationFinding] = field(default_factory=list)

    def add(self, finding: ModerationFinding) -> None:
        self.findings.append(finding)
        if finding.classification == "VIOLATION":
            self.violations += 1
        elif finding.classification == "WARNING":
            self.warnings += 1
        else:
            self.ok += 1
        self.checked += 1

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "checked": self.checked,
            "counts": {
                "ok": self.ok,
                "warning": self.warnings,
                "violation": self.violations,
            },
            "findings": [
                {
                    "relative_path": f.relative_path,
                    "kind": f.kind,
                    "classification": f.classification,
                    "standards": f.standards,
                    "evidence": f.evidence,
                    "source_document": f.source_document,
                }
                for f in self.findings
                if f.classification != "OK"
            ],
        }


# ---- Patterns ----

# Profanity / slur patterns (lowercase). This is intentionally a small
# high-confidence list to avoid false positives. The Roblox upload API
# has its own comprehensive filter; this catches things that would get
# the *account* moderated, not just the asset rejected.
_SLUR_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bn[i1]gg(?:er|a|az)\b",
        r"\bf[a@]gg?(?:ot|it)\b",
        r"\bk[i1]ke\b",
        r"\bsp[i1]c[ks]?\b",
        r"\bch[i1]nk\b",
        r"\bretard(?:ed)?\b",
    ]
]

# PII / credential exfiltration patterns in scripts
_EXFIL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"HttpService\s*[:\.].*Post", re.IGNORECASE),
     "HttpService POST to external URL"),
    (re.compile(r"\.ROBLOSECURITY", re.IGNORECASE),
     "ROBLOSECURITY cookie access"),
    (re.compile(r"password|passwd|credentials?\b", re.IGNORECASE),
     "credential-related string"),
    (re.compile(r"document\.cookie|localStorage\.", re.IGNORECASE),
     "browser credential access pattern"),
]

# Off-platform URL patterns
_OFFPLATFORM_PATTERNS: list[re.Pattern] = [
    re.compile(r"discord\.gg/|discord\.com/invite/", re.IGNORECASE),
    re.compile(r"t\.me/|telegram\.me/", re.IGNORECASE),
    re.compile(r"bit\.ly/|tinyurl\.com/", re.IGNORECASE),
]

# Copyrighted music filename patterns (artist - title)
_MUSIC_PATTERN = re.compile(
    r"^(?P<artist>[A-Za-z][\w\s.&'-]+?)\s*[-_]\s*(?P<title>[A-Za-z][\w\s.&'()-]+)$"
)

# Known copyrighted franchise terms that could indicate IP infringement
_IP_TERMS: list[str] = [
    "nintendo", "pokemon", "pikachu", "mario", "zelda", "mickey mouse",
    "disney", "marvel", "dc comics", "batman", "superman", "spider-man",
    "star wars", "harry potter", "coca-cola", "pepsi", "mcdonald",
    "fortnite", "minecraft",
]


def _screen_filename(name: str, kind: str) -> ModerationFinding | None:
    """Check filename for slurs, IP terms, or suspicious patterns."""
    lower = name.lower()

    for pat in _SLUR_PATTERNS:
        if pat.search(lower):
            return ModerationFinding(
                relative_path=name,
                kind=kind,
                classification="VIOLATION",
                standards=["Civility"],
                evidence=f"Filename contains slur/hate speech pattern",
                source_document="#1",
            )

    for term in _IP_TERMS:
        if term in lower:
            return ModerationFinding(
                relative_path=name,
                kind=kind,
                classification="WARNING",
                standards=["Integrity", "ToU-DMCA"],
                evidence=f"Filename contains potential IP/trademark term: '{term}'",
                source_document="#6",
            )

    return None


def _screen_script(path: Path, relative_path: str) -> list[ModerationFinding]:
    """Scan a script file for security/civility violations."""
    findings = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings

    for pat in _SLUR_PATTERNS:
        match = pat.search(text)
        if match:
            findings.append(ModerationFinding(
                relative_path=relative_path,
                kind="script",
                classification="VIOLATION",
                standards=["Civility"],
                evidence=f"Script contains slur/hate speech",
                source_document="#1",
            ))
            break

    for pat, desc in _EXFIL_PATTERNS:
        if pat.search(text):
            findings.append(ModerationFinding(
                relative_path=relative_path,
                kind="script",
                classification="WARNING",
                standards=["Security"],
                evidence=desc,
                source_document="#1",
            ))

    for pat in _OFFPLATFORM_PATTERNS:
        if pat.search(text):
            findings.append(ModerationFinding(
                relative_path=relative_path,
                kind="script",
                classification="WARNING",
                standards=["Security"],
                evidence="Off-platform URL/link",
                source_document="#1",
            ))
            break

    return findings


def _screen_audio_filename(name: str, relative_path: str) -> ModerationFinding | None:
    """Check if an audio filename looks like a copyrighted song."""
    stem = Path(name).stem
    match = _MUSIC_PATTERN.match(stem)
    if match:
        artist = match.group("artist").strip()
        title = match.group("title").strip()
        # Only flag if both parts look like real words (not asset pack names)
        if len(artist) > 2 and len(title) > 2 and " " in artist or " " in title:
            return ModerationFinding(
                relative_path=relative_path,
                kind="audio",
                classification="WARNING",
                standards=["Integrity", "ToU-DMCA"],
                evidence=f"Audio filename resembles copyrighted song: '{artist}' - '{title}'",
                source_document="#6",
            )
    return None


def moderate_assets(
    manifest,  # AssetManifest
    project_name: str,
    scripts_dir: Path | None = None,
) -> ModerationReport:
    """Screen all assets in the manifest for safety violations.

    Args:
        manifest: The AssetManifest from extract_assets.
        project_name: Name of the project (for the report).
        scripts_dir: Optional directory containing .cs scripts to scan.

    Returns:
        ModerationReport with findings.
    """
    report = ModerationReport(project=project_name)

    for asset in manifest.assets:
        rel = str(asset.relative_path)
        kind = asset.kind

        # Filename check (all asset types)
        finding = _screen_filename(rel, kind)
        if finding:
            finding.relative_path = rel
            report.add(finding)
        else:
            report.add(ModerationFinding(
                relative_path=rel, kind=kind, classification="OK",
                standards=[], evidence="", source_document="",
            ))

        # Audio-specific: copyrighted song detection
        if kind == "audio":
            audio_finding = _screen_audio_filename(asset.path.name, rel)
            if audio_finding:
                report.add(audio_finding)

    # Script screening
    if scripts_dir and scripts_dir.is_dir():
        for script_path in scripts_dir.rglob("*.cs"):
            rel = str(script_path.relative_to(scripts_dir.parent.parent))
            findings = _screen_script(script_path, rel)
            if findings:
                for f in findings:
                    report.add(f)
            else:
                report.add(ModerationFinding(
                    relative_path=rel, kind="script", classification="OK",
                    standards=[], evidence="", source_document="",
                ))

    return report


def write_report(report: ModerationReport, output_dir: Path) -> Path:
    """Write the moderation report to JSON."""
    report_path = output_dir / "asset_safety_report.json"
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2),
        encoding="utf-8",
    )
    return report_path
