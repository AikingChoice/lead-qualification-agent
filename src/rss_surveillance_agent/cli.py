import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from .analyzers import KeywordAnalyzer, OpenAIAnalyzer
from .miniflux import MinifluxSource
from .models import ArticleCategory, ProcessingStatus, RuleConfig
from .notifications import SlackWebhookNotifier, deliver_pending
from .pipeline import SurveillancePipeline
from .storage import SQLiteEntryStore, create_store
from .worker import process_batch


def main() -> None:
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="RSS article surveillance workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo_parser = subparsers.add_parser("demo", help="Run offline sample data")
    demo_parser.add_argument("--db", default="rss-demo.sqlite3")
    demo_parser.add_argument("--entries", type=Path, default=(
        Path(__file__).parents[2] / "tests" / "fixtures" / "entries.json"
    ))
    demo_parser.add_argument("--simulate-timeout", action="store_true")
    run_parser = subparsers.add_parser("run", help="Poll Miniflux once")
    run_parser.add_argument("--db", default=os.getenv("RSS_AGENT_DB", "rss-agent.sqlite3"))
    run_parser.add_argument("--max-entries", type=int, default=20)
    run_parser.add_argument(
        "--daily-model-limit", type=int,
        default=int(os.getenv("DAILY_MODEL_CALL_LIMIT", "200")),
    )
    run_parser.add_argument("--initial-lookback-hours", type=int, default=48)
    run_parser.add_argument("--analyzer", choices=("openai", "keyword"), default="openai")
    run_parser.add_argument("--deliver-notifications", action="store_true")
    run_parser.add_argument(
        "--allowed-categories", nargs="*", choices=[c.value for c in ArticleCategory],
        default=None,
    )
    health_parser = subparsers.add_parser("health", help="Check database and poll freshness")
    health_parser.add_argument("--db", default=os.getenv("RSS_AGENT_DB", "rss-agent.sqlite3"))
    health_parser.add_argument("--max-staleness-seconds", type=int, default=900)
    outbox_parser = subparsers.add_parser("outbox", help="Inspect or retry dead-letter notifications")
    outbox_parser.add_argument("--db", default=os.getenv("RSS_AGENT_DB", "rss-agent.sqlite3"))
    outbox_parser.add_argument("--retry-dead-letters", action="store_true")
    outbox_parser.add_argument("--entry-id", type=int, action="append")
    args = parser.parse_args()
    if args.command == "demo":
        _run_demo(args)
    elif args.command == "run":
        _run_live(args)
    elif args.command == "health":
        _run_health(args)
    else:
        _run_outbox(args)


def _run_demo(args: argparse.Namespace) -> None:
    from .demo import TimeoutAnalyzer
    from .models import FeedEntry

    entries = [
        FeedEntry.model_validate(item)
        for item in json.loads(args.entries.read_text(encoding="utf-8"))
    ]
    analyzer = TimeoutAnalyzer() if args.simulate_timeout else KeywordAnalyzer()
    with SQLiteEntryStore(args.db) as store:
        pipeline = SurveillancePipeline(analyzer, store)
        for entry in entries:
            result = pipeline.process(entry)
            print(f"entry_id={entry.id} {result.model_dump_json()}")
        print(json.dumps(store.counts()))


def _run_live(args: argparse.Namespace) -> None:
    base_url = os.getenv("MINIFLUX_URL")
    token = os.getenv("MINIFLUX_API_TOKEN")
    if not base_url or not token:
        raise SystemExit("Set MINIFLUX_URL and MINIFLUX_API_TOKEN first.")
    if args.max_entries < 1:
        raise SystemExit("--max-entries must be at least 1.")
    if args.daily_model_limit < 1 or args.initial_lookback_hours < 1:
        raise SystemExit("Daily model limit and initial lookback must be positive.")
    if args.analyzer == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("Set OPENAI_API_KEY or use --analyzer keyword.")
        analyzer = OpenAIAnalyzer(
            api_key=api_key,
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )
    else:
        analyzer = KeywordAnalyzer()
    allowed = None if args.allowed_categories is None else {
        ArticleCategory(value) for value in args.allowed_categories
    }
    config = RuleConfig() if allowed is None else RuleConfig(allowed_categories=allowed)
    source = MinifluxSource(base_url, token)
    with create_store(args.db) as store:
        run_id = str(uuid4())
        store.start_poll(run_id, "miniflux")
        counts = {"processed_this_run": 0}
        try:
            pipeline = SurveillancePipeline(
                analyzer, store, config, daily_model_limit=args.daily_model_limit
            )
            cursor = store.get_cursor("miniflux")
            initial_cutoff = None
            if cursor == 0:
                initial_cutoff = int(
                    (datetime.now(UTC) - timedelta(
                        hours=args.initial_lookback_hours
                    )).timestamp()
                )
            counts = process_batch(
                "miniflux",
                source.fetch_after(cursor, published_after=initial_cutoff),
                pipeline,
                max_entries=args.max_entries,
            )
            delivery = {"sent": 0, "failed": 0}
            if args.deliver_notifications:
                webhook = os.getenv("SLACK_WEBHOOK_URL")
                if not webhook:
                    raise RuntimeError("Set SLACK_WEBHOOK_URL to deliver notifications.")
                delivery = deliver_pending(store, SlackWebhookNotifier(webhook))
            counts["delivery"] = delivery
            counts["database"] = store.counts()
            deferred = counts.get(ProcessingStatus.DEFERRED.value, 0) > 0
            delivery_failed = delivery["failed"] > 0
            store.finish_poll(
                run_id,
                status="degraded" if deferred or delivery_failed else "succeeded",
                processed_count=counts["processed_this_run"],
                error_code=(
                    "notification_delivery_failed" if delivery_failed
                    else "article_deferred" if deferred
                    else None
                ),
            )
            print(json.dumps(counts, ensure_ascii=False, indent=2))
        except Exception as exc:
            store.finish_poll(
                run_id,
                status="failed",
                processed_count=counts.get("processed_this_run", 0),
                error_code=type(exc).__name__,
            )
            raise


def _run_health(args: argparse.Namespace) -> None:
    with create_store(args.db) as store:
        latest = store.latest_poll()
        now = datetime.now(UTC)
        healthy = latest is not None and latest["status"] == "succeeded"
        age_seconds = None
        if latest and latest["finished_at"]:
            finished = datetime.fromisoformat(latest["finished_at"])
            age_seconds = max(0, int((now - finished).total_seconds()))
            healthy = healthy and age_seconds <= args.max_staleness_seconds
        else:
            healthy = False
        output = {
            "status": "healthy" if healthy else "unhealthy",
            "latest_poll": latest,
            "age_seconds": age_seconds,
        }
        print(json.dumps(output, ensure_ascii=False, default=str))
        if not healthy:
            raise SystemExit(1)


def _run_outbox(args: argparse.Namespace) -> None:
    if args.entry_id and not args.retry_dead_letters:
        raise SystemExit("Use --retry-dead-letters when specifying --entry-id.")
    with create_store(args.db) as store:
        retried = 0
        if args.retry_dead_letters:
            retried = store.retry_dead_letters(args.entry_id)
        print(json.dumps({"retried": retried, "counts": store.outbox_counts()}))


if __name__ == "__main__":
    main()
