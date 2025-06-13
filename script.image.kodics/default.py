import xbmc
import xbmcgui
import xbmcvfs
import xbmcaddon
import os
import sys
import zipfile
import tempfile
import shutil
import json
from io import BytesIO
import threading
from PIL import Image
from collections import OrderedDict

from xbmcgui import (
    ACTION_MOVE_UP, ACTION_MOVE_DOWN, ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT,
    ACTION_PAGE_UP, ACTION_PAGE_DOWN, ACTION_NEXT_ITEM, ACTION_PREV_ITEM,
    ACTION_NAV_BACK, ACTION_PREVIOUS_MENU
)

class VolumeManager:
    def __init__(self):
        self.original_volume = None
        self.changed = False

    def is_audio_playing(self):
        return xbmc.Player().isPlayingAudio()

    def get_volume(self):
        query = {
            "jsonrpc": "2.0",
            "method": "Application.GetProperties",
            "params": {"properties": ["volume"]},
            "id": 1
        }
        result = xbmc.executeJSONRPC(json.dumps(query))
        return json.loads(result).get("result", {}).get("volume")

    def set_volume(self, value):
        query = {
            "jsonrpc": "2.0",
            "method": "Application.SetVolume",
            "params": {"volume": value},
            "id": 1
        }
        xbmc.executeJSONRPC(json.dumps(query))

    def maybe_mute_volume(self):
        if not self.is_audio_playing():
            self.original_volume = self.get_volume()
            if self.original_volume is not None and self.original_volume > 0:
                self.set_volume(0)
                self.changed = True

    def maybe_restore_volume(self):
        if self.changed and self.original_volume is not None:
            self.set_volume(self.original_volume)
            self.changed = False

