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

    STUDIO_PROCESS_NAME = "RobloxStudioBeta.exe"

    def __init__(self, place_id: int, universe_id: int, user_id: int):
        # Roblox place/universe/user IDs for launching Studio via protocol URL
        self.place_id = place_id
        self.universe_id = universe_id
        self.user_id = user_id

        # Process management - we track PID since we discover the process after launch
        self.studio_pid: int | None = None

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

    def _build_protocol_url(self) -> str:
        """Build the roblox-studio: protocol URL for launching Studio"""
        return (
            f"roblox-studio:1+userId:{self.user_id}"
            f"+task:EditPlace+placeId:{self.place_id}+universeId:{self.universe_id}"
        )

    def _find_studio_pids(self) -> list[int]:
        """Find all running RobloxStudioBeta.exe process IDs"""
        if sys.platform != "win32":
            return []

        try:
            # Use tasklist to find Studio processes
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"IMAGENAME eq {self.STUDIO_PROCESS_NAME}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            pids = []
            for line in result.stdout.strip().split("\n"):
                if line and self.STUDIO_PROCESS_NAME in line:
                    # CSV format: "RobloxStudioBeta.exe","12345","Console","1","123,456 K"
                    parts = line.split(",")
                    if len(parts) >= 2:
                        # Remove quotes and parse PID
                        pid_str = parts[1].strip('"')
                        try:
                            pids.append(int(pid_str))
                        except ValueError:
                            pass
            return pids
        except Exception as e:
            logger.warning(f"Failed to find Studio processes: {e}")
            return []

    def _is_pid_running(self, pid: int) -> bool:
        """Check if a process with the given PID is still running"""
        if sys.platform != "win32":
            return False

        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    async def _launch_studio_process(self) -> bool:
        """Launch Studio via the roblox-studio: protocol URL using PowerShell"""
        protocol_url = self._build_protocol_url()
        logger.info(f"Starting Roblox Studio via protocol URL: {protocol_url}")

        if sys.platform != "win32":
            logger.error("Protocol URL launch is only supported on Windows")
            return False

        # Record existing Studio PIDs so we can identify the new one
        existing_pids = set(self._find_studio_pids())
        logger.debug(f"Existing Studio PIDs before launch: {existing_pids}")

        # Use PowerShell's Start-Process to launch via protocol handler (fire-and-forget)
        cmd = ["powershell", "-Command", f'Start-Process "{protocol_url}"']

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        logger.debug(f"PowerShell launcher process created with PID: {process.pid}")

        # Wait for launcher to complete (it exits quickly after spawning the handler)
        process.wait()
        logger.debug(f"PowerShell launcher exited with code: {process.returncode}")

        return True

    async def _verify_studio_startup(self, timeout: float = 60.0) -> bool:
        """Wait for Studio process to appear and verify it started successfully"""
        logger.info("Waiting for Roblox Studio process to start...")

        start_time = asyncio.get_event_loop().time()

        # Poll for Studio process to appear
        while asyncio.get_event_loop().time() - start_time < timeout:
            pids = self._find_studio_pids()
            if pids:
                # Take the first (or newest) Studio process we find
                self.studio_pid = pids[0]
                logger.info(f"Found Roblox Studio process with PID: {self.studio_pid}")

                # Start health monitoring
                asyncio.create_task(self._monitor_process_health())

                # Give Studio a moment to fully initialize
                await asyncio.sleep(2)

                # Verify it's still running
                if self._is_pid_running(self.studio_pid):
                    logger.info("Roblox Studio started successfully")
                    return True
                else:
                    logger.error("Studio process exited shortly after starting")
                    self.studio_pid = None
                    return False

            await asyncio.sleep(1)

        logger.error(f"Studio process did not start within {timeout} seconds")
        return False

    async def start_studio(self) -> bool:
        """Start Roblox Studio with the configured place/universe IDs"""
        try:
            # Launch Studio via protocol URL
            if not await self._launch_studio_process():
                return False

            # Verify startup
            return await self._verify_studio_startup()

        except Exception as e:
            logger.error(f"Failed to start Studio: {e}")
            return False

    async def _terminate_studio_process(self) -> None:
        """Attempt graceful termination of Studio process"""
        if self.studio_pid is None:
            return

        if sys.platform == "win32":
            # Windows: Send WM_CLOSE via taskkill (without /F for graceful)
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.studio_pid)],
                    check=False,
                    capture_output=True,
                )
                logger.debug(
                    f"Sent graceful shutdown signal to Studio (PID: {self.studio_pid})"
                )
            except Exception as e:
                logger.warning(f"Could not send graceful shutdown: {e}")

    async def _force_kill_studio_process(self) -> None:
        """Force kill Studio process"""
        if self.studio_pid is None:
            return

        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.studio_pid)],
                check=False,
                capture_output=True,
            )
            logger.debug(f"Sent force kill to Studio (PID: {self.studio_pid})")

    async def stop_studio(self, skip_graceful: bool = False) -> bool:
        """Stop Roblox Studio gracefully or forcefully"""
        if self.studio_pid is None:
            logger.info("No Studio process to stop")
            return True

        try:
            if skip_graceful:
                logger.info(
                    f"Force killing Studio process PID {self.studio_pid} (skipping graceful shutdown)..."
                )
                await self._force_kill_studio_process()

                # Wait for force kill
                try:
                    await asyncio.wait_for(self._wait_for_process(), timeout=5.0)
                    logger.info("Studio process force killed successfully")
                except TimeoutError:
                    logger.error("Failed to kill Studio process")
            else:
                logger.info(f"Stopping Roblox Studio (PID: {self.studio_pid})...")

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

            self.studio_pid = None
            logger.info("Roblox Studio stopped")
            return True

        except Exception as e:
            logger.error(f"Failed to stop Studio: {e}")
            return False

    async def _wait_for_process(self) -> None:
        """Wait for Studio process to terminate"""
        while self.studio_pid and self._is_pid_running(self.studio_pid):
            await asyncio.sleep(0.5)

    async def _monitor_process_health(self) -> None:
        """Monitor the health of the Studio process"""
        try:
            check_count = 0
            while self.studio_pid:
                await asyncio.sleep(1)
                # Re-check after sleep since stop_studio() may have cleared the PID
                if self.studio_pid is None:
                    break

                is_running = self._is_pid_running(self.studio_pid)
                check_count += 1

                if not is_running:
                    logger.info(
                        f"Studio process (PID: {self.studio_pid}) exited after {check_count} seconds"
                    )
                    break
        except Exception as e:
            logger.error(f"Error monitoring Studio process health: {e}")

    def is_running(self) -> bool:
        """Check if Studio process is currently running"""
        return self.studio_pid is not None and self._is_pid_running(self.studio_pid)

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
            "place_id": self.place_id,
            "universe_id": self.universe_id,
        }


@asynccontextmanager
async def managed_studio(
    place_id: int,
    universe_id: int,
    user_id: int,
    plugin_manager=None,
    fflag_manager=None,
):
    """
    Context manager that ensures Studio is properly started and stopped.

    Args:
        place_id: Roblox place ID to open
        universe_id: Roblox universe ID for the place
        user_id: Roblox user ID for authentication
        plugin_manager: Optional pre-installed PluginManager to reuse
        fflag_manager: Optional pre-configured FFlagManager to reuse

    If plugin_manager and fflag_manager are provided, they are reused
    (useful for caching across multiple Studio instances in a session).
    If not provided, new ones are created and cleaned up on exit.
    """
    studio_manager = StudioManager(place_id, universe_id, user_id)

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
