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
        {'text': 'There is a bug causing errors when exporting'}
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


# ============ Google Analytics 4 Tests ============

def test_collect_ga4_metrics_not_configured(data_collector):
    """Test GA4 collection when not configured"""
    data_collector.ga4_client = None
    data_collector.ga4_property_id = None

    result = data_collector._collect_ga4_metrics()

    assert 'error' in result
    assert 'not configured' in result['error']


def test_collect_ga4_metrics_success(data_collector):
    """Test successful GA4 metrics collection with mocked client"""
    # Create mock response for engagement metrics
    mock_engagement_row = Mock()
    mock_engagement_row.metric_values = [
        Mock(value='1000'),   # sessions
        Mock(value='800'),    # totalUsers
        Mock(value='750'),    # activeUsers
        Mock(value='5000'),   # screenPageViews
        Mock(value='0.45'),   # bounceRate
        Mock(value='180.5'),  # averageSessionDuration
        Mock(value='600'),    # engagedSessions
        Mock(value='50000'),  # userEngagementDuration
    ]

    mock_engagement_response = Mock()
    mock_engagement_response.rows = [mock_engagement_row]
    # Note: Mock(name=...) sets the mock's debug name, not an attribute.
    # We need to create mocks and set name attribute separately.
    metric_headers = []
    for metric_name in ['sessions', 'totalUsers', 'activeUsers', 'screenPageViews',
                        'bounceRate', 'averageSessionDuration', 'engagedSessions',
                        'userEngagementDuration']:
        header = Mock()
        header.name = metric_name
        metric_headers.append(header)
    mock_engagement_response.metric_headers = metric_headers

    # Create mock response for traffic sources
    mock_traffic_row = Mock()
    mock_traffic_row.dimension_values = [Mock(value='Organic Search')]
    mock_traffic_row.metric_values = [Mock(value='500'), Mock(value='400'), Mock(value='10')]

    mock_traffic_response = Mock()
    mock_traffic_response.rows = [mock_traffic_row]

    # Create mock response for pages
    mock_pages_row = Mock()
    mock_pages_row.dimension_values = [Mock(value='/studio')]
    mock_pages_row.metric_values = [Mock(value='1000'), Mock(value='120.5')]

    mock_pages_response = Mock()
    mock_pages_response.rows = [mock_pages_row]

    # Create mock response for conversions (empty)
    mock_conversion_response = Mock()
    mock_conversion_response.rows = []

    # Set up the mock client
    mock_client = Mock()
    mock_client.run_report.side_effect = [
        mock_engagement_response,
        mock_traffic_response,
        mock_pages_response,
        mock_conversion_response,
    ]

    data_collector.ga4_client = mock_client
    data_collector.ga4_property_id = 'properties/123456'

    result = data_collector._collect_ga4_metrics()

    assert result['sessions'] == 1000
    assert result['total_users'] == 800
    assert result['active_users'] == 750
    assert result['pageviews'] == 5000
    assert result['bounce_rate'] == 45.0  # 0.45 * 100
    assert result['avg_session_duration_seconds'] == 180.5
    assert result['engaged_sessions'] == 600
    assert 'Organic Search' in result['traffic_sources']
    assert len(result['top_pages']) == 1
    assert result['period_days'] == 7


# ============ CrUX API Tests ============

def test_collect_crux_metrics_not_configured(data_collector):
    """Test CrUX collection when API key not configured"""
    data_collector.crux_api_key = None

    result = data_collector._collect_crux_metrics()

    assert 'error' in result
    assert 'not configured' in result['error']


def test_collect_crux_metrics_no_origin(data_collector):
    """Test CrUX collection when origin not configured"""
    data_collector.crux_api_key = 'test-key'
    data_collector.gentube_origin = None

    result = data_collector._collect_crux_metrics()

    assert 'error' in result
    assert 'GENTUBE_ORIGIN' in result['error']


@patch('data_collector.requests.post')
def test_collect_crux_metrics_success(mock_post, data_collector):
    """Test successful CrUX metrics collection"""
    # Mock mobile response
    mock_mobile_response = Mock()
    mock_mobile_response.status_code = 200
    mock_mobile_response.json.return_value = {
        'record': {
            'metrics': {
                'largest_contentful_paint': {
                    'percentiles': {'p75': 2100},
                    'histogram': [
                        {'density': 0.65},
                        {'density': 0.25},
                        {'density': 0.10},
                    ]
                },
                'cumulative_layout_shift': {
                    'percentiles': {'p75': 0.08},
                    'histogram': [
                        {'density': 0.80},
                        {'density': 0.15},
                        {'density': 0.05},
                    ]
                },
                'interaction_to_next_paint': {
                    'percentiles': {'p75': 150},
                    'histogram': [
                        {'density': 0.70},
                        {'density': 0.20},
                        {'density': 0.10},
                    ]
                },
                'first_contentful_paint': {
                    'percentiles': {'p75': 1800},
                    'histogram': [
                        {'density': 0.60},
                        {'density': 0.30},
                        {'density': 0.10},
                    ]
                },
            }
        }
    }

    # Mock desktop response
    mock_desktop_response = Mock()
    mock_desktop_response.status_code = 200
    mock_desktop_response.json.return_value = {
        'record': {
            'metrics': {
                'largest_contentful_paint': {'percentiles': {'p75': 1800}},
                'cumulative_layout_shift': {'percentiles': {'p75': 0.05}},
                'interaction_to_next_paint': {'percentiles': {'p75': 100}},
            }
        }
    }

    mock_post.side_effect = [mock_mobile_response, mock_desktop_response]

    data_collector.crux_api_key = 'test-key'
    data_collector.gentube_origin = 'https://gentube.ai'

    result = data_collector._collect_crux_metrics()

    assert result['lcp']['p75_ms'] == 2100
    assert result['lcp']['status'] == 'good'  # <= 2500
    assert result['lcp']['distribution']['good'] == 65.0

    assert result['cls']['p75'] == 0.08
    assert result['cls']['status'] == 'good'  # <= 0.1

    assert result['inp']['p75_ms'] == 150
    assert result['inp']['status'] == 'good'  # <= 200

    assert result['fcp']['p75_ms'] == 1800

    assert result['core_web_vitals_passing'] == True
    assert result['form_factor'] == 'PHONE'
    assert 'desktop' in result


