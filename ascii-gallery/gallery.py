#!/usr/bin/env python3
"""ASCII gallery: a screensaver of generative paintings rendered as live ASCII art.

Scenes: railway, live coding, ocean, space, night sky, night city, piano, plus
paintings after Van Gogh, Hokusai, Kandinsky. Every run shuffles them, seeds
each one fresh and joins them with a random transition, so no two runs look the same. Any key or mouse motion
exits.

    python3 gallery.py              run fullscreen in the current terminal
    python3 gallery.py --dump WxH   print one frame of every scene as plain text
    python3 gallery.py --bench WxH  time frame generation + encoding
    python3 gallery.py --preview WxH DIR   save one PNG per scene (needs Pillow)
"""
import math
import os
import random
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty

import numpy as np

FPS = 24
SCENE_SECONDS = 30
FADE_SECONDS = 2.5
GRACE_SECONDS = 1.0  # ignore input right after launch (key release, mouse settle)
RAMP = np.array(list(" .'`,:;-~=+*ox%#&@"))
TAU = 2 * math.pi


def col(r, g, b):
    return np.array((r, g, b), float)


def mix(a, b, t):
    """Blend colors a -> b by t; a/b are (3,) or (h,w,3), t is (h,w)."""
    t = np.clip(t, 0, 1)[..., None]
    return a + (b - a) * t


def ramp(v):
    idx = (np.clip(v, 0, 1) * (len(RAMP) - 1)).astype(int)
    return RAMP[idx]


def grid(w, h):
    # Terminal cells are about twice as tall as wide, so x is scaled to keep
    # shapes round. y spans -0.5 (top) .. 0.5 (bottom).
    x = (np.arange(w) + 0.5 - w / 2) / h * 0.5
    y = (np.arange(h) + 0.5 - h / 2) / h
    return np.meshgrid(x, y)


class Sines:
    """Cheap smooth noise: a sum of randomly oriented, drifting sine waves."""

    def __init__(self, rng, n=6, scale=3.0):
        self.k = np.array([rng.normal(0, scale * 1.6 ** i, 2) for i in range(n)])
        self.p = rng.uniform(0, TAU, n)
        self.s = rng.uniform(-1, 1, n)
        self.a = 1 / (1 + np.arange(n))
        self.a /= self.a.sum()

    def __call__(self, X, Y, t=0.0):
        out = np.zeros(np.broadcast(X, Y).shape)
        for (kx, ky), p, s, a in zip(self.k, self.p, self.s, self.a):
            out += a * np.sin(kx * X + ky * Y + p + s * t)
        return out


class Scene:
    title = ""
    bg = 0.0  # background = foreground color * bg (0 keeps a black canvas)

    def __init__(self, w, h, rng):
        self.w, self.h, self.rng = w, h, rng
        self.X, self.Y = grid(w, h)
        self.xr = float(self.X.max())

    def canvas(self, color=(0, 0, 0), char=" "):
        chars = np.full((self.h, self.w), char, dtype="<U1")
        rgb = np.empty((self.h, self.w, 3))
        rgb[:] = color
        return chars, rgb

    def to_cell(self, x, y):
        cx = np.round(x * self.h * 2 + self.w / 2 - 0.5).astype(int)
        cy = np.round(y * self.h + self.h / 2 - 0.5).astype(int)
        return cx, cy


def stroke_chars(ang):
    """Brush-stroke characters following a flow direction (screen y is down)."""
    q = (((ang + math.pi / 8) % math.pi) / (math.pi / 4)).astype(int) % 4
    return np.array(list("-\\|/"))[q]


class StarryNight(Scene):
    title = "The Starry Night  -  after Vincent van Gogh"
    bg = 0.25

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        xr = self.xr
        self.vort = [(rng.uniform(-0.8, 0.8) * xr, rng.uniform(-0.38, -0.05),
                      rng.uniform(0.6, 1.4) * rng.choice((-1, 1)))
                     for _ in range(rng.integers(3, 6))]
        side = rng.choice((-1, 1))
        self.moon = (side * xr * rng.uniform(0.55, 0.85), -0.32, 0.07)
        self.stars = [(rng.uniform(-xr, xr), rng.uniform(-0.45, -0.02), rng.uniform(0.012, 0.03))
                      for _ in range(rng.integers(7, 13))]
        self.cypress_x = -side * xr * rng.uniform(0.35, 0.7)
        X, Y = self.X, self.Y
        f1, p1 = rng.uniform(1.5, 3.5), rng.uniform(0, TAU)
        self.horizon = 0.14 + 0.05 * np.sin(X * f1 + p1) + 0.02 * np.sin(X * 7 + p1)
        # a village: a band of little houses with lit windows
        hx = np.floor(X * 14 + rng.uniform(0, 1))
        hh = 0.03 + 0.04 * (np.sin(hx * 12.9898) * 43758.5453 % 1)
        self.houses = (Y > self.horizon - hh + 0.06) & (Y < self.horizon + 0.12) & (np.abs(X - 0.15 * side) < 0.6 * xr)
        self.windows = self.houses & (rng.random((h, w)) < 0.06)

    def frame(self, t):
        X, Y = self.X, self.Y
        vx = 0.6 + 0.15 * np.sin(Y * 9 + t * 0.4)
        vy = 0.1 * np.sin(X * 5 - t * 0.3)
        psi = 0.6 * Y + 0.02 * np.sin(X * 6 + t * 0.2)
        glow = np.zeros_like(X)
        for cx, cy, s in self.vort:
            cx += 0.03 * math.sin(t * 0.2 + s)
            dx, dy = X - cx, Y - cy
            r2 = dx * dx + dy * dy + 0.004
            vx += -s * dy / r2 * 0.08
            vy += s * dx / r2 * 0.08
            psi += s * 0.04 * np.log(r2)
            glow += np.exp(-r2 / 0.006) * 0.6
        chars = stroke_chars(np.arctan2(vy, vx))
        stripe = 0.5 + 0.5 * np.sin(psi * 55 - t * 1.6)
        rgb = mix(col(0.04, 0.09, 0.32), col(0.35, 0.55, 0.9), stripe ** 1.5)
        rgb = mix(rgb, col(0.75, 0.85, 0.95), glow * stripe)

        for i, (sx, sy, r) in enumerate(self.stars + [self.moon]):
            d = np.hypot(X - sx, Y - sy)
            ring = 0.5 + 0.5 * np.sin(d * 90 - t * 3 + i)
            amt = np.exp(-(d / (r * 3.2)) ** 2) * (0.5 + 0.5 * ring)
            rgb = mix(rgb, col(1.0, 0.85, 0.3), amt)
            core = d < r
            if (sx, sy, r) == self.moon:
                core &= np.hypot(X - sx - r * 0.45, Y - sy + r * 0.2) > r * 0.8
            rgb[core] = (1.0, 0.95, 0.6)
            chars[core] = "@"
            chars[(amt > 0.45) & ~core] = "o"

        land = Y > self.horizon
        lstripe = 0.5 + 0.5 * np.sin((Y - self.horizon) * 70 + np.sin(X * 4) * 3)
        rgb[land] = mix(col(0.03, 0.06, 0.16), col(0.12, 0.22, 0.25), lstripe)[land]
        chars[land] = np.where(lstripe > 0.5, "~", "-")[land]
        rgb[self.houses] = (0.1, 0.12, 0.22)
        chars[self.houses] = "#"
        lit = self.windows & (np.sin(t * 0.7 + X * 50) > -0.6)
        rgb[lit] = (1.0, 0.8, 0.25)
        chars[lit] = "*"

        top = -0.38
        width = 0.085 * np.clip((Y - top) / (0.5 - top), 0, 1) ** 0.7 * (1 + 0.25 * np.sin(Y * 40 + t * 2))
        off = X - self.cypress_x - 0.02 * np.sin(Y * 12 + t)
        tree = (np.abs(off) < width) & (Y > top)
        bark = 0.5 + 0.5 * np.sin(Y * 90 + off * 60)
        rgb[tree] = mix(col(0.02, 0.07, 0.04), col(0.1, 0.25, 0.12), bark)[tree]
        chars[tree] = np.where(off < 0, ")", "(")[tree]
        return chars, rgb


class GreatWave(Scene):
    title = "The Great Wave off Kanagawa  -  after Hokusai"
    bg = 0.45

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.fuji_x = self.xr * rng.uniform(0.15, 0.5)
        self.crest_x = -self.xr * rng.uniform(0.2, 0.45)
        self.paper = rng.random((h, w))
        self.noise = Sines(rng, 5, 6)

    def frame(self, t):
        X, Y = self.X, self.Y
        rgb = mix(col(0.78, 0.72, 0.6), col(0.93, 0.86, 0.7), (Y + 0.5) * 1.4)
        chars = np.where(self.paper < 0.12, ".", " ")

        fd = np.abs(X - self.fuji_x)
        fuji = (Y > 0.02 + fd * 0.55) & (Y < 0.3)
        snow = fuji & (Y < 0.07 + 0.012 * np.sin(X * 70))
        rgb[fuji] = (0.25, 0.33, 0.52)
        chars[fuji] = np.where(X < self.fuji_x, "/", "\\")[fuji]
        rgb[snow] = (0.97, 0.95, 0.9)
        chars[snow] = "^"

        hgt = 0.47 + 0.04 * math.sin(t * 0.45)
        xc = self.crest_x + 0.06 * math.sin(t * 0.2)
        d = X - xc
        width = np.where(d > 0, 0.17, 0.5)
        swell = 0.22 + 0.025 * np.sin(X * 14 - t * 1.8) + 0.018 * np.sin(X * 23 + t * 2.3)
        surface = np.minimum(swell, 0.24 - hgt * np.exp(-(d / width) ** 2))
        depth = Y - surface
        water = depth > 0

        # the curling lip, with Hokusai's foam "claws" hanging off its edge
        ccx, ccy = xc + 0.16, 0.24 - hgt + 0.12
        r = np.hypot(X - ccx, Y - ccy)
        a = np.arctan2(Y - ccy, X - ccx)
        arc = (a < math.pi / 3) | (a > math.pi * 0.95)
        lip = arc & (r > 0.085) & (r < 0.14 + 0.01 * np.sin(a * 9 + t))
        claws = arc & (r >= 0.13) & (r < 0.17) & (np.sin(a * 30 + t * 0.5) > 0.3) & (a > -math.pi / 2)
        water |= lip

        n = self.noise(X, Y, t)
        stripe = 0.5 + 0.5 * np.sin(depth * 140 + n * 4 - t * 2)
        stripe = np.where(lip, 0.5 + 0.5 * np.sin(r * 160 - t * 2), stripe)
        sea = mix(col(0.06, 0.16, 0.38), col(0.4, 0.58, 0.75), stripe)
        rgb[water] = sea[water]
        chars[water] = np.where(stripe > 0.55, "~", "=")[water]

        foam = (water & (depth < 0.018 + 0.01 * n)) | claws | (lip & (r > 0.125))
        rgb[foam] = (0.97, 0.95, 0.88)
        chars[foam] = np.where(n > 0, "o", "*")[foam]
        spray = ~water & (np.hypot(X - ccx + 0.05, Y - ccy + 0.05) < 0.2) & (self.rng.random(X.shape) < 0.05)
        rgb[spray] = (1, 1, 1)
        chars[spray] = "."
        return chars, rgb


