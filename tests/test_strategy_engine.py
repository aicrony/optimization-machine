"""
Unit tests for Strategy Engine
"""

import pytest
from unittest.mock import Mock, patch
from strategy_engine import StrategyEngine


@pytest.fixture
def strategy_engine():
    """Create a strategy engine instance for testing"""
    with patch('strategy_engine.Anthropic'):
        engine = StrategyEngine()
        return engine


def test_generate_strategy_success(strategy_engine):
    """Test successful strategy generation"""
    # Mock data
    data = {
        'metrics': {
            'dau': 100,
            'mau': 500,
            'arpu': 15,
            'revenue': 7500,
            'churn_rate': 10,
            'gen_success_rate': 85,
            'credit_usage': 10000,
            'avg_session_iterations': 3.5
        },
        'feedback': [
            {'text': 'Great app!', 'sentiment': 'positive', 'rating': 5},
            {'text': 'Slow loading', 'sentiment': 'negative', 'rating': 2}
        ],
        'goals': {
            'target_mrr': 10000,
            'target_arpu': 20,
            'target_retention': 85
        },
        'current_features': ['AI Generation', 'Credit System'],
        'constraints': {
            'risk_tolerance': 'Low',
            'budget': 1000
        }
    }

    # Mock Claude API response
    mock_response = Mock()
    mock_response.content = [Mock(text='{"summary": "Test strategy", "expected_impact": "+10% DAU", "focus_area": "retention", "priority": 8, "changes": [{"action": "Test change", "type": "ui_change", "details": "Test", "expected_impact": "+5%", "risk_level": "low", "effort": "low", "files_to_modify": ["test.js"], "test_plan": "Manual testing"}]}')]

    strategy_engine.client.messages.create = Mock(return_value=mock_response)

    # Test
    strategy = strategy_engine.generate_strategy(data)

    # Assertions
    assert 'summary' in strategy
    assert 'changes' in strategy
    assert 'expected_impact' in strategy
    assert strategy['priority'] > 0


def test_summarize_feedback(strategy_engine):
    """Test feedback summarization"""
    feedback = [
        {'text': 'Great app!', 'sentiment': 'positive'},
        {'text': 'Love it', 'sentiment': 'positive'},
        {'text': 'Too slow', 'sentiment': 'negative'},
        {'text': 'Okay', 'sentiment': 'neutral'}
    ]

    summary = strategy_engine._summarize_feedback(feedback)

    assert 'Total: 4' in summary
    assert 'Positive: 2' in summary
    assert 'Negative: 1' in summary


def test_evaluate_strategy_success(strategy_engine):
    """Test strategy evaluation"""
    metrics_before = {
        'dau': 100,
        'revenue': 5000,
        'retention': 80
    }

    metrics_after = {
        'dau': 115,
        'revenue': 5750,
        'retention': 85
    }

    mock_response = Mock()
    mock_response.content = [Mock(text='{"success": true, "actual_impact": "+15% DAU, +15% revenue", "key_improvements": ["DAU increased", "Revenue increased"], "key_regressions": [], "recommendations": "Continue similar optimizations", "priority_score": 9, "metrics_comparison": {"dau_change_percent": 15, "revenue_change_percent": 15, "retention_change_percent": 6.25}}')]

    strategy_engine.client.messages.create = Mock(return_value=mock_response)

    evaluation = strategy_engine.evaluate_strategy_success(
        metrics_before,
        metrics_after,
        "+10% DAU"
    )

    assert 'success' in evaluation
    assert 'actual_impact' in evaluation
