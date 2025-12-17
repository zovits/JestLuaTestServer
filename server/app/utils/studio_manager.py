import asyncio
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from app.config_manager import config as app_config
from app.utils.fflag_manager import managed_fflags
from app.utils.plugin_manager import managed_plugin

logger = logging.getLogger(__name__)


class StudioManager:
    """Manages Roblox Studio process lifecycle for RL training evaluations"""

    def __init__(self, place_file: Path):
        # The place file to open in Studio
        self.place_file = place_file

        # Process management
        self.process: subprocess.Popen | None = None

        # Component managers
        self.plugin_manager = None  # Set by managed_studio context
        self.fflag_manager = None  # Set by managed_studio context

        # Heartbeat tracking (for future persistent mode)
        self._last_heartbeat: datetime | None = None
        self._plugin_connections: set = set()

        # Studio paths
        self.studio_dir = Path.home() / "AppData" / "Local" / "Roblox Studio"
        self.studio_path = self._find_studio_executable()

    def _find_studio_executable(self) -> Path:
        """Find Roblox Studio executable path"""
        default_path = self.studio_dir / "RobloxStudioBeta.exe"

        # Check default location first
        if default_path.exists():
            return default_path

        # Try registry lookup (Windows only)
        registry_path = self._find_studio_from_registry()
        if registry_path:
            return registry_path

        # Try alternative locations
        alt_paths = [
            Path.home()
            / "AppData"
            / "Local"
            / "Roblox"
            / "Versions"
            / "RobloxStudioBeta.exe",
            Path("C:/Program Files/Roblox/RobloxStudioBeta.exe"),
            Path("C:/Program Files (x86)/Roblox/RobloxStudioBeta.exe"),
        ]

        for path in alt_paths:
            if path.exists():
                logger.debug(f"Found Studio at alternative location: {path}")
                return path

        # Return default path even if not found (will error later with clear message)
        return default_path

    def _find_studio_from_registry(self) -> Path | None:
        """Try to find Studio path from Windows registry"""
        if sys.platform != "win32":
            return None

        try:
            import winreg

            # Try current user registry
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\ROBLOX Corporation\Environments\roblox-studio",
                )
                studio_exe, _ = winreg.QueryValueEx(key, "clientExe")
                winreg.CloseKey(key)
                studio_exe = Path(studio_exe)
                if studio_exe.exists():
                    logger.debug(f"Found Studio via registry (HKCU): {studio_exe}")
                    return studio_exe
            except (FileNotFoundError, OSError):
                pass

        except ImportError:
            logger.debug("winreg module not available")
        except Exception as e:
            logger.debug(f"Registry lookup failed: {e}")

        return None

    def _clean_lock_file(self) -> None:
        """Remove any stale lock files from previous sessions"""
        if not self.place_file:
            return

        lock_file_path = Path(str(self.place_file) + ".lock")
        if lock_file_path.exists():
            logger.debug(f"Removing stale lock file: {lock_file_path}")
            try:
                lock_file_path.unlink()
                logger.debug("Lock file removed successfully")
            except Exception as e:
                logger.warning(f"Failed to clean up stale lock file: {e}")

    async def _launch_studio_process(self) -> bool:
        """Launch the Studio process with appropriate settings"""
        cmd = [
            str(self.studio_path),
            "-localPlaceFile",
            str(self.place_file),
        ]

        logger.info(f"Starting Roblox Studio: {' '.join(cmd)}")

        # Platform-specific process creation
        if sys.platform == "win32":
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

        logger.debug(f"Studio process created with PID: {self.process.pid}")
        return True

    async def _verify_studio_startup(self) -> bool:
        """Verify that Studio started successfully"""
        if self.process is None:
            logger.error("Studio process was not created")
            return False

        # Check immediate startup
        await asyncio.sleep(0.1)
        if self.process.poll() is not None:
            logger.error(
                f"Studio process died immediately with return code: {self.process.poll()}"
            )
            stdout, stderr = self.process.communicate()
            if stdout:
                logger.error(f"STDOUT: {stdout}")
            if stderr:
                logger.error(f"STDERR: {stderr}")
            return False

        # Start health monitoring
        asyncio.create_task(self._monitor_process_health())

        # Wait for full startup
        await asyncio.sleep(5)

        if self.process.poll() is not None:
            logger.error(
                f"Studio process exited during startup with code: {self.process.returncode}"
            )
            return False

        logger.info("Roblox Studio started successfully")
        return True

    async def start_studio(self) -> bool:
        """Start Roblox Studio with the configured place file"""
        # Verify Studio is installed
        if not self.studio_path.exists():
            logger.error(f"Roblox Studio not found at: {self.studio_path}")
            return False

        # Verify place file exists
        if not self.place_file.exists():
            logger.error(f"Place file not found at: {self.place_file}")
            return False

        try:
            # Clean any stale lock files
            self._clean_lock_file()

            # Launch Studio
            if not await self._launch_studio_process():
                return False

            # Verify startup
            return await self._verify_studio_startup()

        except Exception as e:
            logger.error(f"Failed to start Studio: {e}")
            return False

    async def _terminate_studio_process(self) -> None:
        """Attempt graceful termination of Studio process"""
        if self.process is None:
            return

        if sys.platform == "win32":
            # Windows: Send WM_CLOSE
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid)],
                    check=False,
                    capture_output=True,
                )
                logger.debug("Sent graceful shutdown signal to Studio")
            except Exception as e:
                logger.warning(f"Could not send graceful shutdown: {e}")
        else:
            # Unix: Send SIGTERM
            self.process.terminate()
            logger.info("Sent SIGTERM to Studio process")

    async def _force_kill_studio_process(self) -> None:
        """Force kill Studio process"""
        if self.process is None:
            return

        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                check=False,
                capture_output=True,
            )
        else:
            self.process.kill()

    async def stop_studio(self, skip_graceful: bool = False) -> bool:
        """Stop Roblox Studio gracefully or forcefully"""
        if not self.process:
            logger.info("No Studio process to stop")
            return True

        try:
            if skip_graceful:
                logger.info(
                    "Force killing Studio process (skipping graceful shutdown)..."
                )
                await self._force_kill_studio_process()

                # Wait for force kill
                try:
                    await asyncio.wait_for(self._wait_for_process(), timeout=5.0)
                    logger.info("Studio process force killed successfully")
                except TimeoutError:
                    logger.error("Failed to kill Studio process")
            else:
                logger.info("Stopping Roblox Studio...")

                # Attempt graceful termination
                await self._terminate_studio_process()

                # Wait for graceful shutdown
                try:
                    await asyncio.wait_for(
                        self._wait_for_process(), timeout=app_config.shutdown_timeout
                    )
                    logger.info("Studio terminated gracefully")
                except TimeoutError:
                    logger.warning("Graceful shutdown timed out, force killing...")

                    # Force kill
                    await self._force_kill_studio_process()

                    # Wait for force kill
                    try:
                        await asyncio.wait_for(self._wait_for_process(), timeout=5.0)
                        logger.info("Studio process force killed successfully")
                    except TimeoutError:
                        logger.error("Failed to kill Studio process")

            # Cleanup
            self._clean_lock_file()
            self.process = None

            logger.info("Roblox Studio stopped")
            return True

        except Exception as e:
            logger.error(f"Failed to stop Studio: {e}")
            return False

    async def _wait_for_process(self) -> None:
        """Wait for process to terminate"""
        while self.process and self.process.poll() is None:
            await asyncio.sleep(0.5)

    async def _monitor_process_health(self) -> None:
        """Monitor the health of the Studio process"""
        try:
            check_count = 0
            while self.process:
                await asyncio.sleep(1)
                poll_result = self.process.poll()
                check_count += 1

                if poll_result is not None:
                    if poll_result == 0:
                        logger.info(
                            f"Studio process exited normally after {check_count} seconds"
                        )
                    else:
                        logger.error(
                            f"Studio process exited with code {poll_result} after {check_count} seconds"
                        )
                    break
        except Exception as e:
            logger.error(f"Error monitoring Studio process health: {e}")

    def is_running(self) -> bool:
        """Check if Studio process is currently running"""
        return self.process is not None and self.process.poll() is None

    def update_heartbeat(self) -> None:
        """Update the last heartbeat timestamp"""
        self._last_heartbeat = datetime.now()

    def is_healthy(self) -> dict:
        """Check health status of all components"""
        return {
            "studio_running": self.is_running(),
            "plugin_installed": (
                self.plugin_manager.is_installed() if self.plugin_manager else False
            ),
            "plugin_connected": len(self._plugin_connections) > 0,
            "fflags_applied": (
                self.fflag_manager._applied if self.fflag_manager else False
            ),
            "place_file_exists": self.place_file is not None
            and self.place_file.exists(),
        }


