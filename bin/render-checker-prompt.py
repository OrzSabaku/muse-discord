#!/usr/bin/env python3
"""render-checker-prompt.py -- the web watch checker's prompt for one wake.

    python3 bin/render-checker-prompt.py --due web_watches_due.json
    printf '%s' "$entries" | python3 bin/render-checker-prompt.py --due -

Renders watch-checker-prompt.template.md (the fixed checker prompt;
hooks/watch-due.sh runs this on every wake) to stdout, or to --out:

- drops the installer notes above the template's first `---` line;
- runs bin/ tools with the package's own Python and absolute path;
- replaces every `<<END MARKER>>` with a random heredoc end marker for
  the report tool's summary (watch-report.py --stdin);
- replaces the `<<DUE CHECKS>>` line with the due checks as a data
  block: one JSON object per line ({id, nonce, what, expires}, ASCII
  only, so no entry spans lines), between `BEGIN DATA <token>` and
  `END DATA <token>` lines whose token is random for every render, so
  no text inside an entry can end the block early.

Exits 1 (printing nothing to stdout) when the entries do not parse or
the template has no data line.
"""
import argparse
import json
import os
import re
import secrets
import shlex
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "watch-checker-prompt.template.md"
DATA_LINE = "<<DUE CHECKS>>"
END_MARKER = "<<END MARKER>>"
FIELDS = ("id", "nonce", "what", "expires")


def package_python(pkg):
    """.venv/bin/python3 once bin/install.sh has checked it, else
    python3 (as render-worker-prompt.py)."""
    venv = os.path.join(pkg, ".venv", "bin", "python3")
    ok = os.path.exists(os.path.join(pkg, ".venv", ".muse-ok"))
    return venv if ok and os.access(venv, os.X_OK) else "python3"


def data_block(entries, token=None):
    token = token or secrets.token_hex(8)
    lines = [f"BEGIN DATA {token}"]
    for e in entries:
        lines.append(json.dumps({k: e.get(k) for k in FIELDS},
                                ensure_ascii=True))
    lines.append(f"END DATA {token}")
    return "\n".join(lines)


def render(template, pkg, entries, python="python3", token=None):
    """The checker prompt with entries in its data block (ValueError
    when the template has no data line)."""
    m = re.search(r"^---[ \t]*$", template, re.M)
    text = template[m.end():].lstrip("\n") if m else template
    if text.count(DATA_LINE) != 1:
        raise ValueError(f"the template needs exactly one {DATA_LINE} line")
    text = re.sub(r"python3 <package dir>/(bin/[\w.-]+)",
                  lambda m: f"{shlex.quote(python)} "
                            f"{shlex.quote(os.path.join(pkg, m.group(1)))}",
                  text)
    text = text.replace("<package dir>", pkg)
    # The report's heredoc end marker: random per render, so no page
    # text the checker copies can end it early.
    text = text.replace(END_MARKER, "END_" + secrets.token_hex(8))
    # The data goes in last, so nothing above rewrites inside it.
    return text.replace(DATA_LINE, data_block(entries, token))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--due", default=os.path.join(PKG_DIR,
                                                  "web_watches_due.json"),
                    help="the due checks (JSON list; - for stdin)")
    ap.add_argument("--out", default="-", help="where to write (- stdout)")
    args = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.due == "-" else open(args.due).read()
        entries = json.loads(raw)
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) for e in entries):
            raise ValueError("not a list of objects")
        with open(os.path.join(PKG_DIR, TEMPLATE)) as f:
            text = render(f.read(), PKG_DIR, entries,
                          package_python(PKG_DIR))
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
