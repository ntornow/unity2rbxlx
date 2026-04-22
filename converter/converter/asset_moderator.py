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

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None  # type: ignore[assignment]

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
        r"(?:^|[^a-zA-Z])n[i1]gg(?:er|a|az|as)s?(?:[^a-zA-Z]|$)",
        r"(?:^|[^a-zA-Z])f[a@]gg?(?:ot|it)(?:[^a-zA-Z]|$)",
        r"(?:^|[^a-zA-Z])k[i1]ke(?:[^a-zA-Z]|$)",
        r"(?:^|[^a-zA-Z])sp[i1]c[ks]?(?:[^a-zA-Z]|$)",
        r"(?:^|[^a-zA-Z])ch[i1]nk(?:[^a-zA-Z]|$)",
        r"(?:^|[^a-zA-Z])retard(?:ed)?(?:[^a-zA-Z]|$)",
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


def _screen_image_content(
    image_paths: list[tuple[Path, str]],
    batch_size: int = 20,
) -> list[ModerationFinding]:
    """Screen image files for inappropriate content using Claude vision.

    Sends batches of images to the Anthropic API for content analysis.
    Returns findings for any images flagged as problematic.
    """
    import config

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        log.warning("[moderate_assets] No ANTHROPIC_API_KEY — skipping image content screening")
        return []

    if _anthropic is None:
        log.warning("[moderate_assets] anthropic package not installed — skipping image content screening")
        return []

    client = _anthropic.Anthropic(api_key=api_key)
    model = getattr(config, "ANTHROPIC_MODEL", "claude-sonnet-4-6")
    findings = []

    for batch_start in range(0, len(image_paths), batch_size):
        batch = image_paths[batch_start:batch_start + batch_size]

        # Build content blocks: each image + its filename
        content = []
        filenames = []
        for img_path, rel_path in batch:
            try:
                import base64
                img_data = img_path.read_bytes()
                # Determine media type
                suffix = img_path.suffix.lower()
                media_map = {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                }
                media_type = media_map.get(suffix)
                if not media_type:
                    # Convert non-standard formats to PNG
                    try:
                        from PIL import Image
                        import io
                        img = Image.open(img_path)
                        buf = io.BytesIO()
                        img.convert("RGBA").save(buf, format="PNG")
                        img_data = buf.getvalue()
                        media_type = "image/png"
                    except Exception:
                        continue

                b64 = base64.b64encode(img_data).decode()
                # Skip very large images (>5MB encoded)
                if len(b64) > 5_000_000:
                    continue
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })
                content.append({
                    "type": "text",
                    "text": f"Image {len(filenames) + 1}: {rel_path}",
                })
                filenames.append(rel_path)
            except Exception as exc:
                log.debug("Failed to read image %s: %s", rel_path, exc)
                continue

        if not content:
            continue

        content.append({
            "type": "text",
            "text": (
                "You are a content moderator for Roblox, a platform primarily used by "
                "children. Review each image above against Roblox Community Standards. "
                "Roblox moderation is VERY strict. Flag as VIOLATION any image containing:\n"
                "- Nudity: any exposed nipples, genitalia, or buttocks (even stylized, cartoon, or on non-human characters)\n"
                "- Sexual content: suggestive poses, revealing clothing showing undergarments, sexual themes\n"
                "- Tobacco/smoking: cigarettes, cigars, pipes, vaping, any smoking depiction\n"
                "- Alcohol: bottles, cans, glasses of alcohol, drinking, intoxication, bars\n"
                "- Drugs: drug use, drug paraphernalia, marijuana, syringes used recreationally\n"
                "- Violence/gore: realistic blood, dismemberment, graphic injuries, torture\n"
                "- Weapons: realistic modern firearms, real-world military weapons (fantasy/stylized OK)\n"
                "- Hate symbols: swastikas, confederate flags, SS bolts, any recognized hate imagery\n"
                "- Gambling: slot machines, poker chips, casino imagery\n"
                "- Real-world brands/logos: trademarked logos, brand names, copyrighted characters\n"
                "- Self-harm: cutting, suicide imagery\n"
                "- Profanity: visible text containing slurs or strong profanity\n"
                "- CSAM: any sexualized depiction of minors (absolute block)\n\n"
                "Flag as WARNING anything borderline or ambiguous.\n\n"
                "For each image, respond with EXACTLY one line in the format:\n"
                "IMAGE_NUMBER|CLASSIFICATION|REASON\n"
                "Where CLASSIFICATION is OK, WARNING, or VIOLATION.\n"
                "Example: 1|OK|Fantasy game character, appropriate\n"
                "Example: 3|VIOLATION|Character has exposed nipples\n"
                "Example: 5|VIOLATION|Character is smoking a cigar\n"
                "Example: 7|WARNING|Character holding bottle that may be alcohol\n"
                "Only output the lines, nothing else."
            ),
        })

        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": content}],
            )
            result_text = response.content[0].text.strip()

            for line in result_text.split("\n"):
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                try:
                    idx = int(parts[0].strip()) - 1
                except (ValueError, IndexError):
                    continue
                classification = parts[1].strip().upper()
                reason = parts[2].strip()

                if classification in ("WARNING", "VIOLATION") and 0 <= idx < len(filenames):
                    findings.append(ModerationFinding(
                        relative_path=filenames[idx],
                        kind="texture",
                        classification=classification,
                        standards=["Safety"],
                        evidence=reason,
                        source_document="#1",
                    ))

            log.info(
                "[moderate_assets] Image batch %d-%d: screened %d images",
                batch_start + 1, batch_start + len(batch), len(filenames),
            )

        except Exception as exc:
            log.warning("[moderate_assets] Image screening API call failed: %s", exc)

    return findings


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

    # Collect texture paths for vision-based screening
    texture_paths: list[tuple[Path, str]] = []

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

        # Collect textures for image content screening
        if kind == "texture":
            texture_paths.append((asset.path, rel))

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

    # Vision-based image content screening
    if texture_paths:
        log.info("[moderate_assets] Screening %d texture images via Claude vision...", len(texture_paths))
        image_findings = _screen_image_content(texture_paths)
        for f in image_findings:
            report.add(f)

    return report


def write_report(report: ModerationReport, output_dir: Path) -> Path:
    """Write the moderation report to JSON."""
    report_path = output_dir / "asset_safety_report.json"
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2),
        encoding="utf-8",
    )
    return report_path
