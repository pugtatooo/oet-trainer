#!/usr/bin/env python3
"""OET Trainer - Daily OET 365+ preparation system for nursing professionals"""

import json
import os
import sys
import webbrowser
import threading
import time
import requests
from datetime import date, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_file, render_template_string
import anthropic

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
PROGRESS_FILE = BASE_DIR / "progress.json"
AUDIO_FILE = BASE_DIR / "lesson_audio.mp3"
LESSON_CACHE = BASE_DIR / "today_lesson.json"

LANGUAGES = {
    "zh-TW": {"name": "繁體中文", "flag": "🇹🇼", "prompt": "Traditional Chinese (Taiwan)"},
    "zh-CN": {"name": "简体中文", "flag": "🇨🇳", "prompt": "Simplified Chinese"},
    "ja":    {"name": "日本語",   "flag": "🇯🇵", "prompt": "Japanese"},
    "ko":    {"name": "한국어",   "flag": "🇰🇷", "prompt": "Korean"},
    "th":    {"name": "ภาษาไทย", "flag": "🇹🇭", "prompt": "Thai"},
    "vi":    {"name": "Tiếng Việt","flag":"🇻🇳", "prompt": "Vietnamese"},
    "id":    {"name": "Bahasa Indonesia","flag":"🇮🇩","prompt": "Indonesian"},
}

app = Flask(__name__)
_config = None

# ─── Config & Progress ────────────────────────────────────────────────────────

def get_config():
    global _config
    if not _config:
        if CONFIG_FILE.exists():
            _config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        else:
            _config = {
                "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "elevenlabs_api_key": os.environ.get("ELEVENLABS_API_KEY", ""),
                "elevenlabs_voice_id": os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),
                "elevenlabs_model": os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
                "daily_minutes": 30,
                "target_score": 365,
                "exam_date": None
            }
    return _config

DEFAULT_PROGRESS = {
    "start_date": None, "current_day": 1, "streak": 0,
    "total_completed": 0, "last_session": None,
    "completed_dates": [], "missed_dates": [], "weak_areas": ["speaking", "writing"]
}

def load_p():
    if not PROGRESS_FILE.exists():
        return dict(DEFAULT_PROGRESS)
    return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))

def save_p(p):
    PROGRESS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")

def init_p(p):
    if not p.get("start_date"):
        p["start_date"] = date.today().isoformat()
        p["current_day"] = 1
        save_p(p)

def check_missed(p):
    if not p.get("last_session"):
        return []
    last = date.fromisoformat(p["last_session"])
    missed = []
    for i in range(1, (date.today() - last).days):
        d = (last + timedelta(days=i)).isoformat()
        if d not in p["completed_dates"] and d not in p["missed_dates"]:
            p["missed_dates"].append(d)
            missed.append(d)
    if missed:
        save_p(p)
    return missed

def get_phase(day):
    if day <= 90:
        return 1
    if day <= 180:
        return 2
    return 3

# ─── AI Services ──────────────────────────────────────────────────────────────

def generate_lesson(p, lang="zh-TW"):
    today = date.today().isoformat()
    cache_file = BASE_DIR / f"today_lesson_{lang}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("date") == today:
            return cached

    day = max(p.get("current_day", 1), 1)
    ph = get_phase(day)
    lang_info = LANGUAGES.get(lang, LANGUAGES["zh-TW"])
    native = lang_info["prompt"]
    phases = {
        1: "Foundation: nursing vocabulary, OET format introduction, basic referral letter structure",
        2: "Core Skills: patient consultation role-plays, complete referral letters, clinical listening",
        3: "Exam Mode: timed mock tests, accent refinement, targeting weak areas"
    }

    prompt = f"""You are a senior OET examiner and trainer with 10+ years' experience preparing nurses for Band B (350+).
Create a 30-minute daily lesson. Student: nurse with B2 English targeting OET 365+.
Native language: {native}. Write ALL explanations, tips, feedback, encouragement in {native}.

Day {day} / 270 | Phase {ph}: {phases[ph]}
Weak areas: {p.get('weak_areas', ['speaking', 'writing'])}

OET DESIGN RULES (follow strictly):
- Vocabulary: real clinical nursing terms used in OET sub-tests
- Listening: must be a nurse–patient or nurse–carer consultation (not general conversation). Vary scenario types: history-taking, discharge instruction, medication explanation, patient concern.
- Speaking: OET role-play format — nurse-initiated interaction with a patient or carer. Task must require the nurse to explain, clarify, reassure, or elicit history. key_phrases must be clinically appropriate.
- Reading: OET Part C style — professional healthcare passage, mix question types: main idea, vocabulary-in-context, inference/detail.
- Writing: always OET referral letter style. tip must reference an actual OET writing criterion (purpose, content, conciseness, register, layout).

Return ONLY a valid JSON object — no markdown, no extra text:
{{
  "date": "{today}",
  "day": {day},
  "phase": {ph},
  "encouragement": "1 motivating sentence in {native} for Day {day}",
  "vocabulary": [
    {{
      "word": "clinical nursing term",
      "ipa": "/IPA/",
      "native": "translation in {native}",
      "example": "1 sentence showing clinical use",
      "tip": "memory tip or common error — in {native}"
    }},
    {{"word": "term 2","ipa": "/IPA/","native": "translation in {native}","example": "clinical sentence","tip": "tip in {native}"}},
    {{"word": "term 3","ipa": "/IPA/","native": "translation in {native}","example": "clinical sentence","tip": "tip in {native}"}}
  ],
  "listening": {{
    "scenario": "One-sentence OET-style clinical scenario",
    "dialogue": [
      {{"speaker": "Nurse", "text": "Opening — greeting or initial assessment"}},
      {{"speaker": "Patient", "text": "Patient response with relevant clinical info"}},
      {{"speaker": "Nurse", "text": "Follow-up question"}},
      {{"speaker": "Patient", "text": "More clinical detail"}},
      {{"speaker": "Nurse", "text": "Clarification or instruction"}},
      {{"speaker": "Patient", "text": "Patient concern or question"}},
      {{"speaker": "Nurse", "text": "Reassurance or closing plan"}}
    ],
    "questions": [
      {{"q": "Detail/fact question", "options": ["A. …","B. …","C. …","D. …"], "answer": "A", "explanation": "reason in {native}"}},
      {{"q": "Inference or implication question", "options": ["A. …","B. …","C. …","D. …"], "answer": "B", "explanation": "reason in {native}"}},
      {{"q": "Nurse's purpose or communication strategy question", "options": ["A. …","B. …","C. …","D. …"], "answer": "C", "explanation": "reason in {native}"}}
    ]
  }},
  "speaking": {{
    "scenario": "OET role-play: patient name, age, presenting issue, relationship to nurse (e.g. post-op ward, discharge)",
    "task": "Nurse's communication task: what to explain/elicit/reassure — written in English",
    "sample": "3-4 sentence model answer showing OET B-level clinical language",
    "key_phrases": ["clinical phrase 1", "clinical phrase 2", "clinical phrase 3", "clinical phrase 4"],
    "watch_out": "One specific error nurses make in this scenario type — in {native}"
  }},
  "reading": {{
    "title": "Professional clinical article title",
    "article": "6-8 sentence OET Part C style passage. Contains at least 2 vocabulary words. B-level register.",
    "questions": [
      {{"q": "Main idea or purpose of the passage", "options": ["A. …","B. …","C. …","D. …"], "answer": "A", "explanation": "in {native}"}},
      {{"q": "Vocabulary-in-context: what does [word] mean here?", "options": ["A. …","B. …","C. …","D. …"], "answer": "B", "explanation": "in {native}"}},
      {{"q": "Inference question — what can be inferred?", "options": ["A. …","B. …","C. …","D. …"], "answer": "C", "explanation": "in {native}"}}
    ]
  }},
  "writing": {{
    "tip": "Specific OET referral letter criterion tip (purpose/content/conciseness/register/layout)",
    "before": "Weak non-OET sentence from a student",
    "after": "OET Band B rewrite of the same sentence",
    "task": "Write [specific referral letter section] for this patient: [brief clinical scenario with relevant details]"
  }}
}}"""

    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4200,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()
    if "```" in text:
        parts = text.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 1:
                text = part.lstrip("json").strip()
                break

    lesson = json.loads(text)
    cache_file.write_text(json.dumps(lesson, indent=2, ensure_ascii=False), encoding="utf-8")
    return lesson

def generate_audio(text):
    c = get_config()
    voice_id = c.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": c["elevenlabs_api_key"]
        },
        json={
            "text": text,
            "model_id": c.get("elevenlabs_model", "eleven_turbo_v2_5"),
            "voice_settings": {"stability": 0.6, "similarity_boost": 0.75}
        },
        timeout=30
    )
    print(f"[ElevenLabs] status={resp.status_code} size={len(resp.content)}", flush=True)
    if resp.status_code == 200:
        AUDIO_FILE.write_bytes(resp.content)
        return True, None
    err = resp.text[:300]
    print(f"[ElevenLabs] Error body: {err}", flush=True)
    return False, f"status={resp.status_code} {err}"