@asynccontextmanager
async def managed_studio(
    place_file: Path,
    plugin_manager=None,
    fflag_manager=None,
):
    """
    Context manager that ensures Studio is properly started and stopped.

    Args:
        place_file: Path to the .rbxl place file to open
        plugin_manager: Optional pre-installed PluginManager to reuse
        fflag_manager: Optional pre-configured FFlagManager to reuse

    If plugin_manager and fflag_manager are provided, they are reused
    (useful for caching across multiple Studio instances in a session).
    If not provided, new ones are created and cleaned up on exit.
    """
    studio_manager = StudioManager(place_file)

    # If managers are provided, reuse them; otherwise create and manage lifecycle
    if plugin_manager is not None and fflag_manager is not None:
        # Reuse existing managers (no cleanup on exit)
        studio_manager.fflag_manager = fflag_manager
        studio_manager.plugin_manager = plugin_manager

        try:
            success = await studio_manager.start_studio()
            if not success:
                raise RuntimeError("Failed to start Roblox Studio")
            yield studio_manager
        finally:
            await studio_manager.stop_studio()
    else:
        # Create and manage lifecycle of new managers
        async with (
            managed_fflags(studio_manager.studio_dir) as new_fflag_manager,
            managed_plugin() as new_plugin_manager,
        ):
            studio_manager.fflag_manager = new_fflag_manager
            studio_manager.plugin_manager = new_plugin_manager

            try:
                success = await studio_manager.start_studio()
                if not success:
                    raise RuntimeError("Failed to start Roblox Studio")
                yield studio_manager
            finally:
                await studio_manager.stop_studio()
