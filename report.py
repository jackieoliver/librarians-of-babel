"""One self-contained HTML page per run."""
from __future__ import annotations

import html
from pathlib import Path

CSS = """
body{font:16px/1.55 Georgia,'Merriweather',serif;background:#f6f1e7;color:#22201c;max-width:860px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:30px;margin:0 0 4px;letter-spacing:-.5px} .sub{color:#6d655a;font-style:italic;margin-bottom:28px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:2px;color:#8a4b2a;border-bottom:1px solid #d8cdb8;padding-bottom:6px;margin-top:40px}
.card{background:#fffdf8;border:1px solid #e2d8c5;border-radius:6px;padding:16px 18px;margin:14px 0}
.card a.t{font-size:19px;color:#1d3c5a;text-decoration:none;font-weight:bold} .meta{font:13px ui-monospace,monospace;color:#7b7164;margin:2px 0 8px}
.blurb{font-size:17px;margin:6px 0} details{margin-top:8px;font-size:14px} summary{cursor:pointer;color:#8a4b2a}
ul{margin:6px 0 6px 18px;padding:0} li{margin:3px 0} .pill{display:inline-block;font:12px ui-monospace,monospace;background:#efe6d4;border-radius:10px;padding:1px 8px;margin-right:4px}
.gem{border-left:4px solid #b8862b} table{border-collapse:collapse;font-size:14px;width:100%} td{padding:4px 6px;border-bottom:1px solid #eee3d0;vertical-align:top}
.stats{font:13px ui-monospace,monospace;color:#6d655a}
"""


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def write_report(run: Path, ranking: list[dict], dives: list[dict], screened: list[dict], stats: dict) -> None:
    by_name = {d["full_name"]: d for d in dives}
    parts = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Librarians of Babel · {run.name}</title><style>{CSS}</style>",
             "<h1>Librarians of Babel</h1>",
             f"<div class=sub>New repos worth reading before anyone else does · run {e(run.name)}</div>",
             f"<div class=stats>{stats['candidates']} new repos found → {stats['triage_kept']} past triage → "
             f"{stats['screened_ok']} screened by a cheap model → {stats['deep']} read by an agent · total model spend ${stats['spend_usd']:.2f}</div>"]
    for tier, title in (("gem", "Gems"), ("worth a look", "Worth a look"), ("pass", "Read, then passed")):
        rows = [r for r in ranking if r.get("tier") == tier and r.get("repo") in by_name]
        if not rows:
            continue
        parts.append(f"<h2>{title} ({len(rows)})</h2>")
        for r in rows:
            d = by_name[r["repo"]]
            v = d.get("verdict") or {}
            s = d.get("screen") or {}
            parts.append(
                f"<div class='card {'gem' if tier == 'gem' else ''}'><a class=t href='{e(d['url'])}'>{e(d['full_name'])}</a>"
                f"<div class=meta>★{d['stars']} · {e(d['language'])} · created {e(d['created'][:10])} · "
                f"code {v.get('real_code')}/10 · original {v.get('originality')}/10 · useful {v.get('usefulness')}/10</div>"
                f"<div class=blurb>{e(r.get('blurb'))}</div><div>{e(v.get('what'))}</div>"
                f"<details><summary>reviewer's notes</summary><p><b>Built on:</b> {e(v.get('built_on'))} · <b>For:</b> {e(v.get('audience'))}</p>"
                f"<b>Evidence</b><ul>{''.join(f'<li>{e(x)}</li>' for x in v.get('evidence') or [])}</ul>"
                f"<b>Concerns</b><ul>{''.join(f'<li>{e(x)}</li>' for x in v.get('concerns') or [])}</ul>"
                f"<p><i>{e(v.get('verdict'))}</i></p><p class=stats>screener: {e(s.get('why'))}</p></details></div>")
    rest = [s for s in screened if s["full_name"] not in by_name][:60]
    if rest:
        parts.append("<h2>Next in line (screened only, not read)</h2><table>")
        for s in rest:
            sc = s["screen"]
            parts.append(f"<tr><td><a href='{e(s['url'])}'>{e(s['full_name'])}</a><br><span class=stats>★{s['stars']} · {e(s['language'])} · score {sc['score']}</span></td>"
                         f"<td>{e(sc.get('what'))}</td></tr>")
        parts.append("</table>")
    (run / "report.html").write_text("\n".join(parts))
