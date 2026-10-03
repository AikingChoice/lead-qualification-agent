from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .analyzers import ArticleAnalyzer
from .models import ArticleAnalysis, FeedEntry, RuleConfig, RuleDecision
from .rules import evaluate_rules


class AnalysisState(TypedDict, total=False):
    entry: FeedEntry
    analysis: ArticleAnalysis
    rule_decision: RuleDecision


def build_analysis_graph(analyzer: ArticleAnalyzer, config: RuleConfig) -> Any:
    """A small, inspectable workflow: model analysis followed by deterministic rules."""

    def analyze(state: AnalysisState) -> dict[str, ArticleAnalysis]:
        return {"analysis": ArticleAnalysis.model_validate(
            analyzer.analyze(state["entry"])
        )}

    def apply_rules(state: AnalysisState) -> dict[str, RuleDecision]:
        return {"rule_decision": evaluate_rules(state["analysis"], config)}

    graph = StateGraph(AnalysisState)
    graph.add_node("analyze_article", analyze)
    graph.add_node("apply_rules", apply_rules)
    graph.add_edge(START, "analyze_article")
    graph.add_edge("analyze_article", "apply_rules")
    graph.add_edge("apply_rules", END)
    return graph.compile()