class Aurora(Scene):
    title = "Aurora over the Fjord"
    bg = 0.35

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.star_mask = rng.random((h, w)) < 0.015
        self.star_phase = rng.uniform(0, TAU, (h, w))
        X = self.X
        ridge = Sines(rng, 6, 4)
        self.ridge = 0.12 - 0.12 * np.abs(ridge(X, 0)) - 0.06 * np.exp(-((X - rng.uniform(-1, 1) * self.xr) / 0.2) ** 2)
        self.lake = 0.24
        self.rock = rng.random((h, w))
        self.hue = rng.uniform(0, 1)
        self.f = rng.uniform(2, 4, 3)

    def sky(self, X, Y, t):
        rgb = mix(col(0.0, 0.0, 0.02), col(0.03, 0.05, 0.14), Y + 0.5)
        f = self.f
        base = -0.12 + 0.07 * np.sin(f[0] * X + t * 0.3) + 0.04 * np.sin(f[1] * 2 * X - t * 0.5) + 0.02 * np.sin(f[2] * 5 * X + t)
        rays = np.sin(60 * X + 3 * np.sin(t * 0.7 + X * 5)) ** 2
        above = np.clip(base - Y, 0, None)
        curtain = 1.3 * np.exp(-above / 0.3) * (Y < base) * (0.35 + 0.65 * rays) + np.exp(-((Y - base) / 0.018) ** 2)
        curtain *= 0.7 + 0.3 * np.sin(X * 3 - t * 0.4)
        green = col(0.2, 1.0, 0.55) if self.hue < 0.7 else col(0.3, 0.9, 1.0)
        tint = mix(green, col(0.75, 0.3, 0.95), np.clip(above / 0.3, 0, 1))
        rgb = rgb + tint * np.clip(curtain, 0, 1)[..., None]
        return rgb, curtain

    def frame(self, t):
        X, Y = self.X, self.Y
        rgb, cur = self.sky(X, Y, t)
        chars = np.where(cur > 0.55, "|", np.where(cur > 0.25, ":", np.where(cur > 0.08, ".", " ")))
        tw = 0.5 + 0.5 * np.sin(t * 3 + self.star_phase)
        stars = self.star_mask & (cur < 0.4)
        chars[stars] = np.where(tw > 0.8, "*", ".")[stars]
        rgb[stars] = mix(rgb, col(1, 1, 1), tw)[stars]

        lake = Y > self.lake
        wob = 0.006 * np.sin(Y * 200 - t * 3)
        ref, rcur = self.sky(X + wob, 2 * self.lake - Y, t)
        ripple = 0.5 + 0.5 * np.sin(Y * 260 + np.sin(X * 30) - t * 2)
        rgb[lake] = (ref * (0.35 + 0.15 * ripple[..., None]))[lake]
        chars[lake] = np.where(rcur * ripple > 0.15, "~", np.where(ripple > 0.85, "-", " "))[lake]

        mountain = (Y > self.ridge) & (Y <= self.lake)
        mirror = lake & (2 * self.lake - Y > self.ridge)
        shade = 0.5 + 0.5 * np.sin(X * 40 + Y * 30)
        rgb[mountain] = mix(col(0.01, 0.015, 0.03), col(0.05, 0.06, 0.1), shade)[mountain]
        chars[mountain] = np.where(self.rock < 0.08, "^", " ")[mountain]
        rgb[mirror] = (0.01, 0.015, 0.035)
        chars[mirror] = np.where(ripple > 0.7, "~", " ")[mirror]
        snowline = mountain & (Y < self.ridge + 0.015)
        rgb[snowline] = (0.55, 0.6, 0.7)
        chars[snowline] = "^"
        return chars, rgb


class CherryBlossom(Scene):
    title = "Cherry Blossom at Dusk  -  in the manner of ukiyo-e"
    GROW = 6.0

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.tchars, self.trgb = self.canvas()
        self.order = np.full((h, w), np.inf)
        self.blossom = np.zeros((h, w), bool)
        self.max_order = 1.0
        root = rng.uniform(-0.5, 0.5) * self.xr
        self.branch(root, 0.52, -math.pi / 2 + rng.uniform(-0.25, 0.25), 0.26, 0.022, 7, 0.0)
        side = -1 if root > 0 else 1
        self.moon = (side * self.xr * rng.uniform(0.4, 0.75), -0.25, rng.uniform(0.09, 0.13))
        self.hills = [Sines(rng, 5, 3) for _ in range(2)]
        n = 160
        self.petals = np.column_stack([rng.uniform(-self.xr, self.xr, n), rng.uniform(-0.5, 0.5, n),
                                       rng.uniform(0, TAU, n), rng.uniform(0.5, 1.5, n)])
        self.last_t = 0.0

    def branch(self, x, y, ang, length, thick, depth, order):
        rng = self.rng
        x2, y2 = x + math.cos(ang) * length, y + math.sin(ang) * length
        steps = int(length * self.h * 2.5) + 2
        for i in range(steps):
            s = i / steps
            px, py = x + (x2 - x) * s, y + (y2 - y) * s
            half = max(0, int(thick * (1 - 0.3 * s) * self.h * 2))
            cx, cy = self.to_cell(np.array(px), np.array(py))
            if not 0 <= cy < self.h:
                continue
            for c in range(cx - half, cx + half + 1):
                if 0 <= c < self.w:
                    o = order + s * length
                    if o < self.order[cy, c]:
                        self.order[cy, c] = o
                        self.tchars[cy, c] = "|" if abs(math.cos(ang)) < 0.4 else ("/" if math.cos(ang) > 0 else "\\")
                        shade = 0.7 + 0.3 * rng.random()
                        self.trgb[cy, c] = (0.22 * shade, 0.12 * shade, 0.12 * shade)
        end = order + length
        self.max_order = max(self.max_order, end + 0.05)
        if depth == 0 or length < 0.025:
            for _ in range(18):
                bx, by = x2 + rng.normal(0, 0.035), y2 + rng.normal(0, 0.03)
                cx, cy = self.to_cell(np.array(bx), np.array(by))
                if 0 <= cx < self.w and 0 <= cy < self.h:
                    self.blossom[cy, cx] = True
                    self.order[cy, cx] = min(self.order[cy, cx], end + rng.uniform(0, 0.05))
                    self.tchars[cy, cx] = rng.choice(list("*@o&%"))
                    pink = rng.uniform(0, 1)
                    self.trgb[cy, cx] = (1.0, 0.55 + 0.3 * pink, 0.7 + 0.2 * pink)
            return
        kids = 3 if rng.random() < 0.2 else 2
        for k in range(kids):
            spread = rng.uniform(0.3, 0.65) * (1 if k % 2 else -1)
            self.branch(x2, y2, ang + spread, length * rng.uniform(0.66, 0.8), thick * 0.68, depth - 1, end)

    def frame(self, t):
        X, Y = self.X, self.Y
        dt, self.last_t = max(0.0, t - self.last_t), t
        rgb = mix(col(0.1, 0.06, 0.16), col(0.45, 0.22, 0.3), (Y + 0.5) ** 1.3)
        chars = np.full(X.shape, " ", dtype="<U1")
        mx, my, mr = self.moon
        d = np.hypot(X - mx, Y - my)
        rgb = mix(rgb, col(0.95, 0.85, 0.75), np.exp(-((d - mr) / 0.08).clip(0) ** 2) * 0.4)
        disc = d < mr
        rgb[disc] = (0.98, 0.93, 0.82)
        chars[disc] = np.where(np.sin(X * 80) * np.sin(Y * 60) > 0.6, ".", " ")[disc]
        for i, hill in enumerate(self.hills):
            line = 0.1 + 0.08 * i - 0.08 * hill(X, 0, t * 0.02)
            m = Y > line
            tone = col(0.28, 0.17, 0.27) * (1 - 0.35 * i)
            rgb[m] = tone
            chars[m] = np.where((Y - line) < 0.012, "_", " ")[m]

        shown = self.order <= (t / self.GROW) * self.max_order
        rgb[shown] = self.trgb[shown]
        chars[shown] = self.tchars[shown]
        sway = self.blossom & shown
        rgb[sway] *= (0.8 + 0.2 * np.sin(t * 2 + X * 25))[sway][:, None]

        p = self.petals
        if t > self.GROW * 0.6:
            p[:, 0] += dt * (0.05 + 0.05 * np.sin(t * 0.7 + p[:, 2])) * p[:, 3]
            p[:, 1] += dt * 0.05 * p[:, 3]
            p[:, 0] = (p[:, 0] + self.xr) % (2 * self.xr) - self.xr
            p[:, 1] = (p[:, 1] + 0.5) % 1.0 - 0.5
            cx, cy = self.to_cell(p[:, 0], p[:, 1])
            ok = (cx >= 0) & (cx < self.w) & (cy >= 0) & (cy < self.h)
            spin = np.sin(t * 3 + p[:, 2]) > 0
            chars[cy[ok], cx[ok]] = np.where(spin, ",", "`")[ok]
            rgb[cy[ok], cx[ok]] = (1.0, 0.7, 0.8)
        return chars, rgb


class JuliaPortrait(Scene):
    title = "Julia Set  -  a portrait of z^2 + c"
    bg = 0.25
    ITER = 48

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.a0 = rng.uniform(0, TAU)
        self.radius = rng.uniform(0.76, 0.79)
        self.pal = rng.uniform(0, 1, (2, 3))
        self.Z0 = (self.X + 1j * self.Y) * 1.9

    def frame(self, t):
        c = self.radius * complex(math.cos(self.a0 + t * 0.1), math.sin(self.a0 + t * 0.1))
        z = self.Z0 * complex(math.cos(t * 0.03), math.sin(t * 0.03))
        n = np.full(z.shape, float(self.ITER))
        alive = np.ones(z.shape, bool)
        for i in range(self.ITER):
            z[alive] = z[alive] ** 2 + c
            out = alive & (np.abs(z) > 4)
            n[out] = i + 1 - np.log2(np.log(np.abs(z[out])))
            alive &= ~out
            if not alive.any():
                break
        v = n / self.ITER
        c1, c2 = self.pal
        rgb = 0.5 + 0.5 * np.cos(TAU * (c1 * v[..., None] * 3 + c2 + t * 0.03))
        rgb *= np.clip(v * 3, 0, 1)[..., None]
        rgb[alive] = (0.02, 0.02, 0.04)
        chars = ramp(np.clip(v * 2.2, 0, 1) ** 0.8)
        chars[alive] = " "
        return chars, rgb


