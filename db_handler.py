"""
Central Database Handler
Only online component - handles all data synchronization via Supabase
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from supabase import create_client, Client
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DatabaseHandler:
    """Handles all database operations with retry logic and error handling"""

    def __init__(self):
        self.url = os.getenv('SUPABASE_URL')
        self.key = os.getenv('SUPABASE_KEY')

        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in config.env")

        self.client: Client = create_client(self.url, self.key)
        logger.info("Database handler initialized")

    def _retry_operation(self, operation, max_retries=3, delay=2):
        """Retry database operations with exponential backoff"""
        for attempt in range(max_retries):
            try:
                return operation()
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = delay * (2 ** attempt)
                    logger.warning(f"Operation failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Operation failed after {max_retries} attempts: {e}")
                    raise

    # ============ STRATEGIES ============

    def write_strategy(self, strategy: Dict[str, Any]) -> int:
        """Write a new strategy to the database"""
        def _write():
            result = self.client.table('strategies').insert({
                'summary': strategy['summary'],
                'changes': json.dumps(strategy['changes']) if isinstance(strategy['changes'], (dict, list)) else strategy['changes'],
                'expected_impact': strategy.get('expected_impact'),
                'status': strategy.get('status', 'pending'),
                'priority': strategy.get('priority', 0),
                'focus_area': strategy.get('focus_area'),
                'data_snapshot': json.dumps({
                    **strategy.get('data_snapshot', {}),
                    **(({'dependency_analysis': strategy['dependency_analysis']} if 'dependency_analysis' in strategy else {}))
                })
            }).execute()

            strategy_id = result.data[0]['id']
            logger.info(f"Strategy written successfully: ID {strategy_id}")
            return strategy_id

        return self._retry_operation(_write)

    def get_strategy(self, strategy_id: int) -> Optional[Dict]:
        """Get a strategy by ID"""
        def _get():
            result = self.client.table('strategies').select('*').eq('id', strategy_id).execute()
            return result.data[0] if result.data else None

        return self._retry_operation(_get)

    def get_pending_strategies(self) -> List[Dict]:
        """Get all pending strategies, most recent first"""
        def _get():
            # Order by created_at descending to get the most recent pending strategy first
            # This ensures the mobile interface responds to the strategy the loop controller
            # is currently waiting on, not an old one
            result = self.client.table('strategies').select('*').eq('status', 'pending').order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def get_saved_strategies(self) -> List[Dict]:
        """Get all saved strategies (not yet implemented), most recent first"""
        def _get():
            result = self.client.table('strategies').select('*').eq('status', 'saved').order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def get_failed_strategies(self) -> List[Dict]:
        """Get all failed strategies, most recent first"""
        def _get():
            result = self.client.table('strategies').select('*').eq('status', 'failed').order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def get_rejected_strategies(self) -> List[Dict]:
        """Get all rejected strategies, most recent first"""
        def _get():
            result = self.client.table('strategies').select('*').eq('status', 'rejected').order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def get_approved_strategies(self) -> List[Dict]:
        """Get all approved strategies (ready for execution), most recent first"""
        def _get():
            result = self.client.table('strategies').select('*').eq('status', 'approved').order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def update_strategy_status(self, strategy_id: int, status: str, **kwargs) -> None:
        """Update strategy status and optional fields"""
        def _update():
            update_data = {'status': status}
            update_data.update(kwargs)
            self.client.table('strategies').update(update_data).eq('id', strategy_id).execute()
            logger.info(f"Strategy {strategy_id} status updated to {status}")

        self._retry_operation(_update)

    def split_strategy(self, strategy_id: int) -> List[int]:
        """Split a multi-change strategy into individual strategies, one per change.
        Marks the original as rejected. Returns list of new strategy IDs."""
        original = self.get_strategy(strategy_id)
        if not original:
            raise ValueError(f"Strategy {strategy_id} not found")

        changes = original.get('changes', [])
        if isinstance(changes, str):
            changes = json.loads(changes)

        if len(changes) <= 1:
            raise ValueError("Strategy has only one change, nothing to split")

        new_ids = []
        for change in changes:
            new_strategy = {
                'summary': change.get('action', original['summary']),
                'changes': [change],
                'expected_impact': change.get('expected_impact', original.get('expected_impact')),
                'status': original.get('status', 'pending'),
                'priority': original.get('priority', 0),
                'focus_area': original.get('focus_area'),
                'data_snapshot': original.get('data_snapshot', {}),
            }
            # data_snapshot may already be a dict from get_strategy
            if isinstance(new_strategy['data_snapshot'], str):
                try:
                    new_strategy['data_snapshot'] = json.loads(new_strategy['data_snapshot'])
                except (json.JSONDecodeError, TypeError):
                    new_strategy['data_snapshot'] = {}
            new_id = self.write_strategy(new_strategy)
            new_ids.append(new_id)

        # Mark original as rejected
        self.update_strategy_status(strategy_id, 'rejected')
        logger.info(f"Strategy {strategy_id} split into {len(new_ids)} strategies: {new_ids}")
        return new_ids

    def update_execution_stage(self, strategy_id: int, stage: str, metadata: Optional[Dict] = None) -> None:
        """
        Update the execution stage of a strategy for progress tracking.

        Stages:
        - waiting_approval: Initial state
        - generating_code: Code generation in progress
        - pushing_branch: Pushing to GitHub
        - waiting_merge: Preview deployed, waiting for merge
        - merging: Merge in progress
        - completed: Done

        Args:
            strategy_id: Strategy ID
            stage: Execution stage name
            metadata: Optional metadata (branch_name, preview_url, etc.) for resume
        """
        def _update():
            update_data = {
                'execution_stage': stage,
                'stage_updated_at': datetime.utcnow().isoformat()
            }
            if metadata:
                update_data['execution_metadata'] = json.dumps(metadata)
            self.client.table('strategies').update(update_data).eq('id', strategy_id).execute()
            logger.info(f"Strategy {strategy_id} execution stage: {stage}")

        self._retry_operation(_update)

    def get_stuck_strategies(self) -> List[Dict]:
        """
        Get strategies that are in an intermediate execution state (potentially stuck).
        These are strategies with status 'approved' or 'preview' that may need to be resumed.
        """
        def _get():
            # Get strategies in approved or preview status (not terminal states)
            result = self.client.table('strategies').select('*').in_(
                'status', ['approved', 'preview']
            ).order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def get_strategy_execution_metadata(self, strategy_id: int) -> Optional[Dict]:
        """Get execution metadata for a strategy (for resume)"""
        strategy = self.get_strategy(strategy_id)
        if strategy and strategy.get('execution_metadata'):
            try:
                return json.loads(strategy['execution_metadata'])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    # ============ APPROVALS ============

    def write_approval(self, strategy_id: int, user_response: str, response_type: str, notes: Optional[str] = None) -> int:
        """Write an approval decision"""
        def _write():
            result = self.client.table('approvals').insert({
                'strategy_id': strategy_id,
                'user_response': user_response,
                'response_type': response_type,
                'notes': notes,
                'approved_at': datetime.utcnow().isoformat() if response_type == 'approve' else None
            }).execute()

            approval_id = result.data[0]['id']
            logger.info(f"Approval written: Strategy {strategy_id}, Type {response_type}")
            return approval_id

        return self._retry_operation(_write)

    def get_approval(self, strategy_id: int, after_timestamp: Optional[str] = None) -> Optional[Dict]:
        """
        Get the latest approval for a strategy

        Args:
            strategy_id: Strategy ID to get approval for
            after_timestamp: If provided, only return approvals created after this ISO timestamp

        Returns:
            Latest approval dict or None
        """
        def _get():
            query = self.client.table('approvals').select('*').eq('strategy_id', strategy_id)

            # Filter by timestamp if provided
            if after_timestamp:
                query = query.gt('created_at', after_timestamp)

            # Get latest approval (most recent first)
            result = query.order('created_at', desc=True).limit(1).execute()
            return result.data[0] if result.data else None

        return self._retry_operation(_get)

    def wait_for_approval(self, strategy_id: int, timeout_minutes: int = 1440,
                          poll_interval: int = 300, response_types: Optional[List[str]] = None,
                          fast_poll_interval: int = 15, fast_poll_duration: int = 600,
                          after_timestamp: Optional[str] = None,
                          require_new: bool = False) -> Optional[Dict]:
        """
        Wait for user approval with adaptive polling.

        Args:
            strategy_id: Strategy to wait for approval on
            timeout_minutes: Total timeout (default 24 hours)
            poll_interval: Normal poll interval in seconds (default 5 min)
            response_types: Optional list of response types to filter for
            fast_poll_interval: Fast poll interval in seconds (default 15s)
            fast_poll_duration: Duration to use fast polling in seconds (default 10 min)
            after_timestamp: Only consider approvals created after this ISO timestamp
            require_new: If True, only look for approvals created after we start waiting

        Returns:
            Approval dict if received, None if timeout
        """
        start_time = time.time()
        timeout_seconds = timeout_minutes * 60

        # Only filter by timestamp if explicitly requested (for merge approval flow)
        # For initial approval, we want to find ANY approval for this strategy
        if require_new and after_timestamp is None:
            after_timestamp = datetime.utcnow().isoformat()

        logger.info(f"Waiting for approval on strategy {strategy_id} "
                    f"(fast poll: {fast_poll_interval}s for {fast_poll_duration}s, then {poll_interval}s)")

        while time.time() - start_time < timeout_seconds:
            approval = self.get_approval(strategy_id, after_timestamp=after_timestamp)

            if approval:
                # If filtering by response types, check if this matches
                if response_types and approval.get('response_type') not in response_types:
                    # Got a response but not the type we're waiting for, keep polling
                    pass
                else:
                    logger.info(f"Approval received for strategy {strategy_id}: {approval['response_type']}")
                    return approval

            # Adaptive polling: fast for first N seconds, then slow
            elapsed = time.time() - start_time
            if elapsed < fast_poll_duration:
                current_interval = fast_poll_interval
            else:
                current_interval = poll_interval

            time.sleep(current_interval)

        logger.warning(f"Approval timeout for strategy {strategy_id}")
        return None

    # ============ METRICS ============

    def log_metrics(self, data: Dict[str, Any]) -> int:
        """Log metrics data"""
        def _write():
            result = self.client.table('metrics').insert({
                'strategy_id': data.get('strategy_id'),
                'cycle_id': data.get('cycle_id'),
                'kpi_data': json.dumps(data.get('kpi_data', {})),
                'dau': data.get('dau'),
                'mau': data.get('mau'),
                'arpu': data.get('arpu'),
                'revenue': data.get('revenue'),
                'churn_rate': data.get('churn_rate'),
                'credit_usage': data.get('credit_usage'),
                'gen_success_rate': data.get('gen_success_rate'),
                'avg_session_iterations': data.get('avg_session_iterations'),
                'period_start': data.get('period_start'),
                'period_end': data.get('period_end')
            }).execute()

            metric_id = result.data[0]['id']
            logger.info(f"Metrics logged: ID {metric_id}")
            return metric_id

        return self._retry_operation(_write)

    def get_latest_metrics(self, limit: int = 10) -> List[Dict]:
        """Get latest metrics"""
        def _get():
            result = self.client.table('metrics').select('*').order('timestamp', desc=True).limit(limit).execute()
            return result.data

        return self._retry_operation(_get)

    def get_metrics_for_strategy(self, strategy_id: int) -> List[Dict]:
        """Get metrics associated with a specific strategy"""
        def _get():
            result = self.client.table('metrics').select('*').eq('strategy_id', strategy_id).order('timestamp', desc=False).execute()
            return result.data

        return self._retry_operation(_get)

    # ============ FEEDBACK ============

    def log_feedback(self, user_id: str, text: str, rating: Optional[int] = None,
                     sentiment: Optional[str] = None, category: Optional[str] = None,
                     metadata: Optional[Dict] = None) -> int:
        """Log user feedback"""
        def _write():
            result = self.client.table('feedback').insert({
                'user_id': user_id,
                'text': text,
                'rating': rating,
                'sentiment': sentiment,
                'category': category,
                'metadata': json.dumps(metadata) if metadata else None
            }).execute()

            feedback_id = result.data[0]['id']
            logger.info(f"Feedback logged: ID {feedback_id}")
            return feedback_id

        return self._retry_operation(_write)

    def get_recent_feedback(self, days: int = 7, limit: int = 100) -> List[Dict]:
        """Get recent feedback"""
        def _get():
            result = self.client.table('feedback').select('*').order('created_at', desc=True).limit(limit).execute()
            return result.data

        return self._retry_operation(_get)

    # ============ DEPLOYMENTS ============

    def log_deployment(self, strategy_id: int, status: str, git_commit_hash: Optional[str] = None,
                       vercel_deployment_url: Optional[str] = None, changes_applied: Optional[Dict] = None,
                       rollback_info: Optional[Dict] = None) -> int:
        """Log a deployment"""
        def _write():
            result = self.client.table('deployments').insert({
                'strategy_id': strategy_id,
                'status': status,
                'git_commit_hash': git_commit_hash,
                'vercel_deployment_url': vercel_deployment_url,
                'changes_applied': json.dumps(changes_applied) if changes_applied else None,
                'rollback_info': json.dumps(rollback_info) if rollback_info else None
            }).execute()

            deployment_id = result.data[0]['id']
            logger.info(f"Deployment logged: ID {deployment_id}, Status {status}")
            return deployment_id

        return self._retry_operation(_write)

    def update_deployment_status(self, deployment_id: int, status: str, **kwargs) -> None:
        """Update deployment status"""
        def _update():
            update_data = {'status': status}
            update_data.update(kwargs)
            self.client.table('deployments').update(update_data).eq('id', deployment_id).execute()
            logger.info(f"Deployment {deployment_id} status updated to {status}")

        self._retry_operation(_update)

    # ============ CYCLE LOGS ============

    def start_cycle(self, cycle_id: str, metrics_before: Optional[Dict] = None) -> int:
        """Start a new cycle"""
        def _write():
            result = self.client.table('cycle_logs').insert({
                'cycle_id': cycle_id,
                'status': 'running',
                'metrics_before': json.dumps(metrics_before) if metrics_before else None
            }).execute()

            log_id = result.data[0]['id']
            logger.info(f"Cycle started: {cycle_id}")
            return log_id

        return self._retry_operation(_write)

    def complete_cycle(self, cycle_id: str, status: str, strategy_id: Optional[int] = None,
                       metrics_after: Optional[Dict] = None, success_indicators: Optional[Dict] = None,
                       errors: Optional[str] = None) -> None:
        """Complete a cycle"""
        def _update():
            self.client.table('cycle_logs').update({
                'completed_at': datetime.utcnow().isoformat(),
                'status': status,
                'strategy_id': strategy_id,
                'metrics_after': json.dumps(metrics_after) if metrics_after else None,
                'success_indicators': json.dumps(success_indicators) if success_indicators else None,
                'errors': errors
            }).eq('cycle_id', cycle_id).execute()
            logger.info(f"Cycle completed: {cycle_id}, Status {status}")

        self._retry_operation(_update)

    def get_cycle_log(self, cycle_id: str) -> Optional[Dict]:
        """Get cycle log by ID"""
        def _get():
            result = self.client.table('cycle_logs').select('*').eq('cycle_id', cycle_id).execute()
            return result.data[0] if result.data else None

        return self._retry_operation(_get)

    # ============ ALERTS ============

    def create_alert(self, alert_type: str, message: str, severity: str = 'medium',
                     cycle_id: Optional[str] = None, strategy_id: Optional[int] = None) -> int:
        """Create an alert"""
        def _write():
            result = self.client.table('alerts').insert({
                'alert_type': alert_type,
                'severity': severity,
                'message': message,
                'cycle_id': cycle_id,
                'strategy_id': strategy_id
            }).execute()

            alert_id = result.data[0]['id']
            logger.warning(f"Alert created: {alert_type} - {message}")
            return alert_id

        return self._retry_operation(_write)

    def get_unacknowledged_alerts(self) -> List[Dict]:
        """Get all unacknowledged alerts"""
        def _get():
            result = self.client.table('alerts').select('*').eq('acknowledged', False).order('created_at', desc=True).execute()
            return result.data

        return self._retry_operation(_get)

    def acknowledge_alert(self, alert_id: int) -> None:
        """Acknowledge an alert"""
        def _update():
            self.client.table('alerts').update({
                'acknowledged': True,
                'acknowledged_at': datetime.utcnow().isoformat()
            }).eq('id', alert_id).execute()
            logger.info(f"Alert {alert_id} acknowledged")

        self._retry_operation(_update)


# Singleton instance
_db_handler = None

def get_db_handler() -> DatabaseHandler:
    """Get or create database handler singleton"""
    global _db_handler
    if _db_handler is None:
        _db_handler = DatabaseHandler()
    return _db_handler
