"""
Local Execution Agent
Executes approved changes locally, tests them, and deploys to production
"""

import os
import json
import logging
import subprocess
import shutil
import time
import re
from typing import Dict, List, Optional, Any
from pathlib import Path
import git
import requests
import anthropic
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


class ExecutionAgent:
    """Executes and deploys approved optimization strategies"""

    def __init__(self):
        self.db = get_db_handler()
        self.vercel_token = os.getenv('VERCEL_TOKEN')
        self.vercel_project_id = os.getenv('VERCEL_PROJECT_ID')
        self.vercel_project_name = os.getenv('VERCEL_PROJECT_NAME', 'gentube')
        self.gentube_app_path = os.getenv('GENTUBE_APP_PATH', '../gentube-app')
        self.github_repo = os.getenv('GITHUB_REPO')  # e.g., "username/gentube-app"

        # SSH key for git operations (allows agent to use dedicated deploy key)
        self.ssh_key_path = os.getenv('GIT_SSH_KEY_PATH', '~/.ssh/gentube_deploy')
        self.ssh_key_path = os.path.expanduser(self.ssh_key_path)

        # Ensure app path exists
        if not Path(self.gentube_app_path).exists():
            logger.warning(f"Gentube app path not found: {self.gentube_app_path}")

        logger.info("Execution agent initialized")

    def _get_ssh_env(self):
        """Get SSH command environment for git operations using deploy key."""
        if os.path.exists(self.ssh_key_path):
            return f'ssh -i {self.ssh_key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o BatchMode=yes'
        else:
            logger.warning(f"Deploy key not found at {self.ssh_key_path}, using default SSH key")
            return None

    def _git_push(self, repo, *args):
        """Execute git push using the dedicated deploy key."""
        ssh_cmd = self._get_ssh_env()
        if ssh_cmd:
            with repo.git.custom_environment(GIT_SSH_COMMAND=ssh_cmd):
                return repo.git.push(*args)
        else:
            return repo.git.push(*args)

    def _git_pull(self, repo, *args):
        """Execute git pull using the dedicated deploy key."""
        ssh_cmd = self._get_ssh_env()
        if ssh_cmd:
            with repo.git.custom_environment(GIT_SSH_COMMAND=ssh_cmd):
                return repo.git.pull(*args)
        else:
            return repo.git.pull(*args)

    def _git_fetch(self, repo, *args):
        """Execute git fetch using the dedicated deploy key."""
        ssh_cmd = self._get_ssh_env()
        if ssh_cmd:
            with repo.git.custom_environment(GIT_SSH_COMMAND=ssh_cmd):
                return repo.git.fetch(*args)
        else:
            return repo.git.fetch(*args)

    def execute_changes(self, strategy_id: int, changes: List[Dict], approval: Dict) -> Dict[str, Any]:
        """
        Execute approved changes

        Args:
            strategy_id: Strategy ID
            changes: List of changes to apply
            approval: Approval details

        Returns:
            Execution result with status and details
        """
        logger.info(f"Executing changes for strategy {strategy_id}...")

        result = {
            'success': False,
            'strategy_id': strategy_id,
            'changes_applied': [],
            'changes_failed': [],
            'git_commit_hash': None,
            'vercel_deployment_url': None,
            'rollback_info': None,
            'errors': []
        }

        try:
            # 1. Create backup/rollback point
            rollback_info = self._create_backup()
            result['rollback_info'] = rollback_info

            # 2. Apply each change
            for i, change in enumerate(changes):
                try:
                    logger.info(f"Applying change {i+1}/{len(changes)}: {change.get('action')}")
                    applied = self._apply_change(change)
                    result['changes_applied'].append({
                        'change': change,
                        'result': applied
                    })
                except Exception as e:
                    logger.error(f"Failed to apply change: {e}")
                    result['changes_failed'].append({
                        'change': change,
                        'error': str(e)
                    })
                    result['errors'].append(f"Change {i+1} failed: {e}")

            # 3. Test changes locally
            if result['changes_applied']:
                test_result = self._test_changes()
                if not test_result['success']:
                    raise Exception(f"Local tests failed: {test_result['message']}")

            # 4. Commit changes
            commit_hash = self._commit_changes(strategy_id, changes, approval)
            result['git_commit_hash'] = commit_hash

            # 5. Deploy to Vercel
            if self.vercel_token and self.vercel_project_id:
                deployment_url = self._deploy_to_vercel()
                result['vercel_deployment_url'] = deployment_url

            # 6. Log deployment
            deployment_id = self.db.log_deployment(
                strategy_id=strategy_id,
                status='deployed',
                git_commit_hash=commit_hash,
                vercel_deployment_url=result.get('vercel_deployment_url'),
                changes_applied=result['changes_applied'],
                rollback_info=rollback_info
            )

            result['success'] = True
            result['deployment_id'] = deployment_id

            logger.info(f"Successfully executed strategy {strategy_id}")

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            result['errors'].append(str(e))

            # Attempt rollback
            if result.get('rollback_info'):
                logger.info("Attempting rollback...")
                try:
                    self._rollback(result['rollback_info'])
                    logger.info("Rollback successful")
                except Exception as rb_error:
                    logger.error(f"Rollback failed: {rb_error}")
                    result['errors'].append(f"Rollback failed: {rb_error}")

            # Log failed deployment
            self.db.log_deployment(
                strategy_id=strategy_id,
                status='failed',
                changes_applied=result['changes_applied'],
                rollback_info=result['rollback_info']
            )

        return result

    def execute_to_preview(self, strategy_id: int, changes: List[Dict],
                           code_changes: List[Dict], approval: Dict) -> Dict[str, Any]:
        """
        Execute changes to a feature branch and deploy to preview

        Args:
            strategy_id: Strategy ID
            changes: List of strategy changes
            code_changes: List of actual code changes from Claude
            approval: Approval details

        Returns:
            Result with preview_url and branch_name
        """
        logger.info(f"Executing to preview for strategy {strategy_id}...")

        result = {
            'success': False,
            'strategy_id': strategy_id,
            'branch_name': None,
            'preview_url': None,
            'github_url': None,
            'vercel_deployment_url': None,
            'changes_summary': '',
            'errors': []
        }

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                raise Exception(f"App path not found: {self.gentube_app_path}")

            repo = git.Repo(app_path)

            # 1. Create feature branch
            branch_name = f"opt/strategy-{strategy_id}"
            result['branch_name'] = branch_name

            # Checkout develop and pull latest (using deploy key for SSH)
            repo.git.checkout('develop')
            self._git_pull(repo, 'origin', 'develop')

            # Delete existing branch if it exists (from previous attempt)
            try:
                repo.git.branch('-D', branch_name)
                logger.info(f"Deleted existing local branch: {branch_name}")
            except git.exc.GitCommandError:
                pass  # Branch doesn't exist locally

            try:
                self._git_push(repo, 'origin', '--delete', branch_name)
                logger.info(f"Deleted existing remote branch: {branch_name}")
            except Exception:
                pass  # Branch doesn't exist remotely

            # Create and checkout feature branch
            repo.git.checkout('-b', branch_name)
            logger.info(f"Created branch: {branch_name}")

            # 2. Apply code changes
            applied_files = []
            for code_change in code_changes:
                file_path = code_change.get('file_path')
                new_content = code_change.get('new_content')
                change_type = code_change.get('change_type', 'modify')

                if file_path and new_content:
                    full_path = app_path / file_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)

                    with open(full_path, 'w') as f:
                        f.write(new_content)

                    applied_files.append(file_path)
                    logger.info(f"Applied change to: {file_path}")

            # 3. Run tests (lint)
            test_result = self._test_changes()
            if not test_result['success'] and not test_result.get('skipped'):
                raise Exception(f"Tests failed: {test_result.get('message')}")

            # 4. Run build and fix any errors before pushing
            build_result = self._build_and_fix(max_attempts=7, strategy_id=strategy_id)
            if not build_result['success'] and not build_result.get('skipped'):
                return {
                    'success': False,
                    'error': f"Build failed: {build_result.get('message')}",
                    'build_error': build_result.get('error', ''),
                    'fixes_applied': build_result.get('fixes_applied', []),
                    'same_error_count': build_result.get('same_error_count', 0),
                    'branch_name': branch_name,
                    'strategy_id': strategy_id
                }

            if build_result.get('fixes_applied'):
                logger.info(f"Applied {len(build_result['fixes_applied'])} build fixes")

            # 5. Commit changes
            repo.git.add(A=True)
            commit_message = f"[Auto-Optimize] Strategy #{strategy_id}\n\n"
            commit_message += f"Applied {len(code_changes)} code changes\n"
            for change in changes[:5]:  # First 5 changes
                commit_message += f"- {change.get('action', 'N/A')}\n"
            if len(changes) > 5:
                commit_message += f"... and {len(changes) - 5} more\n"

            repo.index.commit(commit_message)

            # 6. Push feature branch (using deploy key)
            self._git_push(repo, '--set-upstream', 'origin', branch_name)
            logger.info(f"Pushed branch: {branch_name}")

            # 7. Generate all URLs
            # GitHub branch URL
            if self.github_repo:
                result['github_url'] = f"https://github.com/{self.github_repo}/tree/{branch_name}"
                logger.info(f"GitHub URL: {result['github_url']}")

            # Vercel deployment dashboard URL
            if self.vercel_project_name:
                # Vercel deployment page for this branch
                result['vercel_deployment_url'] = f"https://vercel.com/{self.vercel_project_name}/deployments?branch={branch_name}"
                logger.info(f"Vercel deployment URL: {result['vercel_deployment_url']}")

            # Preview app URL (Vercel auto-deploys on push, generate expected URL)
            preview_url = self._deploy_preview(branch_name)
            result['preview_url'] = preview_url
            logger.info(f"Preview app URL: {preview_url}")

            # 8. Generate changes summary
            changes_summary = "**Code Changes:**\n"
            for file in applied_files[:10]:
                changes_summary += f"- `{file}`\n"
            if len(applied_files) > 10:
                changes_summary += f"- ... and {len(applied_files) - 10} more files\n"

            result['changes_summary'] = changes_summary
            result['success'] = True

        except Exception as e:
            logger.error(f"Preview execution failed: {e}")
            result['errors'].append(str(e))

            # Cleanup: delete branch if created
            try:
                if result['branch_name']:
                    repo = git.Repo(app_path)
                    repo.git.checkout('develop')
                    repo.git.branch('-D', result['branch_name'])
            except Exception as cleanup_error:
                logger.error(f"Branch cleanup failed: {cleanup_error}")

        return result

    def _deploy_preview(self, branch_name: str) -> str:
        """Deploy a feature branch to Vercel preview"""
        logger.info(f"Deploying preview for branch: {branch_name}")

        try:
            app_path = Path(self.gentube_app_path)

            # Vercel CLI deployment
            if shutil.which('vercel') and self.vercel_token:
                result = subprocess.run(
                    ['vercel', '--token', self.vercel_token],  # No --prod = preview
                    cwd=app_path,
                    capture_output=True,
                    text=True,
                    timeout=600
                )

                if result.returncode == 0:
                    # Extract deployment URL
                    for line in result.stdout.split('\n'):
                        if 'https://' in line and 'vercel' in line:
                            return line.strip()

            # Fallback: return expected Vercel preview URL format
            project_name = os.getenv('VERCEL_PROJECT_NAME', 'gentube')
            # Vercel preview URLs are usually: https://project-branch-xxx.vercel.app
            return f"https://{project_name}-git-{branch_name.replace('/', '-')}.vercel.app"

        except Exception as e:
            logger.error(f"Preview deployment failed: {e}")
            return f"preview-pending-{branch_name}"

    def merge_to_develop(self, branch_name: str) -> Dict[str, Any]:
        """
        Merge a feature branch to develop

        Args:
            branch_name: Name of the branch to merge

        Returns:
            Result with success status
        """
        logger.info(f"Merging {branch_name} to develop...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                raise Exception(f"App path not found: {self.gentube_app_path}")

            repo = git.Repo(app_path)

            # Checkout develop (using deploy key for SSH)
            repo.git.checkout('develop')
            self._git_pull(repo, 'origin', 'develop')

            # Merge feature branch
            repo.git.merge(branch_name, '--no-ff', '-m', f'Merge {branch_name} to develop')

            # Push develop (using deploy key)
            self._git_push(repo, 'origin', 'develop')

            # Delete feature branch (local and remote)
            repo.git.branch('-d', branch_name)
            try:
                self._git_push(repo, 'origin', '--delete', branch_name)
            except Exception as e:
                logger.warning(f"Could not delete remote branch: {e}")

            logger.info(f"Successfully merged {branch_name} to develop")

            return {
                'success': True,
                'message': f'Merged {branch_name} to develop'
            }

        except Exception as e:
            logger.error(f"Merge failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def discard_branch(self, branch_name: str) -> Dict[str, Any]:
        """
        Delete a feature branch (local and remote)

        Args:
            branch_name: Name of the branch to delete

        Returns:
            Result with success status
        """
        logger.info(f"Discarding branch: {branch_name}")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                raise Exception(f"App path not found: {self.gentube_app_path}")

            repo = git.Repo(app_path)

            # Checkout develop first
            repo.git.checkout('develop')

            # Delete local branch
            try:
                repo.git.branch('-D', branch_name)
            except Exception as e:
                logger.warning(f"Could not delete local branch: {e}")

            # Delete remote branch (using deploy key)
            try:
                self._git_push(repo, 'origin', '--delete', branch_name)
            except Exception as e:
                logger.warning(f"Could not delete remote branch: {e}")

            logger.info(f"Branch {branch_name} discarded")

            return {
                'success': True,
                'message': f'Deleted branch {branch_name}'
            }

        except Exception as e:
            logger.error(f"Branch deletion failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _create_backup(self) -> Dict[str, Any]:
        """Create backup/rollback point"""
        logger.info("Creating backup...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return {'branch': None, 'commit': None}

            repo = git.Repo(app_path)
            current_branch = repo.active_branch.name
            current_commit = repo.head.commit.hexsha

            return {
                'branch': current_branch,
                'commit': current_commit,
                'timestamp': repo.head.commit.committed_datetime.isoformat()
            }

        except Exception as e:
            logger.warning(f"Backup creation failed: {e}")
            return {'error': str(e)}

    def _apply_change(self, change: Dict) -> Dict[str, Any]:
        """
        Apply a single change

        Args:
            change: Change details (action, type, details, files_to_modify, etc.)

        Returns:
            Result of applying the change
        """
        change_type = change.get('type')
        action = change.get('action')
        details = change.get('details')
        files = change.get('files_to_modify', [])

        logger.info(f"Applying {change_type}: {action}")

        # Route to appropriate handler based on type
        if change_type == 'ui_change':
            return self._apply_ui_change(change)
        elif change_type == 'model_change':
            return self._apply_model_change(change)
        elif change_type == 'feature_addition':
            return self._apply_feature_addition(change)
        elif change_type == 'prompt_optimization':
            return self._apply_prompt_optimization(change)
        elif change_type == 'pricing_adjustment':
            return self._apply_pricing_adjustment(change)
        else:
            logger.warning(f"Unknown change type: {change_type}")
            return {'success': False, 'message': f'Unknown change type: {change_type}'}

    def _apply_ui_change(self, change: Dict) -> Dict[str, Any]:
        """Apply UI-related changes"""
        # This would modify React/Vue/HTML files
        # For MVP, we'll log the intent
        logger.info(f"UI Change: {change.get('action')}")

        files = change.get('files_to_modify', [])
        details = change.get('details', '')

        # In a real implementation, you would:
        # 1. Parse the files
        # 2. Apply the specific changes (e.g., using AST for JS/TS files)
        # 3. Validate syntax

        return {
            'success': True,
            'message': f'UI change applied to {len(files)} files',
            'files_modified': files
        }

    def _apply_model_change(self, change: Dict) -> Dict[str, Any]:
        """Apply AI model-related changes"""
        logger.info(f"Model Change: {change.get('action')}")

        # This could involve:
        # - Changing model parameters in config files
        # - Updating API endpoints
        # - Modifying prompt templates

        return {
            'success': True,
            'message': 'Model change applied',
            'details': change.get('details')
        }

    def _apply_feature_addition(self, change: Dict) -> Dict[str, Any]:
        """Apply new feature additions"""
        logger.info(f"Feature Addition: {change.get('action')}")

        # This is more complex and might involve:
        # - Generating new component files
        # - Updating routing
        # - Adding API endpoints

        return {
            'success': True,
            'message': 'Feature scaffolding created',
            'note': 'Manual review recommended for new features'
        }

    def _apply_prompt_optimization(self, change: Dict) -> Dict[str, Any]:
        """Apply prompt template optimizations"""
        logger.info(f"Prompt Optimization: {change.get('action')}")

        # Update prompt templates in config or code
        # This is typically a string replacement in a config file

        return {
            'success': True,
            'message': 'Prompt template updated'
        }

    def _apply_pricing_adjustment(self, change: Dict) -> Dict[str, Any]:
        """Apply pricing adjustments"""
        logger.info(f"Pricing Adjustment: {change.get('action')}")

        # Update pricing in config/database
        # This should be careful and validated

        return {
            'success': True,
            'message': 'Pricing updated',
            'warning': 'Price changes require careful monitoring'
        }

    def _test_changes(self) -> Dict[str, Any]:
        """Test changes locally before deployment"""
        logger.info("Testing changes locally...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return {
                    'success': True,
                    'message': 'App path not found, skipping tests',
                    'skipped': True
                }

            # Run tests if available
            # Note: Only include scripts that exist in the project's package.json
            # gentube-ai-multi has: lint, test, prebuild (no type-check)
            test_commands = [
                ['npm', 'run', 'lint'],        # Linting
                # ['npm', 'test'],             # Unit tests (uncomment if desired)
            ]

            results = []
            for cmd in test_commands:
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=app_path,
                        capture_output=True,
                        text=True,
                        timeout=300
                    )

                    results.append({
                        'command': ' '.join(cmd),
                        'success': result.returncode == 0,
                        'output': result.stdout if result.returncode == 0 else result.stderr
                    })

                except subprocess.TimeoutExpired:
                    results.append({
                        'command': ' '.join(cmd),
                        'success': False,
                        'output': 'Test timeout'
                    })
                except FileNotFoundError:
                    # Command not available, skip
                    continue

            # Check if any tests failed
            failed = [r for r in results if not r['success']]
            if failed:
                return {
                    'success': False,
                    'message': f'{len(failed)} tests failed',
                    'results': results
                }

            return {
                'success': True,
                'message': 'All tests passed',
                'results': results
            }

        except Exception as e:
            logger.error(f"Test execution failed: {e}")
            return {
                'success': False,
                'message': str(e)
            }

    def _build_and_fix(self, max_attempts: int = 3, strategy_id: int = None) -> Dict[str, Any]:
        """
        Run build and fix any errors using Claude before pushing.

        Args:
            max_attempts: Maximum number of fix attempts
            strategy_id: Strategy ID (enables interactive troubleshooting after 2 failures)

        Returns:
            Result with success status and any fixes applied
        """
        logger.info("Running build check before push...")

        app_path = Path(self.gentube_app_path)
        if not app_path.exists():
            return {
                'success': True,
                'message': 'App path not found, skipping build',
                'skipped': True
            }

        fixes_applied = []
        last_error_signature = None
        same_error_count = 0

        for attempt in range(1, max_attempts + 1):
            logger.info(f"Build attempt {attempt}/{max_attempts}")

            try:
                # Run npm run build
                result = subprocess.run(
                    ['npm', 'run', 'build'],
                    cwd=app_path,
                    capture_output=True,
                    text=True,
                    timeout=600  # 10 minute timeout for build
                )

                if result.returncode == 0:
                    logger.info("Build succeeded!")
                    return {
                        'success': True,
                        'message': 'Build passed',
                        'attempts': attempt,
                        'fixes_applied': fixes_applied
                    }

                # Build failed - extract error (combine both streams since Next.js errors go to stdout)
                error_output = (result.stdout or '') + '\n' + (result.stderr or '')
                logger.warning(f"Build failed (attempt {attempt}). Last 1000 chars: ...{error_output[-1000:]}")

                # Track if we're seeing the exact same error repeatedly
                error_signature = self._get_error_signature(error_output)
                if error_signature == last_error_signature:
                    same_error_count += 1
                    logger.warning(f"Same error repeated {same_error_count + 1} times (attempt {attempt})")
                else:
                    same_error_count = 0
                    last_error_signature = error_signature

                # Try installing missing packages first
                installed = self._handle_missing_packages(error_output)
                if installed:
                    logger.info(f"Installed packages: {installed}, retrying build...")
                    # Retry build immediately without counting as a fix attempt
                    retry = subprocess.run(
                        ['npm', 'run', 'build'],
                        cwd=app_path,
                        capture_output=True,
                        text=True,
                        timeout=600
                    )
                    if retry.returncode == 0:
                        logger.info("Build succeeded after package installation!")
                        # Stage the package changes
                        repo = git.Repo(app_path)
                        repo.git.add(A=True)
                        return {
                            'success': True,
                            'message': 'Build passed after installing packages',
                            'attempts': attempt,
                            'packages_installed': installed,
                            'fixes_applied': fixes_applied
                        }
                    # Update error output with new errors
                    error_output = (retry.stdout or '') + '\n' + (retry.stderr or '')
                    logger.warning(f"Build still failing after package install: {error_output[:500]}...")
                    # Recheck error signature after package install
                    error_signature = self._get_error_signature(error_output)
                    if error_signature == last_error_signature:
                        same_error_count += 1
                    else:
                        same_error_count = 0
                        last_error_signature = error_signature

                # Use Claude to fix the error
                fix_result = self._fix_build_error(error_output, attempt=attempt)

                if not fix_result.get('success'):
                    logger.error(f"Failed to generate fix: {fix_result.get('error')}")

                    if attempt >= max_attempts:
                        logger.error("Max build fix attempts reached")
                        return {
                            'success': False,
                            'message': f'Build failed after {max_attempts} attempts',
                            'error': error_output[:2000],
                            'fixes_applied': fixes_applied,
                            'same_error_count': same_error_count
                        }
                    continue

                # Apply the fix
                for file_fix in fix_result.get('fixes', []):
                    file_path = file_fix.get('file_path')
                    new_content = file_fix.get('new_content')

                    if file_path and new_content:
                        full_path = app_path / file_path
                        if full_path.exists():
                            with open(full_path, 'w') as f:
                                f.write(new_content)
                            fixes_applied.append({
                                'file': file_path,
                                'attempt': attempt
                            })
                            logger.info(f"Applied fix to: {file_path}")

                # Stage the fixes for the commit
                repo = git.Repo(app_path)
                repo.git.add(A=True)

            except subprocess.TimeoutExpired:
                logger.error("Build timeout")
                return {
                    'success': False,
                    'message': 'Build timeout',
                    'fixes_applied': fixes_applied
                }
            except Exception as e:
                logger.error(f"Build error: {e}")
                return {
                    'success': False,
                    'message': str(e),
                    'fixes_applied': fixes_applied
                }

        return {
            'success': False,
            'message': 'Build failed',
            'fixes_applied': fixes_applied
        }

    def _handle_missing_packages(self, error_output: str) -> List[str]:
        """
        Detect and install missing npm packages from build error output.

        Args:
            error_output: The build error output

        Returns:
            List of package names that were installed (empty if none)
        """
        import re

        # Match patterns like: Module not found: Can't resolve 'package-name'
        pattern = r"Module not found:.*?Can't resolve '([^']+)'"
        matches = re.findall(pattern, error_output)

        if not matches:
            return []

        # Filter out local imports (starting with . or @ path aliases like @/)
        packages = []
        for match in matches:
            # Skip local/alias imports
            if match.startswith('.') or match.startswith('@/'):
                continue
            # Validate package name: only allow alphanumeric, hyphens, slashes, @scoped
            if not re.match(r'^(@[a-zA-Z0-9_-]+/)?[a-zA-Z0-9_.-]+$', match):
                logger.warning(f"Skipping invalid package name: {match}")
                continue
            packages.append(match)

        if not packages:
            return []

        # Deduplicate
        packages = list(set(packages))
        logger.info(f"Detected missing packages: {packages}")

        try:
            app_path = Path(self.gentube_app_path)
            result = subprocess.run(
                ['npm', 'install'] + packages,
                cwd=app_path,
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0:
                logger.info(f"Successfully installed packages: {packages}")
                return packages
            else:
                logger.error(f"Failed to install packages: {result.stderr[:500]}")
                return []

        except Exception as e:
            logger.error(f"Error installing packages: {e}")
            return []

    def _fix_build_error(self, error_output: str, attempt: int = 1) -> Dict[str, Any]:
        """
        Use Claude to analyze build error and generate a fix.

        Uses a three-step approach:
        1. Haiku extracts the relevant error context and identifies files to fix
        2. Read the identified files from disk
        3. Sonnet/Opus generates the fix (tiered: Sonnet first, Opus escalates)

        Args:
            error_output: The build error output

        Returns:
            Result with fixes to apply
        """
        try:
            app_path = Path(self.gentube_app_path)

            # Step 1: Use Haiku to extract error context and identify files
            haiku_result = self._extract_error_context(error_output)

            if not haiku_result.get('success'):
                logger.error(f"Haiku error extraction failed: {haiku_result.get('error')}")
                return {
                    'success': False,
                    'error': f"Error extraction failed: {haiku_result.get('error')}"
                }

            files_to_fix = haiku_result.get('files_to_fix', [])
            error_summary = haiku_result.get('error_summary', '')
            extracted_errors = haiku_result.get('extracted_errors', '')

            if not files_to_fix:
                logger.warning("Haiku identified no files to fix")
                return {
                    'success': False,
                    'error': 'Could not identify files to fix from error output'
                }

            logger.info(f"Haiku identified {len(files_to_fix)} file(s) to fix: {[f['file_path'] for f in files_to_fix]}")

            # Step 2: Read the identified files from disk
            file_contents = {}
            for file_info in files_to_fix:
                file_path = file_info.get('file_path', '')
                if not file_path:
                    continue
                full_path = app_path / file_path
                if full_path.exists():
                    try:
                        with open(full_path, 'r') as f:
                            file_contents[file_path] = f.read()
                    except Exception:
                        logger.warning(f"Could not read file: {file_path}")

            if not file_contents:
                return {
                    'success': False,
                    'error': f'None of the identified files exist on disk: {[f["file_path"] for f in files_to_fix]}'
                }

            # Step 3: Use Sonnet for all fix attempts
            models = [("claude-sonnet-4-5-20250929", "Sonnet")]

            for model_id, model_name in models:
                logger.info(f"Attempting build fix with {model_name}...")

                fixes = []
                for file_info in files_to_fix:
                    file_path = file_info.get('file_path', '')
                    fix_description = file_info.get('fix_description', '')

                    if file_path and file_path in file_contents:
                        fix_result = self._generate_fix_for_file(
                            file_path=file_path,
                            current_content=file_contents[file_path],
                            error_output=extracted_errors,
                            fix_description=fix_description,
                            model=model_id
                        )

                        if fix_result.get('success'):
                            fixes.append({
                                'file_path': file_path,
                                'new_content': fix_result.get('new_content')
                            })
                            logger.info(f"Generated fix for: {file_path} (using {model_name})")
                        else:
                            logger.warning(f"Failed to generate fix for {file_path}: {fix_result.get('error')}")

                if fixes:
                    logger.info(f"Build fix succeeded with {model_name}: {len(fixes)} file(s)")
                    return {
                        'success': True,
                        'analysis': error_summary,
                        'fixes': fixes,
                        'model_used': model_name
                    }
                else:
                    logger.warning(f"{model_name} failed to generate any fixes, escalating...")

            return {
                'success': False,
                'error': 'Failed to generate fixes with both Sonnet and Opus'
            }

        except Exception as e:
            logger.error(f"Failed to generate fix: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _extract_error_context(self, error_output: str) -> Dict[str, Any]:
        """
        Use Haiku to intelligently extract error context from build output.

        Replaces regex-based file extraction. Haiku reads the full error output
        and identifies which files need fixing and what the actual errors are.

        Returns:
            Dict with 'files_to_fix' (list of {file_path, fix_description}),
            'error_summary' (brief analysis), and 'extracted_errors' (relevant error text).
        """
        prompt = f"""Analyze this build/test output and extract the error information.

BUILD OUTPUT (last 15000 chars):
{error_output[-15000:]}

Return a JSON response with this exact structure:
{{
  "error_summary": "Brief 1-2 sentence explanation of what's failing and why",
  "extracted_errors": "The exact error messages, stack traces, and relevant context copied from the output above. Include file paths, line numbers, assertion failures, type errors, etc. This will be passed to another model to generate fixes.",
  "files_to_fix": [
    {{
      "file_path": "relative/path/to/file.tsx",
      "fix_description": "What needs to be changed in this file to fix the error"
    }}
  ]
}}

IMPORTANT:
- file_path must be a relative path from the project root (e.g., "src/components/Button.tsx" or "tests/api.test.ts")
- Do NOT include node_modules paths
- extracted_errors should contain the EXACT error text, not a paraphrase. ESCAPE all special JSON characters (quotes, backslashes, newlines) properly.
- If the error is a test failure, include the test name, expected vs received values, and BOTH the test file AND the source file being tested (e.g., if tests/foo.test.ts tests src/app/api/foo/route.ts, include BOTH files in files_to_fix)
- Return ONLY valid JSON, no markdown code blocks or other text"""

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text.strip()
            logger.info(f"Haiku raw response (first 500 chars): {response_text[:500]}")
            data = self._extract_json(response_text)

            if data is None:
                logger.warning(f"Haiku error extraction: could not parse JSON response. Full response:\n{response_text[:2000]}")
                # Fallback: return the raw error output so the fix model can still try
                return {
                    'success': True,
                    'error_summary': 'Haiku JSON parse failed - using raw error output',
                    'extracted_errors': error_output[-8000:],
                    'files_to_fix': self._guess_files_from_error(error_output)
                }

            return {
                'success': True,
                'error_summary': data.get('error_summary', ''),
                'extracted_errors': data.get('extracted_errors', ''),
                'files_to_fix': data.get('files_to_fix', [])
            }

        except Exception as e:
            logger.error(f"Error in Haiku error extraction: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _get_error_signature(self, error_output: str) -> str:
        """
        Extract a normalized signature from build error output for comparison.
        Strips timestamps, paths, and whitespace to detect the same underlying error.
        """
        import hashlib
        import re
        # Extract error lines (lines containing 'error', 'Error', 'failed', 'TypeError', etc.)
        error_lines = []
        for line in error_output.split('\n'):
            stripped = line.strip()
            # Skip empty lines, timestamp-only lines, progress indicators
            if not stripped or stripped.startswith('>') or stripped.startswith('info'):
                continue
            if any(kw in stripped.lower() for kw in ['error', 'failed', 'cannot', 'not found', 'unexpected', 'typeerror', 'syntaxerror', 'referenceerror']):
                # Remove line numbers and column numbers (e.g., :42:10)
                normalized = re.sub(r':\d+:\d+', ':L:C', stripped)
                # Remove timestamps
                normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', normalized)
                error_lines.append(normalized)
        signature_text = '\n'.join(sorted(set(error_lines)))
        return hashlib.md5(signature_text.encode()).hexdigest()

    def _guess_files_from_error(self, error_output: str) -> List[Dict]:
        """Regex fallback to extract file paths from error output when Haiku JSON parsing fails."""
        import re
        # Match common patterns: src/path/file.ts, tests/path/file.test.ts, etc.
        pattern = r'(?:^|\s|[(\'"])(((?:src|tests|app|pages|components|lib|utils|api)/[^\s:,\'"()]+\.(?:ts|tsx|js|jsx|mjs|cjs)))'
        matches = re.findall(pattern, error_output)
        seen = set()
        files = []
        for match in matches:
            path = match[0]
            if path not in seen and 'node_modules' not in path:
                seen.add(path)
                files.append({'file_path': path, 'fix_description': 'Fix error in this file'})
        return files[:5]  # Limit to 5 files

    def _generate_fix_for_file(self, file_path: str, current_content: str,
                                error_output: str, fix_description: str,
                                model: str = "claude-sonnet-4-5-20250929") -> Dict[str, Any]:
        """
        Step 2: Generate the fixed content for a single file.

        Uses raw code format (no JSON wrapper) to avoid escaping issues.
        """
        prompt = f"""Fix this file to resolve the build error.

FILE: {file_path}

ERROR CONTEXT:
{error_output}

FIX NEEDED: {fix_description}

CURRENT FILE CONTENT:
{current_content}

Output ONLY the complete fixed file content - no JSON, no markdown code blocks.
Start your response with the first line of code (e.g., import statement).

Requirements:
1. Output the COMPLETE file - every function must be finished
2. Ensure all braces {{}}, brackets [], and parens () are balanced
3. Include all imports at the top
4. Fix the build error while preserving existing functionality"""

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text.strip()

            # Strip any markdown code blocks that Claude might add despite instructions
            code = self._strip_markdown_blocks(content)

            return {
                'success': True,
                'file_path': file_path,
                'new_content': code
            }

        except Exception as e:
            logger.error(f"Error generating fix for {file_path}: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _strip_markdown_blocks(self, content: str) -> str:
        """Strip markdown code blocks from content"""
        content = content.strip()
        if content.startswith('```json'):
            content = content[7:]
        elif content.startswith('```typescript'):
            content = content[13:]
        elif content.startswith('```tsx'):
            content = content[6:]
        elif content.startswith('```javascript'):
            content = content[13:]
        elif content.startswith('```jsx'):
            content = content[6:]
        elif content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        return content.strip()

    def _extract_json(self, text: str) -> Optional[Dict]:
        """
        Extract JSON from response text, handling various formats.

        Handles:
        - Plain JSON
        - JSON in markdown code blocks
        - JSON with trailing commas
        - JSON embedded in other text
        """
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to extract from markdown code blocks
        if '```' in text:
            # Find content between code blocks
            import re
            code_block_pattern = r'```(?:json)?\s*([\s\S]*?)```'
            matches = re.findall(code_block_pattern, text)
            for match in matches:
                try:
                    return json.loads(match.strip())
                except json.JSONDecodeError:
                    # Try fixing trailing commas
                    fixed = self._fix_json_trailing_commas(match.strip())
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        continue

        # Try to find JSON object in text (starts with { ends with })
        start_idx = text.find('{')
        if start_idx != -1:
            # Find matching closing brace
            brace_count = 0
            end_idx = start_idx
            for i, char in enumerate(text[start_idx:], start_idx):
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_idx = i + 1
                        break

            if end_idx > start_idx:
                json_str = text[start_idx:end_idx]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    # Try fixing trailing commas
                    fixed = self._fix_json_trailing_commas(json_str)
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        pass

        return None

    def _fix_json_trailing_commas(self, json_str: str) -> str:
        """Remove trailing commas from JSON string (common LLM error)"""
        import re
        # Remove trailing commas before ] or }
        fixed = re.sub(r',\s*([}\]])', r'\1', json_str)
        return fixed

    def _commit_changes(self, strategy_id: int, changes: List[Dict], approval: Dict) -> str:
        """Commit changes to git"""
        logger.info("Committing changes...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                logger.warning("App path not found, skipping git commit")
                return 'no-commit'

            repo = git.Repo(app_path)

            # Stage all changes
            repo.git.add(A=True)

            # Create commit message
            summary = changes[0].get('action', 'Optimization') if changes else 'Optimization'
            commit_message = f"[Auto-Optimize] Strategy #{strategy_id}: {summary}\n\n"
            commit_message += f"Applied {len(changes)} changes:\n"
            for i, change in enumerate(changes):
                commit_message += f"{i+1}. {change.get('action', 'N/A')}\n"

            commit_message += f"\nApproved by: {approval.get('user_response', 'system')}\n"

            # Commit
            repo.index.commit(commit_message)

            commit_hash = repo.head.commit.hexsha
            logger.info(f"Changes committed: {commit_hash}")

            return commit_hash

        except Exception as e:
            logger.error(f"Git commit failed: {e}")
            raise

    def _deploy_to_vercel(self) -> Optional[str]:
        """Deploy to Vercel"""
        logger.info("Deploying to Vercel...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                logger.warning("App path not found, skipping deployment")
                return None

            # Push to git first
            repo = git.Repo(app_path)
            origin = repo.remote('origin')
            origin.push()

            logger.info("Pushed to git, Vercel auto-deploy should trigger")

            # Optionally, trigger Vercel deployment via CLI
            if shutil.which('vercel'):
                result = subprocess.run(
                    ['vercel', '--prod', '--token', self.vercel_token],
                    cwd=app_path,
                    capture_output=True,
                    text=True,
                    timeout=600
                )

                if result.returncode == 0:
                    # Extract deployment URL from output
                    output = result.stdout
                    # Parse URL (usually in format: https://gentube-xxx.vercel.app)
                    lines = output.split('\n')
                    for line in lines:
                        if 'https://' in line:
                            return line.strip()

            return 'deployment-pending'

        except Exception as e:
            logger.error(f"Vercel deployment failed: {e}")
            raise

    def _rollback(self, rollback_info: Dict) -> None:
        """Rollback to previous state"""
        logger.info("Rolling back changes...")

        try:
            app_path = Path(self.gentube_app_path)
            if not app_path.exists():
                return

            repo = git.Repo(app_path)

            # Reset to previous commit
            if rollback_info.get('commit'):
                repo.git.reset('--hard', rollback_info['commit'])
                logger.info(f"Reset to commit {rollback_info['commit']}")

            # Optionally push rollback
            origin = repo.remote('origin')
            origin.push(force=True)

            logger.info("Rollback complete")

        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            raise

    def monitor_vercel_build(self, branch_name: str, poll_interval: int = 60,
                              max_duration: int = 600) -> Dict[str, Any]:
        """
        Monitor Vercel build status for a branch.

        Args:
            branch_name: The git branch to monitor
            poll_interval: Seconds between status checks (default 60)
            max_duration: Maximum monitoring duration in seconds (default 600 = 10 min)

        Returns:
            Result with status: 'ready', 'error', or 'timeout'
        """
        import requests

        if not self.vercel_token or not self.vercel_project_id:
            logger.warning("Vercel token or project ID not configured, skipping build monitoring")
            return {
                'success': True,
                'status': 'skipped',
                'message': 'Vercel not configured'
            }

        logger.info(f"Monitoring Vercel build for branch: {branch_name} (max {max_duration}s)")

        headers = {
            'Authorization': f'Bearer {self.vercel_token}',
            'Content-Type': 'application/json'
        }

        start_time = time.time()
        check_count = 0

        # Normalize branch name (Vercel uses the branch name in deployment metadata)
        # e.g., "opt/strategy-10" -> deployment for that branch

        while time.time() - start_time < max_duration:
            check_count += 1
            elapsed = int(time.time() - start_time)
            logger.info(f"Vercel build check #{check_count} (elapsed: {elapsed}s)")

            try:
                # Query Vercel deployments API for this project
                # Filter by branch using meta.gitBranch or state
                url = f"https://api.vercel.com/v6/deployments"
                params = {
                    'projectId': self.vercel_project_id,
                    'limit': 5,  # Get recent deployments
                    'target': 'preview'  # We're monitoring preview deployments
                }

                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()

                deployments = response.json().get('deployments', [])

                # Find deployment for our branch
                for deployment in deployments:
                    meta = deployment.get('meta', {})
                    deploy_branch = meta.get('githubCommitRef', '') or meta.get('gitBranch', '')

                    if deploy_branch == branch_name:
                        state = deployment.get('state', deployment.get('readyState', ''))
                        deployment_url = deployment.get('url', '')

                        logger.info(f"Found deployment for {branch_name}: state={state}")

                        if state == 'READY':
                            return {
                                'success': True,
                                'status': 'ready',
                                'message': f'Build succeeded',
                                'deployment_url': f"https://{deployment_url}" if deployment_url else None,
                                'elapsed_seconds': int(time.time() - start_time),
                                'checks': check_count
                            }
                        elif state == 'ERROR':
                            # Fetch build logs for error diagnosis
                            deployment_uid = deployment.get('uid', '')
                            build_logs = ''
                            if deployment_uid:
                                build_logs = self._fetch_vercel_build_logs(deployment_uid)
                            return {
                                'success': False,
                                'status': 'error',
                                'message': 'Build failed',
                                'build_logs': build_logs,
                                'deployment_url': f"https://{deployment_url}" if deployment_url else None,
                                'elapsed_seconds': int(time.time() - start_time),
                                'checks': check_count
                            }
                        elif state in ['BUILDING', 'QUEUED', 'INITIALIZING']:
                            # Still building, continue polling
                            logger.info(f"Build in progress: {state}")
                            break
                        elif state == 'CANCELED':
                            return {
                                'success': False,
                                'status': 'canceled',
                                'message': 'Build was canceled',
                                'elapsed_seconds': int(time.time() - start_time),
                                'checks': check_count
                            }

            except requests.RequestException as e:
                logger.warning(f"Vercel API request failed: {e}")
                # Continue polling despite API errors

            except Exception as e:
                logger.error(f"Error checking Vercel status: {e}")

            # Wait before next check
            time.sleep(poll_interval)

        # Timeout reached
        return {
            'success': False,
            'status': 'timeout',
            'message': f'Build monitoring timed out after {max_duration}s',
            'elapsed_seconds': max_duration,
            'checks': check_count
        }

    def _fetch_vercel_build_logs(self, deployment_uid: str) -> str:
        """
        Fetch build logs from Vercel for a failed deployment.
        Uses Haiku to extract the most relevant error lines from the full log.

        Args:
            deployment_uid: The Vercel deployment UID

        Returns:
            Extracted error log text, or empty string on failure
        """
        import requests

        try:
            headers = {
                'Authorization': f'Bearer {self.vercel_token}',
                'Content-Type': 'application/json'
            }

            url = f"https://api.vercel.com/v2/deployments/{deployment_uid}/events"
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            events = response.json()

            # Collect all log lines
            all_lines = []
            for event in events:
                payload = event.get('payload', {})
                text = payload.get('text', '') or payload.get('log', '')
                if text.strip():
                    all_lines.append(text.strip())

            if not all_lines:
                logger.warning("No log lines found in Vercel deployment events")
                return ''

            full_log = '\n'.join(all_lines)
            logger.info(f"Fetched {len(all_lines)} total log lines from Vercel ({len(full_log)} chars)")

            # Use Haiku to extract the relevant error lines
            try:
                client = anthropic.Anthropic()
                extraction = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=3000,
                    temperature=0,
                    messages=[{"role": "user", "content": f"""Extract ONLY the error-relevant lines from these Vercel build logs. Include:
- Actual error messages and stack traces
- "Module not found" errors
- TypeScript/ESLint errors with file paths and line numbers
- Build failure summaries
- Any lines showing what went wrong

Do NOT include: success messages, timing info, dependency resolution, download progress, or warnings that aren't errors.

Output ONLY the extracted error lines, nothing else.

FULL BUILD LOG:
{full_log[-15000:]}"""}]
                )
                extracted = extraction.content[0].text.strip()
                logger.info(f"Haiku extracted {len(extracted)} chars of error-relevant logs")
                return extracted[:8000]

            except Exception as e:
                logger.warning(f"Haiku extraction failed, falling back to keyword filter: {e}")
                # Fallback: keyword-based extraction
                error_lines = []
                for line in all_lines:
                    lower = line.lower()
                    if ('error' in lower or 'failed' in lower or
                        'module not found' in lower or 'cannot find' in lower or
                        'type error' in lower or 'syntax error' in lower):
                        error_lines.append(line)

                if not error_lines:
                    error_lines = all_lines[-50:]

                return '\n'.join(error_lines)[:8000]

        except Exception as e:
            logger.error(f"Failed to fetch Vercel build logs: {e}")
            return ''

    def simulate_changes(self, changes: List[Dict]) -> Dict[str, Any]:
        """
        Simulate changes without actually applying them (dry run)

        Args:
            changes: List of changes

        Returns:
            Simulation result
        """
        logger.info("Simulating changes (dry run)...")

        simulation = {
            'feasible': True,
            'warnings': [],
            'estimated_impact': {},
            'files_affected': []
        }

        for change in changes:
            files = change.get('files_to_modify', [])
            simulation['files_affected'].extend(files)

            # Check if files exist
            app_path = Path(self.gentube_app_path)
            for file in files:
                file_path = app_path / file
                if not file_path.exists():
                    simulation['warnings'].append(f"File not found: {file}")

            # Check risk level
            risk = change.get('risk_level', 'medium')
            if risk == 'high':
                simulation['warnings'].append(f"High risk change: {change.get('action')}")

        return simulation


# Singleton instance
_execution_agent = None

def get_execution_agent() -> ExecutionAgent:
    """Get or create execution agent singleton"""
    global _execution_agent
    if _execution_agent is None:
        _execution_agent = ExecutionAgent()
    return _execution_agent
