"""
Unit tests for Data Collector
"""

import pytest
from unittest.mock import Mock, patch
from data_collector import DataCollector


@pytest.fixture
def data_collector():
    """Create a data collector instance for testing"""
    with patch('data_collector.get_db_handler'):
        with patch('data_collector.stripe'):
            collector = DataCollector()
            return collector


def test_categorize_feedback(data_collector):
    """Test feedback categorization"""
    feedback = [
        {'text': 'The image quality is blurry'},
        {'text': 'Very slow loading times'},
        {'text': 'Confusing user interface'},
        {'text': 'Too expensive for what it offers'},
        {'text': 'I found a bug in the export feature'}
    ]

    categories = data_collector._categorize_feedback(feedback)

    assert categories['generation_quality'] >= 1
    assert categories['performance'] >= 1
    assert categories['ui_ux'] >= 1
    assert categories['pricing'] >= 1
    assert categories['bugs'] >= 1


def test_calculate_derived_metrics(data_collector):
    """Test derived metrics calculation"""
    metrics = {
        'stripe': {
            'revenue': 5000,
            'successful_charges': 95,
            'failed_charges': 5,
            'active_subscriptions': 80,
            'active_customers': 100
        },
        'app_usage': {
            'dau': 250,
            'mau': 1000,
            'total_generations': 10000
        }
    }

    derived = data_collector._calculate_derived_metrics(metrics)

    # ARPU = Revenue / MAU
    assert derived['arpu'] == 5.0

    # Churn rate
    assert derived['churn_rate'] == 20.0

    # DAU/MAU ratio
    assert derived['dau_mau_ratio'] == 0.25

    # Revenue per generation
    assert derived['revenue_per_generation'] == 0.5

    # Payment success rate
    assert derived['payment_success_rate'] == 95.0


def test_compare_metrics(data_collector):
    """Test metrics comparison"""
    metrics_before = {
        'dau': 100,
        'revenue': 5000,
        'arpu': 10
    }

    metrics_after = {
        'dau': 120,
        'revenue': 6000,
        'arpu': 12
    }

    comparison = data_collector.compare_metrics(metrics_before, metrics_after)

    assert 'changes' in comparison
    assert comparison['changes']['dau']['change_percent'] == 20.0
    assert comparison['changes']['dau']['direction'] == 'up'
    assert comparison['changes']['revenue']['change_percent'] == 20.0
    assert comparison['changes']['arpu']['change_percent'] == 20.0


def test_analyze_feedback_sentiments(data_collector):
    """Test sentiment analysis on feedback"""
    if not data_collector.sentiment_analyzer:
        pytest.skip("Sentiment analyzer not available")

    texts = [
        "This app is amazing! Love the quality.",
        "Terrible experience, very disappointed.",
        "It's okay, nothing special."
    ]

    analysis = data_collector.analyze_feedback(texts)

    assert analysis['total'] == 3
    assert 'positive' in analysis
    assert 'negative' in analysis
    assert analysis['positive'] + analysis['negative'] <= analysis['total']
