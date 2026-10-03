import argparse
import json
from pathlib import Path

from .analyzers import KeywordAnalyzer
from .models import ArticleCategory, FeedEntry, RuleConfig
from .pipeline import SurveillancePipeline
from .storage import SQLiteEntryStore


class TimeoutAnalyzer:
    def analyze(self, entry: FeedEntry):
        raise TimeoutError("Simulated timeout")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline RSS workflow lesson")
    parser.add_argument("--db", default="rss-demo.sqlite3")
    parser.add_argument("--entries", type=Path, default=(
        Path(__file__).parents[2] / "tests" / "fixtures" / "entries.json"
    ))
    parser.add_argument("--simulate-timeout", action="store_true")
    parser.add_argument(
        "--allowed-categories", nargs="*", choices=list(ArticleCategory),
        default=None,
    )
    args = parser.parse_args()
    fixture_path = args.entries
    entries = [
        FeedEntry.model_validate(item)
        for item in json.loads(fixture_path.read_text(encoding="utf-8"))
    ]
    config = RuleConfig() if args.allowed_categories is None else RuleConfig(
        allowed_categories=set(args.allowed_categories)
    )
    with SQLiteEntryStore(args.db) as store:
        pipeline = SurveillancePipeline(
            analyzer=TimeoutAnalyzer() if args.simulate_timeout else KeywordAnalyzer(),
            store=store,
            config=config,
        )
        for entry in entries:
            result = pipeline.process(entry)
            print(f"entry_id={entry.id}")
            print(result.model_dump_json(indent=2))
        print(json.dumps(store.counts()))


if __name__ == "__main__":
    main()
