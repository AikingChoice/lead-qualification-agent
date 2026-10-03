from .models import ArticleAnalysis, RuleConfig, RuleDecision, RuleDisposition


def evaluate_rules(analysis: ArticleAnalysis, config: RuleConfig) -> RuleDecision:
    if not analysis.relevant:
        return RuleDecision(
            disposition=RuleDisposition.REJECT,
            reason="Article is not relevant.",
        )
    if analysis.category not in config.allowed_categories:
        return RuleDecision(
            disposition=RuleDisposition.REJECT,
            reason=f"Category {analysis.category.value} is not allowed.",
        )
    return RuleDecision(
        disposition=RuleDisposition.ALLOW,
        reason="Category rules passed; create a research review signal.",
    )
