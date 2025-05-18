import xbmc
import xbmcgui
import xbmcvfs
import xbmcaddon
import os
import sys
import zipfile
import tempfile
import shutil
import threading
from io import BytesIO

# Add the 'lib' directory to the Python path for bundled dependencies
addon_dir = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('path'))
sys.path.append(os.path.join(addon_dir, 'lib'))

# Import Pillow
from PIL import Image

# Import action constants for navigation/scrolling
from xbmcgui import (
    ACTION_MOVE_UP, ACTION_MOVE_DOWN, ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT,
    ACTION_PAGE_UP, ACTION_PAGE_DOWN, ACTION_NEXT_ITEM, ACTION_PREV_ITEM,
    ACTION_NAV_BACK, ACTION_PREVIOUS_MENU
)

class FitWidthImageViewer(xbmcgui.Window):
    def __init__(self):
        super().__init__()
        self.image_control = None
        self.image_list = []
        self.current_image_index = 0
        self.offset_y = 0  # Used for panning
        self.max_offset_y = 0  # Maximum allowable vertical offset
        self.running = True
        self.temp_dir = None  # Temporary directory for extracted images
        self.last_image_index = None   # track last image for caching

        # RAM cache: image_path -> (scaled_image_bytes, scaled_height)
        self.scaled_cache = {}
        self.cache_size = 5  # Current, next/prev, and a couple more
        self.cache_lock = threading.Lock()

        # Screen dimensions
        self.screen_width = self.getWidth()
        self.screen_height = self.getHeight()

        # Silent audio playback
        self.silence_player = None
        self.silence_active = False

    def add_default_source(self):
        """
        Add a default source directory for comics if it doesn't already exist.
        """
        source_name = "Comics"
        source_path = "/storage/comics"  # Default directory for CBZ files
        source_type = "pictures"  # Source type must match the add-on's purpose

        # Check if the source already exists
        sources_path = xbmcvfs.translatePath("special://profile/sources.xml")
        if not os.path.exists(sources_path):
            xbmc.log(f"Sources file does not exist: {sources_path}", xbmc.LOGWARNING)
            return

        with open(sources_path, "r") as file:
            sources_content = file.read()
            if source_name in sources_content:
                xbmc.log(f"Source '{source_name}' already exists.", xbmc.LOGINFO)
                return

        # Add the source
        source_entry = f"""
        <source>
            <name>{source_name}</name>
            <path pathversion="1">{source_path}</path>
            <allowsharing>true</allowsharing>
        </source>
        """
        with open(sources_path, "a") as file:
            file.write(source_entry)
        xbmc.log(f"Source '{source_name}' added successfully.", xbmc.LOGINFO)

    def display_image(self, image_path):
        """
        Display an image on the screen, scaled to fit the width of the screen.
        Uses RAM cache for speed. Preloads adjacent images.
        """
        # Only reload/rescale if the image has changed
        if self.current_image_index != self.last_image_index:
            scaled_image_bytes, scaled_height = self.get_or_scale_image(image_path)
            self.scaled_height = scaled_height

            # Remove previous control and add new one
            if self.image_control:
                self.removeControl(self.image_control)

            # Write the scaled image from memory to a temp file for Kodi to load
            temp_scaled_path = os.path.join(tempfile.gettempdir(), f"kodics_scaled_{hash(image_path)}.jpg")
            with open(temp_scaled_path, "wb") as f:
                f.write(scaled_image_bytes.getbuffer())

            self.image_control = xbmcgui.ControlImage(
                0, -self.offset_y, self.screen_width, self.scaled_height, temp_scaled_path, aspectRatio=2
            )
            self.addControl(self.image_control)
            self.max_offset_y = max(0, self.scaled_height - self.screen_height)
            self.last_image_index = self.current_image_index
        else:
            # Only move the control for scrolling
            if self.image_control:
                self.image_control.setPosition(0, -self.offset_y)

        # Preload next/prev images in background
        self.preload_adjacent_images()

    def get_or_scale_image(self, image_path):
        # Use RAM cache, thread-safe
        with self.cache_lock:
            cached = self.scaled_cache.get(image_path)
            if cached:
                return cached

        try:
            # Load and scale the image in-memory
            with Image.open(image_path) as img:
                image_width, image_height = img.size
                scale_factor = self.screen_width / image_width
                scaled_width = self.screen_width
                scaled_height = int(image_height * scale_factor)
                img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
                memfile = BytesIO()
                img.save(memfile, "JPEG")
                memfile.seek(0)
                result = (memfile, scaled_height)
        except Exception as e:
            xbmcgui.Dialog().ok("Error", f"Unable to display image: {str(e)}")
            # fallback: blank image
            blank = BytesIO()
            Image.new("RGB", (self.screen_width, self.screen_height), (0, 0, 0)).save(blank, "JPEG")
            blank.seek(0)
            result = (blank, self.screen_height)

        # Store in cache, evict LRU if needed
        with self.cache_lock:
            self.scaled_cache[image_path] = result
            if len(self.scaled_cache) > self.cache_size:
                # Remove the oldest (first) inserted (LRU)
                oldest = next(iter(self.scaled_cache))
                del self.scaled_cache[oldest]
        return result

    def preload_adjacent_images(self):
        # Preload next and previous images in the background for instant navigation
        indices = []
        if self.current_image_index + 1 < len(self.image_list):
            indices.append(self.current_image_index + 1)
        if self.current_image_index - 1 >= 0:
            indices.append(self.current_image_index - 1)
        for idx in indices:
            path = self.image_list[idx]
            with self.cache_lock:
                if path in self.scaled_cache:
                    continue
            t = threading.Thread(target=self.get_or_scale_image, args=(path,))
            t.daemon = True
            t.start()

    def select_folder_and_image(self):
        dialog = xbmcgui.Dialog()
        file_path = dialog.browse(1, "Select an Image or CBZ File", "files", ".jpg|.png|.jpeg|.cbz")
        if not file_path:
            return None, None

        # Handle CBZ files
        if file_path.lower().endswith(".cbz"):
            self.temp_dir = tempfile.mkdtemp()
            self.extract_cbz(file_path)
            folder_path = self.temp_dir
            self.image_list = []
            # Recursively find all images in temp_dir and subfolders
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith((".jpg", ".png", ".jpeg")):
                        self.image_list.append(os.path.join(root, f))
            self.image_list.sort()
            self.current_image_index = 0
            return folder_path, self.image_list[0] if self.image_list else None

        # Handle image files
        folder_path = os.path.dirname(file_path)
        self.image_list = [
            os.path.join(folder_path, f)
            for f in os.listdir(folder_path)
            if f.lower().endswith((".jpg", ".png", ".jpeg"))
        ]
        self.image_list.sort()
        self.current_image_index = self.image_list.index(file_path)
        return folder_path, file_path

    def extract_cbz(self, cbz_path):
        try:
            # Extract the CBZ file to the temporary directory
            with zipfile.ZipFile(cbz_path, 'r') as cbz:
                cbz.extractall(self.temp_dir)
        except Exception as e:
            xbmcgui.Dialog().ok(
                "Error",
                f"Failed to extract CBZ file.\nError: {str(e)}"
            )

    # --- Silent audio playback to suppress GUI sounds ---

    def is_media_playing(self):
        player = xbmc.Player()
        return player.isPlaying() or player.isPlayingAudio() or player.isPlayingVideo()

    def start_silence(self):
        """
        Play a silent audio file to suppress GUI navigation sounds (if nothing else is playing).
        """
        if not self.is_media_playing():
            # Make sure the silent file exists in your add-on resources folder
            # You should bundle a short silent mp3 or wav file, e.g., silence.mp3, in resources/media/
            silence_path = xbmcvfs.translatePath("special://home/addons/" + xbmcaddon.Addon().getAddonInfo('id') + "/resources/media/silence.mp3")
            if xbmcvfs.exists(silence_path):
                self.silence_player = xbmc.Player()
                self.silence_player.play(silence_path)
                self.silence_active = True
            else:
                xbmc.log(f"Silent file not found: {silence_path}", xbmc.LOGWARNING)
                self.silence_active = False
        else:
            self.silence_active = False

    def stop_silence(self):
        """
        Stop playing the silent audio file, if we started it.
        """
        if self.silence_active and self.silence_player and self.silence_player.isPlaying():
            self.silence_player.stop()
            self.silence_active = False

    # ----------------------------------------------------

    def onAction(self, action):
        action_id = action.getId()
        # Scroll/pan the image vertically
        if action_id in (ACTION_MOVE_UP, ACTION_PAGE_UP):
            old_offset = self.offset_y
            self.offset_y = max(0, self.offset_y - 100)
            if self.offset_y != old_offset:
                self.display_image(self.image_list[self.current_image_index])
        elif action_id in (ACTION_MOVE_DOWN, ACTION_PAGE_DOWN):
            old_offset = self.offset_y
            self.offset_y = min(self.max_offset_y, self.offset_y + 100)
            if self.offset_y != old_offset:
                self.display_image(self.image_list[self.current_image_index])
        # Next image
        elif action_id in (ACTION_MOVE_RIGHT, ACTION_NEXT_ITEM):
            if self.current_image_index < len(self.image_list) - 1:
                self.current_image_index += 1
                self.offset_y = 0
                self.display_image(self.image_list[self.current_image_index])
        # Previous image
        elif action_id in (ACTION_MOVE_LEFT, ACTION_PREV_ITEM):
            if self.current_image_index > 0:
                self.current_image_index -= 1
                self.offset_y = 0
                self.display_image(self.image_list[self.current_image_index])
        # Close window
        elif action_id in (ACTION_NAV_BACK, ACTION_PREVIOUS_MENU):
            self.running = False

    def run(self):
        """
        Main entry point for the add-on.
        """
        self.add_default_source()

        # Start silent audio if needed to suppress GUI sounds
        self.start_silence()

        try:
            folder_path, image_path = self.select_folder_and_image()
            if not image_path:
                xbmcgui.Dialog().ok("Error", "No image or CBZ file selected.")
                return

            self.display_image(image_path)
            self.show()

            while self.running:
                xbmc.sleep(100)
        finally:
            # Stop silent audio if it was started
            self.stop_silence()
            if self.temp_dir and os.path.exists(self.temp_dir):
                for f in os.listdir(self.temp_dir):
                    path = os.path.join(self.temp_dir, f)
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                os.rmdir(self.temp_dir)
        self.close()

# Run the viewer
viewer = FitWidthImageViewer()
viewer.run()
