"""Ariel / faces - zapis, ulozeni, porovnani a mazani tvari.

Ukladaji se jen embeddingy (128 cisel z modelu SFace), sifrovane AES-256-GCM.
V rezimu "field" (venku) se nikdy neukladaji fotografie ani vyrezy.
Duplicity (stejna osoba vicekrat) se jen OZNACI ke kontrole clovekem, nic se nerozhoduje automaticky.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACES_DATA = ROOT / "data" / "faces"
FACES_MODELS = ROOT / "models" / "faces"
