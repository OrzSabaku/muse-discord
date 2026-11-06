#!/usr/bin/env python3
"""render-worker-prompt.py -- the installed reply worker prompt, in one
command.

    python3 bin/render-worker-prompt.py --media images,video
    python3 bin/render-worker-prompt.py --media none --out -

Renders worker-prompt.template.md for this install and writes it to
worker-prompt.md in the package dir (not tracked by git; re-render it
after every update), or to --out (- for stdout). It:

- drops the installer notes above the template's first `---` line;
- fills the PERSONA seam: the worker uses the persona text in the wake
  payload, which the wake hook reads from persona.md on every wake, so
  persona edits apply at once;
- fills the generation-limits bullet from --media: what THIS runtime
  can make (none, images, video, or images,video);
- replaces every `<package dir>` with the package's absolute path, and
  runs the bin/ tools with the package's own Python (.venv/bin/python3
  when bin/install.sh made one).

The CLASSIFICATION / JUDGMENT seam keeps the template's defaults.
e2e/claude_worker.py renders its test prompt with render() below.
"""
import argparse
import os
import re
import shlex
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "worker-prompt.template.md"
OUTPUT = "worker-prompt.md"

PERSONA_SEAM = """\
Use the persona text from the wake payload (`persona`, step 1 above);
the wake hook reads it fresh on every wake."""

MEDIA_LIMITS = {
    "none": "this runtime has NO media generation tools. You cannot "
            "produce images, video, or audio, so never generate and never "
            "claim to; if asked, say so briefly.",
    "images": "this runtime can generate images, but not video or "
              "audio.",
    "video": "this runtime can generate video, but not images or audio.",
    "images,video": "this runtime can generate images and video, but not "
                    "audio.",
}


def limits_bullet(media):
    words = MEDIA_LIMITS[media].split()
    lines, cur = [], "- Know and state your limits honestly:"
    for w in words:
        if len(cur) + 1 + len(w) > 70:
            lines.append(cur)
            cur = "  " + w
        else:
            cur += " " + w
    return "\n".join(lines + [cur])


def media_arg(value):
    parts = sorted({p.strip() for p in value.lower().split(",")
                    if p.strip()} - {"none"})
    key = ",".join(parts) or "none"
    if key not in MEDIA_LIMITS:
        raise argparse.ArgumentTypeError(
            "use none, images, video, or images,video")
    return key


def fill_seam(text, name, body):
    """Replace the body of `=== SEAM: name ===` (markers kept). Returns
    (text, found)."""
    pat = re.compile(r"(=== SEAM: %s ===\n).*?(\n=== END SEAM: %s ===)"
                     % (re.escape(name), re.escape(name)), re.S)
    new, n = pat.subn(lambda m: m.group(1) + body + m.group(2), text,
                      count=1)
    return new, n == 1


def package_python(pkg):
    """.venv/bin/python3 once bin/install.sh has checked it, else
    python3."""
    venv = os.path.join(pkg, ".venv", "bin", "python3")
    ok = os.path.exists(os.path.join(pkg, ".venv", ".muse-ok"))
    return venv if ok and os.access(venv, os.X_OK) else "python3"


def render(template, pkg, media="none", python="python3"):
    """The installed worker prompt for package dir `pkg`, plus a list of
    warnings for seams the template no longer has."""
    warnings = []
    m = re.search(r"^---[ \t]*$", template, re.M)
    text = template[m.end():].lstrip("\n") if m else template
    text, ok = fill_seam(text, "PERSONA", PERSONA_SEAM)
    if not ok:
        warnings.append("PERSONA seam not found")
    text, n = re.subn(r"- Know and state your limits honestly:.*?"
                      r"(?=\n- |\n=== END SEAM)",
                      lambda _m: limits_bullet(media), text, count=1,
                      flags=re.S)
    if not n:
        warnings.append("GENERATION POLICY limits bullet not found")
    # Commands get shell-quoted paths (a package dir may have spaces).
    text = re.sub(r"python3 <package dir>/(bin/[\w.-]+)",
                  lambda m: f"{shlex.quote(python)} "
                            f"{shlex.quote(os.path.join(pkg, m.group(1)))}",
                  text)
    text = text.replace("<package dir>", pkg)
    return text, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--media", type=media_arg, default="none",
                    help="what this runtime can generate: none, images, "
                         "video, or images,video (default none)")
    ap.add_argument("--out", help=f"where to write (default: {OUTPUT} in "
                                  f"the package dir; - for stdout)")
    args = ap.parse_args(argv)
    with open(os.path.join(PKG_DIR, TEMPLATE)) as f:
        template = f.read()
    text, warnings = render(template, PKG_DIR, args.media,
                            package_python(PKG_DIR))
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.out == "-":
        sys.stdout.write(text)
        return 1 if warnings else 0
    out = args.out or os.path.join(PKG_DIR, OUTPUT)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, out)
    print(f"Wrote {out}")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
