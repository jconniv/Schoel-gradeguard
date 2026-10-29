"""Native Tk opening animation. No network, media player or extra dependencies."""
import math
import time
import tkinter as tk


class DozerScene:
    WIDTH = 960
    HEIGHT = 560
    GROUND = 414

    def __init__(self, canvas, version, resource_path, scale=1.0):
        self.canvas = canvas
        self.scale = scale
        self.logo = None
        self._background(version, resource_path)

    def coords(self, points):
        return [value * self.scale for value in points]

    def line(self, points, color, width=1, **kwargs):
        return self.canvas.create_line(*self.coords(points), fill=color, width=width * self.scale, **kwargs)

    def polygon(self, points, fill, outline="", width=1, **kwargs):
        return self.canvas.create_polygon(*self.coords(points), fill=fill, outline=outline, width=width * self.scale, **kwargs)

    def rect(self, box, fill, outline="", **kwargs):
        return self.canvas.create_rectangle(*self.coords(box), fill=fill, outline=outline, **kwargs)

    def text(self, x, y, text, size, color, bold=False, **kwargs):
        return self.canvas.create_text(x * self.scale, y * self.scale, text=text, fill=color,
                                       font=("Segoe UI", max(7, int(size * self.scale)), "bold" if bold else "normal"), **kwargs)

    def _background(self, version, resource_path):
        self.rect((0, 0, 960, 560), "#111e2a")
        self.rect((0, 470, 960, 560), "#0c1721")
        self.rect((0, 0, 6, 560), "#8ca85d")
        for x in range(16, 961, 32):
            self.line((x, 242, x, 465), "#1a2b37")
        for y in range(242, 466, 32):
            self.line((6, y, 960, y), "#1a2b37")
        for index in range(6):
            dy = index * 15
            self.line((510, 254 + dy, 580, 224 + dy, 660, 230 + dy,
                       735, 193 + dy, 810, 200 + dy, 890, 170 + dy, 980, 180 + dy),
                      "#29403e", smooth=True, splinesteps=24)
        self.text(52, 51, "S C H O E L   E N G I N E E R I N G", 10, "#acbf8c", anchor="nw", bold=True)
        self.text(48, 91, "GradeGuard", 49, "#f3f5ed", anchor="nw", bold=True)
        self.line((54, 176, 124, 176), "#d9af52", 3)
        self.text(52, 197, "SURVEY  /  GRADING  /  OFFICE QA", 11, "#a7b8c4", anchor="nw")
        self.text(908, 198, f"v{version}", 10, "#8ea0b0", anchor="ne")
        # Preserve the approved logo on a light tile so its black lettering stays readable.
        self.rect((782, 42, 911, 153), "#edf1e9")
        try:
            original = tk.PhotoImage(file=resource_path("gradeguard_logo.png"))
            factor = max(1, math.ceil(max(original.width() / (116 * self.scale), original.height() / (99 * self.scale))))
            self.logo = original.subsample(factor, factor)
            self.canvas.create_image(846 * self.scale, 97 * self.scale, image=self.logo)
        except (tk.TclError, OSError):
            self.text(846, 84, "SCHOEL", 14, "#233142", bold=True)
            self.text(846, 108, "GradeGuard", 10, "#4b6832")
        self.line((52, 481, 908, 481), "#2b3d49")
        self.text(52, 504, "SETTING THE GRADE.", 11, "#c2cdb6", anchor="nw", bold=True)
        self.text(52, 530, "Enter or Esc to skip", 9, "#8295a5", anchor="nw")

    @staticmethod
    def position(progress):
        return -290.0 + 1260.0 * max(0.0, min(1.0, progress))

    @staticmethod
    def _tread_point(distance):
        """Point on the rounded track perimeter, for visible circulating treads."""
        straight, radius = 120.0, 25.0
        arc = math.pi * radius
        distance %= 2 * straight + 2 * arc
        if distance < straight:
            return 20 + distance, 83
        distance -= straight
        if distance < arc:
            angle = -math.pi / 2 + distance / radius
            return 140 + radius * math.cos(angle), 108 + radius * math.sin(angle)
        distance -= arc
        if distance < straight:
            return 140 - distance, 133
        angle = math.pi / 2 + (distance - straight) / radius
        return 20 + radius * math.cos(angle), 108 + radius * math.sin(angle)

    def draw_frame(self, progress, seconds):
        c = self.canvas
        c.delete("motion")
        x = self.position(progress)
        blade = x + 258
        ground = self.GROUND
        cut = max(0.0, min(960.0, blade))
        top = [0, ground, cut, ground]
        for px in range(int(cut) + 1, 977, 16):
            rise = min(1.0, max(0.0, (px - cut) / 42.0))
            height = (12 + 7 * math.sin(px / 47) + 5 * math.sin(px / 19)) * rise
            top.extend((px, ground - height))
        self.polygon(top + [960, 469, 0, 469], "#263a3c", tags="motion")
        self.polygon((0, 442, 960, 442, 960, 469, 0, 469), "#203033", tags="motion")
        self.line((0, ground, cut, ground), "#a8c87d", 2, tags="motion")
        # Small station ticks are visual scene detail, not survey data.
        for px in range(52, int(cut), 96):
            self.line((px, ground + 4, px, ground + 11), "#728d65", tags="motion")
        # Dust is generated from a fixed pattern; no images or randomness are needed.
        for i in range(16):
            age = (seconds * 1.6 + i * 0.173) % 1.0
            dx = x + 8 - age * (65 + (i % 4) * 10)
            dy = ground - 8 - age * (14 + (i % 5) * 6)
            radius = 2 + age * 6
            color = ("#716b52", "#555c4d", "#3a4a43", "#2a3e3d")[min(3, int(age * 4))]
            c.create_oval(*self.coords((dx - radius, dy - radius, dx + radius, dy + radius)), fill=color, outline="", tags="motion")
        self._dozer(x, ground - 133 + 0.55 * math.sin(seconds * 18), seconds)
        # The blade pushes a low pile ahead of the cutting edge.
        pile = (blade - 8, ground, blade + 5, ground - 14, blade + 15, ground - 22,
                blade + 24, ground - 15, blade + 33, ground - 10, blade + 43, ground)
        self.polygon(pile, "#a0824e", "#c6a363", tags="motion")
        for i in range(6):
            px = blade + 9 + i * 5
            py = ground - 4 - (i % 3) * 4
            self.line((px, py, px + 3, py - 2), "#d2b478", 2, tags="motion")

    def _dozer(self, x, y, seconds):
        c, s = self.canvas, self.scale
        def points(values):
            return [(value + (x if i % 2 == 0 else y)) * s for i, value in enumerate(values)]
        def poly(values, fill, outline="#19232b", width=2):
            return c.create_polygon(*points(values), fill=fill, outline=outline, width=width * s, tags=("motion", "dozer"))
        def line(values, fill, width=2, **kwargs):
            return c.create_line(*points(values), fill=fill, width=width * s, tags=("motion", "dozer"), **kwargs)
        def oval(box, fill, outline="", width=1):
            return c.create_oval(*points(box), fill=fill, outline=outline, width=width * s, tags=("motion", "dozer"))
        # Undercarriage and revolving track pads.
        oval((-13, 122, 224, 141), "#14232b")
        poly((18, 80, 141, 80, 158, 87, 168, 101, 168, 116, 156, 130, 141, 136,
              18, 136, 2, 130, -8, 117, -8, 100, 2, 87), "#25303a", "#798890", 3)
        for i in range(28):
            distance = i * (240 + 50 * math.pi) / 28 + seconds * 130
            a, b = self._tread_point(distance - 4), self._tread_point(distance + 4)
            line((*a, *b), "#829196", 5)
        for cx in (20, 51, 82, 113, 143):
            oval((cx - 16, 92, cx + 16, 124), "#53616a", "#18222a", 2)
            oval((cx - 11, 97, cx + 11, 119), "#303f48", "#8d9b9e", 1)
            for spoke in range(5):
                angle = seconds * 6 + spoke * math.tau / 5
                line((cx, 108, cx + 10 * math.cos(angle), 108 + 10 * math.sin(angle)), "#a1aca9", 1)
            oval((cx - 4, 104, cx + 4, 112), "#d0aa50")
        # Chassis, rear counterweight, hood and radiator.
        poly((0, 73, 119, 70, 135, 57, 187, 61, 204, 78, 201, 96, 7, 96), "#d99d28")
        poly((8, 68, 119, 68, 124, 84, 10, 84), "#f1bd45")
        poly((118, 62, 181, 64, 193, 72, 121, 72), "#f5ca60")
        poly((181, 73, 200, 77, 198, 91, 181, 90), "#26343d", width=1)
        for grille in range(4):
            line((184 + grille * 4, 76, 184 + grille * 4, 88), "#718080", 1)
        poly((-6, 79, 16, 79, 16, 95, -6, 95), "#33414a")
        # Cab frame, blue glass, door seals and roof.
        poly((31, 68, 37, 19, 98, 19, 115, 68), "#e9ad30")
        poly((44, 26, 92, 26, 104, 59, 41, 59), "#42677c", "#182731", 3)
        poly((47, 29, 78, 29, 47, 51), "#81b4c6", outline="", width=0)
        poly((84, 29, 91, 29, 100, 54, 88, 54), "#6093aa", outline="", width=0)
        line((79, 25, 82, 61), "#1c2b35", 3)
        line((43, 63, 101, 63), "#fff0ae", 2)
        line((84, 53, 91, 53), "#c4d2ce", 2)
        poly((30, 14, 103, 14, 109, 23, 29, 23), "#f5c456")
        line((35, 15, 100, 15), "#ffe7a2", 2)
        # Exhaust, safety rail, amber beacon and work lights.
        poly((153, 33, 162, 33, 162, 61, 153, 61), "#35434a", width=1)
        poly((151, 29, 164, 29, 164, 35, 151, 35), "#536369", width=1)
        line((20, 68, 20, 42, 31, 42), "#e2ad39", 3)
        oval((84, 3, 96, 15), "#ffe19a" if math.sin(seconds * 10) > 0 else "#d69421")
        poly((102, 24, 110, 24, 111, 31, 104, 31), "#f9f2c7", width=1)
        self.text(x + 49, y + 76, "SCHOEL", 7, "#24313a", bold=True, tags=("motion", "dozer"))
        # Hydraulic lift arms and curved steel blade face.
        line((143, 98, 230, 116), "#1a252d", 15)
        line((143, 98, 230, 116), "#d49e30", 9)
        line((178, 80, 235, 100), "#1b2931", 10)
        line((178, 80, 210, 91), "#d5a23a", 7)
        line((209, 91, 240, 102), "#c4d5d8", 4)
        poly((239, 68, 254, 63, 267, 73, 260, 132, 232, 128), "#8eafb7", "#1a2b33", 2)
        poly((254, 68, 262, 76, 255, 125, 244, 125), "#cbdcda", outline="", width=0)
        poly((232, 123, 260, 127, 260, 134, 229, 130), "#586d73", width=1)
        line((239, 70, 254, 65), "#e8f0df", 2)


