"""
Thread-Safe GitHub Backup Manager for VoraHub Bot
Fixes: Git concurrency issues, branch mismatch, token authentication

Key Features:
- Queue-based backup (no parallel git operations)
- File-based locking mechanism
- Batched commits (multiple files in 1 commit)
- Proper token authentication in remote URL
- Uses 'main' branch (not 'master')
"""

import os
import subprocess
import threading
import queue
import time
import logging
from datetime import datetime
from typing import List, Optional, Set
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [BACKUP] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class GitLock:
    """File-based lock to prevent concurrent Git operations"""
    
    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self.lock_fd = None
    
    def acquire(self, timeout: int = 30) -> bool:
        """Acquire lock with timeout"""
        start_time = time.time()
        
        while True:
            try:
                # Try to create lock file exclusively
                self.lock_fd = os.open(
                    self.lock_file,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                # Write PID to lock file
                os.write(self.lock_fd, str(os.getpid()).encode())
                logger.debug(f"Lock acquired: {self.lock_file}")
                return True
            except FileExistsError:
                # Lock file exists, check if stale
                if os.path.exists(self.lock_file):
                    # Check lock age
                    lock_age = time.time() - os.path.getmtime(self.lock_file)
                    if lock_age > 300:  # 5 minutes = stale lock
                        logger.warning(f"Removing stale lock (age: {lock_age:.0f}s)")
                        try:
                            os.remove(self.lock_file)
                        except:
                            pass
                
                # Check timeout
                if time.time() - start_time > timeout:
                    logger.error(f"Failed to acquire lock after {timeout}s")
                    return False
                
                # Wait and retry
                time.sleep(0.5)
    
    def release(self):
        """Release lock"""
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
                os.remove(self.lock_file)
                logger.debug(f"Lock released: {self.lock_file}")
            except Exception as e:
                logger.error(f"Error releasing lock: {e}")
            finally:
                self.lock_fd = None
    
    def __enter__(self):
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class BackupQueue:
    """Queue for batching backup requests"""
    
    def __init__(self, batch_interval: float = 5.0):
        self.files_to_backup: Set[str] = set()
        self.lock = threading.Lock()
        self.batch_interval = batch_interval
        self.last_backup_time = 0
    
    def add_file(self, filename: str):
        """Add file to backup queue"""
        with self.lock:
            self.files_to_backup.add(filename)
            logger.debug(f"Added to queue: {filename}")
    
    def get_pending_files(self) -> List[str]:
        """Get and clear pending files"""
        with self.lock:
            files = list(self.files_to_backup)
            self.files_to_backup.clear()
            return files
    
    def should_backup(self) -> bool:
        """Check if enough time has passed or queue is full"""
        with self.lock:
            if not self.files_to_backup:
                return False
            
            time_since_last = time.time() - self.last_backup_time
            return time_since_last >= self.batch_interval or len(self.files_to_backup) >= 4
    
    def mark_backup_done(self):
        """Mark backup as completed"""
        self.last_backup_time = time.time()


class GitBackupManager:
    """Thread-safe Git backup manager with queue-based batching"""
    
    def __init__(self, repo_path: str, remote_url: str, auth_token: str):
        """
        Initialize backup manager
        
        Args:
            repo_path: Path to repository (bot directory)
            remote_url: GitHub repository URL (https://github.com/user/repo.git)
            auth_token: GitHub Personal Access Token
        """
        self.repo_path = Path(repo_path).resolve()
        self.remote_url = remote_url
        self.auth_token = auth_token
        self.lock_file = self.repo_path / ".git" / "backup.lock"
        self.backup_queue = BackupQueue(batch_interval=5.0)
        self.worker_thread = None
        self.running = False
        
        # Initialize Git repository
        self._init_git()
        
        # Start background worker
        self._start_worker()
    
    def _run_git_command(self, command: List[str], timeout: int = 30) -> tuple[bool, str]:
        """
        Run Git command safely
        
        Args:
            command: Git command as list
            timeout: Command timeout in seconds
            
        Returns:
            (success: bool, output: str)
        """
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}  # Disable interactive prompts
            )
            
            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                error_msg = result.stderr.strip()
                logger.warning(f"Git command failed: {' '.join(command)}\nError: {error_msg}")
                return False, error_msg
        
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out: {' '.join(command)}")
            return False, "Command timed out"
        except Exception as e:
            logger.error(f"Error running git command: {e}")
            return False, str(e)
    
    def _init_git(self):
        """Initialize Git repository with proper configuration"""
        git_dir = self.repo_path / ".git"
        
        # Initialize if needed
        if not git_dir.exists():
            logger.info("Initializing Git repository...")
            success, _ = self._run_git_command(["git", "init"])
            if not success:
                raise RuntimeError("Failed to initialize Git repository")
            
            # Set initial branch to main
            self._run_git_command(["git", "branch", "-M", "main"])
        
        # Configure Git user
        self._run_git_command(["git", "config", "user.name", "VoraHub Bot"])
        self._run_git_command(["git", "config", "user.email", "bot@vorahub.local"])
        
        # Configure remote with token authentication
        self._configure_remote()
        
        logger.info("Git repository initialized successfully")
    
    def _configure_remote(self):
        """Configure remote with token authentication"""
        # Build authenticated URL
        if self.auth_token and "github.com" in self.remote_url:
            # Convert https://github.com/user/repo.git to https://TOKEN@github.com/user/repo.git
            if self.remote_url.startswith("https://"):
                auth_url = self.remote_url.replace("https://", f"https://{self.auth_token}@")
            else:
                logger.error("Remote URL must use HTTPS for token authentication")
                auth_url = self.remote_url
        else:
            auth_url = self.remote_url
        
        # Check if remote exists
        success, _ = self._run_git_command(["git", "remote", "get-url", "origin"])
        
        if success:
            # Update existing remote
            self._run_git_command(["git", "remote", "set-url", "origin", auth_url])
            logger.info("Remote 'origin' updated with token authentication")
        else:
            # Add new remote
            self._run_git_command(["git", "remote", "add", "origin", auth_url])
            logger.info("Remote 'origin' added with token authentication")
        
        # Set upstream branch to main
        self._run_git_command(["git", "branch", "--set-upstream-to=origin/main", "main"])
    
    def _perform_backup(self, files: List[str]) -> bool:
        """
        Perform actual backup with file locking
        
        Args:
            files: List of filenames to backup
            
        Returns:
            True if successful, False otherwise
        """
        if not files:
            return True
        
        lock = GitLock(str(self.lock_file))
        
        # Acquire lock
        if not lock.acquire(timeout=30):
            logger.error("Failed to acquire Git lock, skipping backup")
            return False
        
        try:
            # Verify files exist
            existing_files = []
            for filename in files:
                filepath = self.repo_path / filename
                if filepath.exists():
                    existing_files.append(filename)
                else:
                    logger.warning(f"File not found: {filename}")
            
            if not existing_files:
                logger.info("No files to backup")
                return True
            
            # Stage files
            for filename in existing_files:
                success, output = self._run_git_command(["git", "add", filename])
                if not success:
                    logger.error(f"Failed to stage {filename}: {output}")
                    return False
            
            # Check if there are changes to commit
            success, output = self._run_git_command(["git", "diff", "--cached", "--quiet"])
            if success:
                logger.info("No changes to commit")
                return True
            
            # Create commit
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            files_str = ", ".join(existing_files)
            commit_msg = f"Auto-backup: {files_str} - {timestamp}"
            
            success, output = self._run_git_command(["git", "commit", "-m", commit_msg])
            if not success:
                logger.error(f"Failed to commit: {output}")
                return False
            
            logger.info(f"Committed: {commit_msg}")
            
            # Push to remote (main branch)
            success, output = self._run_git_command(["git", "push", "origin", "main"], timeout=60)
            if not success:
                logger.error(f"Failed to push to GitHub: {output}")
                # Try to set upstream and push again
                self._run_git_command(["git", "push", "-u", "origin", "main"], timeout=60)
                return False
            
            logger.info(f"Successfully pushed to GitHub: {files_str}")
            return True
        
        except Exception as e:
            logger.error(f"Error during backup: {e}")
            return False
        
        finally:
            lock.release()
    
    def _worker_loop(self):
        """Background worker that processes backup queue"""
        logger.info("Backup worker thread started")
        
        while self.running:
            try:
                # Check if backup is needed
                if self.backup_queue.should_backup():
                    files = self.backup_queue.get_pending_files()
                    if files:
                        logger.info(f"Processing backup queue: {len(files)} file(s)")
                        self._perform_backup(files)
                        self.backup_queue.mark_backup_done()
                
                # Sleep to avoid busy waiting
                time.sleep(1)
            
            except Exception as e:
                logger.error(f"Error in worker loop: {e}")
                time.sleep(5)
        
        logger.info("Backup worker thread stopped")
    
    def _start_worker(self):
        """Start background worker thread"""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.running = True
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
            logger.info("Background backup worker started")
    
    def queue_backup(self, filename: str):
        """
        Queue a file for backup (non-blocking)
        
        Args:
            filename: Name of file to backup (relative to repo_path)
        """
        self.backup_queue.add_file(filename)
    
    def backup_now(self, files: List[str]) -> bool:
        """
        Perform immediate backup (blocking)
        
        Args:
            files: List of filenames to backup
            
        Returns:
            True if successful
        """
        return self._perform_backup(files)
    
    def shutdown(self):
        """Shutdown backup manager gracefully"""
        logger.info("Shutting down backup manager...")
        self.running = False
        
        # Process remaining queue
        files = self.backup_queue.get_pending_files()
        if files:
            logger.info(f"Processing final backup: {len(files)} file(s)")
            self._perform_backup(files)
        
        # Wait for worker thread
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=10)
        
        logger.info("Backup manager shutdown complete")


