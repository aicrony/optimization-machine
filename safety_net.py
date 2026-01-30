"""
Safety Net & Monitoring
Provides guardrails, validation, and monitoring for the optimization system
"""

import os
import logging
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TwilioClient = None
    TWILIO_AVAILABLE = False
from dotenv import load_dotenv
from db_handler import get_db_handler

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SafetyNet:
    """Safety and monitoring system with guardrails"""

    def __init__(self):
        self.db = get_db_handler()

        # Load safety configuration
        self.max_metric_drop = float(os.getenv('MAX_METRIC_DROP_PERCENT', 20))
        self.rollback_enabled = os.getenv('ROLLBACK_ENABLED', 'true').lower() == 'true'
        self.min_approval_wait = int(os.getenv('MIN_APPROVAL_WAIT_MINUTES', 5))

        # Initialize Twilio for SMS alerts (optional)
        self.twilio_client = None
        self.alert_phone = os.getenv('ALERT_PHONE_NUMBER')

        if TWILIO_AVAILABLE:
            try:
                twilio_sid = os.getenv('TWILIO_ACCOUNT_SID')
                twilio_token = os.getenv('TWILIO_AUTH_TOKEN')
                twilio_phone = os.getenv('TWILIO_PHONE_NUMBER')

                if twilio_sid and twilio_token and twilio_phone:
                    self.twilio_client = TwilioClient(twilio_sid, twilio_token)
                    self.twilio_phone = twilio_phone
                    logger.info("Twilio SMS alerts initialized")
            except Exception as e:
                logger.warning(f"Twilio initialization failed: {e}")

        logger.info("Safety net initialized")

    # ============ PRE-EXECUTION VALIDATION ============

    def validate_changes(self, changes: List[Dict], current_metrics: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate changes before execution

        Args:
            changes: List of proposed changes
            current_metrics: Current performance metrics

        Returns:
            Validation result with safe/unsafe status and warnings
        """
        logger.info("Validating changes...")

        validation = {
            'safe': True,
            'warnings': [],
            'errors': [],
            'recommendations': []
        }

        try:
            for i, change in enumerate(changes):
                change_num = i + 1

                # Check 1: Risk level
                risk = change.get('risk_level', 'medium')
                if risk == 'high':
                    validation['warnings'].append(
                        f"Change {change_num}: High risk - {change.get('action')}"
                    )

                # Check 2: Pricing changes
                if change.get('type') == 'pricing_adjustment':
                    if not self._validate_pricing_change(change, current_metrics):
                        validation['errors'].append(
                            f"Change {change_num}: Unsafe pricing change - exceeds threshold"
                        )
                        validation['safe'] = False

                # Check 3: Files to modify exist
                files = change.get('files_to_modify', [])
                if not files:
                    validation['warnings'].append(
                        f"Change {change_num}: No files specified"
                    )

                # Check 4: Test plan exists
                if not change.get('test_plan'):
                    validation['warnings'].append(
                        f"Change {change_num}: No test plan specified"
                    )

                # Check 5: Conflicting changes
                conflicts = self._check_conflicts(change, changes)
                if conflicts:
                    validation['warnings'].append(
                        f"Change {change_num}: May conflict with other changes"
                    )

            # Overall risk assessment
            high_risk_count = sum(1 for c in changes if c.get('risk_level') == 'high')
            if high_risk_count > 1:
                validation['warnings'].append(
                    f"Multiple high-risk changes ({high_risk_count}) in single deployment"
                )
                validation['recommendations'].append(
                    "Consider splitting into separate deployments"
                )

        except Exception as e:
            logger.error(f"Validation error: {e}")
            validation['errors'].append(f"Validation failed: {e}")
            validation['safe'] = False

        logger.info(f"Validation result: {'SAFE' if validation['safe'] else 'UNSAFE'}")
        return validation

    def _validate_pricing_change(self, change: Dict, current_metrics: Optional[Dict]) -> bool:
        """Validate that pricing changes are within safe thresholds"""
        details = change.get('details', '')

        # Check for drastic price drops
        if 'decrease' in details.lower() or 'drop' in details.lower() or 'reduce' in details.lower():
            # Extract percentage if mentioned
            # This is simplified - in production, you'd parse more carefully
            for word in details.split():
                if '%' in word:
                    try:
                        percent = float(word.replace('%', ''))
                        if percent > self.max_metric_drop:
                            logger.warning(f"Price drop of {percent}% exceeds threshold")
                            return False
                    except ValueError:
                        pass

        return True

    def _check_conflicts(self, change: Dict, all_changes: List[Dict]) -> List[str]:
        """Check if change conflicts with other changes"""
        conflicts = []

        change_files = set(change.get('files_to_modify', []))

        for other in all_changes:
            if other == change:
                continue

            other_files = set(other.get('files_to_modify', []))

            # Check for file conflicts
            overlap = change_files & other_files
            if overlap:
                conflicts.append(f"File overlap: {', '.join(overlap)}")

        return conflicts

    # ============ POST-DEPLOYMENT MONITORING ============

    def monitor_cycle(self, cycle_id: str, strategy_id: int,
                      metrics_before: Dict, check_interval_minutes: int = 30,
                      duration_hours: int = 6) -> Dict[str, Any]:
        """
        Monitor a deployment cycle for anomalies

        Args:
            cycle_id: Cycle identifier
            strategy_id: Strategy being monitored
            metrics_before: Baseline metrics before deployment
            check_interval_minutes: How often to check
            duration_hours: Total monitoring duration

        Returns:
            Monitoring result
        """
        logger.info(f"Starting monitoring for cycle {cycle_id}...")

        monitoring = {
            'cycle_id': cycle_id,
            'strategy_id': strategy_id,
            'status': 'monitoring',
            'checks': [],
            'alerts_triggered': [],
            'should_rollback': False
        }

        try:
            import time
            checks_count = int((duration_hours * 60) / check_interval_minutes)

            for check_num in range(checks_count):
                logger.info(f"Check {check_num + 1}/{checks_count}")

                # Collect current metrics
                from data_collector import get_data_collector
                collector = get_data_collector()
                current_metrics = collector.collect_metrics(
                    strategy_id=strategy_id,
                    cycle_id=cycle_id,
                    wait_hours=0  # Don't wait, check immediately
                )

                # Analyze for anomalies
                anomalies = self._detect_anomalies(metrics_before, current_metrics)

                check_result = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'check_number': check_num + 1,
                    'anomalies': anomalies,
                    'metrics': current_metrics
                }

                monitoring['checks'].append(check_result)

                # Check if rollback needed
                if anomalies.get('critical'):
                    logger.warning(f"Critical anomalies detected: {anomalies['critical']}")

                    alert = self._create_alert(
                        alert_type='critical_anomaly',
                        message=f"Critical anomalies in cycle {cycle_id}: {anomalies['critical']}",
                        severity='critical',
                        cycle_id=cycle_id,
                        strategy_id=strategy_id
                    )

                    monitoring['alerts_triggered'].append(alert)
                    monitoring['should_rollback'] = True
                    monitoring['status'] = 'rollback_required'

                    # Send SMS alert if configured
                    self.send_sms_alert(
                        f"🔥 CRITICAL: Anomalies detected in cycle {cycle_id}. Rollback recommended."
                    )

                    break

                # Wait before next check
                if check_num < checks_count - 1:
                    time.sleep(check_interval_minutes * 60)

            if monitoring['status'] == 'monitoring':
                monitoring['status'] = 'completed'

        except Exception as e:
            logger.error(f"Monitoring error: {e}")
            monitoring['status'] = 'error'
            monitoring['error'] = str(e)

        return monitoring

    def _detect_anomalies(self, metrics_before: Dict, metrics_after: Dict) -> Dict[str, List[str]]:
        """
        Detect anomalies in metrics

        Args:
            metrics_before: Baseline metrics
            metrics_after: Current metrics

        Returns:
            Anomalies categorized by severity
        """
        anomalies = {
            'critical': [],
            'warning': [],
            'info': []
        }

        # Define critical metrics and thresholds
        critical_metrics = {
            'revenue': self.max_metric_drop,
            'dau': self.max_metric_drop,
            'gen_success_rate': self.max_metric_drop
        }

        warning_metrics = {
            'mau': self.max_metric_drop,
            'arpu': self.max_metric_drop * 0.5,
            'avg_session_iterations': self.max_metric_drop * 0.5
        }

        # Check critical metrics
        for metric, threshold in critical_metrics.items():
            before = metrics_before.get(metric, 0)
            after = metrics_after.get(metric, 0)

            if before > 0:
                change_percent = ((after - before) / before) * 100

                if change_percent < -threshold:
                    anomalies['critical'].append(
                        f"{metric} dropped {abs(change_percent):.1f}% (threshold: {threshold}%)"
                    )

        # Check warning metrics
        for metric, threshold in warning_metrics.items():
            before = metrics_before.get(metric, 0)
            after = metrics_after.get(metric, 0)

            if before > 0:
                change_percent = ((after - before) / before) * 100

                if change_percent < -threshold:
                    anomalies['warning'].append(
                        f"{metric} dropped {abs(change_percent):.1f}% (threshold: {threshold}%)"
                    )

        # Check for unexpected improvements (might indicate data issues)
        for metric in ['revenue', 'dau', 'mau']:
            before = metrics_before.get(metric, 0)
            after = metrics_after.get(metric, 0)

            if before > 0:
                change_percent = ((after - before) / before) * 100

                # If improvement is too good to be true (>100%), flag it
                if change_percent > 100:
                    anomalies['info'].append(
                        f"{metric} increased {change_percent:.1f}% - verify data accuracy"
                    )

        return anomalies

    def _create_alert(self, alert_type: str, message: str, severity: str = 'medium',
                      cycle_id: Optional[str] = None, strategy_id: Optional[int] = None) -> Dict:
        """Create and log an alert"""
        alert_id = self.db.create_alert(
            alert_type=alert_type,
            message=message,
            severity=severity,
            cycle_id=cycle_id,
            strategy_id=strategy_id
        )

        logger.warning(f"Alert created: {alert_type} - {message}")

        return {
            'id': alert_id,
            'type': alert_type,
            'message': message,
            'severity': severity,
            'timestamp': datetime.utcnow().isoformat()
        }

    # ============ ALERT SYSTEM ============

    def send_sms_alert(self, message: str) -> bool:
        """
        Send SMS alert via Twilio

        Args:
            message: Alert message

        Returns:
            True if sent successfully
        """
        if not self.twilio_client or not self.alert_phone:
            logger.warning("SMS alerts not configured")
            return False

        try:
            self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_phone,
                to=self.alert_phone
            )

            logger.info("SMS alert sent")
            return True

        except Exception as e:
            logger.error(f"SMS alert failed: {e}")
            return False

    def check_health(self) -> Dict[str, Any]:
        """
        Perform system health check

        Returns:
            Health status
        """
        logger.info("Performing health check...")

        health = {
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'components': {}
        }

        try:
            # Check database
            try:
                self.db.get_pending_strategies()
                health['components']['database'] = 'healthy'
            except Exception as e:
                health['components']['database'] = f'unhealthy: {e}'
                health['status'] = 'degraded'

            # Check alerts
            unack_alerts = self.db.get_unacknowledged_alerts()
            health['components']['alerts'] = {
                'status': 'healthy',
                'unacknowledged_count': len(unack_alerts)
            }

            if len(unack_alerts) > 10:
                health['status'] = 'degraded'
                health['components']['alerts']['status'] = 'warning: many unacknowledged alerts'

            # Check recent cycles
            # (In a full implementation, you'd check cycle status)

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            health['status'] = 'unhealthy'
            health['error'] = str(e)

        return health

    # ============ ROLLBACK SUPPORT ============

    def should_rollback(self, metrics_before: Dict, metrics_after: Dict) -> Dict[str, Any]:
        """
        Determine if a rollback is needed

        Args:
            metrics_before: Pre-deployment metrics
            metrics_after: Post-deployment metrics

        Returns:
            Rollback decision with reasons
        """
        logger.info("Evaluating rollback necessity...")

        decision = {
            'should_rollback': False,
            'reasons': [],
            'confidence': 0.0
        }

        if not self.rollback_enabled:
            decision['reasons'].append("Rollback disabled in configuration")
            return decision

        # Detect anomalies
        anomalies = self._detect_anomalies(metrics_before, metrics_after)

        if anomalies['critical']:
            decision['should_rollback'] = True
            decision['reasons'].extend(anomalies['critical'])
            decision['confidence'] = 0.9

        elif len(anomalies['warning']) >= 3:
            # Multiple warnings indicate a problematic deployment
            decision['should_rollback'] = True
            decision['reasons'].append("Multiple warning-level issues detected")
            decision['reasons'].extend(anomalies['warning'])
            decision['confidence'] = 0.7

        return decision

    def create_checkpoint(self, cycle_id: str, data: Dict) -> int:
        """
        Create a checkpoint for potential rollback

        Args:
            cycle_id: Cycle identifier
            data: Checkpoint data (metrics, state, etc.)

        Returns:
            Checkpoint ID
        """
        logger.info(f"Creating checkpoint for cycle {cycle_id}...")

        # Store checkpoint in database (using cycle_logs table)
        # This is already handled by db_handler.start_cycle()
        # But we could extend it with additional checkpoint data

        return 0  # Placeholder


# Singleton instance
_safety_net = None

def get_safety_net() -> SafetyNet:
    """Get or create safety net singleton"""
    global _safety_net
    if _safety_net is None:
        _safety_net = SafetyNet()
    return _safety_net