class GradeGuardSplash(tk.Toplevel):
    """Five-second, skippable intro with one cancellable animation timer."""
    DURATION = 5.0

    def __init__(self, master, version, resource_path, on_complete, duration=None):
        super().__init__(master)
        self.withdraw()
        self._complete = on_complete
        self._closed = False
        self._timer = None
        self.duration = self.DURATION if duration is None else max(0.01, duration)
        self.title(f"Schoel GradeGuard v{version} | Opening")
        self.configure(background="#111e2a")
        self.resizable(False, False)
        # Keep a native window frame for keyboard access, task switching and closing.
        scale = min(1.0, (self.winfo_screenwidth() - 60) / 960, (self.winfo_screenheight() - 80) / 560)
        width, height = round(960 * scale), round(560 * scale)
        left = max(0, (self.winfo_screenwidth() - width) // 2)
        top = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.canvas = tk.Canvas(self, width=width, height=height, highlightthickness=1,
                                highlightbackground="#3a505a", background="#111e2a")
        self.canvas.pack(fill="both", expand=True)
        self.scene = DozerScene(self.canvas, version, resource_path, scale)
        self.scene.rect((696, 499, 908, 545), "#9bb76e", tags="skip")
        self.scene.text(802, 522, "Open GradeGuard  →", 11, "#162519", bold=True, tags="skip")
        self.canvas.tag_bind("skip", "<Button-1>", self.finish)
        self.canvas.tag_bind("skip", "<Enter>", lambda _event: self.canvas.configure(cursor="hand2"))
        self.canvas.tag_bind("skip", "<Leave>", lambda _event: self.canvas.configure(cursor=""))
        for key in ("<Escape>", "<Return>", "<space>"):
            self.bind(key, self.finish)
        self.protocol("WM_DELETE_WINDOW", self.finish)
        self.bind("<Destroy>", self._on_destroy, add=True)
        self.started = time.perf_counter()
        self.deiconify()
        self.lift()
        self.focus_set()
        self._tick()

    def _tick(self):
        self._timer = None
        if self._closed:
            return
        elapsed = time.perf_counter() - self.started
        if elapsed >= self.duration:
            self.finish()
            return
        self.scene.draw_frame(elapsed / self.duration, elapsed)
        self._timer = self.after(25, self._tick)

    def _cancel_timer(self):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def _on_destroy(self, event):
        if event.widget == self:
            self._closed = True
            self._cancel_timer()

    def finish(self, _event=None):
        if self._closed:
            return
        self._closed = True
        self._cancel_timer()
        self.destroy()
        self._complete()
