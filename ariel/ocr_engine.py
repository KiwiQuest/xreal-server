"""OCR pro xreal-web.

Primarne OCR vestavene ve Windows (Windows.Media.Ocr pres balicky pywinrt):
rychle, offline, cestina s diakritikou (kdyz je ve Windows jazyk cestina).
Zaloha: RapidOCR (ONNX) - kdyz winrt neni; cesky diakritiku neumi dobre.

recognize(bgr, lang) -> {"engine", "lang", "ms", "lines": [{"text", "box": [x1,y1,x2,y2] 0..1,
                         "words": [{"text","box"}]}], "text"}
"""
import asyncio
import threading
import time

_lock = threading.Lock()
_engines = {}            # jazyk -> OcrEngine
_state = {"backend": None, "error": None, "langs": []}


# ── Windows.Media.Ocr ────────────────────────────────────────────────────────
def _win_import():
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter
    return OcrEngine, Language, SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode, DataWriter


def _win_langs():
    OcrEngine, *_ = _win_import()
    return [l.language_tag for l in OcrEngine.available_recognizer_languages]


def _win_engine(lang):
    OcrEngine, Language, *_ = _win_import()
    key = lang or "auto"
    if key not in _engines:
        eng = None
        if lang:
            try:
                eng = OcrEngine.try_create_from_language(Language(lang))
            except Exception:
                eng = None
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("Windows nema zadny jazyk pro OCR (Nastaveni > Cas a jazyk > Jazyk)")
        _engines[key] = eng
    return _engines[key]


def _win_recognize(bgr, lang):
    import cv2
    OcrEngine, Language, SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode, DataWriter = _win_import()
    eng = _win_engine(lang)
    h, w = bgr.shape[:2]
    mx = int(getattr(OcrEngine, "max_image_dimension", 2600) or 2600)
    if max(w, h) > mx:                                   # Windows OCR ma limit velikosti obrazku
        k = mx / max(w, h)
        bgr = cv2.resize(bgr, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        h, w = bgr.shape[:2]
    def bitmap(gray):
        dw = DataWriter()
        if gray:                                         # sedotonovy obrazek staci a je 4x mensi
            dw.write_bytes(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).tobytes())
            return SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.GRAY8, w, h)
        dw.write_bytes(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA).tobytes())
        return SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h,
                                                      BitmapAlphaMode.PREMULTIPLIED)

    async def run(sb):
        return await eng.recognize_async(sb)

    try:
        res = asyncio.run(run(bitmap(_state.get("gray", True))))
    except Exception:
        if not _state.get("gray", True):
            raise
        _state["gray"] = False                           # tahle verze Windows chce BGRA
        res = asyncio.run(run(bitmap(False)))
    lines = []
    for ln in res.lines:
        words = []
        for wd in ln.words:
            r = wd.bounding_rect
            words.append({"text": wd.text,
                          "box": [r.x / w, r.y / h, (r.x + r.width) / w, (r.y + r.height) / h]})
        if not words:
            continue
        box = [min(x["box"][0] for x in words), min(x["box"][1] for x in words),
               max(x["box"][2] for x in words), max(x["box"][3] for x in words)]
        lines.append({"text": ln.text, "box": box, "words": words})
    tag = eng.recognizer_language.language_tag
    return lines, tag


# ── RapidOCR (zaloha) ────────────────────────────────────────────────────────
_rapid = None


def _rapid_recognize(bgr):
    global _rapid
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    h, w = bgr.shape[:2]
    out, _ = _rapid(bgr)
    lines = []
    for pts, text, score in out or []:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = [min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h]
        lines.append({"text": text, "box": box, "words": [{"text": text, "box": box}], "conf": round(float(score), 2)})
    return lines, "rapidocr"


# ── predcitani nahlas ────────────────────────────────────────────────────────
# 1) hlasy Windows (OneCore, cesky "Jakub") pres winrt -> WAV -> winsound (do reproduktoru bryli,
#    kdyz jsou vychozi zvuk);  2) zaloha: PowerShell System.Speech (starsi hlasy SAPI5)
_tts = {"proc": None, "wav": None}


