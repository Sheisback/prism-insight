"""After-close re-entry v2 SHADOW runner (pivot breakout). No orders; one BUY-agent recheck per
forward trigger only when REENTRY_V2_LLM_RECHECK=true (.env or environment; default off),
never with --no-llm or a dry run.

    python tools/run_reentry_v2_shadow.py --market KR
    python tools/run_reentry_v2_shadow.py --market US --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import reentry_v2_shadow as V2  # noqa: E402
from tools import run_reentry_shadow as collectors  # noqa: E402

# Pivot detection needs ~65 + 55 sessions before a watch that may have started 70 days ago.
HISTORY_DAYS = 320
KR_CACHE_DIR = ROOT / "runtime/reentry_v2_kr_daily_cache"
log = logging.getLogger("reentry_v2_shadow")


def collect(market, tickers, completed):
    collectors.HISTORY_DAYS = HISTORY_DAYS
    if market == "KR":
        return collectors.collect_kr(tickers, completed, cache_dir=KR_CACHE_DIR)
    return collectors.collect_us(tickers, completed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(V2.DB_PATH))
    parser.add_argument("--state-root", default=str(V2.STATE_DIR))
    parser.add_argument("--reports-root", default=str(ROOT))
    parser.add_argument("--archive-db", default=str(V2.ARCHIVE_DB))
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM recheck of new triggers")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)      # explicit environment (e.g. cron) wins
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not V2.enabled(args.market) and not args.dry_run:
        log.info("reentry v2 shadow disabled for %s", args.market)
        return 0
    completed = collectors.completed_session(args.market)
    summary = V2.run(args.market, completed, collector=lambda t, c: collect(args.market, t, c), db_path=args.db,
                     root=args.state_root, reports_root=args.reports_root, archive_db=args.archive_db,
                     dry_run=args.dry_run, llm_recheck=False if args.no_llm else None)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