class SeveralCircles(Scene):
    title = "Several Circles  -  after Wassily Kandinsky"
    bg = 0.2
    PALETTE = [(0.95, 0.3, 0.2), (0.98, 0.8, 0.2), (0.2, 0.45, 0.95), (0.3, 0.8, 0.5),
               (0.85, 0.35, 0.75), (0.95, 0.55, 0.15), (0.4, 0.85, 0.95), (0.95, 0.95, 0.9)]

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        n = rng.integers(12, 22)
        self.circles = sorted(
            [dict(x=rng.uniform(-0.9, 0.9) * self.xr, y=rng.uniform(-0.4, 0.4),
                  r=0.025 + 0.25 * rng.random() ** 2.5,
                  c=np.array(self.PALETTE[rng.integers(len(self.PALETTE))]),
                  ox=rng.uniform(0.01, 0.05), oy=rng.uniform(0.01, 0.04),
                  sp=rng.uniform(0.05, 0.2), ph=rng.uniform(0, TAU)) for _ in range(n)],
            key=lambda c: -c["r"])
        self.lines = [(rng.uniform(-self.xr, self.xr), rng.uniform(-0.4, 0.4), rng.uniform(0, math.pi))
                      for _ in range(rng.integers(2, 4))]
        self.grain = rng.random((h, w))

    def frame(self, t):
        X, Y = self.X, self.Y
        rgb = mix(col(0.02, 0.02, 0.05), col(0.07, 0.06, 0.1), 0.5 + 0.5 * np.sin(X * 2 + Y * 3))
        chars = np.where(self.grain < 0.04, ".", " ")
        dens = np.zeros_like(X)
        for c in self.circles:
            cx = c["x"] + c["ox"] * math.sin(t * c["sp"] + c["ph"])
            cy = c["y"] + c["oy"] * math.cos(t * c["sp"] * 1.3 + c["ph"])
            d = np.hypot(X - cx, Y - cy)
            inside = d < c["r"]
            halo = np.exp(-((d - c["r"]) / 0.012) ** 2)
            rgb = mix(rgb, c["c"], inside * 0.55 + halo * 0.7 + np.exp(-(d / c["r"]) ** 2) * 0.15 * inside)
            dens += inside * 0.25 + halo * 0.5
        chars = np.where(dens > 0.05, ramp(np.clip(dens, 0, 1) * 0.85 + 0.1), chars)
        for lx, ly, la in self.lines:
            la += 0.05 * math.sin(t * 0.1)
            dist = np.abs((X - lx) * math.sin(la) - (Y - ly) * math.cos(la))
            on = dist < 0.006
            rgb[on] = (0.9, 0.88, 0.8)
            chars[on] = stroke_chars(np.full(X.shape, la))[on]
        return chars, rgb


class LavaGarden(Scene):
    title = "Metamorphosis  -  liquid light"
    bg = 0.35

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        n = rng.integers(6, 10)
        self.blobs = [(rng.uniform(0.1, 0.4), rng.uniform(0.2, 0.6), rng.uniform(0, TAU), rng.uniform(0.04, 0.08))
                      for _ in range(n)]
        warm = rng.random() < 0.5
        self.hot = col(1.0, 0.85, 0.3) if warm else col(0.5, 1.0, 0.9)
        self.mid = col(0.9, 0.2, 0.25) if warm else col(0.3, 0.3, 0.95)
        self.cold = col(0.12, 0.02, 0.1) if warm else col(0.02, 0.03, 0.12)

    def frame(self, t):
        X, Y = self.X, self.Y
        field = np.zeros_like(X)
        for i, (sx, sy, ph, r) in enumerate(self.blobs):
            bx = self.xr * 0.8 * math.sin(t * sx + ph)
            by = 0.42 * math.sin(t * sy + ph * 1.7)
            field += r * r / ((X - bx) ** 2 + (Y - by) ** 2 + 1e-4)
        v = np.clip(field, 0, 2)
        rgb = mix(mix(self.cold, self.mid, np.clip(v * 1.6, 0, 1)), self.hot, np.clip((v - 0.8) * 1.5, 0, 1))
        chars = ramp(np.clip(v * 0.9, 0, 1))
        edge = np.abs(v - 0.9) < 0.04
        chars[edge] = "o"
        return chars, rgb


def blit(chars, rgb, lines, row, col, color_of, opaque=None):
    """Draw a sprite (list of strings); spaces are transparent unless `opaque` says so."""
    h, w = chars.shape
    for r, line in enumerate(lines):
        y = row + r
        if not 0 <= y < h:
            continue
        for c, ch in enumerate(line):
            x = col + c
            if 0 <= x < w and (ch != " " or (opaque is not None and opaque[r][c])):
                chars[y, x] = ch
                rgb[y, x] = color_of(ch, r, c)


def put_text(chars, rgb, row, col, text, color):
    h, w = chars.shape
    if not 0 <= row < h:
        return
    a, b = max(0, col), min(w, col + len(text))
    if a < b:
        chars[row, a:b] = list(text[a - col:b - col])
        rgb[row, a:b] = color


def hash01(n):
    return (np.sin(n * 12.9898 + 78.233) * 43758.5453) % 1.0


class Railway(Scene):
    title = "Night Train  -  the last service west"
    LOCO = [
        "                 __          ",
        "   ________     |  |___      ",
        "  | [][][] |____|      |___  ",
        "  |        |   ___   ___   |>",
        " =|________|__|___|_|___|__|=",
        "    (O)  (O)  (O)(O)(O)  (o) ",
    ]
    CAR = [
        "                          ",
        "  ______________________  ",
        " | [] [] [] [] [] [] [] | ",
        " |                      | ",
        "=|______________________|=",
        "    (O)  (O)    (O)  (O)  ",
    ]

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.sunset = rng.random() < 0.5
        self.mount = Sines(rng, 5, 3)
        self.hill = Sines(rng, 5, 6)
        self.stars = rng.random((h, w)) < 0.01
        self.ballast = rng.choice(list(" .:',"), (h, w))
        self.track = int(h * 0.8)
        self.ncars = int(rng.integers(2, 5))
        pieces = [(self.CAR, (0.18, 0.32, 0.5) if rng.random() < 0.5 else (0.2, 0.42, 0.3))] * self.ncars
        pieces.append((self.LOCO, (0.6, 0.12, 0.12)))
        self.sprite = ["".join(p[0][r] for p in pieces) for r in range(6)]
        self.body = np.zeros((6, len(self.sprite[0]), 3))
        self.opaque = np.zeros((6, len(self.sprite[0])), bool)
        x = 0
        for lines, color in pieces:
            for r in range(1, 5):
                seg = lines[r]
                filled = [i for i, ch in enumerate(seg) if ch != " "]
                if filled and r >= 2:
                    self.opaque[r, x + filled[0]:x + filled[-1] + 1] = True
            self.body[:, x:x + len(lines[0])] = color
            x += len(lines[0])
        self.chimney = len(self.sprite[0]) - len(self.LOCO[0]) + 17
        self.smoke = np.zeros((0, 3))
        self.last_t = 0.0
        side = rng.choice((-1, 1))
        self.moon = (side * self.xr * rng.uniform(0.3, 0.8), rng.uniform(-0.38, -0.2))

    def frame(self, t):
        X, Y = self.X, self.Y
        h, w = self.h, self.w
        dt, self.last_t = max(0.0, min(0.2, t - self.last_t)), t
        if self.sunset:
            rgb = mix(col(0.15, 0.08, 0.3), col(1.0, 0.5, 0.25), (Y + 0.5) ** 1.6 * 1.6)
            sky_body = col(1.0, 0.75, 0.4)
        else:
            rgb = mix(col(0.01, 0.02, 0.07), col(0.1, 0.12, 0.26), (Y + 0.5) * 1.3)
            sky_body = col(0.95, 0.93, 0.85)
        chars = np.full((h, w), " ", dtype="<U1")
        if not self.sunset:
            chars[self.stars] = "."
            rgb[self.stars] = (0.8, 0.8, 0.9)
        mx, my = self.moon
        d = np.hypot(X - mx, Y - my)
        rgb = mix(rgb, sky_body, np.exp(-(d / 0.14) ** 2) * 0.5)
        disc = d < 0.06
        rgb[disc] = sky_body * 0.75
        chars[disc] = np.where(hash01(X * 400 + Y * 90) < 0.15, ".", " ")[disc]

        far = Y > 0.06 - 0.14 * np.abs(self.mount(X + t * 0.015, 0))
        rgb[far] = (0.3, 0.15, 0.32) if self.sunset else (0.05, 0.07, 0.15)
        chars[far] = np.where(hash01(np.floor((X + t * 0.015) * 200)) < 0.05, "^", " ")[far]
        line_h = 0.24 - 0.04 * self.hill(X + t * 0.06, 0)
        u = (X + t * 0.06) * 25
        cell = np.floor(u)
        hs = hash01(cell)
        top = line_h - 0.04 - 0.07 * hs
        tree = (hs < 0.4) & (Y > top) & (Y < line_h + 0.02) & (np.abs(u - cell - 0.5) / 25 < (Y - top) * 0.35)
        hills = Y > line_h
        rgb[hills] = (0.12, 0.06, 0.12) if self.sunset else (0.02, 0.04, 0.06)
        chars[hills] = " "
        rgb[tree] = (0.06, 0.1, 0.06) if self.sunset else (0.02, 0.08, 0.05)
        chars[tree] = "^"

        track_y = (self.track + 0.5 - h / 2) / h
        cw = 0.5 / h
        up = (X + t * 0.5) % 0.7
        pole_top = track_y - 0.3
        pole = (up < cw) & (Y > pole_top) & (Y < track_y)
        rgb[pole] = (0.25, 0.18, 0.15)
        chars[pole] = "|"
        sag = pole_top + 0.01 + 0.04 * (1 - ((up / 0.7) * 2 - 1) ** 2)
        wire = (np.abs(Y - sag) < 0.5 / h) & ~pole
        rgb[wire] = (0.35, 0.3, 0.35)
        chars[wire] = "-"

        cols = np.arange(w)
        shift = int(t * 60)
        tr = self.track
        if tr < h:
            chars[tr] = "="
            rgb[tr] = (0.6, 0.62, 0.68)
        if tr + 1 < h:
            chars[tr + 1] = np.where((cols + shift) % 3 == 0, "|", "_")
            rgb[tr + 1] = (0.35, 0.22, 0.15)
        below = slice(tr + 2, h)
        chars[below] = self.ballast[below][:, (cols + shift) % w]
        rgb[below] = (0.25, 0.22, 0.22)

        bgc = rgb * 0.25
        bgc[disc] = sky_body * 0.9
        length = len(self.sprite[0])
        target = int(w * 0.88) - length
        enter = max(0.0, 4.0 - t) / 4.0
        x0 = int(target - enter * (target + length))
        y0 = tr - 6
        wheel = "O" if int(t * 10) % 2 else "o"

        def color_of(ch, r, c):
            if ch in "[]":
                return (1.0, 0.85, 0.35)
            if ch in "(Oo)":
                return (0.7, 0.7, 0.75)
            if ch == ">":
                return (1.0, 1.0, 0.7)
            if ch == "=":
                return (0.5, 0.5, 0.55)
            return np.clip(self.body[r, c] * 1.8, 0, 1)

        sprite = [line.replace("O", wheel) if r == 5 else line for r, line in enumerate(self.sprite)]
        blit(chars, rgb, sprite, y0, x0, color_of, self.opaque)
        ys, xs = np.nonzero(self.opaque)
        ys, xs = ys + y0, xs + x0
        ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        bgc[ys[ok], xs[ok]] = self.body[ys[ok] - y0, xs[ok] - x0] * 0.6
        win = np.isin(chars[ys[ok], xs[ok]], ["[", "]"])
        bgc[ys[ok][win], xs[ok][win]] = (0.5, 0.4, 0.15)

        front = x0 + length
        if front < w:
            dist = cols[None, front:] - front
            beam = np.exp(-dist / 18.0) * 0.45
            for r in range(y0 + 2, min(h, tr + 2)):
                bgc[r, front:] += beam[0, :, None] * np.array((0.6, 0.55, 0.3))
                rgb[r, front:] = np.clip(rgb[r, front:] + beam[0, :, None] * 0.6, 0, 1)

        cx = x0 + self.chimney + 0.5
        if 0 <= cx < w:
            spawn = np.column_stack([np.full(2, cx) + self.rng.normal(0, 0.4, 2),
                                     np.full(2, y0 - 0.5), np.zeros(2)])
            self.smoke = np.vstack([self.smoke, spawn])
        s = self.smoke
        if len(s):
            s[:, 0] -= dt * 22
            s[:, 1] -= dt * (2.5 + s[:, 2] * 1.5)
            s[:, 2] += dt
            self.smoke = s = s[(s[:, 2] < 3.5) & (s[:, 0] > -2) & (s[:, 1] > -1)]
            px, py, age = s[:, 0].astype(int), s[:, 1].astype(int), s[:, 2]
            ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            glyph = np.where(age < 0.6, "O", np.where(age < 1.4, "o", np.where(age < 2.4, ".", "`")))
            fade = np.clip(1 - age / 3.5, 0, 1)[:, None]
            chars[py[ok], px[ok]] = glyph[ok]
            rgb[py[ok], px[ok]] = (0.75 * fade + 0.05)[ok] * np.ones(3)
        return chars, rgb, bgc


