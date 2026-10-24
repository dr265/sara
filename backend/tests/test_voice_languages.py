"""SARA Voice Agent — multilingual layer tests (13 languages, auto-detection)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.voice_languages import (  # noqa: E402
    DEFAULT_LANGUAGE,
    bilingual_greeting,
    detect_language,
    resolve_language,
    sarvam_tts_language,
    supported_language_list,
    twilio_gather_language,
)


def test_13_languages_registered():
    langs = supported_language_list()
    assert len(langs) == 13
    codes = {l["code"] for l in langs}
    for expected in (
        "en-IN", "hi-IN", "ta-IN", "te-IN", "bn-IN", "mr-IN", "gu-IN",
        "kn-IN", "ml-IN", "pa-IN", "od-IN", "as-IN", "ur-IN",
    ):
        assert expected in codes, expected


def test_english_is_default():
    assert DEFAULT_LANGUAGE == "en-IN"
    assert resolve_language(None).code == "en-IN"
    assert resolve_language("").code == "en-IN"
    assert resolve_language("klingon").code == "en-IN"


def test_script_detection_all_languages():
    cases = {
        "मैं आठ तारीख के लिए कमरा बुक करना चाहता हूँ": "hi-IN",
        "எனக்கு எட்டாம் தேதி வேண்டும்": "ta-IN",
        "నాకు ఎనిమిదవ తేదీ కావాలి": "te-IN",
        "আমি আঠো তারিখে রুম বুক করতে চাই": "bn-IN",
        "मला आठव्या तारखेला खोली हवी आहे": "mr-IN",
        "મને આઠ તારીખે રૂમ જોઈએ છે": "gu-IN",
        "ನನಗೆ ಎಂಟನೇ ದಿನಾಂಕದಂದು ಕೋಣೆ ಬೇಕು": "kn-IN",
        "എനിക്ക് എട്ടാം തീയതി വേണം": "ml-IN",
        "ਮੈਨੂੰ ਅੱਠ ਤਰੀਕ ਨੂੰ ਕਮਰਾ ਚਾਹੀਦਾ ਹੈ": "pa-IN",
        "ମୋର ଆଠ ତାରିଖ ଦରକାର": "od-IN",
        "মই আঠা তাৰিখে ৰুম লাগে": "as-IN",
        "مجھے آٹھ تاریخیں چاہیے": "ur-IN",
    }
    for text, expected in cases.items():
        d = detect_language(text)
        assert d.language == expected, f"{text[:20]} -> {d.language} != {expected}"
        assert d.confidence >= 0.55


def test_english_and_hinglish_detection():
    assert detect_language("I want to book a room for the 8th").language == "en-IN"
    # Hinglish (Latin script) maps to Hindi conversation
    d = detect_language("aap 8 tarikh ke liye room book karna hai")
    assert d.language == "hi-IN"


def test_switch_commands():
    assert detect_language("hindi mein baat karo").language == "hi-IN"
    assert detect_language("please talk in Tamil").language == "ta-IN"
    assert detect_language("తెలుగులో మాట్లాడండి", current="en-IN").language == "te-IN"
    assert detect_language("switch to Bengali").language == "bn-IN"


def test_midcall_switch_without_restart():
    # start English
    d1 = detect_language("I want to book for the 8th")
    assert d1.language == "en-IN" and not d1.changed
    # caller switches to Hindi mid-call
    d2 = detect_language("मुझे आठ तारीख के लिए बुकिंग चाहिए", current=d1.language)
    assert d2.language == "hi-IN" and d2.changed and d2.previous == "en-IN"
    # and back to English
    d3 = detect_language("ok now continue in English please", current="hi-IN")
    assert d3.language == "en-IN" and d3.changed


def test_low_confidence_keeps_current_language():
    # one stray Devanagari word inside an English sentence must NOT yank
    # the session language away mid-call
    d = detect_language("ok hai, so book it for the 8th", current="en-IN")
    assert d.language == "en-IN"


def test_tts_fallbacks_are_honest():
    # Urdu has no native Sarvam TTS -> Hindi voice, flagged not-native
    code, native = sarvam_tts_language("ur-IN")
    assert code == "hi-IN" and native is False
    # Assamese Twilio Gather falls back to hi-IN recognizer
    assert twilio_gather_language("as-IN") == "hi-IN"
    # Supported languages keep native codes
    assert sarvam_tts_language("ta-IN") == ("ta-IN", True)
    assert twilio_gather_language("ta-IN") == "ta-IN"


def test_resolve_aliases():
    assert resolve_language("tamil").code == "ta-IN"
    assert resolve_language("bangla").code == "bn-IN"
    assert resolve_language("hi").code == "hi-IN"
    assert resolve_language("हिन्दी").code == "hi-IN"


def test_bilingual_greeting_keeps_english_line():
    g = bilingual_greeting("नमस्ते! मैं सारा बोल रही हूँ।")
    assert g.endswith("How may I help you today?")
    assert g.startswith("नमस्ते!")
