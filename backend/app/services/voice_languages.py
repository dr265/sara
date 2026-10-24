"""SARA Voice Calling Agent — Indian-language support layer.

One table describes every language the voice agent can hold a real
conversation in: its Sarvam STT/TTS codes, its Twilio speech-recognition
locale, native and English names, and how to detect it from raw text.

Design rules (match the product requirements):

* English (en-IN) is the PRIMARY/DEFAULT language everywhere.
* Hindi is always supported.
* Automatic detection at call start: the first caller utterance is
  script-scanned; the detected language becomes the session language.
* Mid-call switching: every turn is re-checked — a caller who switches from
  Hindi to Tamil is followed instantly, without restarting the call.
* Explicit switch commands ("talk in Tamil", "हिंदी में बात करो", "தமிழில்
  பேசவும்") always win over script detection.
* Low confidence / unsupported language -> graceful fallback to English and
  the caller is asked which language they prefer.
* No fake translations: the pipeline is Sarvam saaras STT (auto language id),
  the LLM replying in the session language, and Sarvam bulbul TTS per
  language. Urdu TTS is not offered by Sarvam bulbul — it degrades honestly
  to Hindi TTS (mutually intelligible in spoken form) and this is marked in
  the table so logs/diagnostics never pretend it is native Urdu audio.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("sara.voice.languages")


# ---------------------------------------------------------------------------
# Language table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LanguageInfo:
    code: str                      # canonical Sarvam BCP-47 code, e.g. "hi-IN"
    name: str                      # English name
    native_name: str               # endonym, for switch commands / UI
    twilio_language: Optional[str] # Twilio <Gather language=...> (None -> fallback)
    tts_language: str              # Sarvam bulbul target_language_code
    tts_native: bool = True        # False -> TTS falls back (honest degradation)
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def short(self) -> str:
        return self.code.split("-", 1)[0].lower()


SUPPORTED_LANGUAGES: Dict[str, LanguageInfo] = {
    info.code: info
    for info in [
        LanguageInfo("en-IN", "English", "English", "en-IN", "en-IN",
                     aliases=("english", "angrezi", "inglish")),
        LanguageInfo("hi-IN", "Hindi", "हिन्दी", "hi-IN", "hi-IN",
                     aliases=("hindi", "hnd")),
        LanguageInfo("ta-IN", "Tamil", "தமிழ்", "ta-IN", "ta-IN",
                     aliases=("tamil", "tamizh")),
        LanguageInfo("te-IN", "Telugu", "తెలుగు", "te-IN", "te-IN",
                     aliases=("telugu", "telgu")),
        LanguageInfo("bn-IN", "Bengali", "বাংলা", "bn-IN", "bn-IN",
                     aliases=("bengali", "bangla")),
        LanguageInfo("mr-IN", "Marathi", "मराठी", "mr-IN", "mr-IN",
                     aliases=("marathi",)),
        LanguageInfo("gu-IN", "Gujarati", "ગુજરાતી", "gu-IN", "gu-IN",
                     aliases=("gujarati",)),
        LanguageInfo("kn-IN", "Kannada", "ಕನ್ನಡ", "kn-IN", "kn-IN",
                     aliases=("kannada",)),
        LanguageInfo("ml-IN", "Malayalam", "മലയാളം", "ml-IN", "ml-IN",
                     aliases=("malayalam",)),
        LanguageInfo("pa-IN", "Punjabi", "ਪੰਜਾਬੀ", "pa-Guru-IN", "pa-IN",
                     aliases=("punjabi", "panjabi")),
        LanguageInfo("od-IN", "Odia", "ଓଡ଼ିଆ", "or-IN", "od-IN",
                     aliases=("odia", "oriya")),
        # Assamese: Sarvam saaras STT understands it; Twilio has no as-IN
        # speech locale, so the phone Gather falls back to hi-IN.
        LanguageInfo("as-IN", "Assamese", "অসমীয়া", None, "as-IN",
                     aliases=("assamese",)),
        # Urdu: saaras STT + mayura translate yes; bulbul TTS no -> spoken
        # fallback to Hindi TTS is marked with tts_native=False.
        LanguageInfo("ur-IN", "Urdu", "اردو", "ur-PK", "hi-IN", tts_native=False,
                     aliases=("urdu",)),
    ]
}

DEFAULT_LANGUAGE = os.getenv("VOICE_AGENT_LANGUAGE", "en-IN")
if DEFAULT_LANGUAGE not in SUPPORTED_LANGUAGES:
    DEFAULT_LANGUAGE = "en-IN"

# Languages offered by Sarvam bulbul TTS (everything else falls back).
SARVAM_TTS_CODES = {
    "en-IN", "hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN",
    "mr-IN", "kn-IN", "ml-IN", "od-IN", "pa-IN", "as-IN",
}


def resolve_language(value: Optional[str]) -> LanguageInfo:
    """Resolve any user/config hint (code, name, native name) to a language.

    Unknown/empty -> English (the default), per the fallback requirement.
    """
    if not value:
        return SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE]
    v = str(value).strip().lower()
    if v in SUPPORTED_LANGUAGES:
        return SUPPORTED_LANGUAGES[v]
    short = v.split("-", 1)[0]
    for info in SUPPORTED_LANGUAGES.values():
        if info.short == short:
            return info
    for info in SUPPORTED_LANGUAGES.values():
        if v in info.aliases or v == info.name.lower() or v == info.native_name.lower():
            return info
    return SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE]


def is_supported_language(value: Optional[str]) -> bool:
    """True only for an exact code we can genuinely hold a call in."""
    if not value:
        return False
    v = str(value).strip().lower()
    if v in SUPPORTED_LANGUAGES:
        return True
    short = v.split("-", 1)[0]
    return any(info.short == short for info in SUPPORTED_LANGUAGES.values())


# ---------------------------------------------------------------------------
# Script detection (no network, no cost — runs on every turn)
# ---------------------------------------------------------------------------

_SCRIPT_RANGES: List[Tuple[str, str, re.Pattern]] = [
    # (script, default language, unicode-block regex)
    ("devanagari", "hi-IN", re.compile(r"[\u0900-\u097F]")),
    ("bengali", "bn-IN", re.compile(r"[\u0980-\u09FF]")),
    ("gurmukhi", "pa-IN", re.compile(r"[\u0A00-\u0A7F]")),
    ("gujarati", "gu-IN", re.compile(r"[\u0A80-\u0AFF]")),
    ("oriya", "od-IN", re.compile(r"[\u0B00-\u0B7F]")),
    ("tamil", "ta-IN", re.compile(r"[\u0B80-\u0BFF]")),
    ("telugu", "te-IN", re.compile(r"[\u0C00-\u0C7F]")),
    ("kannada", "kn-IN", re.compile(r"[\u0C80-\u0CFF]")),
    ("malayalam", "ml-IN", re.compile(r"[\u0D00-\u0D7F]")),
    ("arabic", "ur-IN", re.compile(r"[\u0600-\u06FF\u0750-\u077F]")),
]

# Devanagari is shared by Hindi and Marathi — these高频 markers split them.
_MARATHI_MARKERS = re.compile(
    r"(आहे|आहेत|मला|तुम्ही|तू काय|काय|नको|पण|आणि|किती|जमले|हवे|हवं|आहे का)"
)
_HINDI_MARKERS = re.compile(
    r"(है|हैं|क्या|कैसे|मुझे|आप|नहीं|और|कितना|चाहिए|करना|सकते|बोलिए)"
)

# Bengali script is shared by Bengali and Assamese — Assamese has ৰ (09F0) ৱ (09F1).
_ASSAMESE_CHARS = re.compile(r"[\u09F0\u09F1]")

# Latin-script Hinglish cue words -> Hindi conversation.
_HINGLISH_HINT = re.compile(
    r"\b(aap|aapka|kya|kaise|karna|chahiye|nahi|nahin|hai|hain|mujhe|kitna|"
    r"kitne|book\s*karna|karna\s*hai|krna)\b",
    re.IGNORECASE,
)


def detect_language_from_script(text: str) -> Tuple[Optional[str], float]:
    """Detect the language of `text` from its Unicode script.

    Returns (language_code_or_None, confidence 0..1). None means Latin
    script / no confident script signal — the caller treats it as English
    unless Hinglish cues say otherwise.
    """
    text = (text or "").strip()
    if not text:
        return None, 0.0

    counts = {name: len(pat.findall(text)) for name, _, pat in _SCRIPT_RANGES}
    best_script, best_count = max(counts.items(), key=lambda kv: kv[1])
    letters = len(re.sub(r"[^\w\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF"
                         r"\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF"
                         r"\u0D00-\u0D7F\u0600-\u06FF]", "", text)) or 1
    if best_count == 0:
        # Latin script — Hinglish detection. Density matters: a genuinely
        # Hinglish sentence carries several cue words; a single stray "hai"
        # inside an English sentence must not flip the session language.
        hits = len(_HINGLISH_HINT.findall(text))
        if hits >= 3:
            return "hi-IN", 0.72
        if hits == 2:
            return "hi-IN", 0.6
        if hits == 1:
            return "hi-IN", 0.5
        return None, 0.0

    confidence = min(0.95, 0.55 + 0.4 * (best_count / max(1, letters)))

    if best_script == "devanagari":
        mar = len(_MARATHI_MARKERS.findall(text))
        hin = len(_HINDI_MARKERS.findall(text))
        if mar > hin:
            return "mr-IN", confidence
        return "hi-IN", confidence
    if best_script == "bengali":
        if _ASSAMESE_CHARS.search(text):
            return "as-IN", confidence
        return "bn-IN", confidence
    if best_script == "arabic":
        # Urdu vs other Arabic-script languages: within our table only Urdu,
        # but Nastaliq words reinforce it.
        return "ur-IN", min(0.85, confidence)

    for name, code, _pat in _SCRIPT_RANGES:
        if name == best_script:
            return code, confidence
    return None, 0.0  # pragma: no cover


# ---------------------------------------------------------------------------
# Explicit switch commands ("talk to me in Tamil", "हिंदी में बात करो"...)
# ---------------------------------------------------------------------------

_SWITCH_PATTERNS = [
    re.compile(
        r"(talk|speak|continue|switch|baat|baat karein|bolo|boliye|pesa|pesu|"
        r"matlab|prefer)\D{0,12}(in|into|with|me|mein|lo|do|karo|karein)?\s*"
        r"(?P<lang>[A-Za-z\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF"
        r"\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]+)",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<lang>english|hindi|tamil|telugu|bengali|bangla|marathi|"
               r"gujarati|kannada|malayalam|punjabi|odia|oriya|assamese|urdu)",
               re.IGNORECASE),
]


def detect_switch_command(text: str) -> Optional[str]:
    """Return a language code if the caller explicitly asked to switch."""
    text = (text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for info in SUPPORTED_LANGUAGES.values():
        for alias in (info.name.lower(),) + info.aliases + (info.native_name,):
            if alias and alias in lowered:
                return info.code
    return None


# ---------------------------------------------------------------------------
# Per-turn detector used by the live call loop
# ---------------------------------------------------------------------------

@dataclass
class LanguageDecision:
    language: str          # canonical code now in force
    changed: bool          # True when the session language switched
    confidence: float
    source: str            # "switch_command" | "script" | "hinglish" | "kept" | "fallback"
    previous: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "language": self.language,
            "changed": self.changed,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "previous": self.previous,
        }


def detect_language(
    text: str,
    *,
    current: Optional[str] = None,
    threshold: float = 0.55,
) -> LanguageDecision:
    """Decide the language for this turn.

    Order: explicit switch command > script detection > keep current >
    English fallback. A low-confidence script guess never yanks the session
    away from an already-established language mid-call.
    """
    current_info = resolve_language(current) if current else resolve_language(None)
    current_code = current_info.code

    # 1) Explicit command wins outright.
    switch = detect_switch_command(text)
    if switch:
        return LanguageDecision(
            language=switch,
            changed=switch != current_code,
            confidence=1.0,
            source="switch_command",
            previous=current_code if switch != current_code else None,
        )

    # 2) Script detection on this turn's text.
    code, conf = detect_language_from_script(text)
    if code and conf >= threshold:
        if code != current_code:
            # A mid-call AWAY from an established non-English language needs a
            # STRONG signal (whole turn in the new language). From English (the
            # default), a normal first-turn signal is accepted — otherwise the
            # Hindi caller who starts "aap 8 tarikh..." would never be picked
            # up. Stability and pickup are both preserved.
            established = current_code and current_code != DEFAULT_LANGUAGE
            if established and conf < 0.7:
                return LanguageDecision(current_code, False, conf, "kept")
            return LanguageDecision(code, True, conf, "script", previous=current_code)
        return LanguageDecision(code, False, conf, "script")

    if code and code == current_code:
        return LanguageDecision(code, False, conf, "script")

    # 3) Nothing confident: keep the current language, else English fallback.
    if code is None and conf == 0.0 and not (text or "").strip():
        return LanguageDecision(current_code, False, 0.0, "kept")
    return LanguageDecision(current_code, False, max(conf, 0.0), "kept" if current_code else "fallback")


def twilio_gather_language(code: Optional[str]) -> str:
    """Twilio <Gather language=...> for a session language (with fallback)."""
    info = resolve_language(code)
    return info.twilio_language or "hi-IN"


def sarvam_tts_language(code: Optional[str]) -> Tuple[str, bool]:
    """(sarvam TTS code, is_native_tts) — non-native falls back honestly."""
    info = resolve_language(code)
    if info.tts_language in SARVAM_TTS_CODES:
        return info.tts_language, info.tts_native
    return "en-IN", False


def supported_language_list() -> List[Dict[str, object]]:
    """For GET /voice/languages and the frontend picker."""
    out: List[Dict[str, object]] = []
    for code, info in SUPPORTED_LANGUAGES.items():
        out.append({
            "code": code,
            "name": info.name,
            "native_name": info.native_name,
            "tts_native": info.tts_native,
            "phone_supported": info.twilio_language is not None,
            "is_default": code == DEFAULT_LANGUAGE,
        })
    return out


def bilingual_greeting(persona_greeting: str) -> str:
    """Phone greeting: persona language first, then the English line.

    The persona's own greeting (Hindi by default) plays first so the caller
    immediately knows who SARA is; the English line follows so an
    English-preferring caller can simply answer in English and the
    auto-detector follows them from the first turn.
    """
    greeting = (persona_greeting or "").strip()
    if not greeting:
        return "Hello! This is SARA. How may I help you today?"
    code, _ = detect_language_from_script(greeting)
    if code and code != "en-IN":
        return f"{greeting} How may I help you today?"
    return greeting
