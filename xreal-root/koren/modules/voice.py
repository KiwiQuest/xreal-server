"""Modul: kořen mluví (Windows hlas přes System.Speech). Vypnutý ve výchozím stavu.

Hodí se, když brýle zrovna nejsou na očích: notebook v tašce řekne, že čeká na brýle a
za jak dlouho hibernuje. Hlášky jdou do jednoho pomalého vlákna, ať nic nebrzdí.
"""
import queue
import subprocess
import threading

from . import Module


class Voice(Module):
    id = "voice"
    name = "Hlas"
    description = "Kořen klidně řekne, co se děje a jaké má uživatel možnosti."
    z = 0

    def start(self, ctx):
        super().start(ctx)
        self.q = queue.Queue(maxsize=4)
        self.running = True
        threading.Thread(target=self._run, name="voice", daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def speak(self, text: str):
        try:
            self.q.put_nowait(text)
        except queue.Full:                      # radši zahodit, než se opožďovat
            pass

    def _run(self):
        while self.running:
            text = self.q.get()
            if not text:
                continue
            safe = text.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.Rate = -1; $s.Speak('{safe}')")
            try:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               timeout=30, capture_output=True, creationflags=0x08000000)
            except Exception:
                pass


MODULE = Voice