def _tts_winrt(text, lang):
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    from winrt.windows.storage.streams import DataReader
    synth = SpeechSynthesizer()
    voices = [v for v in SpeechSynthesizer.all_voices if v.language.lower().startswith(lang.lower())]
    if not voices and lang != "cs":
        voices = [v for v in SpeechSynthesizer.all_voices if v.language.lower().startswith("cs")]
    if voices:
        synth.voice = voices[0]

    async def run():
        st = await synth.synthesize_text_to_stream_async(text)
        size = int(st.size)
        rd = DataReader(st.get_input_stream_at(0))
        await rd.load_async(size)
        try:
            buf = bytearray(size)
            rd.read_bytes(buf)
            return bytes(buf)
        except Exception:
            return bytes(rd.read_buffer(size))

    return asyncio.run(run()), (voices[0].display_name if voices else "vychozi")


def speak(text, lang="cs"):
    """Precte text nahlas (neblokuje). Vraci nazev hlasu nebo zpusob."""
    import os
    import subprocess
    import sys
    import tempfile
    stop_speaking()
    text = (text or "").strip()[:4000]
    if not text:
        return None
    lang = (lang or "cs").split("-")[0]
    try:
        import winsound
        wav, voice = _tts_winrt(text, lang)
        path = os.path.join(tempfile.gettempdir(), "xreal_ocr_tts.wav")
        with open(path, "wb") as f:
            f.write(wav)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        _tts["wav"] = path
        return voice
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    if sys.platform != "win32":
        raise RuntimeError("predcitani jen ve Windows")
    ps = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; $t=[Console]::In.ReadToEnd(); "
          "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$v=$s.GetInstalledVoices()|?{{$_.VoiceInfo.Culture.Name -like '{lang}*'}}|select -First 1; "
          "if($v){$s.SelectVoice($v.VoiceInfo.Name)}; $s.Speak($t)")
    p = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
    p.stdin.write(text.encode("utf-8"))
    p.stdin.close()
    _tts["proc"] = p
    return f"System.Speech (winrt: {err[:80]})"


def stop_speaking():
    p = _tts.get("proc")
    if p and p.poll() is None:
        p.kill()
    _tts["proc"] = None
    if _tts.get("wav"):
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        _tts["wav"] = None


def copy_to_clipboard(text):
    import subprocess
    import sys
    if sys.platform != "win32":
        return False
    ps = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
    p = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
    p.communicate((text or "").encode("utf-8"), timeout=15)
    return p.returncode == 0


# ── verejne API ──────────────────────────────────────────────────────────────
def backend():
    """Ktery OCR je k dispozici: 'windows', 'rapidocr' nebo None (+ duvod v status())."""
    if _state["backend"] is None and _state["error"] is None:
        try:
            _state["langs"] = _win_langs()
            _state["backend"] = "windows"
        except Exception as e:
            win_err = f"{type(e).__name__}: {e}"
            try:
                import rapidocr_onnxruntime  # noqa: F401
                _state["backend"] = "rapidocr"
                _state["langs"] = ["rapidocr"]
            except Exception:
                _state["error"] = ("OCR neni nainstalovane - spust ocr_install.bat "
                                   f"(Windows OCR: {win_err})")
    return _state["backend"]


def status():
    backend()
    return {"backend": _state["backend"], "error": _state["error"], "langs": _state["langs"]}


def recognize(bgr, lang=None):
    """Precte text z obrazku (BGR numpy). Vlakno-bezpecne (jedno OCR naraz)."""
    be = backend()
    if be is None:
        raise RuntimeError(_state["error"])
    with _lock:
        t = time.time()
        if be == "windows":
            lines, tag = _win_recognize(bgr, lang)
        else:
            lines, tag = _rapid_recognize(bgr)
        ms = round((time.time() - t) * 1000, 1)
    for ln in lines:
        ln["box"] = [round(min(max(v, 0.0), 1.0), 4) for v in ln["box"]]
        for wd in ln.get("words", []):
            wd["box"] = [round(min(max(v, 0.0), 1.0), 4) for v in wd["box"]]
    return {"engine": be, "lang": tag, "ms": ms, "lines": lines,
            "text": "\n".join(l["text"] for l in lines)}
