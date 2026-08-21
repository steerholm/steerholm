"""Build the Allure runs landing page.

Maintains `<site>/runs.json` (a list of run records, newest first) and renders
`<site>/index.html` — a table of CI runs linking to each run's Allure report.
Run once per CI run, after that run's report has been copied into
`<site>/runs/<run-number>/`.
"""

import argparse
import glob
import html
import json
import os
from pathlib import Path

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Harbour — Test Reports</title>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1b1f24; padding: 0 1rem; }}
    h1 {{ font-size: 1.4rem; }}
    p.sub {{ color: #57606a; margin-top: -0.4rem; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.92rem; }}
    th, td {{ text-align: left; padding: 0.5rem 0.7rem; border-bottom: 1px solid #d0d7de; white-space: nowrap; }}
    th {{ background: #f6f8fa; }}
    tr:hover td {{ background: #f6f8fa; }}
    code {{ background: #eff1f3; padding: 0.05rem 0.3rem; border-radius: 4px; }}
    .pass {{ color: #1a7f37; font-weight: 600; }}
    .fail {{ color: #cf222e; font-weight: 600; }}
    .skip {{ color: #9a6700; }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>MCP Harbour — Test Reports</h1>
  <p class="sub">Allure 3 reports per CI run. Newest first.</p>
  <table>
    <thead>
      <tr>
        <th>Run</th><th>Date (UTC)</th><th>Ref</th><th>Commit</th>
        <th>Trigger</th><th>Passed</th><th>Failed</th><th>Skipped</th><th>Report</th>
      </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
</body>
</html>
"""


def count_statuses(results_dir: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    for f in glob.glob(os.path.join(results_dir, "*-result.json")):
        try:
            status = json.loads(Path(f).read_text()).get("status", "unknown")
        except Exception:
            status = "unknown"
        if status == "passed":
            passed += 1
        elif status in ("failed", "broken"):
            failed += 1
        else:
            skipped += 1
    return passed, failed, skipped


def render_rows(runs: list[dict], repo: str) -> str:
    if not runs:
        return '      <tr><td colspan="9">No runs yet.</td></tr>'
    out = []
    for r in runs:
        run_url = f"https://github.com/{repo}/actions/runs/{r['run_id']}"
        commit_url = f"https://github.com/{repo}/commit/{r['commit']}"
        out.append(
            "      <tr>\n"
            f"        <td><a href=\"{run_url}\">#{html.escape(str(r['run_number']))}</a></td>\n"
            f"        <td>{html.escape(r['date'])}</td>\n"
            f"        <td>{html.escape(r['ref'])}</td>\n"
            f"        <td><a href=\"{commit_url}\"><code>{html.escape(r['commit'][:7])}</code></a></td>\n"
            f"        <td>{html.escape(r['trigger'])}</td>\n"
            f"        <td class=\"pass\">{r['passed']}</td>\n"
            f"        <td class=\"fail\">{r['failed']}</td>\n"
            f"        <td class=\"skip\">{r['skipped']}</td>\n"
            f"        <td><a href=\"runs/{html.escape(str(r['run_number']))}/\">Report</a></td>\n"
            "      </tr>"
        )
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--site", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--run-number", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--trigger", required=True)
    args = p.parse_args()

    site = Path(args.site)
    site.mkdir(parents=True, exist_ok=True)
    runs_json = site / "runs.json"
    runs = json.loads(runs_json.read_text()) if runs_json.exists() else []

    passed, failed, skipped = count_statuses(args.results)
    entry = {
        "run_number": int(args.run_number),
        "run_id": args.run_id,
        "date": args.date,
        "ref": args.ref,
        "commit": args.commit,
        "trigger": args.trigger,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }
    runs = [r for r in runs if r.get("run_number") != entry["run_number"]]
    runs.insert(0, entry)
    runs.sort(key=lambda r: r["run_number"], reverse=True)

    runs_json.write_text(json.dumps(runs, indent=2))
    (site / "index.html").write_text(PAGE.format(rows=render_rows(runs, args.repo)))
    print(f"index updated: {len(runs)} run(s); latest #{entry['run_number']} "
          f"({passed} passed, {failed} failed, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
