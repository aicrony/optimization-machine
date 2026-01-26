"""
Iteration Loop Controller
Orchestrates the full optimization cycle
"""

import os
import logging
import asyncio
import schedule
import time
from typing import Dict, Optional, Any
from datetime import datetime
from dotenv import load_dotenv

from db_handler import get_db_handler
from strategy_engine import get_strategy_engine
from mobile_interface import get_mobile_interface
from execution_agent import get_execution_agent
from data_collector import get_data_collector
from safety_net import get_safety_net

# Load environment variables
load_dotenv('config.env')

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/loop_controller.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class LoopController:
    """Main loop controller for the optimization system"""

    def __init__(self):
        self.db = get_db_handler()
        self.strategy_engine = get_strategy_engine()
        self.mobile = get_mobile_interface()
        self.executor = get_execution_agent()
        self.collector = get_data_collector()
        self.safety = get_safety_net()

        # Configuration
        self.cycle_interval_hours = int(os.getenv('CYCLE_INTERVAL_HOURS', 12))
        self.approval_timeout_hours = int(os.getenv('APPROVAL_TIMEOUT_HOURS', 24))

        # State
        self.is_running = False
        self.current_cycle_id = None

        logger.info("Loop controller initialized")

    def run_cycle(self) -> Dict[str, Any]:
        """
        Run a single optimization cycle

        Returns:
            Cycle result summary
        """
        cycle_id = f"cycle_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        self.current_cycle_id = cycle_id

        logger.info(f"========== Starting Cycle: {cycle_id} ==========")

        cycle_result = {
            'cycle_id': cycle_id,
            'started_at': datetime.utcnow().isoformat(),
            'status': 'running',
            'stages': {}
        }

        try:
            # Stage 1: Collect current metrics and feedback
            logger.info("Stage 1: Collecting current data...")
            cycle_result['stages']['data_collection'] = self._stage_collect_data()

            if not cycle_result['stages']['data_collection']['success']:
                raise Exception("Data collection failed")

            # Stage 2: Generate strategy
            logger.info("Stage 2: Generating optimization strategy...")
            cycle_result['stages']['strategy_generation'] = self._stage_generate_strategy(
                cycle_result['stages']['data_collection']['data']
            )

            if not cycle_result['stages']['strategy_generation']['success']:
                raise Exception("Strategy generation failed")

            # Stage 3: Request approval
            logger.info("Stage 3: Requesting approval...")
            cycle_result['stages']['approval'] = self._stage_request_approval(
                cycle_result['stages']['strategy_generation']['strategy']
            )

            if not cycle_result['stages']['approval']['success']:
                logger.info(f"Approval stage result: {cycle_result['stages']['approval']['status']}")
                if cycle_result['stages']['approval']['status'] == 'rejected':
                    cycle_result['status'] = 'rejected'
                elif cycle_result['stages']['approval']['status'] == 'timeout':
                    cycle_result['status'] = 'timeout'
                else:
                    cycle_result['status'] = 'cancelled'

                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 4: Validate changes
            logger.info("Stage 4: Validating changes...")
            cycle_result['stages']['validation'] = self._stage_validate_changes(
                cycle_result['stages']['strategy_generation']['strategy']
            )

            if not cycle_result['stages']['validation']['safe']:
                logger.warning("Changes failed safety validation")
                cycle_result['status'] = 'unsafe'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 5: Execute changes
            logger.info("Stage 5: Executing changes...")
            cycle_result['stages']['execution'] = self._stage_execute_changes(
                cycle_result['stages']['strategy_generation']['strategy'],
                cycle_result['stages']['approval']['approval']
            )

            if not cycle_result['stages']['execution']['success']:
                raise Exception("Execution failed")

            # Stage 6: Monitor deployment
            logger.info("Stage 6: Monitoring deployment...")
            cycle_result['stages']['monitoring'] = self._stage_monitor_deployment(
                cycle_id,
                cycle_result['stages']['strategy_generation']['strategy']['id'],
                cycle_result['stages']['data_collection']['data']['metrics']
            )

            # Check if rollback needed
            if cycle_result['stages']['monitoring'].get('should_rollback'):
                logger.warning("Monitoring triggered rollback")
                cycle_result['status'] = 'rolled_back'
                self._finalize_cycle(cycle_id, cycle_result)
                return cycle_result

            # Stage 7: Collect post-deployment metrics
            logger.info("Stage 7: Collecting post-deployment metrics...")
            cycle_result['stages']['post_metrics'] = self._stage_collect_post_metrics(
                cycle_result['stages']['strategy_generation']['strategy']['id'],
                cycle_id
            )

            # Stage 8: Evaluate success
            logger.info("Stage 8: Evaluating strategy success...")
            cycle_result['stages']['evaluation'] = self._stage_evaluate_success(
                cycle_result['stages']['data_collection']['data']['metrics'],
                cycle_result['stages']['post_metrics']['data']['metrics'],
                cycle_result['stages']['strategy_generation']['strategy'].get('expected_impact', '')
            )

            # Determine final status
            if cycle_result['stages']['evaluation']['success']:
                cycle_result['status'] = 'success'
            else:
                cycle_result['status'] = 'completed_unsuccessful'

            logger.info(f"Cycle {cycle_id} completed: {cycle_result['status']}")

        except Exception as e:
            logger.error(f"Cycle failed: {e}", exc_info=True)
            cycle_result['status'] = 'failed'
            cycle_result['error'] = str(e)

            # Create alert
            self.safety._create_alert(
                alert_type='cycle_failure',
                message=f"Cycle {cycle_id} failed: {e}",
                severity='high',
                cycle_id=cycle_id
            )

        finally:
            cycle_result['completed_at'] = datetime.utcnow().isoformat()
            self._finalize_cycle(cycle_id, cycle_result)

        logger.info(f"========== Cycle {cycle_id} Finished ==========")
        return cycle_result

    def _stage_collect_data(self) -> Dict[str, Any]:
        """Stage 1: Collect current data"""
        try:
            # Collect metrics
            metrics = self.collector.collect_metrics(wait_hours=0)

            # Get recent feedback
            feedback = self.db.get_recent_feedback(days=7, limit=50)

            # Get goals (from config or database)
            goals = {
                'target_mrr': 10000,
                'target_arpu': 20,
                'target_retention': 85
            }

            # Get current features (this would come from your app config)
            current_features = [
                'AI Image Generation',
                'AI Video Generation',
                'Multiple Studio Styles',
                'Credit System'
            ]

            # Get constraints
            constraints = {
                'risk_tolerance': 'Low',
                'budget': 1000,
                'time_horizon': '1-2 weeks'
            }

            return {
                'success': True,
                'data': {
                    'metrics': metrics,
                    'feedback': feedback,
                    'goals': goals,
                    'current_features': current_features,
                    'constraints': constraints
                }
            }

        except Exception as e:
            logger.error(f"Data collection failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_generate_strategy(self, data: Dict) -> Dict[str, Any]:
        """Stage 2: Generate optimization strategy"""
        try:
            # Generate strategy using AI
            strategy = self.strategy_engine.generate_strategy(data)

            # Write to database
            strategy_id = self.db.write_strategy(strategy)
            strategy['id'] = strategy_id

            return {
                'success': True,
                'strategy': strategy,
                'strategy_id': strategy_id
            }

        except Exception as e:
            logger.error(f"Strategy generation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_request_approval(self, strategy: Dict) -> Dict[str, Any]:
        """Stage 3: Request user approval"""
        try:
            strategy_id = strategy['id']

            # Send proposal via mobile
            sent = asyncio.run(self.mobile.send_proposal(strategy))

            if not sent:
                return {
                    'success': False,
                    'status': 'send_failed',
                    'error': 'Failed to send proposal'
                }

            # Wait for approval
            logger.info(f"Waiting for approval (timeout: {self.approval_timeout_hours} hours)...")

            approval = self.db.wait_for_approval(
                strategy_id,
                timeout_minutes=self.approval_timeout_hours * 60,
                poll_interval=300  # Check every 5 minutes
            )

            if not approval:
                self.db.update_strategy_status(strategy_id, 'timeout')
                return {
                    'success': False,
                    'status': 'timeout',
                    'message': 'Approval timeout'
                }

            response_type = approval['response_type']

            if response_type == 'approve':
                self.db.update_strategy_status(strategy_id, 'approved')
                return {
                    'success': True,
                    'status': 'approved',
                    'approval': approval
                }
            elif response_type == 'reject':
                self.db.update_strategy_status(strategy_id, 'rejected')
                return {
                    'success': False,
                    'status': 'rejected',
                    'approval': approval
                }
            else:  # tweak
                # For MVP, treat tweak as rejection
                # In full version, would regenerate strategy with tweaks
                self.db.update_strategy_status(strategy_id, 'pending')
                return {
                    'success': False,
                    'status': 'tweak_requested',
                    'approval': approval,
                    'message': 'Tweak requested - would need regeneration'
                }

        except Exception as e:
            logger.error(f"Approval request failed: {e}")
            return {
                'success': False,
                'status': 'error',
                'error': str(e)
            }

    def _stage_validate_changes(self, strategy: Dict) -> Dict[str, Any]:
        """Stage 4: Validate changes with safety net"""
        try:
            import json
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            # Get current metrics for validation
            latest_metrics = self.db.get_latest_metrics(limit=1)
            current_metrics = latest_metrics[0] if latest_metrics else {}

            # Validate
            validation = self.safety.validate_changes(changes, current_metrics)

            return validation

        except Exception as e:
            logger.error(f"Validation failed: {e}")
            return {
                'safe': False,
                'errors': [str(e)]
            }

    def _stage_execute_changes(self, strategy: Dict, approval: Dict) -> Dict[str, Any]:
        """Stage 5: Execute approved changes"""
        try:
            import json
            strategy_id = strategy['id']
            changes = strategy.get('changes', [])
            if isinstance(changes, str):
                changes = json.loads(changes)

            # Execute
            result = self.executor.execute_changes(strategy_id, changes, approval)

            # Update strategy status
            if result['success']:
                self.db.update_strategy_status(strategy_id, 'executed')
            else:
                self.db.update_strategy_status(strategy_id, 'failed')

            return result

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return {
                'success': False,
                'errors': [str(e)]
            }

    def _stage_monitor_deployment(self, cycle_id: str, strategy_id: int,
                                   metrics_before: Dict) -> Dict[str, Any]:
        """Stage 6: Monitor deployment for issues"""
        try:
            # Monitor for a short period (1 hour for MVP)
            monitoring = self.safety.monitor_cycle(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                metrics_before=metrics_before,
                check_interval_minutes=15,
                duration_hours=1
            )

            return monitoring

        except Exception as e:
            logger.error(f"Monitoring failed: {e}")
            return {
                'status': 'error',
                'error': str(e),
                'should_rollback': False
            }

    def _stage_collect_post_metrics(self, strategy_id: int, cycle_id: str) -> Dict[str, Any]:
        """Stage 7: Collect post-deployment metrics"""
        try:
            # Wait longer period for meaningful data (6 hours)
            metrics = self.collector.collect_metrics(
                strategy_id=strategy_id,
                cycle_id=cycle_id,
                wait_hours=6
            )

            return {
                'success': True,
                'data': {'metrics': metrics}
            }

        except Exception as e:
            logger.error(f"Post-metrics collection failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _stage_evaluate_success(self, metrics_before: Dict, metrics_after: Dict,
                                expected_impact: str) -> Dict[str, Any]:
        """Stage 8: Evaluate if strategy was successful"""
        try:
            evaluation = self.strategy_engine.evaluate_strategy_success(
                metrics_before,
                metrics_after,
                expected_impact
            )

            # Send results notification
            if evaluation.get('success'):
                asyncio.run(self.mobile.send_status_update(
                    f"✅ Strategy successful! {evaluation.get('actual_impact', '')}"
                ))
            else:
                asyncio.run(self.mobile.send_status_update(
                    f"⚠️ Strategy below expectations. {evaluation.get('actual_impact', '')}"
                ))

            return evaluation

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _finalize_cycle(self, cycle_id: str, cycle_result: Dict) -> None:
        """Finalize cycle and log results"""
        try:
            strategy_id = cycle_result.get('stages', {}).get('strategy_generation', {}).get('strategy_id')

            metrics_before = cycle_result.get('stages', {}).get('data_collection', {}).get('data', {}).get('metrics')
            metrics_after = cycle_result.get('stages', {}).get('post_metrics', {}).get('data', {}).get('metrics')

            success_indicators = cycle_result.get('stages', {}).get('evaluation', {})

            errors = cycle_result.get('error', '')
            if not errors:
                # Collect errors from failed stages
                failed_stages = [
                    stage for stage, result in cycle_result.get('stages', {}).items()
                    if isinstance(result, dict) and not result.get('success', True)
                ]
                if failed_stages:
                    errors = f"Failed stages: {', '.join(failed_stages)}"

            # Complete cycle in database
            self.db.complete_cycle(
                cycle_id=cycle_id,
                status=cycle_result['status'],
                strategy_id=strategy_id,
                metrics_after=metrics_after,
                success_indicators=success_indicators,
                errors=errors
            )

            logger.info(f"Cycle {cycle_id} finalized in database")

        except Exception as e:
            logger.error(f"Cycle finalization failed: {e}")

    # ============ SCHEDULING ============

    def run_continuous(self):
        """Run optimization loop continuously on schedule"""
        logger.info(f"Starting continuous optimization loop (interval: {self.cycle_interval_hours}h)")

        self.is_running = True

        # Schedule cycle
        schedule.every(self.cycle_interval_hours).hours.do(self.run_cycle)

        # Run first cycle immediately
        self.run_cycle()

        # Keep running
        while self.is_running:
            schedule.run_pending()
            time.sleep(60)  # Check every minute

    def stop(self):
        """Stop the continuous loop"""
        logger.info("Stopping optimization loop...")
        self.is_running = False


# Singleton instance
_loop_controller = None

def get_loop_controller() -> LoopController:
    """Get or create loop controller singleton"""
    global _loop_controller
    if _loop_controller is None:
        _loop_controller = LoopController()
    return _loop_controller
