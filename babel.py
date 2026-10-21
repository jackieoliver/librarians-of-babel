# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Librarians of Babel v2: find remarkable new GitHub repos before they have stars.

Stages: ingest -> triage -> screen -> deep dive (agent) -> judge -> report.
Each stage writes a JSON file in the run folder; re-running with --resume skips finished stages.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from agent import deep_dive  # noqa: E402
from llm import LLM, BudgetExceeded  # noqa: E402
from report import write_report  # noqa: E402

RUNS = Path("/mnt/data/work/librarian-of-babel/runs")
SCREEN_MODEL = "deepseek/deepseek-v4.1-flash"
DEEP_MODEL = "anthropic/claude-haiku-5.5"
JUDGE_MODEL = "anthropic/claude-haiku-5.5"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self):
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        self.c = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30,
        )

    async def get(self, path, **params):
        for attempt in range(5):
            r = await self.c.get(path, params=params)
            if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or r.status_code == 429):
                reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                wait = max(5, min(120, reset - time.time() + 2))
                log(f"github rate limit, waiting {wait:.0f}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            return r
        return r


async def ingest(gh: GitHub, days: int, min_stars: int, max_stars: int, cap: int) -> list[dict]:
    """Search repos created in each of the last `days` days. One query per day keeps each under the 1000-result cap."""
    repos: dict[str, dict] = {}
    today = datetime.now(timezone.utc).date()
    per_day = max(100, cap // days)
    for d in range(1, days + 1):
        day = today - timedelta(days=d)
        q = f"created:{day} stars:{min_stars}..{max_stars} fork:false archived:false size:>=20"
        got = 0
        for page in range(1, 11):
            r = await gh.get("/search/repositories", q=q, sort="stars", order="desc", per_page=100, page=page)
            if r.status_code != 200:
                log("search failed", r.status_code, r.text[:200])
                break
            items = r.json().get("items", [])
            for it in items:
                repos[it["full_name"]] = slim(it)
            got += len(items)
            if len(items) < 100 or got >= per_day:
                break
            await asyncio.sleep(2.1)  # search API: 30 req/min
        log(f"ingest {day}: {got} repos")
    return list(repos.values())[:cap]


def slim(it: dict) -> dict:
    return {
        "full_name": it["full_name"], "url": it["html_url"], "description": it.get("description") or "",
        "language": it.get("language"), "topics": it.get("topics") or [], "stars": it["stargazers_count"],
        "forks": it["forks_count"], "issues": it["open_issues_count"], "size_kb": it["size"],
        "created": it["created_at"], "pushed": it["pushed_at"], "branch": it.get("default_branch") or "main",
        "license": (it.get("license") or {}).get("spdx_id"), "homepage": it.get("homepage"),
        "owner_type": it["owner"]["type"],
    }


# ---------------------------------------------------------------- triage (free)
# Ported from v1 workers/assess/novelty.py: the patterns that were almost never wrong.

SKIP_NAME = [re.compile(p) for p in [
    r"^\.?dotfiles$", r"^\.config$", r"^(homework|hw|assignment|lab)\d*$", r"^(cs|csc|cse|ece|ee|math|stat|phys)\d{2,4}",
    r"^(pset|problem[-_]?set)\d*$", r"^(exercise|exercises|kata|katas)$", r"^(tutorial|course|bootcamp|udemy|coursera|freecodecamp)[-_]",
    r"^learn[-_]", r"^(100|30)[-_]?days[-_]?of", r"^my[-_]?(app|project|repo|website|portfolio|blog|site|page)$",
    r"^(test|testing|test[-_]repo|demo|example|sample|sandbox|playground|scratch|temp|tmp)$", r"^hello[-_]?world$", r"^untitled",
    r"^(boilerplate|starter|template|scaffold|skeleton|seed)$", r"^\.?(vimrc|bashrc|zshrc|tmux\.conf|gitconfig)$",
    r"^(nvim|neovim|vim)[-_]?(config|setup|dotfiles)$", r"^emacs[-_]?(config|setup|d)$", r"portfolio", r"^awesome[-_]",
]]
SKIP_DESC = ["todo app", "to-do app", "weather app", "my portfolio", "my personal website", "course project", "class project",
             "school project", "homework", "assignment for", "bootcamp", "clone of", "replica of", "hello world",
             "practice project", "learning project", "follow along", "airdrop", "crypto signals", "cheat", "hack tool",
             "free robux", "aimbot", "keygen", "crack"]


def triage(r: dict) -> str | None:
    """Return a skip reason, or None to keep."""
    name = r["full_name"].split("/")[1].lower()
    owner = r["full_name"].split("/")[0].lower()
    desc = r["description"].lower()
    if name == owner:
        return "profile readme"
    for p in SKIP_NAME:
        if p.search(name):
            return f"name:{p.pattern}"
    for phrase in SKIP_DESC:
        if phrase in desc:
            return f"desc:{phrase}"
    if len(desc) < 15:
        return "no description"
    if r["size_kb"] < 30:
        return "tiny"
    if r["language"] is None:
        return "no language"
    return None


# ---------------------------------------------------------------- screen (cheap model)

SCREEN_SYSTEM = """You screen brand-new GitHub repositories for a "hidden gems" index. Be harsh: most new repos are mediocre, and many are AI-generated shells with an impressive README and little real code. Judge from the README and the file tree, never from the stars.

Return ONLY JSON with these keys:
{"what": "<one plain sentence: what it does>",
 "category": "<one or two words>",
 "substance": <0-10, is there real, non-trivial code behind the README? 0 = README-only/empty shell, 10 = large real implementation>,
 "novelty": <0-10, 0 = yet another X, 10 = an idea or approach you have not seen>,
 "usefulness": <0-10, would a working engineer, researcher or artist actually use this today?>,
 "craft": <0-10, tests, docs, structure, care>,
 "slop_flags": ["<signs of low-effort or AI-padded work, e.g. emoji-wall README, claims with no code, generic name>"],
 "base_project": "<well-known project it wraps or forks, or null>",
 "gem": <true only if remarkable AND substantive; at most 1 in 20 should be true>,
 "why": "<one sentence, the honest reason for your scores>"}

Wrappers or forks of known projects: score only the new contribution. Lists, course notes, configs, bots for games, and crypto trading bots are almost never gems."""


def tree_summary(tree: list[dict]) -> str:
    files = [t["path"] for t in tree if t.get("type") == "blob"]
    if not files:
        return "(empty tree)"
    exts: dict[str, int] = {}
    for f in files:
        e = Path(f).suffix.lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
    top = sorted(exts.items(), key=lambda kv: -kv[1])[:12]
    dirs = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in files})[:40]
    flags = []
    if any(re.search(r"(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|test_", f) for f in files):
        flags.append("has tests")
    if any(f.startswith(".github/workflows/") for f in files):
        flags.append("has CI")
    big_code = sum(1 for t in tree if t.get("type") == "blob" and Path(t["path"]).suffix in CODE_EXT and t.get("size", 0) > 2000)
    return (f"{len(files)} files; code files over 2 KB: {big_code}; {', '.join(flags) or 'no tests/CI seen'}\n"
            f"extensions: {', '.join(f'{e}:{n}' for e, n in top)}\n"
            f"top level: {' '.join(dirs)}")


