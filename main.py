#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 GCS - Single File GUI
- MJPEG /stream (OpenCV -> manual) automatic retry
- /capture "Safe Mode" (2-3 FPS) button
- Resolution and quality adjustment (HTTP control)
- Snapshot and Video Recording
- Simple FPS/OSD and log screen
"""

import os, time, threading, queue
from datetime import datetime
from urllib.parse import urljoin

import cv2
import numpy as np
import requests
from PIL import Image, ImageTk
import tkinter as tk

# ---------- USER SETTINGS ----------
BASE_HOST  = "http://192.168.4.1"            # for control/capture
STREAM_URL = "http://192.168.4.1:81/stream"  # MJPEG stream (tries 80 if necessary)
OUTPUT_DIR = "gcs_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def now_ts(): return datetime.now().strftime("%Y%m%d_%H%M%S")

# ---------- HTTP helpers ----------
def http_get(path, timeout=(5, 10)):
    try:
        url = urljoin(BASE_HOST + "/", path.lstrip("/"))
        return requests.get(url, timeout=timeout)
    except Exception:
        return None

def set_quality(v:int):         return http_get(f"/control?var=quality&val={v}")
def set_framesize(code:int):    return http_get(f"/control?var=framesize&val={code}")
def capture_jpg(timeout=(5,10)):
    try:
        r = requests.get(urljoin(BASE_HOST + "/", "capture"), timeout=timeout)
        r.raise_for_status(); return r.content
    except Exception:
        return None

# ---------- Stream workers ----------
class StreamWorker(threading.Thread):
    """Tries OpenCV first, falls back to manual MJPEG if it fails; retries upon disconnection."""
    def __init__(self, url, frame_q, stop_evt, log_fn):
        super().__init__(daemon=True)
        self.url = url
        self.q = frame_q
        self.stop = stop_evt
        self.log = log_fn

    def run(self):
        candidates = [
            self.url,
            self.url.replace(":81","").replace("/stream","/stream"),
            "http://192.168.4.1:81/stream",
            "http://192.168.4.1/stream",
        ]
        tried = set()
        while not self.stop.is_set():
            # next candidate URL
            url = None
            for c in candidates:
                if c not in tried:
                    url = c; break
            if url is None:
                tried = set(); url = candidates[0]
            tried.add(url)

            if self.stop.is_set(): break
            ok = self.try_opencv(url)
            if ok: continue
            if self.stop.is_set(): break
            self.try_manual_mjpeg(url)

    def try_opencv(self, url):
        self.log(f"[INFO] Connecting with OpenCV: {url}")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        start = time.time(); got_first = False
        while not self.stop.is_set():
            ok, frame = cap.read()
            if ok and frame is not None:
                if not got_first:
                    got_first = True
                    self.log("[OK] OpenCV stream started.")
                try: self.q.put(frame, timeout=0.1)
                except queue.Full: pass
                continue
            if not got_first and time.time() - start > 5:
                self.log("[WARN] OpenCV could not get the first frame, switching to manual MJPEG.")
                cap.release()
                return False
            time.sleep(0.02)
        cap.release()
        return True

    def try_manual_mjpeg(self, url):
        while not self.stop.is_set():
            try:
                self.log(f"[INFO] Manual MJPEG: {url}")
                # timeout=(connect, read) -> giving long read time
                r = requests.get(url, stream=True, timeout=(5, 60))
                r.raise_for_status()
                self.log("[OK] Manual MJPEG stream started.")
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=2048):
                    if self.stop.is_set(): break
                    if not chunk: continue
                    buf += chunk
                    a = buf.find(b'\xff\xd8'); b = buf.find(b'\xff\xd9')
                    if a != -1 and b != -1 and b > a:
                        jpg = buf[a:b+2]; buf = buf[b+2:]
                        img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), 1)
                        if img is not None:
                            try: self.q.put(img, timeout=0.1)
                            except queue.Full: pass
                # if loop ends, connection is lost, retry
            except Exception as e:
                self.log(f"[ERR] MJPEG error: {e}; retrying in 3 sec…")
                time.sleep(3)
            finally:
                if self.stop.is_set(): break

class CaptureWorker(threading.Thread):
    """Safe Mode: 2–3 FPS image with /capture."""
    def __init__(self, frame_q, stop_evt, log_fn, interval=0.4):
        super().__init__(daemon=True)
        self.q = frame_q
        self.stop = stop_evt
        self.log = log_fn
        self.interval = interval

    def run(self):
        self.log("[INFO] Safe Mode on (/capture).")
        while not self.stop.is_set():
            try:
                data = capture_jpg(timeout=(5,10))
                if data:
                    img = cv2.imdecode(np.frombuffer(data, np.uint8), 1)
                    if img is not None:
                        try: self.q.put(img, timeout=0.1)
                        except queue.Full: pass
            except Exception as e:
                self.log(f"[ERR] Safe Mode capture: {e}")
            time.sleep(self.interval)

# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Deneyap GCS")
        self.geometry("1000x740")
        self.configure(bg="#202020")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Top bar
        top = tk.Frame(self, bg="#303030"); top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="Stream URL:", fg="white", bg="#303030").pack(side="left")
        self.url_var = tk.StringVar(value=STREAM_URL)
        tk.Entry(top, textvariable=self.url_var, width=52).pack(side="left", padx=6)

        tk.Button(top, text="Start", command=self.start_stream).pack(side="left", padx=3)
        tk.Button(top, text="Stop", command=self.stop_stream).pack(side="left", padx=3)
        tk.Button(top, text="Safe Mode", command=self.start_safe).pack(side="left", padx=3)
        tk.Button(top, text="Snapshot", command=self.snapshot).pack(side="left", padx=3)
        tk.Button(top, text="Record ON/OFF", command=self.toggle_record).pack(side="left", padx=3)

        # Image area
        self.video = tk.Label(self, bg="black"); self.video.pack(fill="both", expand=True, padx=8, pady=8)

        # Controls
        ctl = tk.Frame(self, bg="#303030"); ctl.pack(fill="x", padx=8, pady=4)
        tk.Label(ctl, text="Resolution:", fg="white", bg="#303030").pack(side="left")
        # Most stable profiles
        self.size_buttons = [
            ("QVGA",3),("VGA",5),("SVGA",10),("XGA",11)
        ]
        for name, code in self.size_buttons:
            tk.Button(ctl, text=name, command=lambda c=code: self.apply_framesize(c)).pack(side="left", padx=3)

        tk.Label(ctl, text="  Quality:", fg="white", bg="#303030").pack(side="left", padx=(12,3))
        self.q_label = tk.Label(ctl, text="35", fg="white", bg="#303030"); self.q_label.pack(side="right")
        self.q_scale = tk.Scale(ctl, from_=10, to=55, orient="horizontal",
                                showvalue=False, command=self.on_quality_change,
                                length=280, bg="#303030", fg="white",
                                troughcolor="#505050", highlightthickness=0)
        self.q_scale.set(35); self.q_scale.pack(side="left", padx=6)

        # Log area
        self.log_box = tk.Text(self, height=7, bg="#111", fg="#ddd"); self.log_box.pack(fill="x", padx=8, pady=(0,8))
        self.log("[INFO] Ready. Click 'Start' first.")

        # State
        self.frame_q = queue.Queue(maxsize=5)
        self.stop_evt = threading.Event()
        self.worker = None
        self.fps_cnt, self.fps, self.last_t = 0, 0.0, time.time()
        self.recording, self.rec, self.rec_path = False, None, None

        # UI loop
        self.after(15, self.update_frame)

    # ---- Log helper ----
    def log(self, msg):
        try:
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
        except Exception:
            pass
        print(msg)

    # ---- Buttons ----
    def start_stream(self):
        self.stop_stream()
        url = self.url_var.get().strip()
        self.stop_evt.clear()
        self.worker = StreamWorker(url, self.frame_q, self.stop_evt, self.log)
        self.worker.start()
        self.log(f"[INFO] Connecting: {url}")

    def start_safe(self):
        self.stop_stream()
        self.stop_evt.clear()
        self.worker = CaptureWorker(self.frame_q, self.stop_evt, self.log, interval=0.4)
        self.worker.start()

    def stop_stream(self):
        if self.worker:
            self.stop_evt.set()
            self.worker.join(timeout=1.0)
            self.worker = None
        self.log("[INFO] Stopped.")

    def snapshot(self):
        # Try /capture first
        data = capture_jpg()
        if data:
            p = os.path.join(OUTPUT_DIR, f"snap_{now_ts()}.jpg")
            with open(p, "wb") as f: f.write(data)
            self.log(f"[OK] Snapshot saved: {p}")
            return
        # Otherwise take from last frame
        try:
            frame = self.frame_q.get_nowait()
            p = os.path.join(OUTPUT_DIR, f"snap_{now_ts()}.png")
            cv2.imwrite(p, frame)
            self.log(f"[OK] Snapshot (frame) saved: {p}")
        except queue.Empty:
            self.log("[WARN] No frame for snapshot.")

    def toggle_record(self):
        if not self.recording:
            self.recording = True
            self.rec_path = os.path.join(OUTPUT_DIR, f"rec_{now_ts()}.mp4")
            self.rec = None  # we will open the size at the first frame
            self.log(f"[REC] Recording starting: {self.rec_path}")
        else:
            self.recording = False
            if self.rec:
                self.rec.release(); self.rec = None
                self.log(f"[REC] Recording stopped: {self.rec_path}")

    def apply_framesize(self, code):
        r = set_framesize(code)
        self.log("[INFO] Resolution command sent." if (r and r.ok) else "[ERR] Could not set resolution.")
        # good idea to refresh stream on sensor resolution change
        self.start_stream()

    def on_quality_change(self, _):
        v = int(self.q_scale.get())
        self.q_label.config(text=str(v))
        r = set_quality(v)
        self.log(f"[INFO] quality={v} sent." if (r and r.ok) else "[ERR] quality could not be sent.")

    # ---- Frame drawing ----
    def update_frame(self):
        try:
            frame = self.frame_q.get_nowait()  # get only 1 frame
            # FPS/OSD
            self.fps_cnt += 1
            t = time.time()
            if t - self.last_t >= 1.0:
                self.fps = self.fps_cnt / (t - self.last_t)
                self.fps_cnt = 0;
                self.last_t = t
            cv2.putText(frame, f"FPS: {self.fps:4.1f}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, f"FPS: {self.fps:4.1f}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)

            # Recording
            if self.recording:
                h, w = frame.shape[:2]
                if self.rec is None:
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    self.rec = cv2.VideoWriter(self.rec_path, fourcc, 20.0, (w, h))
                self.rec.write(frame)

            # Draw to TK
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb)
            W = self.video.winfo_width() or 960
            H = self.video.winfo_height() or 540
            im.thumbnail((W, H))
            imgtk = ImageTk.PhotoImage(im)
            self.video.imgtk = imgtk
            self.video.configure(image=imgtk)
            # a little nudge for macOS/Tk
            self.update_idletasks()

        except queue.Empty:
            pass
        except Exception as e:
            self.log(f"[ERR] draw_frame: {e}")

        # around 30–40 fps
        self.after(25, self.update_frame)

    def on_close(self):
        self.stop_stream()
        if self.rec:
            self.rec.release()
        self.destroy()

if __name__ == "__main__":
    App().mainloop()