def evaluate_speaking(spoken, scenario, sample, lang="zh-TW"):
    native = LANGUAGES.get(lang, LANGUAGES["zh-TW"])["prompt"]
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": f"""You are a senior OET speaking examiner. Evaluate using OET's 4 official criteria.
Student's native language: {native}. Write ALL feedback in {native}.

Scenario: {scenario}
Model answer: {sample}
Student said: {spoken}

OET Speaking Scoring (be encouraging but honest):
1 = Unintelligible / completely off-topic
2 = Partially understood, major clinical communication failure
3 = PASSING — covers the clinical task, understandable despite errors
4 = Good — clear clinical communication, minor language errors only
5 = Excellent — natural, all OET criteria met

OET criteria to assess:
- Intelligibility: pronunciation & stress
- Fluency: natural pacing, minimal hesitation
- Appropriateness: correct clinical register
- Grammar/Vocabulary: clinical terms used correctly

If the student addressed the clinical scenario at all, give AT LEAST a 3. Be specific and kind.

Return ONLY JSON (no markdown):
{{"score": 3, "good": "specific OET-criteria praise in {native}", "improve": "ONE concrete OET fix in {native}", "vocabulary": "a better clinical English phrase they should use (show exact English)", "oet_tip": "one practical OET exam strategy for this scenario type in {native}"}}"""}]
    )
    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    return r

@app.route("/")
def index():
    p = load_p()
    init_p(p)
    missed = check_missed(p)
    today = date.today().isoformat()
    done = today in p.get("completed_dates", [])
    tired = today in p.get("missed_dates", [])
    day = p.get("current_day", 1)
    ph = get_phase(day)
    phase_name = {1: "Foundation", 2: "Core Skills", 3: "Exam Mode"}[ph]
    pct = round(day / 270 * 100, 1)
    return render_template_string(
        HTML,
        streak=p.get("streak", 0),
        day=day,
        phase=ph,
        phase_name=phase_name,
        total=p.get("total_completed", 0),
        pct=pct,
        missed=missed,
        done=done,
        tired=tired,
    )

@app.route("/api/lesson")
def api_lesson():
    lang = request.args.get("lang", "zh-TW")
    if lang not in LANGUAGES:
        lang = "zh-TW"
    return jsonify(generate_lesson(load_p(), lang))

@app.route("/api/audio", methods=["POST"])
def api_audio():
    log = BASE_DIR / "audio_debug.log"
    try:
        data = request.json
        text = data.get("text", "") if data else ""
        log.write_text(f"request_json={data}\ntext={text[:50]}\n", encoding="utf-8")
        ok, err = generate_audio(text)
        log.write_text(log.read_text() + f"ok={ok} err={err}\n", encoding="utf-8")
        return jsonify({"ok": ok, "error": err})
    except Exception as e:
        log.write_text(log.read_text(encoding="utf-8") + f"exception={type(e).__name__}: {e}\n", encoding="utf-8")
        return jsonify({"ok": False, "error": str(e)})

@app.route("/audio")
def serve_audio():
    if AUDIO_FILE.exists():
        return send_file(AUDIO_FILE, mimetype="audio/mpeg")
    return "", 404

@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    d = request.json
    lang = d.get("lang", "zh-TW")
    return jsonify(evaluate_speaking(d["spoken"], d["scenario"], d["sample"], lang))

@app.route("/api/evaluate-writing", methods=["POST"])
def api_evaluate_writing():
    d = request.json
    lang = d.get("lang", "zh-TW")
    native = LANGUAGES.get(lang, LANGUAGES["zh-TW"])["prompt"]
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""You are an OET writing examiner for nurses.
The student's native language is {native}. Write ALL feedback in {native}.
Task: {d['task']}
Tip: {d['tip']}
Student wrote: {d['answer']}

Return ONLY JSON (no markdown):
{{"score": 1, "oet_level": "Below B / B / Above B", "grammar": "grammar feedback in {native}", "vocabulary": "vocabulary feedback in {native}", "structure": "structure feedback in {native}", "rewrite": "improved version in English", "summary": "one-line comment in {native}"}}
Score 1-5 where 5 is OET Band B."""}]
    )
    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    return jsonify(json.loads(text))

@app.route("/api/complete", methods=["POST"])
def api_complete():
    p = load_p()
    today = date.today().isoformat()
    if today not in p["completed_dates"]:
        p["completed_dates"].append(today)
        p["current_day"] += 1
        p["streak"] += 1
        p["total_completed"] += 1
        p["last_session"] = today
        save_p(p)
    return jsonify({"ok": True, "streak": p["streak"]})

@app.route("/api/tired", methods=["POST"])
def api_tired():
    p = load_p()
    today = date.today().isoformat()
    if today not in p["completed_dates"] and today not in p["missed_dates"]:
        p["missed_dates"].append(today)
        p["streak"] = 0
        p["last_session"] = today
        save_p(p)
    return jsonify({"ok": True})

# ─── HTML Template ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>OET 訓練營</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{
  --p:#2563eb;--p-dark:#1d4ed8;--p-light:#eff6ff;--p-mid:#dbeafe;
  --success:#16a34a;--danger:#dc2626;--warn:#d97706;
  --bg:#f0f4f8;--surface:#fff;--surface2:#f8fafc;
  --text:#1a2332;--muted:#64748b;--border:#e2e8f0;
  --r:18px;--shadow:0 2px 16px rgba(37,99,235,.07),0 1px 4px rgba(0,0,0,.05);
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans','Noto Sans TC','Noto Sans JP','Noto Sans KR',Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{background:var(--bg);font-family:var(--font);font-size:15px;line-height:1.65;padding-bottom:88px;color:var(--text);-webkit-font-smoothing:antialiased}
a,button{-webkit-tap-highlight-color:transparent}

/* ── Loading Screen ── */
.loading-screen{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:2.5rem 1.5rem;text-align:center;margin-bottom:1rem}
.loader-ring{width:56px;height:56px;border:5px solid var(--p-mid);border-top-color:var(--p);border-right-color:var(--p);border-radius:50%;animation:spin .75s cubic-bezier(.5,0,.5,1) infinite;margin:0 auto .1rem}
@keyframes spin{to{transform:rotate(360deg)}}
.loader-icon{font-size:1.5rem;margin:-.4rem auto .5rem;line-height:1}
.loader-dots{display:flex;justify-content:center;gap:5px;margin:.9rem 0 1.1rem}
.loader-dots i{display:block;width:7px;height:7px;background:var(--p);border-radius:50%;animation:dotBounce 1.3s ease-in-out infinite}
.loader-dots i:nth-child(2){animation-delay:.18s}
.loader-dots i:nth-child(3){animation-delay:.36s}
@keyframes dotBounce{0%,60%,100%{transform:translateY(0);opacity:.25}30%{transform:translateY(-9px);opacity:1}}
#loadingText{font-size:.96rem;font-weight:600;color:var(--text);margin-bottom:.25rem}
#loadingSubText{font-size:.8rem;color:var(--muted)}