@patch('data_collector.requests.post')
def test_collect_crux_metrics_insufficient_traffic(mock_post, data_collector):
    """Test CrUX collection when origin has insufficient traffic"""
    mock_response = Mock()
    mock_response.status_code = 404

    mock_post.return_value = mock_response

    data_collector.crux_api_key = 'test-key'
    data_collector.gentube_origin = 'https://small-site.com'

    result = data_collector._collect_crux_metrics()

    assert 'error' in result
    assert result['error'] == 'insufficient_traffic'


# ============ Derived Metrics with Analytics Tests ============

def test_derived_metrics_with_analytics(data_collector):
    """Test derived metrics calculation with GA4 and CrUX data"""
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
        },
        'google_analytics': {
            'sessions': 1500,
            'total_users': 1000,
            'engaged_sessions': 900,
            'pageviews': 6000,
            'bounce_rate': 35.0,
            'conversion_events': {
                'sign_up': {'count': 50},
                'purchase': {'count': 20},
            },
            'traffic_sources': {
                'Organic Search': {'sessions': 500, 'conversions': 10},
                'Direct': {'sessions': 300, 'conversions': 8},
            }
        },
        'web_vitals': {
            'lcp': {'p75_ms': 2000, 'status': 'good', 'distribution': {'good': 70}},
            'cls': {'p75': 0.05, 'status': 'good', 'distribution': {'good': 85}},
            'inp': {'p75_ms': 150, 'status': 'good', 'distribution': {'good': 75}},
            'core_web_vitals_passing': True,
        }
    }

    derived = data_collector._calculate_derived_metrics(metrics)

    # Existing metrics still work
    assert derived['arpu'] == 5.0
    assert derived['churn_rate'] == 20.0
    assert derived['dau_mau_ratio'] == 0.25

    # New GA4-derived metrics
    assert derived['engagement_rate'] == 60.0  # 900/1500 * 100
    assert derived['pages_per_session'] == 4.0  # 6000/1500
    assert derived['signup_rate'] == 5.0  # 50/1000 * 100
    assert derived['purchase_conversion_rate'] == 2.0  # 20/1000 * 100

    # Traffic conversion rate = (10+8)/(500+300) * 100 = 2.25
    assert derived['traffic_conversion_rate'] == 2.25

    # New performance metrics - all good = 100
    assert derived['performance_score'] == 100

    # Good experience pct = (70+85+75)/3 = 76.67
    assert derived['good_experience_pct'] == 76.67

    # Low bounce (35) + good LCP (2000) = low correlation
    assert derived['performance_bounce_correlation'] == 'low'


def test_derived_metrics_high_bounce_correlation(data_collector):
    """Test performance-bounce correlation detection"""
    metrics = {
        'stripe': {'revenue': 1000, 'successful_charges': 10, 'failed_charges': 0,
                   'active_subscriptions': 8, 'active_customers': 10},
        'app_usage': {'dau': 50, 'mau': 200, 'total_generations': 1000},
        'google_analytics': {
            'sessions': 500,
            'total_users': 400,
            'engaged_sessions': 200,
            'pageviews': 1000,
            'bounce_rate': 55.0,  # High bounce rate
        },
        'web_vitals': {
            'lcp': {'p75_ms': 3000, 'status': 'needs_improvement', 'distribution': {'good': 40}},  # Slow LCP
            'cls': {'p75': 0.15, 'status': 'needs_improvement', 'distribution': {'good': 50}},
            'inp': {'p75_ms': 300, 'status': 'needs_improvement', 'distribution': {'good': 45}},
        }
    }

    derived = data_collector._calculate_derived_metrics(metrics)

    # High bounce (55) + slow LCP (3000 > 2500) = high correlation
    assert derived['performance_bounce_correlation'] == 'high'

    # Performance score with needs_improvement = 50 each
    # (50*0.4 + 50*0.2 + 50*0.4) = 50
    assert derived['performance_score'] == 50
