"""
AI Strategy Engine
Generates optimization strategies using Claude (Anthropic API)
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from anthropic import Anthropic
from dotenv import load_dotenv

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class StrategyEngine:
    """AI-powered strategy generation using Claude"""

    def __init__(self):
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set in config.env")

        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-5-20250929"  # Latest Claude model
        logger.info("Strategy engine initialized with Claude")

    def generate_strategy(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate an optimization strategy based on current data

        Args:
            data: Dictionary containing:
                - metrics: Latest performance metrics
                - feedback: Recent user feedback
                - goals: Business goals (e.g., target MRR, ARPU)
                - current_features: Current app features
                - constraints: Constraints (e.g., budget, risk tolerance)

        Returns:
            Strategy dictionary with summary, changes, expected_impact, priority
        """
        logger.info("Generating strategy...")

        try:
            # Prepare context for Claude
            context = self._prepare_context(data)

            # Generate strategy using Claude
            strategy = self._call_claude(context)

            logger.info(f"Strategy generated: {strategy['summary'][:100]}...")
            return strategy

        except Exception as e:
            logger.error(f"Error generating strategy: {e}")
            raise

    def _prepare_context(self, data: Dict[str, Any]) -> str:
        """Prepare context prompt for Claude"""
        metrics = data.get('metrics', {})
        feedback = data.get('feedback', [])
        goals = data.get('goals', {})
        current_features = data.get('current_features', [])
        constraints = data.get('constraints', {})

        # Extract key metrics
        dau = metrics.get('dau', 'N/A')
        mau = metrics.get('mau', 'N/A')
        arpu = metrics.get('arpu', 'N/A')
        revenue = metrics.get('revenue', 'N/A')
        churn_rate = metrics.get('churn_rate', 'N/A')
        gen_success_rate = metrics.get('gen_success_rate', 'N/A')
        credit_usage = metrics.get('credit_usage', 'N/A')
        avg_session_iterations = metrics.get('avg_session_iterations', 'N/A')

        # Summarize feedback
        feedback_summary = self._summarize_feedback(feedback)

        # Build context
        context = f"""You are an AI optimization strategist for Gentube.ai, an AI image/video generation SaaS platform that aims to be "Netflix for everyday producers."

Your task is to analyze the current data and propose 1-3 data-driven optimization strategies that can achieve incremental gains (5-20% lifts) in key metrics.

## Current Metrics
- Daily Active Users (DAU): {dau}
- Monthly Active Users (MAU): {mau}
- Average Revenue Per User (ARPU): ${arpu}
- Total Revenue: ${revenue}
- Churn Rate: {churn_rate}%
- Generation Success Rate: {gen_success_rate}%
- Credit Usage: {credit_usage}
- Avg Session Iterations: {avg_session_iterations}

## User Feedback Summary
{feedback_summary}

## Business Goals
- Target Monthly Recurring Revenue (MRR): ${goals.get('target_mrr', 10000)}
- Target ARPU: ${goals.get('target_arpu', 20)}
- Target Retention Rate: {goals.get('target_retention', 85)}%

## Current Features
{', '.join(current_features) if current_features else 'Basic AI generation features'}

## Constraints
- Risk Tolerance: {constraints.get('risk_tolerance', 'Low')}
- Budget: ${constraints.get('budget', 1000)}/month
- Time Horizon: {constraints.get('time_horizon', '1-2 weeks')}

## Your Task
Analyze the data above and propose 1-3 specific, actionable optimization strategies. For each strategy:

1. Focus on high-impact, low-risk changes first
2. Base recommendations on actual data patterns
3. Provide clear expected impact (be realistic, aim for 5-20% gains)
4. Specify concrete changes needed (e.g., UI tweaks, prompt modifications, feature additions)
5. Consider quick wins that can compound over time

Prioritize:
- User retention improvements
- Conversion rate optimization
- Generation success rate improvements
- ARPU increases through better monetization
- Paths toward UGC features (sharing, galleries) if metrics are healthy

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
{{
  "summary": "Brief 1-2 sentence summary of the overall strategy",
  "expected_impact": "Expected metric improvements (e.g., '+15% gen success rate, +10% retention')",
  "focus_area": "Primary focus (e.g., 'retention', 'monetization', 'user_experience')",
  "priority": 1-10,
  "changes": [
    {{
      "action": "Specific change to implement",
      "type": "ui_change|model_change|feature_addition|prompt_optimization|pricing_adjustment",
      "details": "Technical details of the change",
      "expected_impact": "Expected impact of this specific change",
      "risk_level": "low|medium|high",
      "effort": "low|medium|high",
      "files_to_modify": ["list", "of", "files"],
      "test_plan": "How to test this change"
    }}
  ]
}}"""

        return context

    def _summarize_feedback(self, feedback: List[Dict]) -> str:
        """Summarize user feedback for context"""
        if not feedback:
            return "No recent feedback available."

        # Group by sentiment
        positive = [f for f in feedback if f.get('sentiment') == 'positive']
        negative = [f for f in feedback if f.get('sentiment') == 'negative']
        neutral = [f for f in feedback if f.get('sentiment') == 'neutral']

        summary = f"Total: {len(feedback)} feedback items\n"
        summary += f"- Positive: {len(positive)}\n"
        summary += f"- Negative: {len(negative)}\n"
        summary += f"- Neutral: {len(neutral)}\n\n"

        # Add sample negative feedback (most actionable)
        if negative:
            summary += "Recent Issues:\n"
            for fb in negative[:3]:
                summary += f"- {fb.get('text', '')[:100]}\n"

        # Add sample positive feedback
        if positive:
            summary += "\nWhat's Working:\n"
            for fb in positive[:2]:
                summary += f"- {fb.get('text', '')[:100]}\n"

        return summary

    def _call_claude(self, context: str) -> Dict[str, Any]:
        """Call Claude API to generate strategy"""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                temperature=0.7,
                messages=[{
                    "role": "user",
                    "content": context
                }]
            )

            # Extract text content
            content = response.content[0].text

            # Parse JSON response
            strategy = json.loads(content)

            # Validate required fields
            required_fields = ['summary', 'expected_impact', 'focus_area', 'priority', 'changes']
            for field in required_fields:
                if field not in strategy:
                    raise ValueError(f"Missing required field: {field}")

            return strategy

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            logger.error(f"Response content: {content}")
            raise ValueError("Claude did not return valid JSON")

        except Exception as e:
            logger.error(f"Error calling Claude API: {e}")
            raise

    def evaluate_strategy_success(self, metrics_before: Dict, metrics_after: Dict,
                                   expected_impact: str) -> Dict[str, Any]:
        """
        Evaluate if a deployed strategy was successful

        Args:
            metrics_before: Metrics before deployment
            metrics_after: Metrics after deployment
            expected_impact: Expected impact from strategy

        Returns:
            Evaluation with success indicators
        """
        logger.info("Evaluating strategy success...")

        try:
            context = f"""You are evaluating the success of an optimization strategy for Gentube.ai.

## Metrics Before Deployment
{json.dumps(metrics_before, indent=2)}

## Metrics After Deployment
{json.dumps(metrics_after, indent=2)}

## Expected Impact
{expected_impact}

## Your Task
Analyze the before/after metrics and determine if the strategy was successful. Calculate actual changes and compare to expected impact.

Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
{{
  "success": true/false,
  "actual_impact": "Description of actual changes observed",
  "key_improvements": ["list", "of", "improvements"],
  "key_regressions": ["list", "of", "regressions"],
  "recommendations": "Next steps based on results",
  "priority_score": 1-10,
  "metrics_comparison": {{
    "dau_change_percent": number,
    "revenue_change_percent": number,
    "retention_change_percent": number
  }}
}}"""

            response = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                temperature=0.5,
                messages=[{
                    "role": "user",
                    "content": context
                }]
            )

            content = response.content[0].text
            evaluation = json.loads(content)

            logger.info(f"Strategy evaluation: {'SUCCESS' if evaluation.get('success') else 'FAILED'}")
            return evaluation

        except Exception as e:
            logger.error(f"Error evaluating strategy: {e}")
            raise

    def analyze_trends(self, historical_metrics: List[Dict]) -> Dict[str, Any]:
        """
        Analyze trends across multiple cycles

        Args:
            historical_metrics: List of metric snapshots over time

        Returns:
            Trend analysis with insights
        """
        if len(historical_metrics) < 2:
            return {
                "trends": "Insufficient data for trend analysis",
                "insights": []
            }

        try:
            context = f"""Analyze the following historical metrics for Gentube.ai and identify trends:

{json.dumps(historical_metrics, indent=2)}

Respond with valid JSON only:
{{
  "trends": "Overall trend description",
  "insights": ["list", "of", "key", "insights"],
  "recommendations": ["recommended", "focus", "areas"]
}}"""

            response = self.client.messages.create(
                model=self.model,
                max_tokens=1000,
                temperature=0.5,
                messages=[{
                    "role": "user",
                    "content": context
                }]
            )

            content = response.content[0].text
            return json.loads(content)

        except Exception as e:
            logger.error(f"Error analyzing trends: {e}")
            return {"trends": "Analysis failed", "insights": []}


# Singleton instance
_strategy_engine = None

def get_strategy_engine() -> StrategyEngine:
    """Get or create strategy engine singleton"""
    global _strategy_engine
    if _strategy_engine is None:
        _strategy_engine = StrategyEngine()
    return _strategy_engine
