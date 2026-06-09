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

def generate_lesson(p):
    today = date.today().isoformat()
    if LESSON_CACHE.exists():
        cached = json.loads(LESSON_CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == today:
            return cached

    day = max(p.get("current_day", 1), 1)
    ph = get_phase(day)
    phases = {
        1: "Foundation: nursing vocabulary, OET format introduction, basic referral letter structure",
        2: "Core Skills: patient consultation role-plays, complete referral letters, clinical listening",
        3: "Exam Mode: timed mock tests, accent refinement, targeting weak areas"
    }

    prompt = f"""You are an expert OET trainer for healthcare professionals.
Create a 30-minute daily lesson. The student is a Taiwanese nurse with B2 English targeting OET 365+.

Day {day} of 270 | Phase {ph}: {phases[ph]}
Weak areas: {p.get('weak_areas', ['speaking', 'writing'])}
Missed sessions so far: {len(p.get('missed_dates', []))}

Return ONLY a valid JSON object with no markdown, no explanation, no extra text:
{{
  "date": "{today}",
  "day": {day},
  "phase": {ph},
  "encouragement": "一句鼓勵話（繁體中文，針對今天是第{day}天）",
  "vocabulary": [
    {{
      "word": "nursing/medical term",
      "ipa": "/pronunciation/",
      "zh": "中文翻譯",
      "example": "Full sentence in nursing clinical context",
      "tip": "記憶技巧或台灣護士常見錯誤"
    }},
    {{
      "word": "second term",
      "ipa": "/pronunciation/",
      "zh": "中文翻譯",
      "example": "Full sentence in nursing clinical context",
      "tip": "記憶技巧"
    }},
    {{
      "word": "third term",
      "ipa": "/pronunciation/",
      "zh": "中文翻譯",
      "example": "Full sentence in nursing clinical context",
      "tip": "記憶技巧"
    }}
  ],
  "listening": {{
    "scenario": "One-sentence description of the clinical situation",
    "dialogue": [
      {{"speaker": "Nurse", "text": "Opening statement to patient"}},
      {{"speaker": "Patient", "text": "Patient response"}},
      {{"speaker": "Nurse", "text": "Follow-up question or instruction"}},
      {{"speaker": "Patient", "text": "Patient provides information"}},
      {{"speaker": "Nurse", "text": "Closing or next step"}}
    ],
    "questions": [
      {{
        "q": "Comprehension question about the dialogue",
        "options": ["A. first option", "B. second option", "C. third option", "D. fourth option"],
        "answer": "A",
        "explanation": "Why this answer is correct"
      }}
    ]
  }},
  "speaking": {{
    "scenario": "Detailed clinical scenario for the role-play",
    "task": "Specific speaking task instruction for the nurse",
    "sample": "A model OET-level response demonstrating appropriate language (3-4 sentences)",
    "key_phrases": ["key phrase 1", "key phrase 2", "key phrase 3"],
    "watch_out": "Common mistake Taiwanese nurses make in this type of scenario"
  }},
  "writing": {{
    "tip": "One specific OET referral letter writing tip",
    "before": "Example of a weak, non-OET sentence to avoid",
    "after": "The improved OET-standard version of the same sentence",
    "task": "Write 2-3 sentences about: specific clinical writing prompt"
  }}
}}"""

    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2800,
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
    LESSON_CACHE.write_text(json.dumps(lesson, indent=2, ensure_ascii=False), encoding="utf-8")
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

def evaluate_speaking(spoken, scenario, sample):
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"""You are an OET speaking examiner.
Scenario: {scenario}
Model answer: {sample}
Student said: {spoken}

Return ONLY JSON:
{{"score": 1, "good": "one thing done well", "improve": "one specific improvement needed", "vocabulary": "word choice feedback", "oet_tip": "exam-specific tip for this type of scenario"}}
Score 1-5 where 5 is OET Band B level."""}]
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
    return jsonify(generate_lesson(load_p()))

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
    return jsonify(evaluate_speaking(d["spoken"], d["scenario"], d["sample"]))