# Global instance
_backup_manager: Optional[GitBackupManager] = None


def init_backup_manager(repo_path: str, remote_url: str, auth_token: str) -> GitBackupManager:
    """
    Initialize global backup manager
    
    Args:
        repo_path: Path to bot directory
        remote_url: GitHub repository URL
        auth_token: GitHub Personal Access Token
        
    Returns:
        GitBackupManager instance
    """
    global _backup_manager
    
    if _backup_manager is None:
        _backup_manager = GitBackupManager(repo_path, remote_url, auth_token)
        logger.info("Backup manager initialized")
    
    return _backup_manager


def get_backup_manager() -> Optional[GitBackupManager]:
    """Get global backup manager instance"""
    return _backup_manager


def backup_to_github(files: List[str], async_mode: bool = True):
    """
    Backup files to GitHub
    
    Args:
        files: List of filenames to backup
        async_mode: If True, queue for async backup. If False, backup immediately.
    """
    if _backup_manager is None:
        logger.warning("Backup manager not initialized, skipping backup")
        return
    
    if async_mode:
        # Queue files for batched backup
        for filename in files:
            _backup_manager.queue_backup(filename)
    else:
        # Immediate backup
        _backup_manager.backup_now(files)


def shutdown_backup():
    """Shutdown backup manager gracefully"""
    if _backup_manager is not None:
        _backup_manager.shutdown()