/* ── Hero ── */
.hero{background:linear-gradient(145deg,#1e3a8a 0%,#2563eb 45%,#0369a1 80%,#0ea5e9 100%);color:#fff;padding:1.35rem 1rem 1.8rem;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-80px;right:-50px;width:200px;height:200px;background:rgba(255,255,255,.06);border-radius:50%;pointer-events:none}
.hero::after{content:'';position:absolute;bottom:-40px;left:-30px;width:130px;height:130px;background:rgba(255,255,255,.04);border-radius:50%;pointer-events:none}
.stat-box{background:rgba(255,255,255,.13);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-radius:13px;padding:.7rem .35rem;text-align:center;border:1px solid rgba(255,255,255,.18);transition:background .2s}
.stat-num{font-size:1.5rem;font-weight:800;line-height:1;letter-spacing:-.02em}
.stat-label{font-size:.65rem;opacity:.8;margin-top:3px;font-weight:500;letter-spacing:.03em;text-transform:uppercase}

/* ── Cards ── */
.card{background:var(--surface);border:none;border-radius:var(--r);box-shadow:var(--shadow);margin-bottom:1rem;overflow:hidden}
.card-header{background:var(--surface);border-bottom:1px solid var(--border);border-left:4px solid var(--p);padding:.85rem 1.1rem .85rem 1rem;font-weight:700;font-size:.88rem;color:var(--text);letter-spacing:.02em;display:flex;align-items:center;gap:.5rem}

/* ── Pill Tabs ── */
.nav-tabs{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:4px;gap:2px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;margin-bottom:1rem}
.nav-tabs::-webkit-scrollbar{display:none}
.nav-tabs .nav-link{color:var(--muted);border:none;padding:.42rem .8rem;font-weight:600;font-size:.8rem;border-radius:10px;transition:all .18s;white-space:nowrap;line-height:1.4}
.nav-tabs .nav-link:hover{color:var(--p);background:var(--p-light)}
.nav-tabs .nav-link.active{color:var(--p);background:var(--surface);box-shadow:0 1px 6px rgba(0,0,0,.1);margin-bottom:0}
.tab-pane{animation:tabIn .2s ease}
@keyframes tabIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

/* ── Encouragement ── */
#encouragement{background:linear-gradient(120deg,#eff6ff,#f0fdf4);border-radius:14px;padding:1rem 1.2rem;color:#1d4ed8;font-weight:500;font-size:.95rem;line-height:1.55;text-align:center;border:1px solid #bfdbfe;box-shadow:0 2px 10px rgba(37,99,235,.08)}

/* ── Vocab Flip Cards ── */
.flip-card{perspective:1000px;cursor:pointer;margin-bottom:.8rem;user-select:none}
.flip-card-inner{position:relative;transition:transform .45s cubic-bezier(.4,0,.2,1);transform-style:preserve-3d}
.flip-card.flipped .flip-card-inner{transform:rotateY(180deg)}
.flip-card-front{backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:14px;padding:1rem 1.1rem;position:relative;background:linear-gradient(135deg,var(--p-light) 0%,#e0f2fe 100%);border:1px solid #bfdbfe;min-height:150px;box-shadow:0 1px 8px rgba(37,99,235,.07)}
.flip-card-back{backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:14px;padding:1rem 1.1rem;position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:1px solid #bbf7d0;transform:rotateY(180deg);box-shadow:0 1px 8px rgba(22,163,74,.07)}
.flip-hint{font-size:.67rem;color:var(--muted);position:absolute;top:.55rem;right:.75rem;opacity:.55;letter-spacing:.02em}
.speak-btn{background:none;border:none;padding:0 .2rem;cursor:pointer;font-size:.95rem;opacity:.55;transition:opacity .15s;line-height:1}
.speak-btn:hover{opacity:1}

/* ── Dialogue ── */
.dialogue-line{display:flex;align-items:flex-start;gap:.6rem;padding:.5rem .3rem;border-radius:8px;transition:background .25s}
.dialogue-line:not(:last-child){border-bottom:1px solid #f1f5f9}
.dialogue-line.active-line{background:var(--p-light);padding-left:.6rem}
.spk-badge{font-size:.68rem;font-weight:700;padding:.2rem .55rem;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:.15rem;letter-spacing:.02em}
.spk-nurse{background:var(--p-mid);color:var(--p)}
.spk-patient{background:#f3e8ff;color:#7c3aed}

/* ── Speaking ── */
.phrase-tag{display:inline-block;background:var(--p-light);color:var(--p);border:1px solid var(--p-mid);border-radius:20px;padding:.22rem .7rem;font-size:.79rem;font-weight:500;margin:.15rem;transition:all .2s}
.phrase-tag.hit{background:#dcfce7;color:var(--success);border-color:#bbf7d0}
.phrase-tag.miss{background:#fee2e2;color:var(--danger);border-color:#fecaca}
.transcript-box{background:var(--surface2);border:1.5px solid var(--border);border-radius:12px;padding:.8rem 1rem;min-height:3.5rem;font-size:.93rem;line-height:1.65}
.word-hit{color:var(--success);font-weight:600}
.score-history{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.score-pill{background:var(--p-light);color:var(--p);border-radius:20px;padding:.18rem .65rem;font-size:.76rem;font-weight:600}
.score-pill.best{background:#dcfce7;color:var(--success)}

/* ── Info Boxes ── */
.feedback-box{background:#f0fdf4;border-left:3px solid #22c55e;border-radius:12px;padding:.95rem 1rem}
.info-box{background:var(--p-light);border-left:3px solid var(--p);border-radius:12px;padding:.85rem 1rem}
.warn-box{background:#fffbeb;border-left:3px solid #f59e0b;border-radius:12px;padding:.85rem 1rem;font-size:.88rem}
.danger-box{background:#fff1f2;border-left:3px solid var(--danger);border-radius:12px;padding:.85rem 1rem;font-size:.88rem}

/* ── Buttons ── */
.btn{font-family:var(--font);font-size:.88rem}
.btn-primary{background:var(--p);border-color:var(--p);border-radius:10px;font-weight:600;letter-spacing:.01em}
.btn-primary:hover,.btn-primary:focus{background:var(--p-dark);border-color:var(--p-dark)}
.btn-success{border-radius:10px;font-weight:600}
.btn-outline-secondary{border-radius:10px;font-weight:500;font-size:.86rem}
.btn-link{font-size:.84rem}
#startBtn,#stopBtn{border-radius:50px;padding:.55rem 1.4rem;font-weight:600;font-size:.88rem;min-height:42px}

/* ── Writing ── */
#writeAnswer{border-radius:12px;border:1.5px solid var(--border);font-size:.93rem;line-height:1.65;font-family:var(--font);transition:border-color .2s,box-shadow .2s;resize:vertical}
#writeAnswer:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(37,99,235,.1);outline:none}
.word-count{font-size:.76rem;color:var(--muted);text-align:right;margin-top:.3rem}

/* ── Bottom Bar ── */
.bottom-bar{position:fixed;bottom:0;left:0;right:0;background:rgba(255,255,255,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-top:1px solid var(--border);padding:.75rem 1rem;z-index:100;box-shadow:0 -2px 16px rgba(0,0,0,.06)}

/* ── Language Selector ── */
.lang-btn{background:rgba(255,255,255,.17);border:1px solid rgba(255,255,255,.28);color:#fff;border-radius:20px;padding:.28rem .8rem;font-size:.76rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:background .15s;letter-spacing:.01em}
.lang-btn:hover{background:rgba(255,255,255,.27)}
.lang-menu{position:absolute;top:calc(100% + 7px);right:0;background:#fff;border-radius:14px;box-shadow:0 8px 36px rgba(0,0,0,.14);min-width:178px;overflow:hidden;z-index:200;border:1px solid var(--border)}
.lang-option{padding:.62rem 1rem;font-size:.84rem;cursor:pointer;transition:background .12s;color:var(--text);display:flex;align-items:center;gap:.5rem}
.lang-option:hover{background:var(--p-light)}
.lang-option.active{background:var(--p-light);color:var(--p);font-weight:600}

/* ── Toast ── */
.toast-msg{position:fixed;top:1.2rem;left:50%;transform:translateX(-50%);background:#1a2332;color:#fff;padding:.55rem 1.3rem;border-radius:20px;font-size:.83rem;z-index:9999;pointer-events:none;animation:toastIn .22s ease;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,.25)}
.toast-msg.error{background:var(--danger)}
@keyframes toastIn{from{opacity:0;transform:translate(-50%,-10px)}to{opacity:1;transform:translate(-50%,0)}}

/* ── Reading ── */
#readArticle{font-size:.93rem;line-height:1.8;color:var(--text);background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:1rem 1.1rem;margin-bottom:1.2rem}
.read-q{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:.9rem 1rem;margin-bottom:.9rem}
.form-check-input:checked{background-color:var(--p);border-color:var(--p)}
.form-check{padding:.2rem 0 .2rem 1.5rem;margin:0}
.form-check-label{cursor:pointer;font-size:.88rem;line-height:1.5}

/* ── IPA ── */
.ipa{color:var(--muted);font-size:.82rem;font-family:'Courier New',monospace;font-style:normal;letter-spacing:.03em}

/* ── Hero Progress ── */
.progress-hero{height:5px;background:rgba(255,255,255,.18);border-radius:4px;overflow:hidden;margin-top:.6rem}
.progress-hero-bar{height:100%;background:linear-gradient(90deg,rgba(255,255,255,.7),#fff);border-radius:4px;transition:width .7s ease}

/* ── Onboarding ── */
.onboard-overlay{position:fixed;inset:0;background:rgba(15,23,42,.78);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);z-index:2000;display:flex;align-items:center;justify-content:center;padding:1rem;animation:obFadeIn .3s ease}
@keyframes obFadeIn{from{opacity:0}to{opacity:1}}
@keyframes obFadeOut{from{opacity:1}to{opacity:0}}
.onboard-card{background:#fff;border-radius:24px;max-width:400px;width:100%;padding:1.8rem 1.4rem 1.3rem;box-shadow:0 28px 80px rgba(0,0,0,.32);position:relative;max-height:92vh;overflow-y:auto}
.onboard-skip{position:absolute;top:.9rem;right:.9rem;background:none;border:none;color:var(--muted);font-size:1rem;cursor:pointer;padding:.3rem .5rem;opacity:.55;transition:opacity .15s;line-height:1;border-radius:6px}
.onboard-skip:hover{opacity:1;background:var(--surface2)}
.ob-slide{min-height:280px;padding-bottom:.5rem}
.ob-art{position:relative;width:104px;height:104px;margin:0 auto 1.3rem;display:flex;align-items:center;justify-content:center}
.ob-ring{position:absolute;border-radius:50%;border:2.5px solid var(--p);animation:obRingPulse 2.4s ease-out infinite}
.ob-ring-1{width:52px;height:52px}
.ob-ring-2{width:76px;height:76px;animation-delay:.6s;opacity:.55}
.ob-ring-3{width:100px;height:100px;animation-delay:1.2s;opacity:.25}
@keyframes obRingPulse{0%{transform:scale(.72);opacity:.8}100%{transform:scale(1.18);opacity:0}}
.ob-emoji{font-size:2.1rem;position:relative;z-index:1;animation:obFloat 3s ease-in-out infinite;line-height:1}
@keyframes obFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
.ob-h1{font-size:1.35rem;font-weight:800;text-align:center;color:var(--text);margin-bottom:.45rem;line-height:1.3;letter-spacing:-.01em}
.ob-sub{font-size:.87rem;color:var(--muted);text-align:center;line-height:1.55;margin:0}
.ob-h2{font-size:1rem;font-weight:700;color:var(--text);margin-bottom:.9rem;text-align:center}
.ob-tab-row{display:flex;align-items:flex-start;gap:.8rem;background:var(--surface2);border-radius:12px;padding:.65rem .85rem;border:1px solid var(--border);margin-bottom:.55rem}
.ob-tab-ico{font-size:1.25rem;flex-shrink:0;margin-top:.05rem}
.ob-tab-name{font-size:.86rem;font-weight:600;line-height:1.3}
.ob-tab-desc{font-size:.74rem;color:var(--muted);margin-top:.1rem;line-height:1.4}
.ob-target-pill{background:linear-gradient(90deg,var(--p-light),#e0f2fe);border:1px solid var(--p-mid);border-radius:20px;padding:.38rem 1rem;font-size:.82rem;font-weight:600;color:var(--p-dark);display:inline-block;margin:0 auto .9rem;text-align:center}
.ob-phases{display:flex;align-items:center;justify-content:center;gap:.3rem;margin-bottom:1rem;flex-wrap:wrap}
.ob-phase{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.4rem .6rem;font-size:.72rem;text-align:center;line-height:1.35}
.ob-phase-num{display:inline-block;background:var(--p);color:#fff;border-radius:5px;padding:.05rem .38rem;font-weight:700;font-size:.68rem;margin-bottom:.15rem}
.ob-arrow{color:var(--border);font-size:.85rem}
.ob-tests{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-top:.3rem}
.ob-test{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:.65rem .7rem;text-align:center}
.ob-test-ico{font-size:1.2rem;margin-bottom:.2rem}
.ob-test-name{font-size:.8rem;font-weight:600;color:var(--text)}
.ob-test-info{font-size:.7rem;color:var(--muted);margin-top:.1rem;line-height:1.4}
.ob-start-ico{font-size:3.5rem;text-align:center;margin:1.2rem 0 .8rem;animation:obFloat 2.5s ease-in-out infinite}
.ob-streak{background:linear-gradient(120deg,var(--p-light),#f0fdf4);border:1px solid var(--p-mid);border-radius:12px;padding:.65rem 1rem;text-align:center;font-size:.85rem;font-weight:500;color:var(--p-dark);margin-top:1rem}
.onboard-foot{display:flex;align-items:center;justify-content:space-between;margin-top:1.3rem;padding-top:1rem;border-top:1px solid var(--border)}
.ob-dots{display:flex;gap:5px;align-items:center}
.ob-dot{width:7px;height:7px;border-radius:50%;background:var(--border);transition:all .25s}
.ob-dot.on{background:var(--p);width:18px;border-radius:4px}
.ob-next{background:var(--p);color:#fff;border:none;border-radius:50px;padding:.52rem 1.35rem;font-size:.86rem;font-weight:600;cursor:pointer;transition:background .15s;font-family:var(--font);letter-spacing:.01em}
.ob-next:hover{background:var(--p-dark)}

/* ── Mobile ── */
@media(max-width:400px){
  body{font-size:14px}
  .stat-num{font-size:1.35rem}
  .nav-tabs .nav-link{padding:.38rem .65rem;font-size:.75rem}
  .card-header{font-size:.84rem}
  #startBtn,#stopBtn{padding:.5rem 1.1rem;font-size:.84rem}
}
</style>
</head>
<body>

<!-- ── Onboarding Overlay ── -->
<div id="onboardOverlay" class="onboard-overlay" style="display:none">
  <div class="onboard-card">
    <button class="onboard-skip" onclick="closeOnboard()" title="Skip">✕</button>

    <!-- Slide 0: Welcome -->
    <div class="ob-slide" id="ob-s0">
      <div class="ob-art">
        <div class="ob-ring ob-ring-1"></div>
        <div class="ob-ring ob-ring-2"></div>
        <div class="ob-ring ob-ring-3"></div>
        <div class="ob-emoji">🩺</div>
      </div>
      <h2 class="ob-h1" id="ob-title"></h2>
      <p class="ob-sub" id="ob-tagline"></p>
    </div>

    <!-- Slide 1: Daily Practice -->
    <div class="ob-slide" id="ob-s1" style="display:none">
      <h3 class="ob-h2" id="ob-s2title"></h3>
      <div class="ob-tab-row"><div class="ob-tab-ico">📚</div><div><div class="ob-tab-name">Vocabulary</div><div class="ob-tab-desc">3 clinical terms · flip cards · 🔊 pronunciation</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">📖</div><div><div class="ob-tab-name">Reading</div><div class="ob-tab-desc">OET Part C article · 3 MCQ (comprehension, vocab, inference)</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">🎧</div><div><div class="ob-tab-name">Listening</div><div class="ob-tab-desc">Nurse–patient dialogue · live highlight · 3 questions</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">🗣️</div><div><div class="ob-tab-name">Speaking</div><div class="ob-tab-desc">OET role-play · AI scored · keyword tracking</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">✍️</div><div><div class="ob-tab-name">Writing</div><div class="ob-tab-desc">OET referral letter practice · AI grading</div></div></div>
    </div>

    <!-- Slide 2: OET Roadmap -->
    <div class="ob-slide" id="ob-s2" style="display:none">
      <h3 class="ob-h2" id="ob-s3title"></h3>
      <div style="text-align:center"><div class="ob-target-pill" id="ob-s3target"></div></div>
      <div class="ob-phases">
        <div class="ob-phase"><div class="ob-phase-num">P1</div><div>Day 1–90</div><div style="color:var(--muted);font-size:.68rem">Foundation</div></div>
        <div class="ob-arrow">›</div>
        <div class="ob-phase"><div class="ob-phase-num">P2</div><div>91–180</div><div style="color:var(--muted);font-size:.68rem">Core Skills</div></div>
        <div class="ob-arrow">›</div>
        <div class="ob-phase"><div class="ob-phase-num">P3</div><div>181–270</div><div style="color:var(--muted);font-size:.68rem">Exam Mode</div></div>
      </div>
      <div class="ob-tests">
        <div class="ob-test"><div class="ob-test-ico">📖</div><div class="ob-test-name">Reading</div><div class="ob-test-info">60 min · 3 parts</div></div>
        <div class="ob-test"><div class="ob-test-ico">🎧</div><div class="ob-test-name">Listening</div><div class="ob-test-info">40 min · 3 parts</div></div>
        <div class="ob-test"><div class="ob-test-ico">🗣️</div><div class="ob-test-name">Speaking</div><div class="ob-test-info">20 min · 2 role-plays</div></div>
        <div class="ob-test"><div class="ob-test-ico">✍️</div><div class="ob-test-name">Writing</div><div class="ob-test-info">45 min · referral letter</div></div>
      </div>
    </div>

    <!-- Slide 3: Start -->
    <div class="ob-slide" id="ob-s3" style="display:none">
      <div class="ob-start-ico">🎯</div>
      <h2 class="ob-h1" id="ob-s4title"></h2>
      <p class="ob-sub" id="ob-s4sub"></p>
      <div class="ob-streak" id="ob-s4streak"></div>
    </div>

    <!-- Footer nav -->
    <div class="onboard-foot">
      <div class="ob-dots" id="ob-dots">
        <div class="ob-dot on"></div>
        <div class="ob-dot"></div>
        <div class="ob-dot"></div>
        <div class="ob-dot"></div>
      </div>
      <button class="ob-next" id="ob-btn" onclick="nextOnboard()"></button>
    </div>
  </div>
</div>

<div class="hero">
  <div class="container-sm">
    <div class="d-flex justify-content-between align-items-start mb-3">
      <div>
        <h5 class="mb-0 fw-bold">🏥 OET 訓練營</h5>
        <div style="font-size:.78rem;opacity:.78">目標 365+ · 9 個月計畫</div>
      </div>
      <div class="text-end d-flex flex-column align-items-end gap-1">
        <!-- Language selector -->
        <div class="lang-wrap" style="position:relative">
          <button class="lang-btn" id="langToggle" onclick="toggleLangMenu()" type="button">
            <span id="langFlag"></span> <span id="langName"></span> ▾
          </button>
          <div class="lang-menu" id="langMenu" style="display:none">
            <div class="lang-option" data-lang="zh-TW">🇹🇼 繁體中文</div>
            <div class="lang-option" data-lang="zh-CN">🇨🇳 简体中文</div>
            <div class="lang-option" data-lang="ja">🇯🇵 日本語</div>
            <div class="lang-option" data-lang="ko">🇰🇷 한국어</div>
            <div class="lang-option" data-lang="th">🇹🇭 ภาษาไทย</div>
            <div class="lang-option" data-lang="vi">🇻🇳 Tiếng Việt</div>
            <div class="lang-option" data-lang="id">🇮🇩 Bahasa Indonesia</div>
          </div>
        </div>
        <div>
          <div style="font-size:1.75rem;font-weight:800;line-height:1">🔥 {{ streak }}</div>
          <div style="font-size:.7rem;opacity:.8">連續天數</div>
        </div>
      </div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="stat-box"><div class="stat-num">{{ day }}</div><div class="stat-label">Day / 270</div></div></div>
      <div class="col-4"><div class="stat-box"><div class="stat-num">P{{ phase }}</div><div class="stat-label">{{ phase_name }}</div></div></div>
      <div class="col-4"><div class="stat-box"><div class="stat-num">{{ total }}</div><div class="stat-label">已完成天</div></div></div>
    </div>
    <div class="progress-hero"><div class="progress-hero-bar" style="width:{{ pct }}%"></div></div>
    <div style="font-size:.72rem;opacity:.7;margin-top:3px;text-align:right">{{ pct }}% 完成</div>
  </div>
</div>

<div class="container-sm mt-3">
  {% if missed %}
  <div class="warn-box mb-3">⚠️ 偵測到 {{ missed|length }} 天未開啟，已自動記錄。今天繼續加油！</div>
  {% endif %}

  <div id="loadingBox" class="loading-screen">
    <div class="loader-ring"></div>
    <div class="loader-icon">🩺</div>
    <div class="loader-dots"><i></i><i></i><i></i></div>
    <div id="loadingText"></div>
    <div id="loadingSubText"></div>
  </div>

  <div id="lessonBox" style="display:none">
    <div id="encouragement" class="mb-3"></div>

    <ul class="nav nav-tabs mb-3">
      <li class="nav-item"><button class="nav-link active" data-tab="vocab">📚 詞彙</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="read">📖 閱讀</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="listen">🎧 聽力</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="speak">🗣️ 口說</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="write">✍️ 寫作</button></li>
    </ul>

    <!-- 詞彙 -->
    <div id="tab-vocab" class="tab-pane">
      <div class="card">
        <div class="card-header">📚 今日詞彙 <span class="badge ms-1" style="background:var(--p-light);color:var(--p);font-size:.72rem">點卡片翻面</span></div>
        <div class="card-body" id="vocabContent"></div>
      </div>
    </div>

    <!-- 閱讀 -->
    <div id="tab-read" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">📖 閱讀測驗 <span class="badge ms-1" style="background:var(--p-light);color:var(--p);font-size:.72rem">3 題</span></div>
        <div class="card-body">
          <div class="fw-semibold small mb-2" id="readTitle"></div>
          <div class="p-3 rounded-3 mb-4 small lh-lg" style="background:#f8fafc;border:1.5px solid var(--border);line-height:1.85" id="readArticle"></div>
          <div id="readQs"></div>
          <button class="btn btn-primary btn-sm mt-3" id="readSubmitBtn" onclick="checkReading()">✓ Submit</button>
          <div id="readResults" style="display:none" class="mt-3"></div>
        </div>
      </div>
    </div>

    <!-- 聽力 -->
    <div id="tab-listen" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">🎧 聽力練習</div>
        <div class="card-body">
          <div class="info-box mb-3 small" id="listenScenario"></div>
          <div class="d-flex gap-2 mb-3 flex-wrap">
            <button class="btn btn-primary px-4" id="playBtn" onclick="playListening()">▶ 播放對話</button>
            <button style="display:none" id="showDialogueBtn" class="btn btn-outline-secondary btn-sm" onclick="toggleDialogue()">📄 對話文字</button>
          </div>
          <div id="dialogueText" style="display:none" class="mb-3 p-3 rounded-3" style="background:#f8fafc">
            <div id="dialogueLines"></div>
          </div>
          <div id="listenQs" style="display:none" class="mt-3"></div>
          <div id="listenAnswers" style="display:none" class="mt-3"></div>
          <button id="showAnswerBtn" style="display:none" class="btn btn-outline-secondary btn-sm mt-2" onclick="showAnswers()">查看解答</button>
        </div>
      </div>
    </div>

    <!-- 口說 -->
    <div id="tab-speak" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">🗣️ 口說練習</div>
        <div class="card-body">
          <div class="info-box mb-3">
            <div class="small fw-semibold mb-1" style="color:var(--p)">情境</div>
            <div id="speakScenario" class="small"></div>
          </div>
          <div class="mb-2 small fw-semibold text-muted">你的任務</div>
          <div id="speakTask" class="mb-3 small"></div>
          <div class="mb-2 small fw-semibold text-muted">💡 重點用語 <span class="fw-normal" style="color:var(--muted)">(🟢說到 🔴漏掉)</span></div>
          <div id="speakPhrases" class="mb-3"></div>
          <div class="warn-box mb-3">⚠️ <span id="watchOut"></span></div>
          <div class="d-flex gap-2 justify-content-center my-3">
            <button class="btn btn-primary" id="startBtn" onclick="startRecord()">🎤 開始錄音</button>
            <button class="btn btn-danger" id="stopBtn" onclick="stopRecord()" disabled>⏹ 停止</button>
          </div>
          <div class="text-center text-muted small mb-2" id="recordStatus"></div>
          <div id="transcriptBox" style="display:none" class="mb-3">
            <div class="small fw-semibold text-muted mb-1">你說的：</div>
            <div id="transcriptText" class="transcript-box"></div>
            <div class="d-flex gap-2 mt-2 align-items-center flex-wrap">
              <button class="btn btn-primary btn-sm" id="evalSpeakBtn" onclick="evalSpeak(this)">AI 評分</button>
              <div id="scoreHistory" class="score-history"></div>
            </div>
          </div>
          <div id="speakFeedback" style="display:none" class="feedback-box mb-3"></div>
          <button class="btn btn-link p-0 text-muted small" onclick="toggleSample()">▸ 查看範例答案</button>
          <div id="speakSample" style="display:none" class="info-box mt-2">
            <div class="small fw-semibold mb-1">範例答案</div>
            <div id="sampleText" class="small fst-italic"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 寫作 -->
    <div id="tab-write" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">✍️ 寫作練習</div>
        <div class="card-body">
          <div class="info-box mb-3">
            <div class="small fw-semibold mb-1" style="color:var(--p)">今日技巧</div>
            <div id="writeTip" class="small"></div>
          </div>
          <div class="row g-2 mb-3">
            <div class="col-12">
              <div class="danger-box"><div class="small fw-semibold text-danger mb-1">❌ 避免</div><div id="writeBefore" class="small fst-italic"></div></div>
            </div>
            <div class="col-12">
              <div class="feedback-box"><div class="small fw-semibold text-success mb-1">✓ OET 標準</div><div id="writeAfter" class="small fst-italic"></div></div>
            </div>
          </div>
          <div class="small fw-semibold mb-1">練習題</div>
          <div class="small text-muted mb-2" id="writeTask"></div>
          <textarea class="form-control" id="writeAnswer" rows="4" placeholder="在這裡輸入你的答案..." oninput="updateWordCount()"></textarea>
          <div class="word-count" id="wordCount">0 字</div>
          <button class="btn btn-primary btn-sm mt-2" id="writeSubmitBtn" onclick="evalWrite()">✏️ AI 批改</button>
          <div id="writeFeedback" style="display:none" class="mt-3 feedback-box"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="bottom-bar" id="bottomBar" style="display:none">
  <div class="container-sm d-flex gap-2">
    {% if done %}
    <div class="flex-fill text-center text-success fw-semibold py-2">✅ 今日課程已完成！明天繼續 💪</div>
    {% elif tired %}
    <div class="flex-fill text-center text-muted py-2">😴 今天休息，明天繼續加油！</div>
    {% else %}
    <button class="btn btn-success flex-fill" onclick="markComplete()" style="border-radius:12px">✅ 完成今日課程</button>
    <button class="btn btn-outline-secondary" onclick="markTired()" style="border-radius:12px;white-space:nowrap">😴 很累</button>
    {% endif %}
  </div>
</div>

<script>
let lesson = null, recognition = null, finalTranscript = '';
let scoreHistory = [];

// ── i18n ──
const LANGS = {
  'zh-TW': {flag:'🇹🇼', name:'繁體中文'},
  'zh-CN': {flag:'🇨🇳', name:'简体中文'},
  'ja':    {flag:'🇯🇵', name:'日本語'},
  'ko':    {flag:'🇰🇷', name:'한국어'},
  'th':    {flag:'🇹🇭', name:'ภาษาไทย'},
  'vi':    {flag:'🇻🇳', name:'Tiếng Việt'},
  'id':    {flag:'🇮🇩', name:'Bahasa Indonesia'},
};
const UI = {
  'zh-TW': {loading:'正在生成今日課程…',loadSub:'首次約需 10 秒',hint:'按「開始錄音」（需 Chrome / Edge）',rec:'🔴 錄音中…',done:'錄音完成，可按 AI 評分',scoring:'評分中…',rescore:'🔄 重新評分',grading:'批改中…',regrade:'✏️ 重新批改',results:'批改結果',attempt:'第{n}次',noRec:'請先錄音',noWrite:'請先輸入答案',notAll:'請先回答所有題目',noBrowser:'請使用 Chrome 或 Edge'},
  'zh-CN': {loading:'正在生成今日课程…',loadSub:'首次约需 10 秒',hint:'按「开始录音」（需 Chrome / Edge）',rec:'🔴 录音中…',done:'录音完成，可按 AI 评分',scoring:'评分中…',rescore:'🔄 重新评分',grading:'批改中…',regrade:'✏️ 重新批改',results:'批改结果',attempt:'第{n}次',noRec:'请先录音',noWrite:'请先输入答案',notAll:'请先回答所有题目',noBrowser:'请使用 Chrome 或 Edge'},
  'ja':    {loading:'本日のレッスンを生成中…',loadSub:'初回は約10秒',hint:'「録音開始」を押す (Chrome/Edge)',rec:'🔴 録音中…',done:'録音完了 — AI採点できます',scoring:'採点中…',rescore:'🔄 再採点',grading:'添削中…',regrade:'✏️ 再添削',results:'採点結果',attempt:'{n}回目',noRec:'先に録音してください',noWrite:'先に答えを入力してください',notAll:'全問に解答してください',noBrowser:'Chrome または Edge を使用してください'},
  'ko':    {loading:'오늘 레슨을 생성 중…',loadSub:'처음에는 약 10초 소요',hint:'「녹음 시작」누르기 (Chrome/Edge)',rec:'🔴 녹음 중…',done:'녹음 완료 — AI 채점 가능',scoring:'채점 중…',rescore:'🔄 재채점',grading:'첨삭 중…',regrade:'✏️ 재첨삭',results:'채점 결과',attempt:'{n}번째',noRec:'먼저 녹음해 주세요',noWrite:'먼저 답을 입력해 주세요',notAll:'모든 문제를 풀어 주세요',noBrowser:'Chrome 또는 Edge를 사용해 주세요'},
  'th':    {loading:'กำลังสร้างบทเรียนวันนี้…',loadSub:'ครั้งแรกใช้เวลา ~10 วินาที',hint:'กด「เริ่มอัดเสียง」(Chrome/Edge)',rec:'🔴 กำลังอัดเสียง…',done:'อัดเสร็จ — กด AI ให้คะแนน',scoring:'กำลังให้คะแนน…',rescore:'🔄 ให้คะแนนใหม่',grading:'กำลังตรวจ…',regrade:'✏️ ตรวจใหม่',results:'ผลคะแนน',attempt:'ครั้งที่ {n}',noRec:'กรุณาอัดเสียงก่อน',noWrite:'กรุณาพิมพ์คำตอบก่อน',notAll:'กรุณาตอบทุกข้อ',noBrowser:'ใช้ Chrome หรือ Edge'},
  'vi':    {loading:'Đang tạo bài học hôm nay…',loadSub:'Lần đầu ~10 giây',hint:'Nhấn「Bắt đầu ghi âm」(Chrome/Edge)',rec:'🔴 Đang ghi âm…',done:'Xong — nhấn AI chấm điểm',scoring:'Đang chấm…',rescore:'🔄 Chấm lại',grading:'Đang sửa…',regrade:'✏️ Sửa lại',results:'Kết quả',attempt:'Lần {n}',noRec:'Vui lòng ghi âm trước',noWrite:'Vui lòng nhập câu trả lời',notAll:'Hãy trả lời tất cả câu hỏi',noBrowser:'Dùng Chrome hoặc Edge'},
  'id':    {loading:'Membuat pelajaran hari ini…',loadSub:'Pertama kali ~10 detik',hint:'Tekan「Mulai Rekam」(Chrome/Edge)',rec:'🔴 Merekam…',done:'Selesai — tekan AI nilai',scoring:'Menilai…',rescore:'🔄 Nilai ulang',grading:'Mengoreksi…',regrade:'✏️ Koreksi ulang',results:'Hasil',attempt:'Ke-{n}',noRec:'Rekam dulu',noWrite:'Isi jawaban dulu',notAll:'Jawab semua soal dulu',noBrowser:'Gunakan Chrome atau Edge'},
};
function t(key, n) {
  const d = UI[currentLang] || UI['zh-TW'];
  return (d[key] || key).replace('{n}', n || '');
}

// ── Toast ──
function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast-msg' + (type === 'error' ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

let currentLang = localStorage.getItem('oet_lang') || 'zh-TW';

// ── Onboarding i18n ──
const OB = {
  'zh-TW': {title:'OET 訓練營 🏥',tagline:'每天 30 分鐘 · 270 天達成 365+\n亞洲護士前進歐美的最強夥伴',s2:'每日五大練習',s3:'OET 考試攻略',target:'目標：Band B = 350 分以上',s4:'準備好了！',s4sub:'每天一小步，護理夢就在前方 ✨',streak:'🔥 連續天數從今天開始',next:'下一步 →',start:'開始今日課程 →'},
  'zh-CN': {title:'OET 训练营 🏥',tagline:'每天 30 分钟 · 270 天达成 365+\n亚洲护士前往欧美的最强伙伴',s2:'每日五大练习',s3:'OET 考试攻略',target:'目标：Band B = 350 分以上',s4:'准备好了！',s4sub:'每天一小步，护理梦就在前方 ✨',streak:'🔥 连续天数从今天开始',next:'下一步 →',start:'开始今日课程 →'},
  'ja':    {title:'OET トレーニング 🏥',tagline:'毎日30分 · 270日で365+達成\nアジアの看護師が世界へ羽ばたく',s2:'毎日の5練習',s3:'OET 試験マップ',target:'目標：Band B = 350点以上',s4:'準備完了！',s4sub:'毎日一歩、看護師の夢に近づく ✨',streak:'🔥 連続日数は今日からスタート',next:'次へ →',start:'今日のレッスンを始める →'},
  'ko':    {title:'OET 트레이닝 🏥',tagline:'매일 30분 · 270일 만에 365+ 달성\n해외 취업을 꿈꾸는 간호사의 최강 파트너',s2:'매일 5가지 연습',s3:'OET 시험 가이드',target:'목표: Band B = 350점 이상',s4:'준비됐어요!',s4sub:'매일 한 걸음, 간호사의 꿈에 가까이 ✨',streak:'🔥 연속 일수 오늘부터 시작',next:'다음 →',start:'오늘 수업 시작 →'},
  'th':    {title:'OET เทรนนิ่ง 🏥',tagline:'ทุกวัน 30 นาที · 270 วัน สู่ 365+\nพยาบาลเอเชียสู่ฝันต่างประเทศ',s2:'5 หัวข้อฝึกประจำวัน',s3:'แผนที่สอบ OET',target:'เป้าหมาย: Band B = 350+ คะแนน',s4:'พร้อมแล้ว!',s4sub:'ทีละก้าว สู่ฝันพยาบาลต่างแดน ✨',streak:'🔥 เริ่มนับวันต่อเนื่องวันนี้',next:'ถัดไป →',start:'เริ่มบทเรียนวันนี้ →'},
  'vi':    {title:'OET Luyện thi 🏥',tagline:'30 phút/ngày · 270 ngày đạt 365+\nHành trình của điều dưỡng châu Á ra thế giới',s2:'5 bài tập hàng ngày',s3:'Bản đồ thi OET',target:'Mục tiêu: Band B = 350+ điểm',s4:'Sẵn sàng!',s4sub:'Mỗi ngày một bước, giấc mơ điều dưỡng chờ bạn ✨',streak:'🔥 Chuỗi ngày bắt đầu từ hôm nay',next:'Tiếp theo →',start:'Bắt đầu bài hôm nay →'},
  'id':    {title:'OET Pelatihan 🏥',tagline:'30 menit/hari · 270 hari raih 365+\nPerawat Asia menuju karir global',s2:'5 latihan harian',s3:'Peta Ujian OET',target:'Target: Band B = 350+ poin',s4:'Siap!',s4sub:'Selangkah demi selangkah, impian perawat menanti ✨',streak:'🔥 Hari berturut-turut mulai hari ini',next:'Selanjutnya →',start:'Mulai pelajaran hari ini →'},
};

let obSlide = 0;
const OB_TOTAL = 4;

function showOnboard() {
  document.getElementById('onboardOverlay').style.display = 'flex';
  setObSlide(0);
}
function closeOnboard() {
  const el = document.getElementById('onboardOverlay');
  el.style.animation = 'obFadeOut .28s ease forwards';
  setTimeout(() => { el.style.display = 'none'; el.style.animation = ''; }, 290);
  localStorage.setItem('oet_onboard_v2', '1');
}
function nextOnboard() {
  if (obSlide >= OB_TOTAL - 1) { closeOnboard(); return; }
  setObSlide(obSlide + 1);
}
function setObSlide(n) {
  obSlide = n;
  const o = OB[currentLang] || OB['zh-TW'];
  for (let i = 0; i < OB_TOTAL; i++) {
    const s = document.getElementById('ob-s' + i);
    if (s) s.style.display = i === n ? 'block' : 'none';
  }
  document.querySelectorAll('.ob-dot').forEach((d, i) => d.classList.toggle('on', i === n));
  const btn = document.getElementById('ob-btn');
  btn.textContent = n === OB_TOTAL - 1 ? o.start : o.next;
  if (n === 0) {
    document.getElementById('ob-title').textContent = o.title;
    document.getElementById('ob-tagline').textContent = o.tagline;
  } else if (n === 1) {
    document.getElementById('ob-s2title').textContent = o.s2;
  } else if (n === 2) {
    document.getElementById('ob-s3title').textContent = o.s3;
    document.getElementById('ob-s3target').textContent = o.target;
  } else if (n === 3) {
    document.getElementById('ob-s4title').textContent = o.s4;
    document.getElementById('ob-s4sub').textContent = o.s4sub;
    document.getElementById('ob-s4streak').textContent = o.streak;
  }
}

function initLangUI() {
  const info = LANGS[currentLang] || LANGS['zh-TW'];
  document.getElementById('langFlag').textContent = info.flag;
  document.getElementById('langName').textContent = info.name;
  document.querySelectorAll('.lang-option').forEach(el => {
    el.classList.toggle('active', el.dataset.lang === currentLang);
    el.onclick = () => setLang(el.dataset.lang);
  });
  // Update loading & record hint text
  const lt = document.getElementById('loadingText');
  const ls = document.getElementById('loadingSubText');
  const rs = document.getElementById('recordStatus');
  if (lt) lt.textContent = t('loading');
  if (ls) ls.textContent = t('loadSub');
  if (rs && !rs.textContent) rs.textContent = t('hint');
}
function toggleLangMenu() {
  const m = document.getElementById('langMenu');
  m.style.display = m.style.display === 'none' ? 'block' : 'none';
}
function setLang(lang) {
  if (lang === currentLang) { document.getElementById('langMenu').style.display='none'; return; }
  localStorage.setItem('oet_lang', lang);
  currentLang = lang;
  document.getElementById('langMenu').style.display = 'none';
  // Reset state
  scoreHistory = [];
  finalTranscript = '';
  if (recognition) { try { recognition.onend=null; recognition.abort(); } catch(e){} recognition=null; }
  // Reset tab to vocab
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.remove('active'));
  document.querySelector('[data-tab="vocab"]').classList.add('active');
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display='none');
  document.getElementById('tab-vocab').style.display='block';
  // Reset dynamic fields
  document.getElementById('transcriptBox').style.display='none';
  document.getElementById('speakFeedback').style.display='none';
  document.getElementById('writeFeedback').style.display='none';
  document.getElementById('readResults').style.display='none';
  document.getElementById('scoreHistory').innerHTML='';
  // Reload lesson
  document.getElementById('lessonBox').style.display = 'none';
  document.getElementById('loadingBox').style.display = 'block';
  initLangUI();
  lesson = null;
  fetch('/api/lesson?lang=' + lang)
    .then(r => r.json())
    .then(l => { lesson = l; renderLesson(l); })
    .catch(e => {
      document.getElementById('loadingText').textContent = '⚠️ ' + e.message;
    });
}
document.addEventListener('click', e => {
  if (!e.target.closest('.lang-wrap')) document.getElementById('langMenu').style.display = 'none';
});

// Tab switching with animation
document.querySelectorAll('[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    const leaving = document.querySelector('[data-tab].active');
    if (leaving && leaving.dataset.tab === 'listen') {
      window.speechSynthesis.cancel();
      const playBtn = document.getElementById('playBtn');
      if (playBtn) { playBtn.disabled = false; playBtn.textContent = '▶ 播放對話'; }
    }
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    const pane = document.getElementById('tab-' + btn.dataset.tab);
    pane.style.display = 'block';
    pane.classList.remove('tab-pane');
    void pane.offsetWidth;
    pane.classList.add('tab-pane');
  });
});

window.onload = async () => {
  try {
    lesson = await (await fetch('/api/lesson?lang=' + currentLang)).json();
    renderLesson(lesson);
  } catch(e) {
    document.getElementById('loadingBox').innerHTML =
      '<div class="danger-box m-3">⚠️ 載入失敗：' + e.message + '<br><button class="btn btn-primary mt-2" onclick="location.reload()">重試</button></div>';
  }
};

function renderLesson(l) {
  document.getElementById('loadingBox').style.display = 'none';
  document.getElementById('lessonBox').style.display = 'block';
  document.getElementById('bottomBar').style.display = 'block';
  document.getElementById('encouragement').textContent = l.encouragement || '今天也要加油！';

  // Vocab flip cards
  document.getElementById('vocabContent').innerHTML = l.vocabulary.map((v,i) => `
    <div class="flip-card" onclick="this.classList.toggle('flipped')">
      <div class="flip-card-inner">
        <div class="flip-card-front">
          <span class="flip-hint">點我翻面 →</span>
          <div class="d-flex align-items-center gap-2 mb-1">
            <span class="fs-5 fw-bold" style="color:var(--p)">${v.word}</span>
            <button class="speak-btn" onclick="event.stopPropagation();speakWord('${v.word.replace(/'/g,"\\'")}')">🔊</button>
          </div>
          <div class="ipa">${v.ipa || ''}</div>
          <div class="mt-2 small" style="color:#7c3aed">💡 ${v.tip}</div>
        </div>
        <div class="flip-card-back">
          <div class="d-flex justify-content-between mb-2">
            <span class="fw-bold" style="color:var(--success)">${v.native || v.zh || ''}</span>
            <span class="flip-hint">← 點我翻回</span>
          </div>
          <div class="small fst-italic text-secondary">"${v.example}"</div>
        </div>
      </div>
    </div>
  `).join('');

  // Listening
  document.getElementById('listenScenario').innerHTML = '📋 ' + l.listening.scenario;
  document.getElementById('dialogueLines').innerHTML = l.listening.dialogue.map((d,i) => `
    <div class="dialogue-line" id="dline-${i}">
      <span class="spk-badge ${d.speaker==='Nurse'?'spk-nurse':'spk-patient'}">${d.speaker}</span>
      <span class="small">${d.text}</span>
    </div>
  `).join('');

  // Speaking
  document.getElementById('speakScenario').textContent = l.speaking.scenario;
  document.getElementById('speakTask').textContent = l.speaking.task;
  renderPhrases(l.speaking.key_phrases, []);
  document.getElementById('watchOut').textContent = l.speaking.watch_out;
  document.getElementById('sampleText').textContent = l.speaking.sample;

  // Reading
  if (l.reading) {
    document.getElementById('readTitle').textContent = l.reading.title || '';
    document.getElementById('readArticle').textContent = l.reading.article || '';
    document.getElementById('readQs').innerHTML = renderReadQuestions(l.reading.questions);
  }

  // Writing
  document.getElementById('writeTip').textContent = l.writing.tip;
  document.getElementById('writeBefore').textContent = l.writing.before;
  document.getElementById('writeAfter').textContent = l.writing.after;
  document.getElementById('writeTask').textContent = l.writing.task;
}

function renderReadQuestions(qs) {
  return (qs || []).map((q, i) => `
    <div class="read-q">
      <div style="font-size:.88rem;font-weight:600;margin-bottom:.6rem;line-height:1.5">${i+1}. ${q.q}</div>
      ${q.options.map(o => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="rq${i}" id="rq${i}${o[0]}" value="${o[0]}">
          <label class="form-check-label" for="rq${i}${o[0]}">${o}</label>
        </div>`).join('')}
    </div>`).join('');
}

function checkReading() {
  const qs = lesson.reading.questions;
  // Validate all answered
  const unanswered = qs.filter((_, i) => !document.querySelector(`input[name="rq${i}"]:checked`));
  if (unanswered.length > 0) { toast(t('notAll'), 'error'); return; }
  let correct = 0;
  let html = `<div class="fw-semibold small mb-2">${t('results')}</div>`;
  qs.forEach((q, i) => {
    const sel = document.querySelector(`input[name="rq${i}"]:checked`);
    const chosen = sel.value;
    const isRight = chosen === q.answer;
    if (isRight) correct++;
    const icon = isRight ? '✅' : '❌';
    const bg = isRight ? '#f0fdf4' : '#fff1f2';
    const bd = isRight ? '#22c55e' : 'var(--danger)';
    html += `<div class="mb-3 p-3 rounded-3 small" style="background:${bg};border-left:3px solid ${bd}">
      <div class="fw-semibold mb-1">${icon} Q${i+1} — ✓ ${q.answer}</div>
      ${!isRight ? `<div class="mb-1" style="color:var(--danger)">✗ ${chosen}</div>` : ''}
      <div>${q.explanation}</div>
    </div>`;
  });
  html += `<div class="fw-bold text-center mt-2" style="font-size:1.1rem">${correct} / ${qs.length} ${'⭐'.repeat(correct)}</div>`;
  document.getElementById('readResults').innerHTML = html;
  document.getElementById('readResults').style.display = 'block';
  document.getElementById('readSubmitBtn').textContent = '🔄';
  document.getElementById('readSubmitBtn').onclick = () => {
    document.querySelectorAll('[name^="rq"]').forEach(r => r.checked = false);
    document.getElementById('readResults').style.display = 'none';
    document.getElementById('readSubmitBtn').textContent = '✓ Submit';
    document.getElementById('readSubmitBtn').onclick = checkReading;
  };
}

// Vocab pronunciation
function speakWord(word) {
  if (!window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(word);
  u.lang = 'en-US'; u.rate = 0.85;
  window.speechSynthesis.speak(u);
}

// Listening with line highlight
function playListening() {
  const btn = document.getElementById('playBtn');
  if (!window.speechSynthesis) { toast(t('noBrowser'), 'error'); return; }
  window.speechSynthesis.cancel();
  btn.disabled = true; btn.textContent = '🔊 播放中...';
  const lines = lesson.listening.dialogue;
  let i = 0;
  function speakNext() {
    document.querySelectorAll('.dialogue-line').forEach(el => el.classList.remove('active-line'));
    if (i >= lines.length) {
      btn.textContent = '▶ 重新播放'; btn.disabled = false;
      document.getElementById('showDialogueBtn').style.display = 'inline';
      document.getElementById('listenQs').innerHTML = renderQuestions(lesson.listening.questions);
      document.getElementById('listenQs').style.display = 'block';
      document.getElementById('showAnswerBtn').style.display = 'inline';
      return;
    }
    const lineEl = document.getElementById('dline-' + i);
    if (lineEl) lineEl.classList.add('active-line');
    const u = new SpeechSynthesisUtterance(lines[i].speaker + ' says: ' + lines[i].text);
    u.lang = 'en-US'; u.rate = 0.88; i++;
    u.onend = speakNext;
    window.speechSynthesis.speak(u);
  }
  speakNext();
}

function renderQuestions(qs) {
  return qs.map((q,i) => `
    <div class="read-q">
      <div style="font-size:.88rem;font-weight:600;margin-bottom:.6rem;line-height:1.5">${i+1}. ${q.q}</div>
      ${q.options.map(o => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="q${i}" id="q${i}${o[0]}">
          <label class="form-check-label" for="q${i}${o[0]}">${o}</label>
        </div>`).join('')}
    </div>`).join('');
}

function showAnswers() {
  document.getElementById('listenAnswers').innerHTML =
    '<div class="fw-semibold small mb-2">解答</div>' +
    lesson.listening.questions.map(q =>
      `<div class="mb-2 small feedback-box"><span class="badge bg-success me-1">${q.answer}</span>${q.explanation}</div>`
    ).join('');
  document.getElementById('listenAnswers').style.display = 'block';
  document.getElementById('showAnswerBtn').style.display = 'none';
}

function toggleDialogue() {
  const d = document.getElementById('dialogueText');
  d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

// ── Speaking ──
function renderPhrases(phrases, spokenWords) {
  const spoken = spokenWords.map(w => w.toLowerCase());
  document.getElementById('speakPhrases').innerHTML = phrases.map(p => {
    const key = p.split(' ')[0].toLowerCase();
    const hit = spoken.some(w => w.includes(key) || key.includes(w));
    const cls = spokenWords.length === 0 ? '' : (hit ? 'hit' : 'miss');
    return `<span class="phrase-tag ${cls}">${p}</span>`;
  }).join('');
}

function highlightTranscript(text) {
  if (!lesson) return text;
  const phrases = lesson.speaking.key_phrases;
  let html = text;
  phrases.forEach(p => {
    const key = p.split(' ')[0];
    const re = new RegExp('(' + key + '\\w*)', 'gi');
    html = html.replace(re, '<span class="word-hit">$1</span>');
  });
  return html;
}

function startRecord() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast(t('noBrowser'), 'error'); return; }
  finalTranscript = '';
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  document.getElementById('recordStatus').textContent = t('rec');
  recognition = new SR();
  recognition.lang = 'en-US'; recognition.continuous = true; recognition.interimResults = true;
  recognition.onresult = e => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalTranscript += e.results[i][0].transcript + ' ';
      else interim += e.results[i][0].transcript;
    }
    const full = finalTranscript + interim;
    document.getElementById('transcriptText').innerHTML = highlightTranscript(full);
    document.getElementById('transcriptBox').style.display = 'block';
    const words = full.trim().split(/\\s+/);
    renderPhrases(lesson.speaking.key_phrases, words);
  };
  recognition.onend = () => {
    if (document.getElementById('startBtn').disabled) try { recognition.start(); } catch(e) {}
  };
  recognition.start();
}

function stopRecord() {
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  document.getElementById('recordStatus').textContent = t('done');
  if (recognition) { try { recognition.onend = null; recognition.abort(); } catch(e) {} recognition = null; }
  const txt = finalTranscript.trim();
  document.getElementById('transcriptText').innerHTML = highlightTranscript(txt);
  if (txt) document.getElementById('transcriptBox').style.display = 'block';
}

async function evalSpeak(btn) {
  const spoken = document.getElementById('transcriptText').textContent.trim();
  if (!spoken) { toast(t('noRec'), 'error'); return; }
  btn.disabled = true; btn.textContent = t('scoring');
  try {
    const f = await (await fetch('/api/evaluate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({spoken, scenario: lesson.speaking.scenario, sample: lesson.speaking.sample, lang: currentLang})
    })).json();
    scoreHistory.push(f.score);
    const best = Math.max(...scoreHistory);
    document.getElementById('scoreHistory').innerHTML =
      scoreHistory.map((s,i) => `<span class="score-pill ${s===best?'best':''}">${t('attempt',i+1)} ${s}⭐</span>`).join('');
    const stars = '⭐'.repeat(f.score) + '☆'.repeat(5 - f.score);
    document.getElementById('speakFeedback').innerHTML = `
      <div class="fw-bold mb-2 fs-5">${stars} <span style="font-size:.95rem">${f.score}/5</span></div>
      <div class="mb-2">✅ ${f.good}</div>
      <div class="mb-2">📈 ${f.improve}</div>
      <div class="mb-2 small">📝 ${f.vocabulary}</div>
      <div class="mt-2 p-2 rounded-2 small" style="background:var(--p-light);color:var(--p-dark)">🎯 ${f.oet_tip}</div>
    `;
    document.getElementById('speakFeedback').style.display = 'block';
    btn.textContent = t('rescore');
  } catch(e) {
    toast('⚠️ ' + e.message, 'error');
    btn.textContent = t('rescore');
  }
  btn.disabled = false;
}

function toggleSample() {
  const s = document.getElementById('speakSample');
  s.style.display = s.style.display === 'none' ? 'block' : 'none';
}

// Writing
function updateWordCount() {
  const words = document.getElementById('writeAnswer').value.trim().split(/\\s+/).filter(w=>w).length;
  document.getElementById('wordCount').textContent = words + ' words';
}

async function evalWrite() {
  const answer = document.getElementById('writeAnswer').value.trim();
  if (!answer) { toast(t('noWrite'), 'error'); return; }
  const btn = document.getElementById('writeSubmitBtn');
  btn.disabled = true; btn.textContent = t('grading');
  try {
    const f = await (await fetch('/api/evaluate-writing', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ answer, task: document.getElementById('writeTask').textContent, tip: document.getElementById('writeTip').textContent, lang: currentLang })
    })).json();
    const stars = '⭐'.repeat(f.score) + '☆'.repeat(5-f.score);
    document.getElementById('writeFeedback').innerHTML = `
      <div class="fw-bold mb-2">${stars} ${f.score}/5 &nbsp;<span class="badge" style="background:var(--p-light);color:var(--p);font-size:.78rem">${f.oet_level}</span></div>
      <div class="mb-1 small">📝 ${f.grammar}</div>
      <div class="mb-1 small">📖 ${f.vocabulary}</div>
      <div class="mb-2 small">🏗️ ${f.structure}</div>
      <div class="p-2 rounded-2 small" style="background:#f0fdf4;border-left:3px solid #22c55e">
        <div class="fw-semibold text-success mb-1">✓</div>
        <div class="fst-italic">${f.rewrite}</div>
      </div>
      <div class="mt-2 small text-muted">💬 ${f.summary}</div>
    `;
    document.getElementById('writeFeedback').style.display = 'block';
    btn.textContent = t('regrade');
  } catch(e) {
    toast('⚠️ ' + e.message, 'error');
    btn.textContent = t('regrade');
  }
  btn.disabled = false;
}

async function markComplete() {
  await fetch('/api/complete', {method:'POST'});
  location.reload();
}
async function markTired() {
  if (!confirm('記錄今天休息？')) return;
  await fetch('/api/tired', {method:'POST'});
  location.reload();
}

// Run immediately — script is at end of body so DOM is ready
initLangUI();
if (!localStorage.getItem('oet_onboard_v2')) showOnboard();
</script>
</body>
</html>"""

# ─── Entry Point ──────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_local = port == 5000
    print("=" * 40)
    print(f"  OET Trainer - Starting on port {port}")
    if is_local:
        print("  http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 40)
    if is_local:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port)