@app.route("/api/evaluate-writing", methods=["POST"])
def api_evaluate_writing():
    d = request.json
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""You are an OET writing examiner for nurses.
Task: {d['task']}
Tip: {d['tip']}
Student wrote: {d['answer']}

Return ONLY JSON (no markdown):
{{"score": 1, "oet_level": "Below B / B / Above B", "grammar": "grammar feedback in Traditional Chinese", "vocabulary": "vocabulary feedback in Traditional Chinese", "structure": "structure/coherence feedback in Traditional Chinese", "rewrite": "improved version of their sentences in English", "summary": "one-line overall comment in Traditional Chinese"}}
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OET 訓練營</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root { --blue: #1a6fb4; --light-blue: #eff6ff; }
body { background: #f4f7fb; font-family: 'Segoe UI', system-ui, sans-serif; padding-bottom: 90px; }
.hero { background: linear-gradient(135deg, #1a6fb4, #0ea5e9); color: white; padding: 1.5rem 1rem 2rem; }
.card { border: none; border-radius: 16px; box-shadow: 0 2px 14px rgba(0,0,0,.07); margin-bottom: 1rem; }
.card-header { background: var(--blue); color: white; border-radius: 16px 16px 0 0 !important; font-weight: 600; padding: .9rem 1.25rem; }
.vocab-item { background: var(--light-blue); border-radius: 12px; padding: 1rem 1.1rem; margin-bottom: .75rem; }
.ipa { color: #64748b; font-size: .88rem; font-style: italic; }
.nav-tabs { border-bottom: 2px solid #e2e8f0; }
.nav-tabs .nav-link { color: #64748b; border: none; padding: .6rem 1rem; font-weight: 500; }
.nav-tabs .nav-link.active { color: var(--blue); border-bottom: 2px solid var(--blue); background: none; margin-bottom: -2px; }
.btn-primary { background: var(--blue); border-color: var(--blue); border-radius: 10px; }
.btn-primary:hover { background: #155a96; border-color: #155a96; }
.btn-record { width: 64px; height: 64px; border-radius: 50%; background: var(--blue); border: none; color: white; font-size: 1.6rem; box-shadow: 0 4px 16px rgba(26,111,180,.4); transition: all .2s; }
.btn-record.active { background: #ef4444; box-shadow: 0 4px 16px rgba(239,68,68,.5); animation: pulse .9s infinite; }
@keyframes pulse { 0%,100%{ transform: scale(1); } 50%{ transform: scale(1.08); } }
.feedback-box { background: #f0fdf4; border-left: 4px solid #22c55e; border-radius: 10px; padding: 1rem 1.1rem; }
.stat-box { background: rgba(255,255,255,.15); border-radius: 12px; padding: .6rem; text-align: center; }
.stat-num { font-size: 1.6rem; font-weight: 700; line-height: 1; }
.stat-label { font-size: .72rem; opacity: .85; margin-top: 2px; }
.badge-phase { background: rgba(255,255,255,.2); border-radius: 20px; padding: .3rem .8rem; font-size: .8rem; }
.bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #e2e8f0; padding: .9rem 1rem; z-index: 100; }
.dialogue-line { padding: .4rem 0; border-bottom: 1px solid #f1f5f9; }
.dialogue-line:last-child { border-bottom: none; }
.speaker-nurse { color: var(--blue); font-weight: 600; }
.speaker-patient { color: #7c3aed; font-weight: 600; }
</style>
</head>
<body>

<!-- Hero Header -->
<div class="hero">
  <div class="container-sm">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h5 class="mb-0 fw-bold">🏥 OET 訓練營</h5>
        <div style="font-size:.8rem;opacity:.8">目標 365+ · 9 個月計畫</div>
      </div>
      <div class="text-end">
        <div style="font-size:1.8rem;font-weight:700;line-height:1">🔥 {{ streak }}</div>
        <div style="font-size:.72rem;opacity:.8">連續天數</div>
      </div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="stat-box">
        <div class="stat-num">{{ day }}</div>
        <div class="stat-label">Day / 270</div>
      </div></div>
      <div class="col-4"><div class="stat-box">
        <div class="stat-num">P{{ phase }}</div>
        <div class="stat-label">{{ phase_name }}</div>
      </div></div>
      <div class="col-4"><div class="stat-box">
        <div class="stat-num">{{ total }}</div>
        <div class="stat-label">已完成天</div>
      </div></div>
    </div>
    <div class="progress" style="height:5px;background:rgba(255,255,255,.2);border-radius:4px">
      <div class="progress-bar bg-white" style="width:{{ pct }}%;border-radius:4px"></div>
    </div>
    <div style="font-size:.75rem;opacity:.7;margin-top:4px;text-align:right">{{ pct }}% 完成</div>
  </div>
</div>

<div class="container-sm mt-3">

  {% if missed %}
  <div class="alert border-0 rounded-3 mb-3" style="background:#fff7ed;color:#c2410c">
    ⚠️ 偵測到 {{ missed|length }} 天未開啟，已自動記錄。今天繼續加油！
  </div>
  {% endif %}

  <!-- Loading State -->
  <div id="loadingBox" class="card p-4 text-center">
    <div class="spinner-border text-primary mb-3" style="margin:0 auto;width:2rem;height:2rem"></div>
    <div class="text-muted">正在為你生成今日課程...</div>
    <div class="text-muted mt-1" style="font-size:.85rem">首次約需 10 秒</div>
  </div>

  <!-- Lesson Content -->
  <div id="lessonBox" style="display:none">

    <div id="encouragement" class="p-3 rounded-3 mb-3 text-center fw-500"
         style="background:#eff6ff;color:#1e40af;font-weight:500"></div>

    <!-- Tabs -->
    <ul class="nav nav-tabs mb-3" id="tabs">
      <li class="nav-item"><button class="nav-link active" data-tab="vocab">📚 詞彙</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="listen">🎧 聽力</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="speak">🗣️ 口說</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="write">✍️ 寫作</button></li>
    </ul>

    <!-- Vocabulary -->
    <div id="tab-vocab">
      <div class="card">
        <div class="card-header">📚 今日詞彙 <span class="badge bg-white text-primary ms-1" style="font-size:.75rem">3 個</span></div>
        <div class="card-body" id="vocabContent"></div>
      </div>
    </div>

    <!-- Listening -->
    <div id="tab-listen" style="display:none">
      <div class="card">
        <div class="card-header">🎧 聽力練習</div>
        <div class="card-body">
          <p class="text-muted mb-3" id="listenScenario"></p>
          <button class="btn btn-primary px-4 mb-3" id="playBtn" onclick="playListening()">▶ 播放對話</button>
          <audio id="audioEl" style="display:none;width:100%" controls></audio>
          <div id="dialogueText" style="display:none" class="mb-3">
            <div class="fw-semibold mb-2 small text-muted">對話文字</div>
            <div id="dialogueLines"></div>
          </div>
          <div id="listenQs" style="display:none" class="mt-3"></div>
          <div id="listenAnswers" style="display:none" class="mt-3"></div>
          <button id="showAnswerBtn" style="display:none" class="btn btn-outline-secondary btn-sm mt-2" onclick="showAnswers()">查看解答</button>
          <button style="display:none" id="showDialogueBtn" class="btn btn-link btn-sm mt-1 ps-0" onclick="toggleDialogue()">顯示對話文字</button>
        </div>
      </div>
    </div>

    <!-- Speaking -->
    <div id="tab-speak" style="display:none">
      <div class="card">
        <div class="card-header">🗣️ 口說練習</div>
        <div class="card-body">
          <div class="vocab-item mb-3">
            <div class="small text-muted mb-1 fw-semibold">情境</div>
            <div id="speakScenario"></div>
          </div>
          <div class="mb-3">
            <div class="small text-muted fw-semibold mb-1">你的任務</div>
            <div id="speakTask"></div>
          </div>
          <div class="mb-3">
            <div class="small text-muted fw-semibold mb-1">💡 重點用語</div>
            <div id="speakPhrases"></div>
          </div>
          <div class="mb-2 p-3 rounded-3" style="background:#fff7ed;font-size:.88rem">
            ⚠️ <span id="watchOut"></span>
          </div>
          <div class="text-center my-3">
            <button class="btn btn-primary px-4 me-2" id="startBtn" onclick="startRecord()">🎤 開始錄音</button>
            <button class="btn btn-danger px-4" id="stopBtn" onclick="stopRecord()" disabled>⏹ 停止</button>
            <div class="mt-2 text-muted small" id="recordStatus">按「開始錄音」（需使用 Chrome 或 Edge）</div>
          </div>
          <div id="transcriptBox" style="display:none" class="mb-3">
            <div class="small text-muted fw-semibold mb-1">你說的：</div>
            <div id="transcriptText" class="p-2 rounded-2" style="background:#f8fafc;min-height:2.5rem"></div>
            <button class="btn btn-primary btn-sm mt-2" onclick="evalSpeak()">AI 評分</button>
          </div>
          <div id="speakFeedback" style="display:none" class="feedback-box mb-3"></div>
          <button class="btn btn-link p-0 text-muted small" onclick="toggleSample()">▸ 查看範例答案</button>
          <div id="speakSample" style="display:none" class="vocab-item mt-2">
            <div class="small fw-semibold mb-1">範例答案</div>
            <div id="sampleText" class="fst-italic"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Writing -->
    <div id="tab-write" style="display:none">
      <div class="card">
        <div class="card-header">✍️ 寫作練習</div>
        <div class="card-body">
          <div class="p-3 rounded-3 mb-3" style="background:#eff6ff;border-left:3px solid var(--blue)">
            <div class="small fw-semibold text-primary mb-1">今日技巧</div>
            <div id="writeTip"></div>
          </div>
          <div class="row g-2 mb-3">
            <div class="col-12">
              <div class="p-3 rounded-3" style="background:#fff1f2;border-left:3px solid #ef4444">
                <div class="small fw-semibold text-danger mb-1">❌ 避免這樣寫</div>
                <div id="writeBefore" class="fst-italic"></div>
              </div>
            </div>
            <div class="col-12">
              <div class="p-3 rounded-3" style="background:#f0fdf4;border-left:3px solid #22c55e">
                <div class="small fw-semibold text-success mb-1">✓ OET 標準寫法</div>
                <div id="writeAfter" class="fst-italic"></div>
              </div>
            </div>
          </div>
          <div class="mb-2 small fw-semibold">練習題</div>
          <div class="text-muted mb-2 small" id="writeTask"></div>
          <textarea class="form-control" id="writeAnswer" rows="4" placeholder="在這裡輸入你的答案..."></textarea>
          <button class="btn btn-primary btn-sm mt-2" id="writeSubmitBtn" onclick="evalWrite()">✏️ AI 批改</button>
          <div id="writeFeedback" style="display:none" class="mt-3 p-3 rounded-3" style="background:#f0fdf4;border-left:4px solid #22c55e"></div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- Bottom Action Bar -->
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
let lesson = null;
let recognition = null;
let shouldRecord = false;
let finalTranscript = '';

// Tab switching
document.querySelectorAll('[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.remove('active'));
    ['vocab','listen','speak','write'].forEach(t => {
      document.getElementById('tab-' + t).style.display = 'none';
    });
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).style.display = 'block';
  });
});

window.onload = async () => {
  try {
    const resp = await fetch('/api/lesson');
    lesson = await resp.json();
    renderLesson(lesson);
  } catch(e) {
    document.getElementById('loadingBox').innerHTML =
      '<div class="text-danger p-3">⚠️ 載入失敗：' + e.message + '<br><button class="btn btn-primary mt-2" onclick="location.reload()">重試</button></div>';
  }
};

function renderLesson(l) {
  document.getElementById('loadingBox').style.display = 'none';
  document.getElementById('lessonBox').style.display = 'block';
  document.getElementById('bottomBar').style.display = 'block';
  document.getElementById('encouragement').textContent = l.encouragement || '今天也要加油！';

  // Vocabulary
  document.getElementById('vocabContent').innerHTML = l.vocabulary.map(v => `
    <div class="vocab-item">
      <div class="d-flex justify-content-between align-items-start">
        <span class="fs-5 fw-bold text-primary">${v.word}</span>
        <span class="badge" style="background:#dbeafe;color:#1e40af">${v.zh}</span>
      </div>
      <div class="ipa mt-1">${v.ipa || ''}</div>
      <div class="mt-2 small fst-italic text-secondary">"${v.example}"</div>
      <div class="mt-1 small" style="color:#7c3aed">💡 ${v.tip}</div>
    </div>
  `).join('');

  // Listening
  document.getElementById('listenScenario').textContent = '📋 ' + l.listening.scenario;
  document.getElementById('dialogueLines').innerHTML = l.listening.dialogue.map(d => `
    <div class="dialogue-line">
      <span class="${d.speaker === 'Nurse' ? 'speaker-nurse' : 'speaker-patient'}">${d.speaker}:</span>
      <span class="ms-2">${d.text}</span>
    </div>
  `).join('');

  // Speaking
  document.getElementById('speakScenario').textContent = l.speaking.scenario;
  document.getElementById('speakTask').textContent = l.speaking.task;
  document.getElementById('speakPhrases').innerHTML = l.speaking.key_phrases.map(p =>
    `<span class="badge me-1 mb-1" style="background:#dbeafe;color:#1e40af;font-size:.85rem">${p}</span>`
  ).join('');
  document.getElementById('watchOut').textContent = l.speaking.watch_out;
  document.getElementById('sampleText').textContent = l.speaking.sample;

  // Writing
  document.getElementById('writeTip').textContent = l.writing.tip;
  document.getElementById('writeBefore').textContent = l.writing.before;
  document.getElementById('writeAfter').textContent = l.writing.after;
  document.getElementById('writeTask').textContent = l.writing.task;
}

// Listening
async function playListening() {
  const btn = document.getElementById('playBtn');
  btn.disabled = true;
  btn.textContent = '⏳ 生成語音中...';
  const text = lesson.listening.dialogue.map(d => d.speaker + ' says: ' + d.text).join('. ');
  const resp = await fetch('/api/audio', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})
  });
  const data = await resp.json();
  if (data.ok) {
    const audio = document.getElementById('audioEl');
    audio.src = '/audio?t=' + Date.now();
    audio.style.display = 'block';
    audio.play();
    btn.textContent = '▶ 重新播放';
    btn.disabled = false;
    document.getElementById('showDialogueBtn').style.display = 'inline';
    audio.onended = () => {
      document.getElementById('listenQs').innerHTML = renderQuestions(lesson.listening.questions);
      document.getElementById('listenQs').style.display = 'block';
      document.getElementById('showAnswerBtn').style.display = 'inline';
    };
  } else {
    btn.textContent = '⚠️ 失敗，請重試';
    btn.disabled = false;
  }
}

function renderQuestions(qs) {
  return '<div class="fw-semibold mb-2">測驗題</div>' + qs.map((q, i) => `
    <div class="mb-3">
      <div class="mb-2">${i + 1}. ${q.q}</div>
      ${q.options.map(o => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="q${i}" id="q${i}${o[0]}">
          <label class="form-check-label" for="q${i}${o[0]}">${o}</label>
        </div>`).join('')}
    </div>`).join('');
}

function showAnswers() {
  document.getElementById('listenAnswers').innerHTML =
    '<div class="fw-semibold mb-2">解答</div>' +
    lesson.listening.questions.map(q => `
      <div class="mb-2 small">
        <span class="badge bg-success me-1">${q.answer}</span>${q.explanation}
      </div>`).join('');
  document.getElementById('listenAnswers').style.display = 'block';
  document.getElementById('showAnswerBtn').style.display = 'none';
}

function toggleDialogue() {
  const d = document.getElementById('dialogueText');
  d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

// Speaking
function startRecord() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert('請使用 Chrome 或 Edge 瀏覽器以使用語音功能'); return; }
  finalTranscript = '';
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  document.getElementById('recordStatus').textContent = '🔴 錄音中...';
  recognition = new SR();
  recognition.lang = 'en-US';
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onresult = e => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalTranscript += e.results[i][0].transcript + ' ';
      else interim += e.results[i][0].transcript;
    }
    document.getElementById('transcriptText').textContent = finalTranscript + interim;
    document.getElementById('transcriptBox').style.display = 'block';
  };
  recognition.onend = () => {
    // Chrome 靜音後自動停止 → 重啟（startBtn 仍 disabled 代表還在錄）
    if (document.getElementById('startBtn').disabled) {
      try { recognition.start(); } catch(e) {}
    }
  };
  recognition.start();
}

function stopRecord() {
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  document.getElementById('recordStatus').textContent = '錄音完成，可按 AI 評分';
  if (recognition) {
    try { recognition.onend = null; recognition.abort(); } catch(e) {}
    recognition = null;
  }
  document.getElementById('transcriptText').textContent = finalTranscript.trim();
  if (finalTranscript.trim()) document.getElementById('transcriptBox').style.display = 'block';
}

async function evalSpeak() {
  const spoken = document.getElementById('transcriptText').textContent.trim();
  if (!spoken) { alert('請先錄音'); return; }
  const btn = event.target;
  btn.disabled = true; btn.textContent = '評分中...';
  const resp = await fetch('/api/evaluate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({spoken, scenario: lesson.speaking.scenario, sample: lesson.speaking.sample})
  });
  const f = await resp.json();
  const stars = '⭐'.repeat(f.score) + '☆'.repeat(5 - f.score);
  document.getElementById('speakFeedback').innerHTML = `
    <div class="fw-bold mb-2">${stars}  ${f.score} / 5</div>
    <div class="mb-1">✅ ${f.good}</div>
    <div class="mb-1">📈 ${f.improve}</div>
    <div class="mb-1">📝 ${f.vocabulary}</div>
    <div class="text-primary small mt-2">🎯 OET 提示：${f.oet_tip}</div>
  `;
  document.getElementById('speakFeedback').style.display = 'block';
  btn.disabled = false; btn.textContent = '重新評分';
}

function toggleSample() {
  const s = document.getElementById('speakSample');
  s.style.display = s.style.display === 'none' ? 'block' : 'none';
}

// Writing eval
async function evalWrite() {
  const answer = document.getElementById('writeAnswer').value.trim();
  if (!answer) { alert('請先輸入你的答案'); return; }
  const btn = document.getElementById('writeSubmitBtn');
  btn.disabled = true; btn.textContent = '批改中...';
  const resp = await fetch('/api/evaluate-writing', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      answer,
      task: document.getElementById('writeTask').textContent,
      tip: document.getElementById('writeTip').textContent
    })
  });
  const f = await resp.json();
  const stars = '⭐'.repeat(f.score) + '☆'.repeat(5 - f.score);
  document.getElementById('writeFeedback').innerHTML = `
    <div class="fw-bold mb-2">${stars} ${f.score}/5 &nbsp;<span class="badge" style="background:#dbeafe;color:#1e40af;font-size:.8rem">${f.oet_level}</span></div>
    <div class="mb-1 small">📝 <b>文法</b>：${f.grammar}</div>
    <div class="mb-1 small">📖 <b>用詞</b>：${f.vocabulary}</div>
    <div class="mb-1 small">🏗️ <b>結構</b>：${f.structure}</div>
    <div class="mt-2 p-2 rounded-2" style="background:#f0fdf4;border-left:3px solid #22c55e">
      <div class="small fw-semibold text-success mb-1">✓ 改寫示範</div>
      <div class="small fst-italic">${f.rewrite}</div>
    </div>
    <div class="mt-2 small text-muted">💬 ${f.summary}</div>
  `;
  document.getElementById('writeFeedback').style.display = 'block';
  btn.disabled = false; btn.textContent = '✏️ 重新批改';
}

// Actions
async function markComplete() {
  const resp = await fetch('/api/complete', {method: 'POST'});
  const d = await resp.json();
  location.reload();
}

async function markTired() {
  if (!confirm('記錄今天休息？連續天數將重置為 0。')) return;
  await fetch('/api/tired', {method: 'POST'});
  location.reload();
}
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
