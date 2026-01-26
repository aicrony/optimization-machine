"""
Unit tests for Safety Net
"""

import pytest
from unittest.mock import Mock, patch
from safety_net import SafetyNet


@pytest.fixture
def safety_net():
    """Create a safety net instance for testing"""
    with patch('safety_net.get_db_handler'):
        net = SafetyNet()
        return net


def test_validate_changes_safe(safety_net):
    """Test validation of safe changes"""
    changes = [
        {
            'action': 'Improve UI responsiveness',
            'type': 'ui_change',
            'risk_level': 'low',
            'effort': 'low',
            'files_to_modify': ['app.js'],
            'test_plan': 'Manual testing'
        }
    ]

    validation = safety_net.validate_changes(changes)

    assert validation['safe'] is True
    assert len(validation['errors']) == 0


def test_validate_changes_high_risk(safety_net):
    """Test validation with high-risk changes"""
    changes = [
        {
            'action': 'Rewrite payment system',
            'type': 'feature_addition',
            'risk_level': 'high',
            'effort': 'high',
            'files_to_modify': ['payment.js'],
            'test_plan': 'Unit tests'
        }
    ]

    validation = safety_net.validate_changes(changes)

    # Should still be safe but have warnings
    assert validation['safe'] is True
    assert len(validation['warnings']) > 0
    assert any('High risk' in w for w in validation['warnings'])


def test_validate_changes_unsafe_pricing(safety_net):
    """Test validation of unsafe pricing changes"""
    changes = [
        {
            'action': 'Reduce prices by 50%',
            'type': 'pricing_adjustment',
            'details': 'Decrease all prices by 50%',
            'risk_level': 'high',
            'effort': 'low',
            'files_to_modify': ['pricing.json'],
            'test_plan': 'Review'
        }
    ]

    validation = safety_net.validate_changes(changes)

    # Should be unsafe due to large price drop
    assert validation['safe'] is False


def test_detect_anomalies_critical(safety_net):
    """Test detection of critical anomalies"""
    metrics_before = {
        'revenue': 10000,
        'dau': 1000,
        'gen_success_rate': 90
    }

    metrics_after = {
        'revenue': 6000,  # 40% drop
        'dau': 950,       # 5% drop
        'gen_success_rate': 88
    }

    anomalies = safety_net._detect_anomalies(metrics_before, metrics_after)

    assert len(anomalies['critical']) > 0
    assert any('revenue' in a.lower() for a in anomalies['critical'])


def test_should_rollback_decision(safety_net):
    """Test rollback decision logic"""
    metrics_before = {
        'revenue': 10000,
        'dau': 1000
    }

    # Scenario 1: Critical drop
    metrics_after_bad = {
        'revenue': 6000,  # 40% drop
        'dau': 700        # 30% drop
    }

    decision = safety_net.should_rollback(metrics_before, metrics_after_bad)
    assert decision['should_rollback'] is True

    # Scenario 2: Good metrics
    metrics_after_good = {
        'revenue': 11000,
        'dau': 1100
    }

    decision = safety_net.should_rollback(metrics_before, metrics_after_good)
    assert decision['should_rollback'] is False