class Coding(Scene):
    title = "Late Night Commit  -  writing this very screensaver"
    KEYWORDS = set("def class return if elif else for while in not and or import from as with try "
                   "except finally raise yield lambda None True False pass break continue is global".split())
    BUILTINS = {"self", "np", "math", "rng", "sys", "os", "len", "range", "int", "float", "min", "max",
                "abs", "list", "zip", "enumerate", "super", "print", "str"}
    TOKEN = re.compile(r"(#.*$)|(\"[^\"]*\"|'[^']*')|(\b\d+\.?\d*\b)|([A-Za-z_]\w*)|(\s+)|(.)")
    BG = col(0.1, 0.11, 0.16)
    FG = col(0.66, 0.69, 0.84)

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        try:
            with open(os.path.abspath(__file__)) as f:
                src = f.read().replace("\t", "    ").splitlines()
        except OSError:
            src = ["def hello():", "    print('hello, world')"]
        starts = [i for i, l in enumerate(src) if l.startswith(("class ", "def ", "    def "))]
        start = int(rng.choice(starts)) if starts else 0
        pre = min(start, 40)
        self.lines = src[start - pre:start + 400]
        self.first_no = start - pre + 1
        self.pre = pre
        self.colors = [self.highlight(l) for l in self.lines]
        # per-character typing times: bursts, pauses at line ends, instant indentation
        times, line_start, t = [], [], 0.0
        for n, line in enumerate(self.lines):
            line_start.append(t)
            if n < pre:
                times.extend([0.0] * (len(line) + 1))
                continue
            indent = len(line) - len(line.lstrip(" "))
            for i, ch in enumerate(line):
                t += 0.004 if i < indent else rng.gamma(2.0, 0.022)
                if rng.random() < 0.01:
                    t += rng.uniform(0.5, 1.4)
                times.append(t)
            t += rng.uniform(0.08, 0.5)
            times.append(t)  # the newline
        self.times = np.array(times)
        self.line_start = np.array(line_start)
        self.offsets = np.cumsum([0] + [len(l) + 1 for l in self.lines])

    def highlight(self, line):
        out = np.empty((len(line), 3))
        out[:] = self.FG
        prev = None
        for m in self.TOKEN.finditer(line):
            a, b = m.span()
            comment, string, num, word = m.group(1, 2, 3, 4)
            if comment:
                out[a:b] = (0.34, 0.37, 0.5)
            elif string:
                out[a:b] = (0.62, 0.81, 0.42)
            elif num:
                out[a:b] = (1.0, 0.62, 0.39)
            elif word:
                if word in self.KEYWORDS:
                    out[a:b] = (0.73, 0.6, 0.97)
                elif prev in ("def", "class") or line[b:b + 1] == "(":
                    out[a:b] = (0.48, 0.64, 0.97)
                elif word in self.BUILTINS:
                    out[a:b] = (0.49, 0.81, 1.0)
                elif word[:1].isupper():
                    out[a:b] = (0.88, 0.69, 0.4)
                prev = word
            elif m.group(6) and m.group(6) in "=+-*/<>%&|":
                out[a:b] = (0.54, 0.86, 0.92)
        return out

    def frame(self, t):
        h, w = self.h, self.w
        chars = np.full((h, w), " ", dtype="<U1")
        rgb = np.empty((h, w, 3))
        rgb[:] = self.FG
        bgc = np.empty((h, w, 3))
        bgc[:] = self.BG
        loop = self.times[-1] + 3.0
        tt = t % loop
        typed = int(np.searchsorted(self.times, tt))
        line = int(np.searchsorted(self.offsets, typed, side="right")) - 1
        line = min(line, len(self.lines) - 1)
        colpos = typed - self.offsets[line]
        idle = tt - (self.times[typed - 1] if typed else 0)

        bgc[0] = (0.08, 0.09, 0.13)
        put_text(chars, rgb, 0, 1, " gallery.py ", (0.85, 0.87, 0.95))
        bgc[0, 1:13] = self.BG
        put_text(chars, rgb, 0, 14, " README.md   hypr/bindings.conf ", (0.4, 0.43, 0.55))
        area = h - 3
        gut = 6
        top = max(0, line - int(area * 0.7))
        for r in range(area):
            i = top + r
            if i > line or i >= len(self.lines):
                break
            y = r + 1
            text = self.lines[i]
            n = len(text) if i < line else max(0, min(len(text), colpos))
            n = min(n, w - gut)
            if i == line:
                bgc[y] = (0.14, 0.15, 0.22)
            no = f"{self.first_no + i:>4} "
            put_text(chars, rgb, y, 1, no, (0.88, 0.69, 0.4) if i == line else (0.25, 0.27, 0.36))
            if i >= self.pre:
                chars[y, 0] = "|"
                rgb[y, 0] = (0.45, 0.7, 0.4)
            if n:
                chars[y, gut:gut + n] = list(text[:n])
                rgb[y, gut:gut + n] = self.colors[i][:n]
        cy = line - top + 1
        cx = gut + min(max(0, colpos), w - gut - 1)
        if 0 < cy <= area and (idle < 0.5 or int(t * 2) % 2 == 0):
            bgc[cy, cx] = (0.75, 0.79, 0.96)
            rgb[cy, cx] = self.BG

        sr = h - 2
        bgc[sr] = (0.12, 0.13, 0.19)
        mode, mcol = (" INSERT ", col(0.62, 0.81, 0.42)) if idle < 0.6 else (" NORMAL ", col(0.48, 0.64, 0.97))
        put_text(chars, rgb, sr, 0, mode, self.BG)
        bgc[sr, 0:len(mode)] = mcol
        put_text(chars, rgb, sr, len(mode), "  main  gallery.py [+] ", (0.75, 0.78, 0.9))
        bgc[sr, len(mode):len(mode) + 7] = (0.23, 0.26, 0.38)
        pct = int(100 * (line + 1) / len(self.lines))
        right = f" utf-8  python  {pct:>3}%  Ln {self.first_no + line}, Col {colpos + 1} "
        put_text(chars, rgb, sr, w - len(right), right, (0.75, 0.78, 0.9))
        bgc[sr, max(0, w - len(right)):] = (0.23, 0.26, 0.38)
        saved = line // 12 * 12
        since = tt - self.line_start[saved] if saved > self.pre else 99
        if since < 2.5:
            put_text(chars, rgb, h - 1, 0, ":w" if since < 0.6 else
                     f'"gallery.py" {len(self.lines) + self.first_no}L written', self.FG)
        return chars, rgb, bgc