CODE_EXT = {".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".kt", ".swift",
            ".zig", ".rb", ".cs", ".lua", ".ml", ".hs", ".ex", ".scala", ".jl", ".nim", ".odin", ".v", ".sv", ".glsl", ".wgsl", ".cu"}


async def fetch_context(gh: GitHub, r: dict) -> dict:
    readme, tree = "", []
    rr = await gh.get(f"/repos/{r['full_name']}/readme")
    if rr.status_code == 200 and rr.json().get("content"):
        readme = base64.b64decode(rr.json()["content"]).decode("utf-8", "ignore")
    tr = await gh.get(f"/repos/{r['full_name']}/git/trees/{r['branch']}", recursive="1")
    if tr.status_code == 200:
        tree = tr.json().get("tree", [])
    return {"readme": readme, "tree": tree}


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object")
    return json.loads(m.group(0))


async def screen_one(llm: LLM, gh: GitHub, r: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            ctx = await fetch_context(gh, r)
        except Exception as e:  # network blips: skip this repo, not the run
            return {**r, "screen_error": f"fetch: {e}"}
        if len(ctx["readme"].strip()) < 200:
            return {**r, "screen_error": "readme missing or under 200 chars"}
        user = (f"Repository: {r['full_name']}\nDescription: {r['description']}\nLanguage: {r['language']}  "
                f"Topics: {', '.join(r['topics']) or 'none'}  License: {r['license']}\n\n"
                f"FILE TREE\n{tree_summary(ctx['tree'])}\n\nREADME (first 6000 chars)\n{ctx['readme'][:6000]}")
        for attempt in range(3):
            try:
                text = await llm.chat(SCREEN_MODEL, [{"role": "system", "content": SCREEN_SYSTEM},
                                                     {"role": "user", "content": user}], max_tokens=900, json_mode=True,
                                     reasoning={"enabled": False})  # a screen needs no thinking; it ate the token cap
                s = parse_json(text)
                for k in ("substance", "novelty", "usefulness", "craft"):
                    s[k] = float(s.get(k) or 0)
                # substance gates everything: a brilliant idea with no code is not a gem
                s["score"] = round(s["substance"] * (0.35 * s["novelty"] + 0.4 * s["usefulness"] + 0.25 * s["craft"]), 1)
                return {**r, "screen": s, "readme_len": len(ctx["readme"])}
            except BudgetExceeded:
                raise
            except Exception as e:
                err = str(e)
        return {**r, "screen_error": err[:200]}


# ---------------------------------------------------------------- judge

JUDGE_SYSTEM = """You are the head librarian. Below are deep-dive verdicts on new GitHub repos, each written by a reviewer who read the code. Rank them from most to least remarkable for a reader who wants to discover genuinely good new work before anyone else does: real substance first, then originality, then usefulness. Penalize anything the reviewer found hollow or derivative.

Return ONLY JSON: {"ranking": [{"repo": "<owner/name>", "tier": "gem" | "worth a look" | "pass", "blurb": "<one vivid sentence for the reader>"}]} covering every repo exactly once."""


async def judge(llm: LLM, dives: list[dict]) -> list[dict]:
    lines = []
    for d in dives:
        v = d.get("verdict") or {}
        lines.append(json.dumps({"repo": d["full_name"], "stars": d["stars"], "language": d["language"],
                                 "what": v.get("what"), "evidence": v.get("evidence"), "concerns": v.get("concerns"),
                                 "real_code": v.get("real_code"), "originality": v.get("originality"),
                                 "usefulness": v.get("usefulness"), "verdict": v.get("verdict")}))
    text = await llm.chat(JUDGE_MODEL, [{"role": "system", "content": JUDGE_SYSTEM},
                                        {"role": "user", "content": "\n".join(lines)}], max_tokens=6000, json_mode=True)
    return parse_json(text)["ranking"]


# ---------------------------------------------------------------- main

def load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def save(p: Path, obj):
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-stars", type=int, default=2)
    ap.add_argument("--max-stars", type=int, default=60)
    ap.add_argument("--max-candidates", type=int, default=1500)
    ap.add_argument("--max-screen", type=int, default=600, help="screen at most this many triage survivors")
    ap.add_argument("--deep", type=int, default=25, help="deep-dive the top N screened")
    ap.add_argument("--budget", type=float, default=2.0, help="hard stop on model spend, USD")
    ap.add_argument("--resume", help="existing run folder name")
    a = ap.parse_args()

    run = RUNS / (a.resume or datetime.now().strftime("%Y%m%d-%H%M"))
    run.mkdir(parents=True, exist_ok=True)
    llm = LLM(budget=a.budget, ledger=run / "costs.jsonl")
    gh = GitHub()
    save(run / "config.json", vars(a) | {"screen_model": SCREEN_MODEL, "deep_model": DEEP_MODEL, "judge_model": JUDGE_MODEL})
    log("run folder", run)

    repos = load(run / "candidates.json")
    if repos is None:
        repos = await ingest(gh, a.days, a.min_stars, a.max_stars, a.max_candidates)
        save(run / "candidates.json", repos)
    log(f"candidates: {len(repos)}")

    kept, skipped = [], {}
    for r in repos:
        why = triage(r)
        if why:
            skipped[why.split(":")[0]] = skipped.get(why.split(":")[0], 0) + 1
        else:
            kept.append(r)
    kept = kept[: a.max_screen]
    log(f"triage kept {len(kept)}; skipped {skipped}")

    screened = load(run / "screened.json")
    if screened is None:
        sem = asyncio.Semaphore(12)
        tasks = [screen_one(llm, gh, r, sem) for r in kept]
        screened = []
        for i, fut in enumerate(asyncio.as_completed(tasks), 1):
            try:
                screened.append(await fut)
            except BudgetExceeded:
                log("budget hit during screen; stopping screen")
                break
            if i % 50 == 0:
                log(f"screened {i}/{len(kept)}  spend ${llm.spent:.3f}")
        save(run / "screened.json", screened)
    ok = sorted([s for s in screened if "screen" in s], key=lambda s: -s["screen"]["score"])
    log(f"screened ok {len(ok)}, errors {len(screened) - len(ok)}, spend ${llm.spent:.3f}")

    dives = load(run / "deep.json")
    if dives is None:
        clones = run / "clones"
        clones.mkdir(exist_ok=True)
        sem = asyncio.Semaphore(4)

        async def one(r):
            async with sem:
                try:
                    v = await deep_dive(llm, DEEP_MODEL, r, clones)
                    log(f"deep {r['full_name']}: {(v or {}).get('verdict', '')[:90]}")
                    return {**r, "verdict": v}
                except BudgetExceeded:
                    return {**r, "verdict": None, "deep_error": "budget"}
                except Exception as e:
                    return {**r, "verdict": None, "deep_error": str(e)[:300]}

        dives = await asyncio.gather(*[one(r) for r in ok[: a.deep]])
        shutil.rmtree(clones, ignore_errors=True)
        save(run / "deep.json", dives)
    log(f"deep dives: {sum(1 for d in dives if d.get('verdict'))} ok, spend ${llm.spent:.3f}")

    ranking = load(run / "ranking.json")
    if ranking is None:
        ranking = await judge(llm, [d for d in dives if d.get("verdict")])
        save(run / "ranking.json", ranking)

    stats = {"candidates": len(repos), "triage_kept": len(kept), "triage_skipped": skipped,
             "screened_ok": len(ok), "deep": len(dives), "spend_usd": round(llm.total_from_ledger(), 4)}
    save(run / "stats.json", stats)
    write_report(run, ranking, dives, ok, stats)
    log(f"done. report: {run / 'report.html'}  total spend ${stats['spend_usd']}")


if __name__ == "__main__":
    asyncio.run(main())
