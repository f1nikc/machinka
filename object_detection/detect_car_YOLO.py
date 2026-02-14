# object_detection/detect_car_YOLO.py
import os
import time
import threading
import traceback
import urllib.error
import importlib
import gc

import torch
import numpy as np

try:
    import certifi
except Exception:
    certifi = None


class DummyResults:
    def __init__(self):
        self.xyxyn = [torch.empty((0, 5), dtype=torch.float32)]


class DummyModel:
    def __init__(self):
        self.conf = 0.5
        self.iou = 0.45
        self.names = {}
        self._device = "cpu"

    def to(self, device):
        self._device = device
        return self

    def __call__(self, frames):
        return DummyResults()

    @property
    def model(self):
        return self


class ObjectDetection:
    def __init__(self, model_path, conf=0.5, iou=0.4, device="cpu"):
        self.__model_path = model_path
        self.device = device
        self.conf = conf
        self.iou = iou

        self.model = DummyModel()
        self._bg_loader_thread = None
        self._stop_bg = threading.Event()
        self._load_lock = threading.Lock()

        # try immediate load (safe)
        try:
            m = self._try_load_once()
            if m is not None:
                self.model = m
        except Exception:
            # fallback to DummyModel; background loader can try later
            traceback.print_exc()

    def _print(self, *args, **kwargs):
        print("[ObjectDetection]", *args, **kwargs)

    def _try_load_once(self):
        """Try loading model using several strategies. Return model or None."""
        tried = []
        # 0) SSL fix
        if certifi is not None:
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
            self._print("Using certifi CA bundle for SSL verification.")

        # 1) local torch cache
        home = os.path.expanduser("~")
        hub_cache_dir = os.path.join(home, ".cache", "torch", "hub", "ultralytics_yolov5_master")
        if os.path.isdir(hub_cache_dir):
            self._print("Found local ultralytics cache:", hub_cache_dir)
            try:
                model = torch.hub.load(hub_cache_dir, "custom", path=self.__model_path, force_reload=False)
                self._print("Loaded YOLO model from local cache.")
                return model
            except Exception as e:
                tried.append(("local_cache", e))
                self._print("Local cache load failed:", repr(e))

        # 2) ultralytics package (prefer if installed)
        try:
            ultralytics = importlib.import_module("ultralytics")
            try:
                from ultralytics import YOLO
                self._print("Using installed 'ultralytics' package. Loading weights...")
                y = YOLO(self.__model_path)
                self._print("Loaded model via ultralytics.YOLO")
                return y
            except Exception as e:
                tried.append(("ultralytics_pkg", e))
                self._print("ultralytics.YOLO load failed:", repr(e))
        except ModuleNotFoundError:
            self._print("'ultralytics' package not installed, skipping.")
        except Exception as e:
            tried.append(("ultralytics_import", e))
            self._print("Error importing ultralytics:", repr(e))

        # 3) torch.hub (last resort; needs internet or local hub)
        try:
            self._print("Attempting torch.hub.load from 'ultralytics/yolov5' (may require internet)...")
            model = torch.hub.load("ultralytics/yolov5", "custom", path=self.__model_path)
            self._print("Loaded YOLO model via torch.hub.")
            return model
        except urllib.error.URLError as e:
            tried.append(("torch_hub_urlerror", e))
            self._print("torch.hub URLError (likely SSL/network issue):", repr(e))
        except Exception as e:
            tried.append(("torch_hub_other", e))
            self._print("torch.hub.load failed:", repr(e))

        # failed all
        self._print("!!! All model-load attempts failed. Falling back to DummyModel.")
        for name, exc in tried:
            self._print(" -", name, ":", repr(exc))
        return None

    def reload_model(self):
        """Try to load a fresh model and atomically swap it in. Raises on failure."""
        with self._load_lock:
            new_model = self._try_load_once()
            if new_model is None:
                raise RuntimeError("reload_model: no available model")
            old = getattr(self, "model", None)
            self.model = new_model
            # try to set conf/iou if supported
            try:
                self.model.conf = self.conf
                self.model.iou = self.iou
            except Exception:
                pass
            # cleanup old
            try:
                del old
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            self._print("Model reloaded successfully.")
            return True

    def start_background_loader(self, initial_delay=1.0, max_backoff=300.0):
        """Start a background thread that will attempt to load model with exponential backoff."""
        if self._bg_loader_thread and self._bg_loader_thread.is_alive():
            return
        self._stop_bg.clear()
        def loader():
            backoff = initial_delay
            while not self._stop_bg.is_set():
                try:
                    self._print(f"Background loader attempt (backoff {backoff}s)...")
                    if self.reload_model():
                        break
                except Exception as e:
                    self._print("Background loader failed:", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
        self._bg_loader_thread = threading.Thread(target=loader, daemon=True)
        self._bg_loader_thread.start()

    def stop_background_loader(self):
        self._stop_bg.set()
        if self._bg_loader_thread:
            self._bg_loader_thread.join(timeout=1.0)

    def score_frame(self, frame):
        """Runs inference and returns (labels, cords) same shape as expected by main pipeline."""
        try:
            if hasattr(self.model, "to"):
                try:
                    self.model.to(self.device)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            results = self.model([frame])
        except Exception:
            traceback.print_exc()
            return np.array([], dtype=np.float32), np.array([], dtype=np.float32).reshape((0, 4))

        try:
            r = results.xyxyn[0]
            if isinstance(r, np.ndarray):
                arr = r
            else:
                try:
                    arr = r.to("cpu").numpy()
                except Exception:
                    arr = np.array(r)
            if arr.size == 0:
                return np.array([], dtype=np.float32), np.array([], dtype=np.float32).reshape((0, 4))
            labels = arr[:, -1].astype(np.float32)
            cords = arr[:, :-1].astype(np.float32)
            return labels, cords
        except Exception:
            traceback.print_exc()
            return np.array([], dtype=np.float32), np.array([], dtype=np.float32).reshape((0, 4))

