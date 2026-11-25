"""Circle click (klikani po zastaveni): IDLE -> DWELL (kruzek se plni) -> klik -> COOLDOWN."""
import math

START_DELAY = 0.20      # jak dlouho musi byt hlava pomala, nez se kruzek objevi (s)


class Dwell:
    def __init__(self):
        self.reset()

    def reset(self):
        self.state = "IDLE"
        self.still = 0.0
        self.progress = 0.0
        self.dx = self.dy = 0.0

    @property
    def active(self) -> bool:
        return self.state == "DWELL"

    def block(self):
        """Tlacitko na bryli se pouziva - dwell pocka, dokud hlavou znovu nepohnes."""
        self.state, self.progress = "COOLDOWN", 0.0

    def update(self, cfg, dt: float, speed: float, px: float, py: float) -> bool:
        """px, py = pohyb kurzoru v tomto vzorku. Vraci True pri kliku."""
        cancel = max(cfg.cancel_speed, cfg.start_speed + 1.0)
        if self.state == "IDLE":
            if speed < cfg.start_speed:
                self.still += dt
                if self.still >= START_DELAY:
                    self.state, self.progress = "DWELL", 0.0
                    self.dx = self.dy = 0.0
            else:
                self.still = 0.0
        elif self.state == "DWELL":
            self.dx += px
            self.dy += py
            if speed > cancel or math.hypot(self.dx, self.dy) > cfg.max_drift:
                self.reset()
                return False
            self.progress += dt / max(0.1, cfg.dwell_time)
            if self.progress >= 1.0:
                self.state, self.progress = "COOLDOWN", 0.0
                return True
        elif self.state == "COOLDOWN":
            if speed > cancel:
                self.reset()
        return False