class Ocean(Scene):
    title = "Moonlit Ocean  -  the lighthouse keeper's view"

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.horizon = rng.uniform(-0.08, 0.0)
        side = rng.choice((-1, 1))
        self.moon = (-side * self.xr * rng.uniform(0.1, 0.5), rng.uniform(-0.36, -0.22))
        self.light = (side * self.xr * 0.78, self.horizon - 0.12)
        self.side = side
        self.stars = rng.random((h, w)) < 0.012
        self.phase = rng.uniform(0, TAU, (h, w))
        self.cliff = Sines(rng, 4, 8)
        self.boat_x = rng.uniform(-0.6, 0.0) * self.xr

    def frame(self, t):
        X, Y = self.X, self.Y
        h, w = self.h, self.w
        hz = self.horizon
        rgb = mix(col(0.01, 0.02, 0.08), col(0.07, 0.12, 0.25), (Y + 0.5) / (hz + 0.5))
        chars = np.full((h, w), " ", dtype="<U1")
        tw = 0.5 + 0.5 * np.sin(t * 2.5 + self.phase)
        st = self.stars & (Y < hz)
        chars[st] = np.where(tw > 0.85, "*", ".")[st]
        rgb[st] = mix(rgb, col(1, 1, 1), tw * 0.8)[st]
        mx, my = self.moon
        d = np.hypot(X - mx, Y - my)
        rgb = mix(rgb, col(0.6, 0.65, 0.8), np.exp(-(d / 0.18) ** 2) * 0.45)
        disc = d < 0.055
        rgb[disc] = (0.7, 0.68, 0.6)
        chars[disc] = np.where(hash01(X * 400 + Y * 90) < 0.15, ".", " ")[disc]
        sky_bg = rgb * 0.22

        sea = Y > hz
        depth = np.maximum(Y - hz, 1e-3)
        v = 1.0 / (depth + 0.01)
        u = X * v
        wv = 0.6 * np.sin(v * 0.9 - t * 1.6 + 0.6 * np.sin(u * 0.15)) + 0.4 * np.sin(v * 0.5 + u * 0.08 - t * 1.1)
        seac = mix(col(0.02, 0.06, 0.14), col(0.18, 0.35, 0.5), 0.5 + 0.5 * wv)
        seac = mix(seac, col(0.1, 0.13, 0.22), np.exp(-depth * 12) * 0.6)
        rgb[sea] = seac[sea]
        chars[sea] = np.where(wv > 0.75, "^", np.where(wv > 0.15, "~", np.where(wv > -0.4, "-", " ")))[sea]
        glit = np.exp(-((X - mx) / (0.02 + depth * 0.3)) ** 2) * (wv > 0.2) * sea
        g = glit > 0.35
        rgb[g] = mix(rgb, col(1.0, 0.96, 0.8), glit)[g]
        chars[g] = np.where(wv > 0.6, "*", "~")[g]

        bx = self.boat_x + t * 0.01
        bcx, bcy = self.to_cell(np.array(bx), np.array(hz + 0.03))
        boat = ["   |\\  ", "   | \\ ", "  _|__\\_", "  \\____/"]
        blit(chars, rgb, boat, int(bcy) - 3, int(bcx), lambda ch, r, c: (0.02, 0.03, 0.06) if r < 3 else (0.1, 0.06, 0.05))

        lx, ly = self.light
        cliff_line = hz + 0.02 - 0.12 * np.clip((X - lx) * self.side * -1 + 0.35, 0, 1) - 0.02 * self.cliff(X, 0)
        cliff = (Y > cliff_line) & ((X - lx) * self.side > -0.35)
        rgb[cliff] = (0.03, 0.03, 0.05)
        chars[cliff] = np.where(hash01(X * 300 + Y * 70) < 0.06, "#", " ")[cliff]
        tower_top = ly - 0.2
        tw_ = np.abs(X - lx) < 0.018 + (Y - tower_top) * 0.04
        tower = tw_ & (Y > tower_top) & (Y < cliff_line + 0.01)
        stripe = (np.floor((Y - tower_top) * 25) % 2) == 0
        rgb[tower] = np.where(stripe[..., None], col(0.75, 0.15, 0.15), col(0.85, 0.85, 0.8))[tower]
        chars[tower] = "#"
        lamp_y = tower_top - 0.02
        a = t * 0.9
        toward = max(0.0, math.sin(a))
        dx, dy = X - lx, Y - lamp_y
        ca = math.cos(a)
        beam = (dx * np.sign(ca) > 0) & (np.abs(dy) < np.abs(dx) * 0.07 + 0.006) & (np.abs(dx) < 2.2 * abs(ca) + 0.05)
        inten = np.exp(-np.abs(dx) / 1.2) * abs(ca) * beam
        flare = np.exp(-(np.hypot(dx, dy) / (0.02 + 0.08 * toward ** 6)) ** 2)
        lightc = col(1.0, 0.92, 0.6)
        rgb = mix(rgb, lightc, np.clip(inten * 0.8 + flare, 0, 1))
        bm = (inten > 0.15) & (chars == " ")
        chars[bm] = "-"
        lamp = np.hypot(dx, dy) < 0.012
        rgb[lamp] = lightc
        chars[lamp] = "@"
        bgc = rgb * 0.22
        bgc[st] = sky_bg[st]
        bgc[disc] = (0.95, 0.93, 0.82)
        bgc += (flare * 0.5 + inten * 0.25)[..., None] * lightc
        return chars, rgb, bgc


class Space(Scene):
    title = "Deep Space  -  warp to a ringed world"

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        n = 450
        self.stars = np.column_stack([rng.uniform(-1, 1, n), rng.uniform(-1, 1, n), rng.uniform(0.05, 1, n)])
        self.neb = [Sines(rng, 5, 2.5), Sines(rng, 5, 3.5)]
        self.nebc = [col(*rng.uniform(0.2, 0.6, 3)) * col(0.8, 0.3, 1.0), col(0.1, 0.5, 0.6)]
        side = rng.choice((-1, 1))
        self.planet = (side * self.xr * rng.uniform(0.35, 0.6), rng.uniform(-0.12, 0.12), rng.uniform(0.2, 0.27))
        self.tilt = rng.uniform(-0.5, 0.5)
        self.pcol = [col(0.85, 0.7, 0.45), col(0.65, 0.4, 0.25)] if rng.random() < 0.6 else [col(0.5, 0.75, 0.9), col(0.25, 0.4, 0.7)]
        self.grain = rng.random((h, w))
        self.last_t = 0.0

    def frame(self, t):
        X, Y = self.X, self.Y
        h, w = self.h, self.w
        dt, self.last_t = max(0.0, min(0.2, t - self.last_t)), t
        n1 = np.clip(self.neb[0](X, Y, t * 0.05), 0, 1)
        n2 = np.clip(self.neb[1](X, Y, -t * 0.04), 0, 1)
        rgb = col(0.0, 0.0, 0.02) + self.nebc[0] * n1[..., None] * 0.35 + self.nebc[1] * n2[..., None] * 0.25
        chars = np.where(self.grain < (n1 + n2) * 0.08, ".", " ")

        speed = 0.12 + 0.9 * (0.5 + 0.5 * math.sin(t * 0.25 - 1.5)) ** 4
        s = self.stars
        s[:, 2] -= dt * speed
        dead = s[:, 2] < 0.04
        k = int(dead.sum())
        if k:
            s[dead] = np.column_stack([self.rng.uniform(-1, 1, k), self.rng.uniform(-1, 1, k), np.ones(k)])
        for j in range(5, -1, -1):
            z = s[:, 2] + j * speed * 0.02
            sx, sy = s[:, 0] / z * 0.3, s[:, 1] / z * 0.3
            cx, cy = self.to_cell(sx, sy)
            ok = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h)
            b = np.clip((1 - s[:, 2]) * 1.4, 0.15, 1) * (1 - j / 6)
            if j:
                glyph = stroke_chars(np.arctan2(sy, sx))
            else:
                glyph = np.where(b > 0.75, "*", np.where(b > 0.4, "+", "."))
            chars[cy[ok], cx[ok]] = glyph[ok]
            rgb[cy[ok], cx[ok]] = (b[:, None] * np.array((0.85, 0.9, 1.0)))[ok]

        px, py, R = self.planet
        px += 0.02 * math.sin(t * 0.05)
        dx, dy = (X - px) / R, (Y - py) / R
        r2 = dx * dx + dy * dy
        disc = r2 < 1
        nz = np.sqrt(np.clip(1 - r2, 0, 1))
        lam = np.clip(dx * -0.6 + dy * -0.35 + nz * 0.72, 0, 1)
        band = 0.5 + 0.5 * np.sin(dy * 16 + 0.6 * np.sin(dx * 3 + t * 0.2))
        pc = mix(self.pcol[0], self.pcol[1], band)
        lon = np.arctan2(dx, nz) + t * 0.15
        spot = (((lon % TAU) - 1.5) ** 2 + ((dy - 0.35) * 3) ** 2) < 0.06
        pc[spot] = (0.8, 0.3, 0.2)
        shaded = pc * (0.04 + lam[..., None] * 0.96)
        c, sn = math.cos(self.tilt), math.sin(self.tilt)
        ux = dx * c + dy * sn
        uy = (-dx * sn + dy * c) / 0.28
        rr = np.hypot(ux, uy)
        ring = (rr > 1.35) & (rr < 2.25) & (np.sin(rr * 38) > -0.4) & ((uy > 0) | ~disc)
        rgb[disc] = shaded[disc]
        chars[disc] = ramp(lam * 0.85 + 0.1)[disc]
        ringc = mix(col(0.5, 0.45, 0.38), col(0.9, 0.85, 0.7), 0.5 + 0.5 * np.sin(rr * 11))
        rgb[ring] = ringc[ring]
        chars[ring] = np.where(uy > 0, "=", "-")[ring]
        bgc = rgb * 0.18
        bgc[disc] = shaded[disc] * 0.7
        bgc[ring] = ringc[ring] * 0.35
        return chars, rgb, bgc


