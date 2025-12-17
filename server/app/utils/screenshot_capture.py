"""
Screenshot capture utility for Roblox Studio window.

Uses mss for cross-platform screen capture, PIL for image processing,
and Windows API to find the Studio window.
"""

import base64
import ctypes
import io
import logging
from dataclasses import dataclass

import mss
import mss.tools
from PIL import Image

from app.config_manager import config as app_config

logger = logging.getLogger(__name__)

# Windows API constants
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001


@dataclass
class WindowRect:
    """Rectangle representing window bounds."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_monitor_dict(self) -> dict:
        """Convert to mss monitor format."""
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def find_studio_window() -> int | None:
    """
    Find the Roblox Studio window handle.

    Returns:
        Window handle (HWND) if found, None otherwise.
    """
    user32 = ctypes.windll.user32

    # Window titles to search for (Studio uses different titles in different states)
    studio_title_patterns = [
        "Roblox Studio",
    ]

    result_hwnd = None

    def enum_callback(hwnd, _):
        nonlocal result_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True

        # Get window title
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value

        # Check if title matches any of our patterns
        for pattern in studio_title_patterns:
            if pattern in title:
                result_hwnd = hwnd
                logger.debug(f"Found Studio window: '{title}' (hwnd={hwnd})")
                return False  # Stop enumeration

        return True  # Continue enumeration

    # Define callback type
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    callback = WNDENUMPROC(enum_callback)

    user32.EnumWindows(callback, 0)

    return result_hwnd


def get_window_rect(hwnd: int) -> WindowRect | None:
    """
    Get the bounding rectangle of a window.

    Args:
        hwnd: Window handle.

    Returns:
        WindowRect if successful, None otherwise.
    """
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)

    return None


def ensure_window_visible(hwnd: int) -> bool:
    """
    Ensure the window is visible and not minimized.

    Args:
        hwnd: Window handle.

    Returns:
        True if window is now visible, False otherwise.
    """
    user32 = ctypes.windll.user32

    # Check if window is minimized (iconic)
    if user32.IsIconic(hwnd):
        logger.info("Studio window is minimized, restoring...")
        user32.ShowWindow(hwnd, SW_RESTORE)

    # Bring window to front (optional, may not be desired in all cases)
    # user32.SetForegroundWindow(hwnd)

    return user32.IsWindowVisible(hwnd)


def crop_to_viewport(image: Image.Image) -> Image.Image:
    """
    Crop the image to the viewport region, removing Studio UI panels.

    Uses configured crop percentages to remove:
    - Left: Explorer panel, toolbox
    - Right: Properties panel
    - Top: Menu bar, ribbon
    - Bottom: Output window, command bar

    Args:
        image: Full window screenshot as PIL Image.

    Returns:
        Cropped image containing just the viewport.
    """
    width, height = image.size

    # Calculate crop boundaries based on percentages
    left = int(width * app_config.screenshot_crop_left)
    right = int(width * (1 - app_config.screenshot_crop_right))
    top = int(height * app_config.screenshot_crop_top)
    bottom = int(height * (1 - app_config.screenshot_crop_bottom))

    # Ensure valid crop bounds
    left = max(0, left)
    top = max(0, top)
    right = min(width, right)
    bottom = min(height, bottom)

    if right <= left or bottom <= top:
        logger.warning(f"Invalid crop bounds: ({left}, {top}, {right}, {bottom}), returning original")
        return image

    cropped = image.crop((left, top, right, bottom))
    logger.debug(f"Cropped image from {width}x{height} to {cropped.width}x{cropped.height}")

    return cropped


def resize_image(image: Image.Image, width: int | None, height: int | None) -> Image.Image:
    """
    Resize image to specified dimensions.

    Args:
        image: Input PIL Image.
        width: Target width (None to skip resizing).
        height: Target height (None to skip resizing).

    Returns:
        Resized image, or original if no dimensions specified.
    """
    if width is None and height is None:
        return image

    # Use original dimensions if one is not specified
    target_width = width if width is not None else image.width
    target_height = height if height is not None else image.height

    if target_width == image.width and target_height == image.height:
        return image

    # Use LANCZOS for high-quality downsampling
    resized = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
    logger.debug(f"Resized image from {image.width}x{image.height} to {target_width}x{target_height}")

    return resized


def process_screenshot(raw_screenshot: mss.base.ScreenShot) -> str:
    """
    Process a raw screenshot: crop to viewport and resize.

    Args:
        raw_screenshot: mss ScreenShot object.

    Returns:
        Base64-encoded image string (PNG or JPEG based on config).
    """
    # Convert mss screenshot to PIL Image
    # mss returns BGRA, PIL expects RGB
    img = Image.frombytes(
        "RGB", raw_screenshot.size, raw_screenshot.bgra, "raw", "BGRX"
    )

    # Crop to viewport
    img = crop_to_viewport(img)

    # Resize to configured dimensions
    img = resize_image(
        img, app_config.screenshot_width, app_config.screenshot_height
    )

    # Convert to bytes based on configured format
    buffer = io.BytesIO()
    img_format = app_config.screenshot_format.upper()

    if img_format == "JPEG":
        img.save(
            buffer,
            format="JPEG",
            quality=app_config.screenshot_jpeg_quality,
            optimize=True,
        )
    else:
        # Default to PNG
        img.save(buffer, format="PNG", optimize=True)

    img_bytes = buffer.getvalue()

    # Encode to base64
    b64_screenshot = base64.b64encode(img_bytes).decode("utf-8")

    return b64_screenshot


def capture_studio_screenshot() -> tuple[str | None, str | None]:
    """
    Capture a screenshot of the Roblox Studio viewport.

    The screenshot is cropped to remove Studio UI panels and resized
    to configured dimensions for consistent model input.

    Returns:
        Tuple of (base64_encoded_png, error_message).
        On success: (screenshot_data, None)
        On failure: (None, error_description)
    """
    # Find the Studio window
    hwnd = find_studio_window()
    if hwnd is None:
        return None, "Roblox Studio window not found"

    # Ensure window is visible
    if not ensure_window_visible(hwnd):
        return None, "Failed to make Studio window visible"

    # Get window bounds
    rect = get_window_rect(hwnd)
    if rect is None:
        return None, "Failed to get Studio window bounds"

    if rect.width <= 0 or rect.height <= 0:
        return None, f"Invalid window dimensions: {rect.width}x{rect.height}"

    logger.debug(f"Capturing Studio window at {rect.left},{rect.top} ({rect.width}x{rect.height})")

    try:
        with mss.mss() as sct:
            # Capture the specific region
            monitor = rect.to_monitor_dict()
            screenshot = sct.grab(monitor)

            # Process: crop to viewport and resize
            b64_screenshot = process_screenshot(screenshot)

            logger.info(
                f"Captured screenshot: {app_config.screenshot_width}x{app_config.screenshot_height}, "
                f"{len(b64_screenshot)} bytes (base64)"
            )
            return b64_screenshot, None

    except Exception as e:
        logger.error(f"Screenshot capture failed: {e}")
        return None, f"Screenshot capture failed: {e}"


def capture_full_screen() -> tuple[str | None, str | None]:
    """
    Capture a screenshot of the entire primary monitor.
    Fallback if window-specific capture fails.

    Returns:
        Tuple of (base64_encoded_png, error_message).
    """
    try:
        with mss.mss() as sct:
            # Capture primary monitor (index 1, as 0 is "all monitors")
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)

            # Process: crop and resize
            b64_screenshot = process_screenshot(screenshot)

            logger.info(f"Captured full screen: {len(b64_screenshot)} bytes (base64)")
            return b64_screenshot, None

    except Exception as e:
        logger.error(f"Full screen capture failed: {e}")
        return None, f"Full screen capture failed: {e}"