class FitWidthImageViewer(xbmcgui.Window):
    def __init__(self):
        super().__init__()
        self.screen_width = self.getWidth()
        self.screen_height = self.getHeight()
        self.image_control = xbmcgui.ControlImage(
            0, 0, self.screen_width, self.screen_height, "", aspectRatio=2
        )
        self.addControl(self.image_control)

        self.image_list = []
        self.current_image_index = 0
        self.offset_y = 0
        self.max_offset_y = 0
        self.running = True
        self.temp_dir = None
        self.scaled_cache = OrderedDict()
        self.cache_size = 5
        self.cache_lock = threading.Lock()
        self.temp_scaled_files = set()
        # For threading/image loading
        self.image_pending = False
        self.image_ready_path = None
        self.image_ready_height = None
        self.image_requested_index = None
        self.image_requested_offset_y = None
        self.lock = threading.Lock()
        self.last_height = self.screen_height
        # Overlay controls
        self.overlay_bg = None
        self.overlay_label = None
        self._show_overlay_next_update = False

    def display_image(self, image_path, offset_y=0):
        with self.lock:
            self.image_pending = True
            self.image_requested_index = self.current_image_index
            self.image_requested_offset_y = offset_y
        t = threading.Thread(target=self.load_and_scale_image, args=(image_path,))
        t.daemon = True
        t.start()
        self.offset_y = offset_y

    def load_and_scale_image(self, image_path):
        scaled_image_bytes, scaled_height, temp_scaled_path = self.get_or_scale_image(image_path)
        with self.lock:
            self.image_ready_path = temp_scaled_path
            self.image_ready_height = scaled_height
            self.image_pending = False

    def update_image_control(self):
        with self.lock:
            path = self.image_ready_path
            height = self.image_ready_height
            offset_y = self.image_requested_offset_y
            index = self.image_requested_index
            self.image_ready_path = None
            self.image_ready_height = None
        if path and height is not None:
            self.image_control.setImage(path)
            # Only update height if changed, to avoid unnecessary redraws
            if height != self.last_height:
                self.image_control.setHeight(height)
                self.last_height = height
            self.image_control.setPosition(0, -offset_y)
            self.max_offset_y = max(0, height - self.screen_height)
        # Show overlay if requested (set by page change)
        if self._show_overlay_next_update:
            self._show_overlay_next_update = False
            self.show_index_overlay()

    def get_or_scale_image(self, image_path):
        with self.cache_lock:
            cached = self.scaled_cache.get(image_path)
            if cached:
                self.scaled_cache.move_to_end(image_path)
                _, _, temp_scaled_path = cached
                return cached[0], cached[1], temp_scaled_path

        try:
            with Image.open(image_path) as img:
                image_width, image_height = img.size
                scale_factor = self.screen_width / image_width
                scaled_width = self.screen_width
                scaled_height = int(image_height * scale_factor)
                img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
                memfile = BytesIO()
                img.save(memfile, "JPEG")
                memfile.seek(0)
                result_bytes = memfile
                result_height = scaled_height
        except Exception as e:
            xbmcgui.Dialog().ok("Error", f"Unable to display image: {str(e)}")
            blank = BytesIO()
            Image.new("RGB", (self.screen_width, self.screen_height), (0, 0, 0)).save(blank, "JPEG")
            blank.seek(0)
            result_bytes = blank
            result_height = self.screen_height

        temp_scaled_path = os.path.join(tempfile.gettempdir(), f"kodics_scaled_{hash(image_path)}.jpg")
        try:
            with open(temp_scaled_path, "wb") as f:
                f.write(result_bytes.getbuffer())
            self.temp_scaled_files.add(temp_scaled_path)
        except Exception as e:
            xbmcgui.Dialog().ok("Error", f"Unable to write temp scaled image: {str(e)}")
        
        result = (result_bytes, result_height, temp_scaled_path)

        with self.cache_lock:
            self.scaled_cache[image_path] = result
            self.scaled_cache.move_to_end(image_path)
            if len(self.scaled_cache) > self.cache_size:
                self.scaled_cache.popitem(last=False)
        return result

    def preload_adjacent_images(self):
        indices = set()
        # Preload 2 pages forward
        if self.current_image_index + 1 < len(self.image_list):
            indices.add(self.current_image_index + 1)
        if self.current_image_index + 2 < len(self.image_list):
            indices.add(self.current_image_index + 2)
        # Preload 1 page backward
        if self.current_image_index - 1 >= 0:
            indices.add(self.current_image_index - 1)
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
        default_path = ""
        file_path = dialog.browse(1, "Select an Image or CBZ File", default_path, ".jpg|.png|.jpeg|.cbz")
        if not file_path:
            return None, None
        if file_path.lower().endswith(".cbz"):
            self.temp_dir = tempfile.mkdtemp()
            self.extract_cbz(file_path)
            folder_path = self.temp_dir
            self.image_list = []
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith((".jpg", ".png", ".jpeg")):
                        self.image_list.append(os.path.join(root, f))
            self.image_list.sort()
            self.current_image_index = 0
            return folder_path, self.image_list[0] if self.image_list else None

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
            with zipfile.ZipFile(cbz_path, 'r') as cbz:
                cbz.extractall(self.temp_dir)
        except Exception as e:
            xbmcgui.Dialog().ok(
                "Error",
                f"Failed to extract CBZ file.\nError: {str(e)}"
            )

    def show_index_overlay(self):
        # Remove existing overlay first if present
        for ctrl in [self.overlay_bg, self.overlay_label]:
            if ctrl:
                try:
                    self.removeControl(ctrl)
                except Exception:
                    pass
        self.overlay_bg = None
        self.overlay_label = None

        if not self.image_list:
            return

        overlay_text = f"{self.current_image_index + 1} / {len(self.image_list)}"
        # 5% of screen width and 3% of screen height for rectangle
        label_width = max(1, int(self.screen_width * 0.05))
        label_height = max(1, int(self.screen_height * 0.03))
        # Place in bottom right, 1% margin from right and bottom
        x = self.screen_width - label_width - int(self.screen_width * 0.01)
        y = self.screen_height - label_height - int(self.screen_height * 0.01)

        # Create a grey rectangle as background (RGBA: 192,192,192,220)
        bg_img = os.path.join(tempfile.gettempdir(), 'kodi_overlay_bg.png')
        # Always re-create temp file to match current size
        from PIL import Image as PILImage
        img = PILImage.new("RGBA", (label_width, label_height), (192, 192, 192, 220))
        img.save(bg_img)

        self.overlay_bg = xbmcgui.ControlImage(
            x, y, label_width, label_height, bg_img
        )
        self.addControl(self.overlay_bg)

        # Use a small font if available in the skin
        try:
            self.overlay_label = xbmcgui.ControlLabel(
                x, y, label_width, label_height, overlay_text,
                textColor='0xFF000000',  # Black text
                alignment=2 | 4 | 8,      # Center (horizontal and vertical), wrap
                font="font12"             # Try a small font, must exist in skin
            )
        except TypeError:
            # Fallback if font argument is not supported
            self.overlay_label = xbmcgui.ControlLabel(
                x, y, label_width, label_height, overlay_text,
                textColor='0xFF000000',
                alignment=2 | 4 | 8
            )
        self.addControl(self.overlay_label)

        def remove_overlay():
            xbmc.sleep(1000)
            for ctrl in [self.overlay_bg, self.overlay_label]:
                if ctrl:
                    try:
                        self.removeControl(ctrl)
                    except Exception:
                        pass
            self.overlay_bg = None
            self.overlay_label = None

        t = threading.Thread(target=remove_overlay)
        t.daemon = True
        t.start()

    def onAction(self, action):
        action_id = action.getId()
        page_changed = False
        if action_id in (ACTION_MOVE_RIGHT, ACTION_NEXT_ITEM):
            if self.current_image_index < len(self.image_list) - 1:
                self.current_image_index += 1
                self.offset_y = 0
                self.display_image(self.image_list[self.current_image_index], self.offset_y)
                page_changed = True
        elif action_id in (ACTION_MOVE_LEFT, ACTION_PREV_ITEM):
            if self.current_image_index > 0:
                self.current_image_index -= 1
                self.offset_y = 0
                self.display_image(self.image_list[self.current_image_index], self.offset_y)
                page_changed = True
        elif action_id in (ACTION_MOVE_UP, ACTION_PAGE_UP):
            old_offset = self.offset_y
            self.offset_y = max(0, self.offset_y - 100)
            if self.offset_y != old_offset:
                self.display_image(self.image_list[self.current_image_index], self.offset_y)
        elif action_id in (ACTION_MOVE_DOWN, ACTION_PAGE_DOWN):
            old_offset = self.offset_y
            self.offset_y = min(self.max_offset_y, self.offset_y + 100)
            if self.offset_y != old_offset:
                self.display_image(self.image_list[self.current_image_index], self.offset_y)
        elif action_id in (ACTION_NAV_BACK, ACTION_PREVIOUS_MENU):
            self.running = False

        if page_changed:
            # Set flag to show overlay after image is actually displayed
            self._show_overlay_next_update = True

    def cleanup_temp_scaled_files(self):
        for path in self.temp_scaled_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    def run(self):
        try:
            folder_path, image_path = self.select_folder_and_image()
            if not image_path:
                xbmcgui.Dialog().ok("Error", "No image or CBZ file selected.")
                return

            # Display first image
            self.display_image(image_path, self.offset_y)
            # Show overlay for first image
            self._show_overlay_next_update = True
            self.show()
            while self.running:
                with self.lock:
                    ready = self.image_ready_path is not None and not self.image_pending
                if ready:
                    self.update_image_control()
                    self.preload_adjacent_images()
                xbmc.sleep(50)
        finally:
            if self.temp_dir and os.path.exists(self.temp_dir):
                for f in os.listdir(self.temp_dir):
                    path = os.path.join(self.temp_dir, f)
                    if os.path.isfile(path) or os.path.islink(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                os.rmdir(self.temp_dir)
            self.cleanup_temp_scaled_files()
        self.close()

# --- Main Entrypoint ---

volume_mgr = VolumeManager()
volume_mgr.maybe_mute_volume()
try:
    viewer = FitWidthImageViewer()
    viewer.run()
finally:
    volume_mgr.maybe_restore_volume()