class NightSky(Scene):
    title = "Under the Milky Way  -  star trails over camp"

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        n = 1100
        self.pole = (rng.uniform(-0.4, 0.4) * self.xr, -0.42)
        self.sr = np.sqrt(rng.random(n)) * self.xr * 1.8
        self.sth = rng.uniform(0, TAU, n)
        self.mag = rng.random(n) ** 3
        self.tint = np.array([(1, 1, 1), (0.75, 0.85, 1), (1, 0.9, 0.7), (1, 0.75, 0.6)])[rng.integers(0, 4, n)]
        self.band = rng.uniform(-0.9, -0.3)
        self.dust = Sines(rng, 6, 9)
        self.grain = rng.random((h, w))
        bright = np.nonzero(self.mag > 0.35)[0]
        self.groups = []
        for _ in range(6):
            if len(bright) < 6:
                break
            chain = [int(rng.choice(bright))]
            for _ in range(int(rng.integers(3, 6))):
                last = chain[-1]
                p = self.star_pos(np.array([last]), 0.0)
                q = self.star_pos(bright, 0.0)
                dist = np.hypot(q[0] - p[0], q[1] - p[1])
                dist[np.isin(bright, chain)] = 9
                chain.append(int(bright[np.argmin(dist)]))
            self.groups.append(chain)
        self.ground = 0.3 + 0.02 * Sines(rng, 4, 5)(self.X, 0)
        pines = np.zeros((h, w), bool)
        for _ in range(int(rng.integers(14, 24))):
            cx = rng.uniform(-1, 1) * self.xr
            if abs(cx) < 0.25:
                continue
            ht = rng.uniform(0.08, 0.28)
            base = 0.32
            top = base - ht
            pines |= (self.Y > top) & (self.Y < base + 0.2) & (np.abs(self.X - cx) < (self.Y - top) * 0.28 * (0.8 + 0.2 * np.sin(self.Y * 120)))
        self.pines = pines
        fx, fy = self.to_cell(np.array(rng.uniform(-0.15, 0.15)), np.array(0.38))
        self.fire = (int(fx), int(fy))
        self.sparks = np.zeros((0, 3))
        self.shoot = None
        self.next_shoot = rng.uniform(2, 5)
        self.last_t = 0.0

    def star_pos(self, idx, rot):
        th = self.sth[idx] + rot
        return self.pole[0] + self.sr[idx] * np.cos(th), self.pole[1] + self.sr[idx] * np.sin(th)

    def frame(self, t):
        X, Y = self.X, self.Y
        h, w = self.h, self.w
        dt, self.last_t = max(0.0, min(0.2, t - self.last_t)), t
        rgb = mix(col(0.0, 0.0, 0.03), col(0.05, 0.06, 0.14), (Y + 0.5) * 1.2)
        chars = np.full((h, w), " ", dtype="<U1")
        rot = t * 0.02
        px, py = self.pole
        c, s = math.cos(-rot), math.sin(-rot)
        Xr = px + (X - px) * c - (Y - py) * s
        Yr = py + (X - px) * s + (Y - py) * c
        d = Xr * math.sin(self.band) - (Yr + 0.1) * math.cos(self.band)
        dens = np.exp(-(d / 0.13) ** 2) * (0.55 + 0.45 * self.dust(Xr, Yr))
        dens -= np.exp(-((d - 0.015) / 0.025) ** 2) * 0.4 * (0.5 + 0.5 * self.dust(Xr * 2, Yr * 2))
        dens = np.clip(dens, 0, 1)
        rgb = rgb + dens[..., None] * np.array((0.28, 0.24, 0.4))
        mw = self.grain < dens * 0.45
        chars[mw] = "."
        sky_bg = rgb * 0.25

        trail = min(14, int(t / 1.5))
        idx = np.arange(len(self.sr))
        for j in range(trail, -1, -1):
            sx, sy = self.star_pos(idx, rot - j * 0.006)
            cx, cy = self.to_cell(sx, sy)
            ok = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h) & (sy < 0.3)
            if j:
                b = self.mag * 0.5 * (1 - j / 15)
                ok &= b > 0.05
                glyph = np.full(len(idx), ".")
            else:
                b = self.mag * (0.75 + 0.25 * np.sin(t * 5 + idx))
                glyph = np.where(b > 0.6, "*", np.where(b > 0.3, "+", "."))
                b = np.clip(b + 0.25, 0, 1)
            chars[cy[ok], cx[ok]] = glyph[ok]
            rgb[cy[ok], cx[ok]] = (self.tint * b[:, None])[ok]

        if self.groups:
            g = self.groups[int(t / 6) % len(self.groups)]
            alpha = math.sin(math.pi * (t % 6) / 6) ** 2
            for a_, b_ in zip(g, g[1:]):
                ax, ay = self.star_pos(np.array([a_]), rot)
                bx, by = self.star_pos(np.array([b_]), rot)
                f = np.linspace(0.08, 0.92, 24)
                cx, cy = self.to_cell(ax + (bx - ax) * f, ay + (by - ay) * f)
                ok = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h)
                free = ok.copy()
                free[ok] = chars[cy[ok], cx[ok]] != "*"
                chars[cy[free], cx[free]] = "."
                rgb[cy[free], cx[free]] = np.array((0.45, 0.65, 1.0)) * alpha

        if self.shoot is None and t > self.next_shoot:
            self.shoot = (t, self.rng.uniform(-0.8, 0.8) * self.xr, self.rng.uniform(-0.45, -0.2),
                          self.rng.choice((-1, 1)) * self.rng.uniform(0.6, 1.0), self.rng.uniform(0.2, 0.4))
            self.next_shoot = t + self.rng.uniform(3, 7)
        if self.shoot is not None:
            t0, sx, sy, vx, vy = self.shoot
            age = t - t0
            if age > 0.9:
                self.shoot = None
            else:
                ang = math.atan2(vy, vx)
                for k in range(10):
                    hx, hy = sx + vx * (age - k * 0.012), sy + vy * (age - k * 0.012)
                    cx, cy = self.to_cell(np.array(hx), np.array(hy))
                    if 0 <= cx < w and 0 <= cy < h:
                        chars[cy, cx] = "*" if k == 0 else stroke_chars(np.array([ang]))[0]
                        rgb[cy, cx] = np.ones(3) * (1 - k / 10) * (1 - age / 0.9)

        land = (Y > self.ground) | self.pines
        rgb[land] = (0.01, 0.012, 0.02)
        chars[land] = " "
        bgc = np.where(land[..., None], rgb * 0.2, sky_bg)
        fx, fy = self.fire
        flick = 0.8 + 0.2 * math.sin(t * 13) * math.sin(t * 7.3)
        dist = np.hypot((np.arange(w)[None, :] - fx) / 2.0, np.arange(h)[:, None] - fy)
        glow = np.exp(-dist / 5.0) * flick * (Y > self.ground - 0.05)
        fc = col(1.0, 0.45, 0.12)
        bgc += glow[..., None] * fc * 0.55
        rgb = np.where(land[..., None], rgb + glow[..., None] * fc * 0.5, rgb)
        tent = ["   /\\   ", "  /  \\  ", " / /\\ \\ ", "/_/  \\_\\"]
        blit(chars, rgb, tent, fy - 4, fx - 14, lambda ch, r, c: (0.9, 0.55, 0.25) * np.array(flick))
        for r in range(3):
            for dxp in range(-1, 2):
                if self.rng.random() < 0.8 - r * 0.2 and 0 <= fy - r < h and 0 <= fx + dxp < w:
                    chars[fy - r, fx + dxp] = self.rng.choice(list("^*'\"" if r else "^#*"))
                    rgb[fy - r, fx + dxp] = (1.0, 0.85 - r * 0.25, 0.3 - r * 0.1)
        if 0 <= fy + 1 < h:
            put_text(chars, rgb, fy + 1, fx - 2, "\\_|_/", (0.35, 0.2, 0.12))
        spawn = np.column_stack([fx + self.rng.normal(0, 0.6, 1), np.full(1, fy - 2.0), np.zeros(1)])
        self.sparks = np.vstack([self.sparks, spawn])
        sp = self.sparks
        sp[:, 1] -= dt * 6
        sp[:, 0] += dt * self.rng.normal(0, 3, len(sp))
        sp[:, 2] += dt
        self.sparks = sp = sp[sp[:, 2] < 1.6]
        sx, sy = sp[:, 0].astype(int), sp[:, 1].astype(int)
        ok = (sx >= 0) & (sx < w) & (sy >= 0) & (sy < h)
        chars[sy[ok], sx[ok]] = "."
        rgb[sy[ok], sx[ok]] = (1.0, 0.6, 0.2)
        return chars, rgb, bgc


class NightCity(Scene):
    title = "Neon City  -  rain after midnight"
    SIGNS = ["HOTEL", "BAR", "24H", "RAMEN", "OPEN", "NOODLES", "ARCADE", "SUSHI", "CLUB", "KARAOKE", "LIVE"]
    NEON = [(1.0, 0.25, 0.6), (0.25, 0.95, 1.0), (0.7, 0.35, 1.0), (1.0, 0.75, 0.2), (0.3, 1.0, 0.5)]

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.road = int(h * 0.8)
        self.bchars = np.full((h, w), " ", dtype="<U1")
        self.brgb = np.zeros((h, w, 3))
        self.solid = np.zeros((h, w), bool)
        wins, self.signs, self.antennas = [], [], []
        for layer in (0, 1):
            x = -int(rng.integers(0, 6))
            while x < w:
                bw = int(rng.integers(6, 14) if layer == 0 else rng.integers(8, 20))
                lo, hi = (0.35, 0.75) if layer == 0 else (0.2, 0.6)
                bh = int(self.road * rng.uniform(lo, hi))
                top = self.road - bh
                a, b = max(0, x), min(w, x + bw)
                if a < b:
                    shade = (0.05, 0.05, 0.1) if layer == 0 else (0.08, 0.07, 0.13)
                    self.solid[top:self.road, a:b] = True
                    self.brgb[top:self.road, a:b] = shade
                    self.bchars[top:self.road, a:b] = " "
                    self.bchars[top, a:b] = "_"
                    self.brgb[top, a:b] = np.array(shade) * 3
                    sx = int(rng.integers(2, 4))
                    warm = rng.random() < 0.7
                    for wy in range(top + 2, self.road - 1, 2):
                        for wx in range(x + 1, x + bw - 1, sx):
                            if 0 <= wx < w:
                                wins.append((wy, wx, layer, warm))
                    if layer == 1 and rng.random() < 0.45:
                        text = str(rng.choice(self.SIGNS))
                        color = self.NEON[rng.integers(len(self.NEON))]
                        vertical = bw < len(text) + 2 or rng.random() < 0.4
                        self.signs.append((text, color, vertical, top + 2, x + bw // 2 if vertical else x + (bw - len(text)) // 2,
                                           rng.choice(("steady", "flicker", "chase")), rng.uniform(0, TAU)))
                    if bh > self.road * 0.6 and rng.random() < 0.6:
                        self.antennas.append((top, x + bw // 2))
                x += bw + int(rng.integers(0, 3))
        wins = np.array(wins) if wins else np.zeros((0, 4))
        self.wy, self.wx = wins[:, 0].astype(int), wins[:, 1].astype(int)
        self.wlayer, self.wwarm = wins[:, 2].astype(int), wins[:, 3].astype(bool)
        self.lit = rng.random(len(self.wy)) < 0.45
        lanes = [self.road + 2, self.road + 4]
        self.cars = [[float(rng.uniform(0, w)), lanes[i % 2], (1 if i % 2 else -1) * rng.uniform(10, 25),
                      self.NEON[rng.integers(len(self.NEON))]] for i in range(int(rng.integers(4, 8)))]
        n = w * h // 18
        self.rain = np.column_stack([rng.uniform(0, w, n), rng.uniform(0, h, n), rng.uniform(25, 45, n)])
        self.last_t = 0.0

    def frame(self, t):
        h, w, rng = self.h, self.w, self.rng
        Y = self.Y
        dt, self.last_t = max(0.0, min(0.2, t - self.last_t)), t
        rgb = mix(col(0.03, 0.01, 0.07), col(0.3, 0.08, 0.25), ((Y + 0.5) / 0.8) ** 2.2)
        chars = np.full((h, w), " ", dtype="<U1")
        bgc = rgb * 0.5
        s = self.solid
        chars[s] = self.bchars[s]
        rgb[s] = self.brgb[s]
        bgc[s] = self.brgb[s] * 0.8

        flip = rng.random(len(self.lit)) < dt * 0.04
        self.lit ^= flip
        warm = np.where(self.wwarm[:, None], np.array((1.0, 0.8, 0.4)), np.array((0.7, 0.85, 1.0)))
        dim = np.where(self.wlayer[:, None] == 0, 0.55, 1.0)
        on = self.lit
        chars[self.wy, self.wx] = np.where(on, "#", ".")
        rgb[self.wy, self.wx] = np.where(on[:, None], warm * dim, np.array((0.12, 0.12, 0.2)))
        bgc[self.wy[on], self.wx[on]] = (warm * dim * 0.35)[on]

        for top, x in self.antennas:
            for r in range(1, 4):
                if 0 <= top - r < h and 0 <= x < w:
                    chars[top - r, x] = "|"
                    rgb[top - r, x] = (0.3, 0.3, 0.35)
            if 0 <= top - 4 < h and 0 <= x < w and int(t * 1.2 + x) % 2 == 0:
                chars[top - 4, x] = "*"
                rgb[top - 4, x] = (1.0, 0.15, 0.15)
                bgc[top - 4, x] = (0.4, 0.0, 0.0)

        for text, color, vertical, row, x, mode, ph in self.signs:
            color = np.array(color)
            for i, ch in enumerate(text):
                y, cx = (row + i, x) if vertical else (row, x + i)
                if not (0 <= y < h and 0 <= cx < w):
                    continue
                if mode == "flicker":
                    a = 0.15 if math.sin(t * 23 + ph) * math.sin(t * 3.1 + ph) > 0.7 else 1.0
                elif mode == "chase":
                    a = 1.0 if (int(t * 6) - i) % len(text) < len(text) - 1 else 0.3
                else:
                    a = 0.85 + 0.15 * math.sin(t * 2 + ph)
                chars[y, cx] = ch
                rgb[y, cx] = np.clip(color * a + 0.25 * a, 0, 1)
                y0, y1, x0, x1 = max(0, y - 1), min(h, y + 2), max(0, cx - 2), min(w, cx + 3)
                bgc[y0:y1, x0:x1] = np.maximum(bgc[y0:y1, x0:x1], color * 0.32 * a)

        road = self.road
        rgb[road:] = (0.04, 0.04, 0.07)
        bgc[road:] = (0.03, 0.03, 0.05)
        chars[road] = "_"
        rgb[road] = (0.3, 0.3, 0.35)
        cols = np.arange(w)
        if road + 3 < h:
            chars[road + 3] = np.where((cols + int(t * 4)) % 6 < 3, "-", " ")
            rgb[road + 3] = (0.6, 0.55, 0.3)
        for r in range(road + 1, h):
            src = road - 1 - (r - road) * 2
            if src < 0:
                continue
            jitter = (cols + int(3 * math.sin(t * 4 + r))) % w
            refl = bgc[src, jitter] * 0.45 + rgb[src, jitter] * 0.1
            bgc[r] = np.maximum(bgc[r], refl)
            spark = (refl.max(axis=1) > 0.15) & (chars[r] == " ") & (rng.random(w) < 0.25)
            chars[r][spark] = "~"
            rgb[r][spark] = np.clip(refl[spark] * 2.2, 0, 1)

        for car in self.cars:
            car[0] += car[2] * dt
            if car[0] > w + 10:
                car[0] = -10
            elif car[0] < -10:
                car[0] = w + 10
            x, lane, v, color = car
            sprite = ["._/^^\\_.", "'-O--O-'"] if v > 0 else [".-/^^\\_.", "'-O--O-'"]
            cx = int(x)
            blit(chars, rgb, sprite, lane - 1, cx, lambda ch, r, c: np.array(color) * 0.8 if r == 0 else (0.5, 0.5, 0.55))
            front, back = (cx + 8, cx - 1) if v > 0 else (cx - 1, cx + 8)
            beam_dir = 1 if v > 0 else -1
            for k in range(1, 12):
                bx = front + beam_dir * (k - 1)
                if 0 <= bx < w and 0 <= lane < h:
                    bgc[lane, bx] = np.maximum(bgc[lane, bx], np.array((0.5, 0.48, 0.3)) * (1 - k / 12))
            if 0 <= back < w and 0 <= lane < h:
                chars[lane, back] = "o"
                rgb[lane, back] = (1.0, 0.1, 0.1)
                bgc[lane, back] = (0.35, 0.0, 0.0)

        r = self.rain
        r[:, 1] += r[:, 2] * dt
        r[:, 0] -= r[:, 2] * dt * 0.25
        wrap = r[:, 1] >= h
        r[wrap, 1] -= h
        r[wrap, 0] = rng.uniform(0, w + 10, int(wrap.sum()))
        rx, ry = r[:, 0].astype(int), r[:, 1].astype(int)
        ok = (rx >= 0) & (rx < w) & (ry >= 0) & (ry < h)
        chars[ry[ok], rx[ok]] = np.where(ry[ok] >= road, ".", "/")
        rgb[ry[ok], rx[ok]] = (0.45, 0.55, 0.75)
        return chars, rgb, bgc


class Piano(Scene):
    title = "Fur Elise  -  Ludwig van Beethoven, at the keys"
    # (eighth-note step, midi note, length in eighths, hand)
    SCORE = [(0, 76, 1, 1), (1, 75, 1, 1), (2, 76, 1, 1), (3, 75, 1, 1), (4, 76, 1, 1), (5, 71, 1, 1),
             (6, 74, 1, 1), (7, 72, 1, 1), (8, 69, 3, 1), (8, 45, 1, 0), (9, 52, 1, 0), (10, 57, 1, 0),
             (11, 60, 1, 1), (12, 64, 1, 1), (13, 69, 1, 1), (14, 71, 3, 1), (14, 40, 1, 0), (15, 52, 1, 0),
             (16, 56, 1, 0), (17, 64, 1, 1), (18, 68, 1, 1), (19, 71, 1, 1), (20, 72, 3, 1), (20, 45, 1, 0),
             (21, 52, 1, 0), (22, 57, 1, 0), (23, 64, 1, 1), (24, 76, 1, 1), (25, 75, 1, 1), (26, 76, 1, 1),
             (27, 75, 1, 1), (28, 76, 1, 1), (29, 71, 1, 1), (30, 74, 1, 1), (31, 72, 1, 1), (32, 69, 3, 1),
             (32, 45, 1, 0), (33, 52, 1, 0), (34, 57, 1, 0), (35, 60, 1, 1), (36, 64, 1, 1), (37, 69, 1, 1),
             (38, 71, 3, 1), (38, 40, 1, 0), (39, 52, 1, 0), (40, 56, 1, 0), (41, 64, 1, 1), (42, 72, 1, 1),
             (43, 71, 1, 1), (44, 69, 4, 1), (44, 45, 1, 0), (45, 52, 1, 0), (46, 57, 2, 0)]
    LOOP = 48
    STEP = 0.24
    LEAD = 3.0
    NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def __init__(self, w, h, rng):
        super().__init__(w, h, rng)
        self.lo, self.hi = 36, 84
        whites = [m for m in range(self.lo, self.hi + 1) if m % 12 in (0, 2, 4, 5, 7, 9, 11)]
        self.kw = max(2, w // len(whites))
        x0 = (w - self.kw * len(whites)) // 2
        self.keys = {}
        for i, m in enumerate(whites):
            self.keys[m] = (x0 + i * self.kw, self.kw, False)
            if m + 1 <= self.hi and (m + 1) % 12 in (1, 3, 6, 8, 10):
                bw = max(1, (self.kw * 2) // 3)
                self.keys[m + 1] = (x0 + (i + 1) * self.kw - bw // 2 - (bw % 2), bw, True)
        self.kb = h - max(6, int(h * 0.28))
        self.black_end = self.kb + int((h - self.kb) * 0.62)
        self.hands = [col(1.0, 0.45, 0.35), col(0.35, 0.8, 1.0)]
        if rng.random() < 0.5:
            self.hands = [col(0.75, 0.45, 1.0), col(0.4, 1.0, 0.6)]
        self.sparks = np.zeros((0, 5))
        self.last_t = 0.0

    def notes(self, t):
        loop = self.LOOP * self.STEP
        first = max(0, int((t - self.LEAD) / loop) - 1)
        for k in range(first, int(t / loop) + 2):
            for step, m, length, hand in self.SCORE:
                s = k * loop + step * self.STEP + self.LEAD
                yield s, s + length * self.STEP * 0.92, m, hand

    def frame(self, t):
        h, w = self.h, self.w
        dt, self.last_t = max(0.0, min(0.2, t - self.last_t)), t
        Y = self.Y
        chars = np.full((h, w), " ", dtype="<U1")
        rgb = np.full((h, w, 3), 0.3)
        bgc = mix(col(0.02, 0.01, 0.05), col(0.1, 0.04, 0.14), (Y + 0.5) * 1.3)
        kb = self.kb
        for m, (x, kw, black) in self.keys.items():
            if m % 12 == 0 and 0 <= x < w:
                chars[:kb, x] = ":"
                rgb[:kb, x] = (0.15, 0.12, 0.25)
        rps = kb / self.LEAD
        pressed = {}
        for s, e, m, hand in self.notes(t):
            if e < t or s - t > self.LEAD or m not in self.keys:
                continue
            x, kw, black = self.keys[m]
            yb = kb - (s - t) * rps
            yt = kb - (e - t) * rps
            r0, r1 = max(0, int(math.ceil(yt))), min(kb, int(yb))
            x0, x1 = max(0, x + (0 if black else 1)), min(w, x + kw - (0 if black else 1))
            color = self.hands[hand] * (0.75 if black else 1.0)
            if r0 < r1 and x0 < x1:
                bgc[r0:r1, x0:x1] = color * 0.85
                rgb[r0:r1, x0:x1] = np.clip(color + 0.4, 0, 1)
                chars[r0, x0:x1] = "_" if r0 > 0 else " "
                if r1 - r0 > 2 and x1 - x0 > 1:
                    chars[r1 - 1, x0] = "'"
            if s <= t < e:
                pressed[m] = color
                for _ in range(2):
                    self.sparks = np.vstack([self.sparks, [x + kw / 2 + self.rng.normal(0, 0.6), kb - 1,
                                                           self.rng.normal(0, 4), -self.rng.uniform(6, 14), hand]])
        hit = np.full(3, 0.35) * col(0.9, 0.3, 0.5)
        chars[kb - 1] = "="
        rgb[kb - 1] = (0.8, 0.3, 0.45)
        for m, (x, kw, black) in sorted(self.keys.items(), key=lambda kv: kv[1][2]):
            a, b = max(0, x), min(w, x + kw)
            if a >= b:
                continue
            if not black:
                face = pressed.get(m)
                key_bg = col(0.92, 0.91, 0.87) if face is None else mix(col(0.92, 0.91, 0.87), face, np.array(0.75))
                bgc[kb:, a:b] = key_bg
                rgb[kb:, a:b] = (0.45, 0.45, 0.45)
                chars[kb:, b - 1] = "|"
                if face is not None:
                    put_text(chars, rgb, h - 2, a + max(0, (kw - 2) // 2), self.NAMES[m % 12], (0.1, 0.1, 0.1))
                    bgc[kb - 1, a:b] = face
            else:
                face = pressed.get(m)
                bgc[kb:self.black_end, a:b] = col(0.05, 0.05, 0.07) if face is None else face * 0.8
                chars[kb:self.black_end, a:b] = " "
                if face is not None:
                    bgc[kb - 1, a:b] = face
        sp = self.sparks
        if len(sp):
            sp[:, 0] += sp[:, 2] * dt
            sp[:, 1] += sp[:, 3] * dt
            sp[:, 3] += dt * 9
            self.sparks = sp = sp[(sp[:, 1] > 0) & (sp[:, 1] < kb - 1)][-600:]
            sx, sy = sp[:, 0].astype(int), sp[:, 1].astype(int)
            ok = (sx >= 0) & (sx < w)
            hc = np.array(self.hands)[sp[:, 4].astype(int)]
            chars[sy[ok], sx[ok]] = np.where(sp[ok, 3] < 0, "*", ".")
            rgb[sy[ok], sx[ok]] = np.clip(hc[ok] + 0.3, 0, 1)
        return chars, rgb, bgc


class Blank(Scene):
    def frame(self, t):
        chars, rgb = self.canvas()
        return chars, rgb


SCENES = [Railway, Coding, Ocean, Space, NightSky, NightCity, Piano,
          StarryNight, GreatWave, Aurora, CherryBlossom, JuliaPortrait, SeveralCircles, LavaGarden]
TRANSITIONS = ("dissolve", "wipe", "iris", "diagonal", "blinds", "clock", "melt", "glitch", "ripple")
EDGE = 0.07
EDGE_CHARS = np.array(list("#%@&*+=$"))


def transition_field(kind, w, h, rng):
    """Per-cell reveal order in [0, 1]: cells with lower values switch first."""
    X, Y = grid(w, h)
    xn = (X - X.min()) / (X.max() - X.min() + 1e-9)
    yn = Y + 0.5
    noise = rng.random((h, w))
    if kind == "wipe":
        m = xn + 0.05 * noise
    elif kind == "iris":
        m = np.hypot(X, Y)
    elif kind == "diagonal":
        m = xn * 0.65 + yn * 0.35 + 0.04 * noise
    elif kind == "blinds":
        m = (np.arange(w) % 12)[None, :] / 12 * 0.75 + xn * 0.25
    elif kind == "clock":
        m = (np.arctan2(X, -Y) + math.pi) / TAU
    elif kind == "melt":
        m = rng.random(w)[None, :] * 0.55 + yn * 0.45
    elif kind == "glitch":
        bands = np.cumsum(rng.integers(1, 5, h))
        m = (hash01(bands * 1.0 + rng.random())[:, None] * 0.85 + noise * 0.15) * np.ones((1, w))
    elif kind == "ripple":
        m = np.hypot(X, Y) + 0.05 * np.sin(np.arctan2(Y, X) * 9)
    else:
        m = noise
    if kind in ("wipe", "iris", "diagonal", "melt") and rng.random() < 0.5:
        m = m.max() - m
    return (m - m.min()) / (m.max() - m.min() + 1e-9)


def caption(chars, rgb, text, alpha):
    h, w = chars.shape
    if alpha <= 0 or len(text) + 4 > w:
        return
    row, start = h - 2, w - len(text) - 3
    chars[row, start:start + len(text)] = list(text)
    rgb[row, start:start + len(text)] = 0.85 * alpha


def render(scene, t):
    out = scene.frame(t)
    if len(out) == 3:
        return out
    chars, rgb = out
    return chars, rgb, rgb * scene.bg


class Gallery:
    def __init__(self, w, h, now):
        self.w, self.h = w, h
        self.rng = np.random.default_rng()
        self.order = []
        self.last_cls = None
        # open with a transition from black
        self.current = Blank(w, h, self.rng)
        self.start = now - (SCENE_SECONDS - FADE_SECONDS)
        self.nxt = None

    def make(self):
        if not self.order:
            self.order = random.sample(SCENES, len(SCENES))
            if self.order[-1] is self.last_cls:
                self.order.reverse()
        cls = self.order.pop()
        self.last_cls = cls
        return cls(self.w, self.h, np.random.default_rng())

    def frame(self, now):
        t = now - self.start
        chars, rgb, bgc = render(self.current, t)
        fade_at = SCENE_SECONDS - FADE_SECONDS
        if t <= fade_at:
            alpha = min(1.0, (t - 1.0) / 1.5, (fade_at - 1.0 - t) / 1.5)
            caption(chars, rgb, self.current.title, alpha)
            return chars, rgb, bgc
        if self.nxt is None:
            self.nxt = self.make()
            self.kind = str(self.rng.choice(TRANSITIONS))
            self.field = transition_field(self.kind, self.w, self.h, self.rng)
        p = (t - fade_at) / FADE_SECONDS
        c2, rgb2, bg2 = render(self.nxt, t - fade_at)
        if self.kind == "glitch" and p < 0.8:
            rows = self.rng.random(self.h) < 0.25
            for r in np.nonzero(rows)[0]:
                k = int(self.rng.integers(-12, 13))
                chars[r], rgb[r], bgc[r] = np.roll(chars[r], k), np.roll(rgb[r], k, 0), np.roll(bgc[r], k, 0)
            rgb[rows] *= (1.0, 0.4, 1.0)
        q = p * (1 + EDGE)
        new = self.field < q - EDGE
        edge = (self.field >= q - EDGE) & (self.field < q)
        chars = np.where(new, c2, chars)
        rgb = np.where(new[..., None], rgb2, rgb)
        bgc = np.where(new[..., None], bg2, bgc)
        if p < 1:
            heat = 1 - (q - self.field[edge]) / EDGE
            chars[edge] = EDGE_CHARS[self.rng.integers(0, len(EDGE_CHARS), int(edge.sum()))]
            rgb[edge] = mix(col(0.4, 0.8, 1.0), col(1.0, 1.0, 1.0), heat)
            bgc[edge] = mix(col(0.05, 0.15, 0.3), col(0.25, 0.4, 0.6), heat)
        else:
            self.current, self.nxt = self.nxt, None
            self.start = now - (t - fade_at)
        return chars, rgb, bgc


ESC_CACHE = {}


def encode(chars, rgb, bgc):
    """Turn a frame into one terminal write, emitting color codes only on change."""
    fg = (np.clip(rgb, 0, 1) * 255).astype(np.int64) & 0xFC
    b = (np.clip(bgc, 0, 1) * 255).astype(np.int64) & 0xF8
    key = (((fg[..., 0] << 16) | (fg[..., 1] << 8) | fg[..., 2]) << 24) | (b[..., 0] << 16) | (b[..., 1] << 8) | b[..., 2]
    rows = []
    for crow, krow in zip(chars.tolist(), key.tolist()):
        parts, last = [], -1
        for ch, k in zip(crow, krow):
            if k != last:
                esc = ESC_CACHE.get(k)
                if esc is None:
                    f, bb = k >> 24, k & 0xFFFFFF
                    esc = (f"\x1b[38;2;{f >> 16};{(f >> 8) & 255};{f & 255};"
                           f"48;2;{bb >> 16};{(bb >> 8) & 255};{bb & 255}m")
                    if len(ESC_CACHE) < 200000:
                        ESC_CACHE[k] = esc
                parts.append(esc)
                last = k
            parts.append(ch)
        rows.append("".join(parts))
    return "\x1b[H" + "\r\n".join(rows)


def hypr_cursor(invisible):
    try:
        subprocess.run(["hyprctl", "keyword", "cursor:invisible", str(invisible).lower()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


def run():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("ascii-gallery: needs a terminal")
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    out = sys.stdout

    def on_term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    try:
        tty.setcbreak(fd)
        hypr_cursor(True)
        # alt screen, hide cursor, no autowrap, report all mouse motion, black background
        out.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1003h\x1b[?1006h\x1b]11;#000000\x07\x1b[2J")
        out.flush()
        launched = time.monotonic()
        size = os.get_terminal_size(out.fileno())
        gallery = Gallery(size.columns, size.lines, launched)
        while True:
            now = time.monotonic()
            size = os.get_terminal_size(out.fileno())
            if (size.columns, size.lines) != (gallery.w, gallery.h):
                gallery = Gallery(size.columns, size.lines, now)
            out.write(encode(*gallery.frame(now)))
            out.flush()
            wait = max(0.0, 1 / FPS - (time.monotonic() - now))
            if select.select([fd], [], [], wait)[0]:
                os.read(fd, 1024)
                if time.monotonic() - launched > GRACE_SECONDS:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[0m\x1b[?1003l\x1b[?1006l\x1b]111\x07\x1b[?7h\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        hypr_cursor(False)


def parse_size(arg):
    w, h = (int(v) for v in arg.lower().split("x"))
    return w, h


def preview(w, h, out_dir, t=9.0):
    """Render one frame of every scene to PNG (needs Pillow); handy for tuning."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default()
    for cls in SCENES:
        scene = cls(w, h, np.random.default_rng())
        for i in range(int(t * 6)):
            render(scene, i / 6)
        chars, rgb, bgc = render(scene, t)
        img = Image.new("RGB", (w * 6, h * 12))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            for x in range(w):
                b = tuple(int(v * 255) for v in np.clip(bgc[y, x], 0, 1))
                if any(b):
                    draw.rectangle((x * 6, y * 12, x * 6 + 5, y * 12 + 11), fill=b)
                draw.text((x * 6, y * 12), chars[y, x], font=font,
                          fill=tuple(int(v * 255) for v in np.clip(rgb[y, x], 0, 1)))
        img.save(os.path.join(out_dir, f"{cls.__name__}.png"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--dump":
        w, h = parse_size(sys.argv[2])
        for cls in SCENES:
            chars, _, _ = render(cls(w, h, np.random.default_rng()), 8.0)
            print(f"== {cls.__name__} ==")
            print("\n".join("".join(r).rstrip() for r in chars.tolist()))
    elif len(sys.argv) > 3 and sys.argv[1] == "--preview":
        preview(*parse_size(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 2 and sys.argv[1] == "--bench":
        w, h = parse_size(sys.argv[2])
        for cls in SCENES:
            scene = cls(w, h, np.random.default_rng())
            t0 = time.perf_counter()
            for i in range(10):
                encode(*render(scene, 5.0 + i / FPS))
            print(f"{cls.__name__:15s} {(time.perf_counter() - t0) * 100:6.1f} ms/frame")
    else:
        run()
