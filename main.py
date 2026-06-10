#!/usr/bin/env python3
"""OET Trainer - Daily OET 365+ preparation system for nursing professionals"""

import json
import os
import re
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
    "completed_dates": [], "missed_dates": [], "weak_areas": ["speaking", "writing"],
    "last_incomplete_tabs": [],   # tabs not done when user clicked "tired"
    "last_tired_date": None       # date of the tired session
}

def load_p():
    if not PROGRESS_FILE.exists():
        return dict(DEFAULT_PROGRESS)
    return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))

def save_p(p):
    PROGRESS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")

def get_today(req=None):
    """Return today's date from client_date param (local timezone), fallback to server date."""
    if req is not None:
        cd = req.args.get('client_date')
        if not cd and req.is_json:
            try:
                cd = (req.get_json(silent=True) or {}).get('client_date')
            except Exception:
                pass
        if cd:
            try:
                cd_dt = date.fromisoformat(cd)
                # Reject dates more than 2 days in the future (max legit timezone offset)
                if (cd_dt - date.today()).days <= 2:
                    return cd_dt
            except ValueError:
                pass
    return date.today()

def init_p(p, today=None):
    if not p.get("start_date"):
        p["start_date"] = (today or date.today()).isoformat()
        p["current_day"] = 1
        save_p(p)

def check_missed(p, today=None):
    if not p.get("last_session"):
        return []
    last = date.fromisoformat(p["last_session"])
    today = today or date.today()
    missed = []
    for i in range(1, (today - last).days):  # exclude today — user may still complete it
        d = (last + timedelta(days=i)).isoformat()
        if d not in p["completed_dates"] and d not in p["missed_dates"]:
            p["missed_dates"].append(d)
            missed.append(d)
    if missed:
        p["streak"] = 0  # break streak on any missed day
        save_p(p)
    return missed

def get_phase(day):
    if day <= 90:
        return 1
    if day <= 180:
        return 2
    return 3

def _parse_ai_json(text):
    """Robustly extract and parse JSON from AI response text."""
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 1:
                text = part.lstrip("json").strip()
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            return json.loads(text[:e.pos])
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group(0))
        raise

# ─── AI Services ──────────────────────────────────────────────────────────────

def _build_compensation_prompt(incomplete, native):
    if not incomplete:
        return ""
    lines = ["═══ PREVIOUS SESSION COMPENSATION ═══",
             f"Student quit early last session and did NOT complete: {', '.join(incomplete)}.",
             "To compensate, add the following WITHIN the normal JSON structure:"]
    if "vocab" in incomplete:
        lines.append("• VOCABULARY: add 2 EXTRA words (total 5 items in vocabulary array) — label them with an extra 'bonus' flag")
    if "listen" in incomplete:
        lines.append("• LISTENING: add a 4th question (type: recall — what did the nurse say?)")
    if "read" in incomplete:
        lines.append("• READING: add a 4th question (type: author's tone or purpose in a specific sentence)")
    if "speak" in incomplete:
        lines.append("• SPEAKING: add a 'remedial_tip' field: one extra strategy sentence for nurses who struggle — in " + native)
    if "write" in incomplete:
        lines.append("• WRITING: add a 'remedial_tip' field: one extra 'before/after' sentence showing the most common error — in " + native)
    return "\n".join(lines)

def _build_compensation_json(incomplete, native):
    if not incomplete:
        return '"none": true'
    parts = []
    if "vocab" in incomplete:
        parts.append(f'"vocab_note": "2 extra vocabulary words added above — review carefully in {native}"')
    if "listen" in incomplete:
        parts.append(f'"listen_note": "Extra Q4 added — review all 4 questions in {native}"')
    if "read" in incomplete:
        parts.append(f'"read_note": "Extra Q4 added — tone/purpose question in {native}"')
    if "speak" in incomplete:
        parts.append(f'"speak_tip": "one extra speaking strategy for this scenario type — in {native}"')
    if "write" in incomplete:
        parts.append(f'"write_tip": "one extra before/after correction example — in {native}"')
    return ", ".join(parts) if parts else '"none": true'

def generate_lesson(p, lang="zh-TW", exam_date=None, focus=None, last_incomplete=None, today=None, missed_days=0):
    today = today or date.today().isoformat()
    # Include focus and missed-days flag in cache key
    focus_key = "_".join(sorted(focus)) if focus else "all"
    return_flag = "_return" if missed_days and missed_days > 0 else ""
    cache_file = BASE_DIR / f"today_lesson_{lang}_{focus_key}{return_flag}.json"
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

    # ── Exam countdown context ──────────────────────────────────────
    days_to_exam = None
    exam_context = "No exam date set — maintain steady daily practice."
    if exam_date:
        try:
            exam_dt = date.fromisoformat(exam_date)
            days_to_exam = (exam_dt - date.fromisoformat(today)).days
            if days_to_exam <= 0:
                exam_context = "Exam date has passed — review mode."
            elif days_to_exam <= 14:
                exam_context = (f"⚠️ EXAM IN {days_to_exam} DAYS — CRITICAL MODE: Every lesson must maximise exam readiness. "
                                f"Focus on exam technique, time management, eliminating careless errors, and highest-yield skills. "
                                f"Use timed practice. Be strict with Band B benchmarks.")
            elif days_to_exam <= 30:
                exam_context = (f"Exam in {days_to_exam} days — INTENSIVE PHASE: Prioritise weakest sub-tests, "
                                f"practise under timed conditions, reinforce OET Band B benchmarks. "
                                f"Include at least one exam-strategy tip in this lesson.")
            elif days_to_exam <= 90:
                exam_context = (f"Exam in {days_to_exam} days — CORE PHASE: Systematic skill development. "
                                f"Address weak areas methodically. Build vocabulary breadth and letter-writing accuracy.")
            else:
                exam_context = (f"Exam in {days_to_exam} days — FOUNDATION PHASE: Build strong clinical English habits. "
                                f"Focus on vocabulary acquisition, OET format familiarity, and natural clinical communication.")
        except ValueError:
            pass

    # ── Focus areas context ─────────────────────────────────────────
    all_skills = ["reading", "listening", "speaking", "writing"]
    if not focus:
        focus = all_skills
    focused = [s for s in focus if s in all_skills]
    if not focused:
        focused = all_skills

    def skill_boost(skill):
        if skill in focused:
            return f"⭐ PRIORITY: Add an extra exam strategy tip or a second example specifically for {skill}."
        return ""

    writing_criterion = ["Purpose & Organisation", "Content & Accuracy", "Conciseness & Clarity", "Genre & Style (Register)"][day % 4]
    listen_type = ["history-taking consultation", "discharge instruction", "medication counselling", "post-operative assessment", "patient concern/refusal management"][day % 5]
    speak_type = ["explaining a clinical procedure", "discharge teaching", "medication counselling", "eliciting patient history", "managing patient anxiety or refusal"][day % 5]

    # ── Difficulty calibration & encouragement ──────────────────────
    if days_to_exam is not None and days_to_exam > 0:
        if days_to_exam <= 14:
            difficulty_ctx = (
                f"DIFFICULTY: EXAM-CRITICAL. {days_to_exam} days to exam. "
                f"Vocabulary = highest OET frequency (must appear in multiple official OET papers). "
                f"Listening = exam-grade dialogues with subtle, clinically plausible distractors. "
                f"Speaking = challenging scenarios requiring advanced empathy & elicitation strategies. "
                f"Reading = dense academic prose; questions demand tight inference. "
                f"Every tip must directly translate to marks on exam day. No beginner scaffolding."
            )
            enc_instr = (f"1 short urgent sentence in {native}: student has {days_to_exam} days left — "
                         f"express final-sprint energy, confidence, and laser focus. No generic warmth.")
        elif days_to_exam <= 30:
            difficulty_ctx = (
                f"DIFFICULTY: INTENSIVE. {days_to_exam} days to exam. "
                f"Vocabulary = exam-high-frequency clinical terms. "
                f"Scenarios = exam-style but slightly scaffolded. "
                f"Every explanation prioritises exam technique over depth. "
                f"Include at least one timed-practice or exam-strategy reminder per section."
            )
            enc_instr = (f"1 sentence in {native}: {days_to_exam} days remain — motivate with urgency AND calm confidence. "
                         f"Reference the specific day count.")
        elif days_to_exam <= 90:
            difficulty_ctx = (
                f"DIFFICULTY: CORE-BUILDING. {days_to_exam} days to exam. "
                f"Vocabulary = frequently tested clinical terms, moderate complexity. "
                f"Scenarios = realistic ward situations with developing complexity. "
                f"Blend skill-building with targeted exam awareness."
            )
            enc_instr = (f"1 sentence in {native}: acknowledge {days_to_exam} days of focused practice ahead. "
                         f"Motivating but steady — systematic progress is the key.")
        else:
            difficulty_ctx = (
                f"DIFFICULTY: FOUNDATION. {days_to_exam} days to exam — no rush. "
                f"Vocabulary = broad clinical spectrum to build comprehensive word knowledge. "
                f"Scenarios = accessible, confidence-building, emphasis on natural clinical communication. "
                f"Prioritise depth of explanation, memory strategies, and habit formation over exam speed."
            )
            enc_instr = (f"1 sentence in {native}: warmly acknowledge the long journey of {days_to_exam} days — "
                         f"daily habit and consistency are the student's biggest strength right now.")
    else:
        difficulty_ctx = (
            "DIFFICULTY: BALANCED. No exam date set. "
            "Mix foundational vocabulary with moderate exam-style content. "
            "Scenarios should build confidence while introducing OET format."
        )
        enc_instr = f"1 warm motivating sentence for Day {day} in {native}. Focus on steady daily progress."

    # ── Phase test results adaptive boost ──────────────────────────
    phase_results = p.get("phase_results", {})
    phase_weak_ctx = ""
    if phase_results:
        latest_ph = str(max(int(k) for k in phase_results.keys()))
        pr = phase_results[latest_ph]
        p_weak = pr.get("weak_areas", [])
        p_scores = pr.get("scores", {})
        if p_weak:
            score_str = ", ".join(f"{k}:{v}/3" for k, v in p_scores.items())
            phase_weak_ctx = (
                f"\n═══ PHASE {latest_ph} ASSESSMENT RESULTS ═══\n"
                f"Scores: {score_str}\n"
                f"WEAK AREAS: {', '.join(p_weak)}\n"
                f"ACTION: For each weak area, raise question difficulty one level, add a targeted diagnostic tip "
                f"that directly addresses the failure pattern, and include a bonus example. "
                f"Also adjust encouragement to acknowledge improvement in {', '.join(p_weak)}."
            )
            # Boost enc_instr to mention weak areas
            enc_instr += (f" Acknowledge Phase {latest_ph} results: student struggled with "
                          f"{', '.join(p_weak)} — express confidence they will improve with today's targeted practice.")

    # ── Missed-days adjustment ──────────────────────────────────────
    missed_ctx = ""
    if missed_days and missed_days > 0:
        if missed_days >= 7:
            enc_instr = (
                f"1–2 sentences in {native}: the student was away for {missed_days} days. "
                f"Welcome them back with absolute warmth and zero judgment. "
                f"Acknowledge the long break, reassure them it's never too late, and express full confidence "
                f"they will rebuild momentum starting today. Inspiring, not guilt-inducing."
            )
            missed_ctx = (
                f"\n═══ STUDENT RETURN AFTER {missed_days}-DAY ABSENCE ═══\n"
                f"Student missed {missed_days} consecutive days. This is a re-engagement session.\n"
                f"DIFFICULTY ADJUSTMENT: Reduce difficulty slightly — use clear vocabulary, accessible clinical scenarios, "
                f"familiar nursing contexts. Priority is re-engagement and rebuilding confidence, not maximum challenge.\n"
                f"ENCOURAGEMENT: Must warmly welcome them back without guilt. Emphasise every day counts."
            )
        elif missed_days >= 3:
            enc_instr = (
                f"1 sentence in {native}: welcome back after missing {missed_days} days. "
                f"Gentle, warm, no guilt — just encourage them to restart momentum today."
            )
            missed_ctx = (
                f"\n═══ STUDENT RETURN AFTER {missed_days}-DAY ABSENCE ═══\n"
                f"Student missed {missed_days} days. Gently ease them back in — avoid overwhelming difficulty. "
                f"Keep vocabulary and scenarios familiar and confidence-building."
            )
        else:
            enc_instr = (
                f"1 sentence in {native}: briefly acknowledge missing {missed_days} day(s), "
                f"then immediately refocus on today with positive energy."
            )
            missed_ctx = (
                f"\n═══ STUDENT RETURN AFTER {missed_days}-DAY ABSENCE ═══\n"
                f"Student missed {missed_days} day(s). Keep content at normal difficulty but acknowledge the gap in encouragement."
            )

    prompt = f"""You are a senior OET examiner, item writer, and Band B coach with 15+ years' experience. You have written official OET practice tests and trained hundreds of nurses to pass.

Student profile: Nurse with B2 English, native language {native}, targeting OET 365+ (Band B). Day {day}/270 of training. Phase {ph}: {phases[ph]}.
ALL explanations, tips, encouragement must be written entirely in {native}. English only in dialogue, articles, sample answers, and key_phrases.

═══ EXAM TIMELINE ═══
{exam_context}

═══ LESSON DIFFICULTY & CONTENT CALIBRATION ═══
{difficulty_ctx}{phase_weak_ctx}{missed_ctx}

═══ STUDENT'S PRIORITY SKILLS (focus areas chosen by student) ═══
Strengthening: {', '.join(focused)}
{skill_boost('listening')}
{skill_boost('speaking')}
{skill_boost('reading')}
{skill_boost('writing')}

Today's Listening type: {listen_type}
Today's Speaking type: {speak_type}
Today's Writing criterion: {writing_criterion}

{_build_compensation_prompt(last_incomplete or [], native)}

═══════════ OFFICIAL OET CONTENT RULES ═══════════

VOCABULARY (3 words — OET sub-test frequency):
• Choose nursing-specific clinical terms that genuinely appear in OET reading/listening passages
• IPA must be phonemically accurate (use standard IPA notation)
• native = precise professional translation (not just dictionary gloss)
• example = realistic ward/clinic sentence (NOT textbook style) showing the word in clinical context
• tip = one common error nurses from {native} background make OR a proven memory strategy — in {native}

LISTENING (OET Part A/C hybrid — simulate exam conditions):
• Write a realistic 7-line nurse–patient or nurse–carer dialogue for: {listen_type}
• Dialogue must contain: specific clinical facts (dates, dosages, symptoms, names) that can be tested
• Language must be authentic spoken English (contractions, natural pacing markers)
• Q1 — DETAIL question: tests a specific fact stated explicitly in the dialogue (one option is clearly supported, others are plausible distractor clinical facts)
• Q2 — INFERENCE question: tests meaning not stated directly; student must reason from evidence in dialogue
• Q3 — COMMUNICATION STRATEGY question: use format "Why does the nurse say '[exact quote from dialogue]'?" — tests the communicative PURPOSE of a nurse utterance (to reassure / to elicit / to clarify / to instruct / to check understanding)
• All 4 options for every question must be clinically plausible — never obviously absurd

SPEAKING (OET Role-play — Nurse Candidate Card format):
• scenario = patient card: full name, age, gender, ward/setting, presenting complaint, relevant history in 2 sentences
• task = candidate instruction card with 4 numbered action points (what the nurse MUST cover in the role-play)
• sample = 4–5 sentence model nurse response showing: opening greeting, rapport/empathy marker, at least 2 task points addressed, checking understanding at end — OET Band B language
• key_phrases = 4 COMPLETE sentences the nurse should produce (not fragments) — naturally clinical, appropriate register
• watch_out = the #1 linguistic error nurses from {native} background make in {speak_type} scenarios — with a 1-sentence fix in {native}

READING (OET Part C — academic healthcare text):
• Write a 7–9 sentence professional healthcare/medical journal article on a nursing-relevant topic
• Register: formal academic English, appropriate for a healthcare professional journal
• Q1 — GLOBAL/PURPOSE question: "What is the main purpose/argument of the passage?" (options differ in scope or focus, not fact)
• Q2 — VOCABULARY-IN-CONTEXT: "The word '[exact word from article]' as used in the passage most nearly means..." (options explore synonymic shifts, NOT definitions from a dictionary)
• Q3 — INFERENCE/IMPLICATION: "What can be inferred about...?" or "The author implies that..." (student must reason beyond stated content)
• Every question must be answerable ONLY from the article — no specialist knowledge required

WRITING (OET Referral/Transfer Letter — criterion focus: {writing_criterion}):
• tip = specific examiner insight about {writing_criterion} — what examiners deduct marks for, and how to score full marks — in {native}
• before = a real weak student sentence showing the problem with {writing_criterion}
• after = the Band B corrected version (same content, improved {writing_criterion})
• task = a complete clinical scenario for a referral letter including:
  - Referring clinician & patient demographics (name, DOB, gender)
  - Primary diagnosis or presenting condition
  - Key clinical findings (at least 3 specific details: vitals, test results, symptoms)
  - Relevant medical/surgical history
  - Current medications with doses
  - Reason for referral and urgency level (routine / urgent / emergency)
  - Receiving specialist/department
  - Instruction: student writes the body of the referral letter (purpose paragraph + clinical details paragraph + request paragraph)

═══════════ OUTPUT FORMAT ═══════════
Return ONLY valid JSON — no markdown fences, no extra text, no comments:
{{
  "date": "{today}",
  "day": {day},
  "phase": {ph},
  "encouragement": "{enc_instr}",
  "vocabulary": [
    {{"word":"nursing term","ipa":"/accurate IPA/","native":"translation in {native}","example":"clinical sentence in English","tip":"tip in {native}"}},
    {{"word":"nursing term","ipa":"/accurate IPA/","native":"translation in {native}","example":"clinical sentence in English","tip":"tip in {native}"}},
    {{"word":"nursing term","ipa":"/accurate IPA/","native":"translation in {native}","example":"clinical sentence in English","tip":"tip in {native}"}}
  ],
  "listening": {{
    "scenario": "Setting: [ward]. Clinical context: [what is happening]",
    "dialogue": [
      {{"speaker":"Nurse","text":"Natural opening — greeting and initial question"}},
      {{"speaker":"Patient","text":"Response with specific clinical detail (symptom/date/dose)"}},
      {{"speaker":"Nurse","text":"Follow-up clinical question"}},
      {{"speaker":"Patient","text":"More specific information or concern"}},
      {{"speaker":"Nurse","text":"Clarification, instruction, or empathy marker"}},
      {{"speaker":"Patient","text":"Further question or expression of worry"}},
      {{"speaker":"Nurse","text":"Reassurance, closing plan, or checking understanding"}}
    ],
    "questions": [
      {{"q":"Detail question about a specific fact in the dialogue","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A","explanation":"in {native}"}},
      {{"q":"Inference question — what can be concluded?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"B","explanation":"in {native}"}},
      {{"q":"Why does the nurse say '[exact nurse quote from dialogue]'?","options":["A. To reassure the patient about ...","B. To instruct the patient to ...","C. To clarify whether ...","D. To elicit information about ..."],"answer":"C","explanation":"in {native}"}}
    ]
  }},
  "speaking": {{
    "scenario": "Patient: [Full Name], [Age]yo [Gender]. Ward: [setting]. Presenting: [chief complaint]. History: [1–2 relevant facts].",
    "task": "Your task as the nurse:\\n1. [Action point 1]\\n2. [Action point 2]\\n3. [Action point 3]\\n4. [Action point 4]",
    "sample": "5-sentence model response at OET Band B — opens with greeting+empathy, covers 2+ task points, ends with comprehension check",
    "key_phrases": ["Complete nurse sentence 1 (opening/empathy)","Complete nurse sentence 2 (explaining)","Complete nurse sentence 3 (instructing/reassuring)","Complete nurse sentence 4 (checking understanding)"],
    "watch_out": "Specific error nurses from {native} background make in {speak_type} + one-sentence fix — in {native}"
  }},
  "reading": {{
    "title": "Specific professional article title",
    "article": "7–9 sentence OET Part C academic passage on a nursing-relevant topic",
    "questions": [
      {{"q":"What is the main purpose/argument of the passage?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A","explanation":"in {native}"}},
      {{"q":"The word \\"[exact word from article]\\" as used in the passage most nearly means...","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"B","explanation":"in {native}"}},
      {{"q":"What can be inferred from the passage about [topic]?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"C","explanation":"in {native}"}}
    ]
  }},
  "writing": {{
    "tip": "OET {writing_criterion} examiner tip — what costs marks and how to avoid it — in {native}",
    "before": "Weak student sentence (register/structure/clarity problem)",
    "after": "OET Band B corrected version of the same sentence",
    "task": "Write the body of a referral letter for: Patient [Full Name], DOB [date], [Gender]. Diagnosis: [condition]. Key findings: [3 specific clinical details]. History: [relevant background]. Medications: [drugs + doses]. Referring to: [specialist/department] for [reason]. Urgency: [routine/urgent/emergency]. Your letter must include: (1) purpose paragraph, (2) clinical details paragraph, (3) referral request paragraph."
  }},
  "compensation": {{{_build_compensation_json(last_incomplete or [], native)}}}
}}"""

    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    last_exc = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8096,
                messages=[{"role": "user", "content": prompt}]
            )
            lesson = _parse_ai_json(resp.content[0].text)
            break
        except (json.JSONDecodeError, Exception) as e:
            last_exc = e
            if attempt == 2:
                raise last_exc
    cache_file.write_text(json.dumps(lesson, indent=2, ensure_ascii=False), encoding="utf-8")
    return lesson

def generate_phase_test(phase, lang="zh-TW"):
    cache_file = BASE_DIR / f"phase_test_{phase}_{lang}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("phase") == phase:
            return cached
    lang_info = LANGUAGES.get(lang, LANGUAGES["zh-TW"])
    native = lang_info["prompt"]
    phase_names = {1:"Foundation (Days 1–90)",2:"Core Skills (Days 91–180)",3:"Exam Mode (Days 181–270)"}
    prompt = f"""You are a senior OET examiner. Generate a Phase {phase} ({phase_names.get(phase,'Foundation')}) assessment test for a nurse whose native language is {native}.
ALL instructions, explanations, tips in {native}. All clinical content and questions in English.
Return ONLY valid JSON (no fences):
{{
  "phase": {phase},
  "title": "Phase {phase} 結業測驗 — short subtitle in {native}",
  "summary_intro": "2 sentences in {native}: what this test covers and why it matters",
  "vocab_questions": [
    {{"q":"The clinical term '[word]' is best defined as...","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A","explanation":"in {native}"}},
    {{"q":"Which sentence uses '[word]' correctly in a clinical context?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"B","explanation":"in {native}"}},
    {{"q":"A nurse says '[clinical phrase]'. This means...","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"C","explanation":"in {native}"}}
  ],
  "listening": {{
    "scenario": "Setting: [ward]. Clinical context: [2 sentences].",
    "dialogue": [
      {{"speaker":"Nurse","text":"..."}},{{"speaker":"Patient","text":"..."}},
      {{"speaker":"Nurse","text":"..."}},{{"speaker":"Patient","text":"..."}},
      {{"speaker":"Nurse","text":"..."}},{{"speaker":"Patient","text":"..."}},
      {{"speaker":"Nurse","text":"..."}}
    ],
    "questions": [
      {{"q":"Detail question about specific fact","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A","explanation":"in {native}"}},
      {{"q":"Inference question","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"B","explanation":"in {native}"}},
      {{"q":"Why does the nurse say '[exact quote]'?","options":["A. To reassure...","B. To instruct...","C. To clarify...","D. To elicit..."],"answer":"C","explanation":"in {native}"}}
    ]
  }},
  "reading": {{
    "article": "7-8 sentence formal healthcare journal article on a nursing-relevant topic",
    "questions": [
      {{"q":"Main purpose of this passage?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"A","explanation":"in {native}"}},
      {{"q":"The word '[word]' most nearly means...","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"B","explanation":"in {native}"}},
      {{"q":"What can be inferred from the passage?","options":["A. ...","B. ...","C. ...","D. ..."],"answer":"C","explanation":"in {native}"}}
    ]
  }}
}}"""
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    last_exc = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=6000,
                messages=[{"role":"user","content":prompt}]
            )
            data = _parse_ai_json(resp.content[0].text)
            cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return data
        except (json.JSONDecodeError, Exception) as e:
            last_exc = e
            if attempt == 2:
                raise last_exc

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
    prompt = f"""You are a senior OET Speaking examiner with 15+ years' experience marking candidate role-plays.
Student's native language: {native}. Write ALL feedback in {native}. English only for exact quotes and correction examples.

Scenario: {scenario}
Band B model answer: {sample}
Student actually said: {spoken}

OET OFFICIAL SPEAKING CRITERIA (score each 1–5):
1. Intelligibility — pronunciation clarity, word stress, sentence rhythm. Could a native speaker understand without effort?
2. Fluency — natural pace, appropriate pausing, minimal disruptive hesitation (um, er), coherent delivery
3. Appropriateness of Language — correct clinical register (not too formal/informal), empathy markers, softeners (e.g. "I understand", "Would you mind"), appropriate politeness
4. Resources of Grammar and Expression — accurate grammar, range and precision of clinical vocabulary, avoidance of repetition

SCORING GUIDE:
5 = Band A+ — Natural, all criteria fully met, examiner would feel clinically confident
4 = Band B — Clear clinical communication, task covered, minor errors don't impede meaning
3 = Borderline Band B — Task addressed, understandable, some gaps in register or accuracy
2 = Band C — Significant communication breakdown, task partially missed
1 = Band D/E — Unintelligible or completely off-task

RULE: If student addressed the clinical scenario at all, minimum score is 3. Be encouraging but clinically honest.

Return ONLY valid JSON (no markdown, no trailing text):
{{
  "score": 3,
  "band": "B",
  "criteria": {{
    "intelligibility": {{"score": 3, "comment": "specific pronunciation/stress observation in {native}"}},
    "fluency": {{"score": 3, "comment": "pacing and hesitation feedback in {native}"}},
    "appropriateness": {{"score": 3, "comment": "register and empathy marker feedback in {native}"}},
    "grammar_vocab": {{"score": 3, "comment": "grammar accuracy and vocabulary precision feedback in {native}"}}
  }},
  "good": "most effective thing student did — cite specific words/phrases they used — in {native}",
  "improve": "the ONE highest-priority fix with an exact before/after English example — explanation in {native}",
  "vocabulary": "a more clinical English phrase they should practise (show exact English sentence)",
  "oet_tip": "one practical OET exam-day strategy for this scenario type — in {native}"
}}"""
    last_exc = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            return _parse_ai_json(resp.content[0].text)
        except (json.JSONDecodeError, Exception) as e:
            last_exc = e
            if attempt == 2:
                raise last_exc

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    return r

@app.route("/")
def index():
    p = load_p()
    today_dt = get_today(request)
    today = today_dt.isoformat()
    init_p(p, today_dt)
    check_missed(p, today_dt)
    p = load_p()  # reload after check_missed may have saved
    done = today in p.get("completed_dates", [])
    tired = today in p.get("missed_dates", [])
    day = p.get("current_day", 1)
    ph = get_phase(day)
    phase_name = {1: "Foundation", 2: "Core Skills", 3: "Exam Mode"}[ph]
    pct = round(day / 270 * 100, 1)
    # Absent days = days between last completed session and today (exclusive of both)
    absent_days = 0
    last_session = p.get("last_session")
    if last_session and not done:
        try:
            absent_days = max(0, (today_dt - date.fromisoformat(last_session)).days - 1)
        except ValueError:
            pass
    # Determine if last session was incomplete (tired, not today)
    last_tired_date = p.get("last_tired_date")
    last_incomplete = p.get("last_incomplete_tabs", [])
    show_incomplete = bool(last_incomplete) and last_tired_date != today
    return render_template_string(
        HTML,
        streak=p.get("streak", 0),
        day=day,
        phase=ph,
        phase_name=phase_name,
        total=p.get("total_completed", 0),
        pct=pct,
        missed_count=absent_days,
        done=done,
        tired=tired,
        show_incomplete=show_incomplete,
        last_incomplete=last_incomplete,
    )

@app.route("/api/lesson")
def api_lesson():
    lang = request.args.get("lang", "zh-TW")
    if lang not in LANGUAGES:
        lang = "zh-TW"
    exam_date = request.args.get("exam_date", None)
    focus_raw = request.args.get("focus", None)
    try:
        focus = json.loads(focus_raw) if focus_raw else None
    except (json.JSONDecodeError, TypeError):
        focus = None
    missed_days = min(max(request.args.get("missed_days", 0, type=int), 0), 365)
    p = load_p()
    today_dt = get_today(request)
    last_tired_date = p.get("last_tired_date")
    last_incomplete = p.get("last_incomplete_tabs", [])
    if last_tired_date:
        try:
            days_since = (today_dt - date.fromisoformat(last_tired_date)).days
            if days_since > 1:
                last_incomplete = []
        except ValueError:
            last_incomplete = []
    return jsonify(generate_lesson(p, lang, exam_date=exam_date, focus=focus, last_incomplete=last_incomplete, today=today_dt.isoformat(), missed_days=missed_days))

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
    spoken = d.get("spoken", "") if d else ""
    scenario = d.get("scenario", "") if d else ""
    sample = d.get("sample", "") if d else ""
    if not spoken:
        return jsonify({"error": "missing spoken"}), 400
    return jsonify(evaluate_speaking(spoken, scenario, sample, lang))

@app.route("/api/evaluate-writing", methods=["POST"])
def api_evaluate_writing():
    d = request.json
    lang = d.get("lang", "zh-TW")
    native = LANGUAGES.get(lang, LANGUAGES["zh-TW"])["prompt"]
    client = anthropic.Anthropic(api_key=get_config()["anthropic_api_key"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1800,
        messages=[{"role": "user", "content": f"""You are a senior OET Writing examiner with 15+ years' experience marking referral and transfer letters.
Student's native language: {native}. Write ALL feedback in {native}. English only for corrected sentences and rewrites.

Writing task: {d.get('task', '')}
Today's criterion focus: {d.get('tip', '')}
Student wrote: {d.get('answer', '')}

OET WRITING OFFICIAL CRITERIA (score each 0–7, displayed as 1–5 here):
1. Purpose & Organisation — Is the letter's reason for writing immediately clear? Logical paragraph structure?
2. Content & Accuracy — Are all clinically relevant details included? Are medical facts accurate and complete?
3. Conciseness & Clarity — Is information expressed efficiently without repetition or irrelevant detail?
4. Genre & Style (Register) — Appropriate formal professional register? Correct salutation/sign-off? Healthcare letter conventions followed?

SCORING GUIDE (overall):
5 = Band A — All 4 criteria excellent; examiner would send this letter without editing
4 = Band B — Meets OET standard; minor issues in 1-2 criteria; letter achieves its purpose
3 = Band C — Task partially addressed; 2+ criteria need improvement; would require editing before sending
2 = Band D — Significant gaps; letter may mislead or omit critical clinical information
1 = Band E — Does not function as a referral letter

Return ONLY valid JSON (no markdown):
{{
  "score": 3,
  "oet_band": "B",
  "criteria": {{
    "purpose": {{"score": 3, "comment": "feedback in {native}"}},
    "content": {{"score": 3, "comment": "feedback in {native}"}},
    "conciseness": {{"score": 3, "comment": "feedback in {native}"}},
    "genre_style": {{"score": 3, "comment": "feedback in {native}"}}
  }},
  "grammar": "key grammar issue with 1 before/after example — in {native}",
  "vocabulary": "better clinical English phrase to use (show exact English sentence)",
  "structure": "structural improvement suggestion — in {native}",
  "rewrite": "improved version of the weakest paragraph in the student's answer (English)",
  "summary": "one encouraging examiner closing comment — in {native}"
}}"""}]
    )
    return jsonify(_parse_ai_json(resp.content[0].text))

@app.route("/api/complete", methods=["POST"])
def api_complete():
    p = load_p()
    today = get_today(request).isoformat()
    if today not in p["completed_dates"]:
        p["completed_dates"].append(today)
        p["current_day"] += 1
        p["streak"] += 1
        p["total_completed"] += 1
        p["last_session"] = today
    # Clear incomplete record when today is fully completed
    p["last_incomplete_tabs"] = []
    p["last_tired_date"] = None
    save_p(p)
    return jsonify({"ok": True, "streak": p["streak"]})

@app.route("/api/tired", methods=["POST"])
def api_tired():
    p = load_p()
    d = request.json or {}
    today = get_today(request).isoformat()
    if today not in p["completed_dates"] and today not in p["missed_dates"]:
        p["missed_dates"].append(today)
        p["streak"] = 0
        p["last_session"] = today
    # Record which tabs were NOT completed for tomorrow's lesson adjustment
    tabs_done = d.get("tabs_done", {})
    all_tabs = ["vocab", "read", "listen", "speak", "write"]
    incomplete = [t for t in all_tabs if not tabs_done.get(t, False)]
    p["last_incomplete_tabs"] = incomplete
    p["last_tired_date"] = today
    save_p(p)
    return jsonify({"ok": True, "incomplete": incomplete})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    import glob as _glob
    default_p = {"current_day":1,"streak":0,"total_completed":0,"completed_dates":[],"missed_dates":[],"last_session":None,"phase_results":{},"weak_areas":[],"last_tired_date":None,"last_incomplete_tabs":[]}
    save_p(default_p)
    for f in _glob.glob(str(BASE_DIR / "today_lesson_*.json")) + _glob.glob(str(BASE_DIR / "phase_test_*.json")):
        try: os.remove(f)
        except: pass
    return jsonify({"ok": True})

@app.route("/api/phase-status")
def api_phase_status():
    p = load_p()
    current_day = p.get("current_day", 1)
    phase_results = p.get("phase_results", {})
    ph = get_phase(current_day)
    prev = ph - 1
    thresholds = {1: 90, 2: 180}
    if prev >= 1 and str(prev) not in phase_results and current_day > thresholds.get(prev, 0):
        return jsonify({"pending_phase": prev})
    return jsonify({"pending_phase": None})

@app.route("/api/phase-test")
def api_phase_test():
    phase = int(request.args.get("phase", 1))
    lang = request.args.get("lang", "zh-TW")
    if lang not in LANGUAGES:
        lang = "zh-TW"
    return jsonify(generate_phase_test(phase, lang))

@app.route("/api/phase-test/save", methods=["POST"])
def api_phase_test_save():
    p = load_p()
    d = request.json or {}
    phase = str(d.get("phase", 1))
    scores = d.get("scores", {})
    weak = [skill for skill, score in scores.items() if score < 2]
    if "phase_results" not in p:
        p["phase_results"] = {}
    p["phase_results"][phase] = {
        "date": get_today(request).isoformat(),
        "scores": scores,
        "weak_areas": weak
    }
    existing_weak = set(p.get("weak_areas", []))
    existing_weak.update(weak)
    p["weak_areas"] = list(existing_weak)
    # Remove phase test cache + lesson cache so next lesson regenerates with updated weak areas
    for lng in LANGUAGES:
        pt_f = BASE_DIR / f"phase_test_{phase}_{lng}.json"
        if pt_f.exists(): pt_f.unlink()
        for lf in BASE_DIR.glob(f"today_lesson_{lng}_*.json"):
            lf.unlink()
    save_p(p)
    return jsonify({"ok": True, "weak_areas": p["weak_areas"]})

# ─── HTML Template ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>不再卡關 B 級！OET 聽說讀寫全攻略</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🌸</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Ma+Shan+Zheng&family=Hachi+Maru+Pop&family=Gaegu:wght@400;700&family=Caveat:wght@400;700&family=Sriracha&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{
  /* ── Dried Rose Primary ── */
  --p:#8C4A50;--p-dark:#6B2D3A;--p-light:#FFF0EE;--p-mid:#F5E0DF;
  /* ── Semantic ── */
  --success:#5D8A6E;--danger:#B03A5A;--warn:#A07858;
  /* ── Per-Tab: Romantic Palette ── */
  --tab-vocab:#9E6B8A;--tab-vocab-bg:#F7EDF5;
  --tab-read:#5D8A6E;--tab-read-bg:#EEF6F1;
  --tab-listen:#8C7AA8;--tab-listen-bg:#F0ECF7;
  --tab-speak:#C25A6E;--tab-speak-bg:#FFEEF2;
  --tab-write:#A07858;--tab-write-bg:#FFF0E6;
  /* ── Surfaces: blush parchment ── */
  --bg:#FFE4E1;--surface:#FFFAF9;--surface2:#FFF0EE;
  --text:#4A3B32;--muted:#9A8A82;--border:#E8C8C6;
  --r:16px;--shadow:0 2px 16px rgba(140,74,80,.09),0 1px 4px rgba(0,0,0,.04);
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans','Noto Sans TC','Noto Sans JP','Noto Sans KR',Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{background:var(--bg);font-family:var(--font);font-size:21px;line-height:1.75;padding-bottom:108px;color:var(--text);-webkit-font-smoothing:antialiased}
a,button{-webkit-tap-highlight-color:transparent}

/* ── Loading Screen ── */
.loading-screen{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:2.5rem 1.5rem;text-align:center;margin-bottom:1rem}
.loader-wrap{position:relative;width:76px;height:76px;margin:0 auto 1.1rem}
.loader-ring{position:absolute;inset:0;border-radius:50%;border:5px solid rgba(140,74,80,.15);border-top-color:var(--p);border-right-color:var(--p);animation:spin .85s cubic-bezier(.5,0,.5,1) infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loader-icon{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:2rem;animation:pulse 1.8s ease-in-out infinite}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.12)}}
.loader-dots{display:flex;justify-content:center;gap:5px;margin:.9rem 0 1.1rem}
.loader-dots i{display:block;width:8px;height:8px;border-radius:50%;animation:dotBounce 1.3s ease-in-out infinite}
.loader-dots i:nth-child(1){background:#8C4A50}
.loader-dots i:nth-child(2){background:#DDA7A5;animation-delay:.18s}
.loader-dots i:nth-child(3){background:#C27878;animation-delay:.36s}
@keyframes dotBounce{0%,60%,100%{transform:translateY(0);opacity:.25}30%{transform:translateY(-9px);opacity:1}}
#loadingText{font-size:.96rem;font-weight:600;color:var(--text);margin-bottom:.25rem}
#loadingSubText{font-size:.8rem;color:var(--muted)}

/* ── Hero ── */
.hero{background:linear-gradient(145deg,#5C1D26 0%,#8C4A50 38%,#C27878 68%,#DDA7A5 100%);color:#fff;padding:1.35rem 1rem 1.8rem;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-80px;right:-50px;width:200px;height:200px;background:rgba(255,255,255,.06);border-radius:50%;pointer-events:none}
.hero::after{content:'';position:absolute;bottom:-40px;left:-30px;width:130px;height:130px;background:rgba(255,255,255,.04);border-radius:50%;pointer-events:none}
.stat-box{background:rgba(255,255,255,.18);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-radius:14px;padding:.75rem .35rem;text-align:center;border:1px solid rgba(255,255,255,.28);transition:background .2s}
.stat-num{font-size:1.65rem;font-weight:900;line-height:1;letter-spacing:-.03em}
.stat-label{font-size:.68rem;opacity:.82;margin-top:4px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}

/* ── Cards ── */
.card{background:var(--surface);border:none;border-radius:var(--r);box-shadow:var(--shadow);margin-bottom:1rem;overflow:hidden}
.card-header{background:var(--surface);border-bottom:1px solid var(--border);border-left:4px solid var(--p);padding:.9rem 1.1rem .9rem 1rem;font-weight:800;font-size:.95rem;color:var(--text);letter-spacing:.01em;display:flex;align-items:center;gap:.5rem}

/* ── Pill Tabs ── */
.nav-tabs{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:5px;gap:3px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;margin-bottom:1rem}
.nav-tabs::-webkit-scrollbar{display:none}
.nav-tabs .nav-link{color:var(--muted);border:none;padding:.48rem .85rem;font-weight:700;font-size:.85rem;border-radius:10px;transition:all .18s;white-space:nowrap;line-height:1.4}
.nav-tabs .nav-link:hover{color:var(--p);background:var(--p-light)}
/* Per-tab macaron active colours */
.nav-tabs .nav-item:nth-child(1) .nav-link.active{color:var(--tab-vocab);background:var(--tab-vocab-bg);box-shadow:0 2px 8px rgba(59,130,246,.18)}
.nav-tabs .nav-item:nth-child(2) .nav-link.active{color:var(--tab-read);background:var(--tab-read-bg);box-shadow:0 2px 8px rgba(5,150,105,.18)}
.nav-tabs .nav-item:nth-child(3) .nav-link.active{color:var(--tab-listen);background:var(--tab-listen-bg);box-shadow:0 2px 8px rgba(124,58,237,.18)}
.nav-tabs .nav-item:nth-child(4) .nav-link.active{color:var(--tab-speak);background:var(--tab-speak-bg);box-shadow:0 2px 8px rgba(225,29,72,.18)}
.nav-tabs .nav-item:nth-child(5) .nav-link.active{color:var(--tab-write);background:var(--tab-write-bg);box-shadow:0 2px 8px rgba(234,88,12,.18)}
.tab-pane{animation:tabIn .2s ease}
@keyframes tabIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

/* ── Encouragement ── */
#encouragement{background:linear-gradient(120deg,#f5f3ff,#fce7f3,#fff7ed);border-radius:14px;padding:1.1rem 1.2rem;color:#5b21b6;font-weight:600;font-size:1rem;line-height:1.6;text-align:center;border:1px solid #ddd6fe;box-shadow:0 2px 14px rgba(124,58,237,.1)}

/* ── Vocab Flip Cards ── */
.flip-card{perspective:1000px;cursor:pointer;margin-bottom:.8rem;user-select:none}
.flip-card-inner{position:relative;transition:transform .45s cubic-bezier(.4,0,.2,1);transform-style:preserve-3d}
.flip-card.flipped .flip-card-inner{transform:rotateY(180deg)}
.flip-card-front{backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:14px;padding:1.2rem;position:relative;background:linear-gradient(135deg,#FFF5F4 0%,#F5E0DF 55%,#FFEEF2 100%);border:1px solid #E8C8C6;min-height:165px;box-shadow:0 2px 12px rgba(140,74,80,.1)}
.flip-card-back{backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:14px;padding:1.2rem;position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,#EEF6F1 0%,#D4EBE0 55%,#EEF6F1 100%);border:1px solid #8DC4A8;transform:rotateY(180deg);box-shadow:0 2px 12px rgba(93,138,110,.1)}
.flip-hint{font-size:.72rem;color:var(--muted);position:absolute;top:.55rem;right:.75rem;opacity:.7;letter-spacing:.02em;background:rgba(140,74,80,.07);border-radius:8px;padding:.1rem .45rem}
.speak-btn{background:none;border:none;padding:0 .2rem;cursor:pointer;font-size:.95rem;opacity:.55;transition:opacity .15s;line-height:1}
.speak-btn:hover{opacity:1}

/* ── Dialogue ── */
.dialogue-line{display:flex;align-items:flex-start;gap:.6rem;padding:.5rem .3rem;border-radius:8px;transition:background .25s}
.dialogue-line:not(:last-child){border-bottom:1px solid #f1f5f9}
.dialogue-line.active-line{background:linear-gradient(90deg,#f5f3ff,#fce7f3);padding-left:.6rem;border-left:3px solid var(--p)}
.spk-badge{font-size:.7rem;font-weight:800;padding:.22rem .6rem;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:.15rem;letter-spacing:.03em}
.spk-nurse{background:#ede9fe;color:#6d28d9}
.spk-patient{background:#fce7f3;color:#be185d}

/* ── Speaking ── */
.phrase-tag{display:inline-block;background:var(--p-light);color:var(--p);border:1px solid var(--p-mid);border-radius:20px;padding:.22rem .7rem;font-size:.79rem;font-weight:500;margin:.15rem;transition:all .2s}
.phrase-tag.hit{background:#dcfce7;color:var(--success);border-color:#bbf7d0}
.phrase-tag.miss{background:#fee2e2;color:var(--danger);border-color:#fecaca}
.transcript-box{background:var(--surface2);border:1.5px solid var(--border);border-radius:12px;padding:.8rem 1rem;min-height:3.5rem;font-size:.93rem;line-height:1.65}
.word-hit{color:var(--success);font-weight:600}
.score-history{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.score-pill{background:var(--p-light);color:var(--p);border-radius:20px;padding:.2rem .7rem;font-size:.8rem;font-weight:700}
.score-pill.best{background:#dcfce7;color:#059669}

/* ── Info Boxes ── */
.feedback-box{background:linear-gradient(120deg,#f0fdf4,#ecfdf5);border-left:4px solid #10b981;border-radius:12px;padding:1rem 1.1rem;font-size:.95rem}
.info-box{background:linear-gradient(120deg,var(--p-light),#fce7f3);border-left:4px solid var(--p);border-radius:12px;padding:.9rem 1.1rem;font-size:.95rem}
.warn-box{background:linear-gradient(120deg,#fff7ed,#fffbeb);border-left:4px solid #f97316;border-radius:12px;padding:.9rem 1.1rem;font-size:.92rem}
.danger-box{background:linear-gradient(120deg,#fff1f2,#ffe4e6);border-left:4px solid var(--danger);border-radius:12px;padding:.9rem 1.1rem;font-size:.92rem}

/* ── Buttons ── */
.btn{font-family:var(--font);font-size:.92rem}
.btn-primary{background:linear-gradient(135deg,var(--p),#a855f7);border:none;border-radius:12px;font-weight:700;letter-spacing:.01em;box-shadow:0 2px 10px rgba(124,58,237,.3)}
.btn-primary:hover,.btn-primary:focus{background:linear-gradient(135deg,var(--p-dark),#9333ea);border:none;box-shadow:0 4px 16px rgba(124,58,237,.4);transform:translateY(-1px)}
.btn-primary:active{transform:translateY(0)}
.btn-success{border-radius:12px;font-weight:700}
.btn-outline-secondary{border-radius:12px;font-weight:600;font-size:.9rem}
.btn-link{font-size:.88rem}
#startBtn{border-radius:50px;padding:.6rem 1.6rem;font-weight:700;font-size:.93rem;min-height:44px;background:linear-gradient(135deg,#e11d48,#f97316);border:none;box-shadow:0 2px 12px rgba(225,29,72,.3)}
#startBtn:hover{background:linear-gradient(135deg,#be123c,#ea580c);box-shadow:0 4px 18px rgba(225,29,72,.4);transform:translateY(-1px)}
#startBtn.recording{animation:recPulse 1.2s ease-in-out infinite;box-shadow:0 0 0 0 rgba(225,29,72,.5)}
@keyframes recPulse{0%,100%{box-shadow:0 0 0 0 rgba(225,29,72,.4)}50%{box-shadow:0 0 0 10px rgba(225,29,72,0)}}
#stopBtn{border-radius:50px;padding:.6rem 1.6rem;font-weight:700;font-size:.93rem;min-height:44px}

/* ── Writing ── */
#writeAnswer{border-radius:12px;border:1.5px solid var(--border);font-size:.93rem;line-height:1.65;font-family:var(--font);transition:border-color .2s,box-shadow .2s;resize:vertical}
#writeAnswer:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(37,99,235,.1);outline:none}
.word-count{font-size:.8rem;color:var(--muted);text-align:right;margin-top:.3rem;font-weight:600}

/* ── Bottom Bar ── */
.bottom-bar{position:fixed;bottom:0;left:0;right:0;background:rgba(255,255,255,.95);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-top:1px solid var(--border);padding:.65rem 1rem .7rem;z-index:100;box-shadow:0 -2px 16px rgba(0,0,0,.06)}
/* Tab progress pills */
.tab-prog-pill{position:relative;width:50px;height:46px;border-radius:12px;background:var(--surface2);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.25rem;transition:all .3s;cursor:default}
.tab-prog-pill.done{background:linear-gradient(135deg,#f0fdf4,#dcfce7);border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.12)}
.prog-check{display:none;position:absolute;top:-6px;right:-6px;background:#10b981;color:#fff;border-radius:50%;width:17px;height:17px;font-size:.62rem;font-weight:900;align-items:center;justify-content:center;line-height:1;box-shadow:0 1px 4px rgba(5,150,105,.3)}
.tab-prog-pill.done .prog-check{display:flex}
/* Incomplete session warning */
.incomplete-warn{background:linear-gradient(120deg,#f5f3ff,#fce7f3);border:1.5px solid #ddd6fe;border-left:4px solid var(--p);border-radius:14px;padding:.9rem 1.1rem}
.incomplete-tab-badge{display:inline-flex;align-items:center;background:#fff;border:1px solid #ddd6fe;border-radius:20px;padding:.18rem .65rem;font-size:.8rem;font-weight:600;margin:.1rem .15rem;color:#5b21b6}

/* ── Language Badge (locked) ── */
.lang-badge{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);color:#fff;border-radius:20px;padding:.28rem .85rem;font-size:.76rem;font-weight:600;white-space:nowrap;letter-spacing:.01em;display:flex;align-items:center;gap:.3rem}

/* ── Toast ── */
.toast-msg{position:fixed;top:1.2rem;left:50%;transform:translateX(-50%);background:#1a2332;color:#fff;padding:.55rem 1.3rem;border-radius:20px;font-size:.83rem;z-index:9999;pointer-events:none;animation:toastIn .22s ease;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,.25)}
.toast-msg.error{background:var(--danger)}
@keyframes toastIn{from{opacity:0;transform:translate(-50%,-10px)}to{opacity:1;transform:translate(-50%,0)}}

/* ── Reading ── */
#readArticle{font-size:.95rem;line-height:1.85;color:var(--text);background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:1.1rem 1.2rem;margin-bottom:1.2rem}
.read-q{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:1rem 1.1rem;margin-bottom:1rem}
.form-check-input:checked{background-color:var(--p);border-color:var(--p)}
.form-check{padding:.2rem 0 .2rem 1.5rem;margin:0}
.form-check-label{cursor:pointer;font-size:.92rem;line-height:1.55;font-weight:500}

/* ── IPA ── */
.ipa{color:#8C4A50;font-size:.88rem;font-family:'Courier New',monospace;font-style:normal;letter-spacing:.04em;background:#FFF0EE;padding:.05rem .38rem;border-radius:5px}

/* ── Hero Progress ── */
.progress-hero{height:5px;background:rgba(255,255,255,.18);border-radius:4px;overflow:hidden;margin-top:.6rem}
.progress-hero-bar{height:100%;background:linear-gradient(90deg,rgba(255,255,255,.7),#fff);border-radius:4px;transition:width .7s ease}

/* ── Onboarding ── */
.onboard-overlay{position:fixed;inset:0;background:rgba(92,29,38,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);z-index:2000;display:flex;align-items:center;justify-content:center;padding:1rem;animation:obFadeIn .3s ease}
@keyframes obFadeIn{from{opacity:0}to{opacity:1}}
@keyframes obFadeOut{from{opacity:1}to{opacity:0}}
.onboard-card{background:linear-gradient(160deg,#fffaf9 0%,#fff8f7 50%,#fff 100%);border-radius:28px;max-width:400px;width:100%;padding:1.8rem 1.4rem 1.3rem;box-shadow:0 32px 80px rgba(140,74,80,.28),0 0 0 2px rgba(221,167,165,.35);position:relative;max-height:92vh;overflow-y:auto;border-top:5px solid var(--p)}
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
.ob-target-pill{background:linear-gradient(90deg,#f5f3ff,#fce7f3,#fff7ed);border:1px solid #ddd6fe;border-radius:20px;padding:.42rem 1.1rem;font-size:.84rem;font-weight:700;color:#5b21b6;display:inline-block;margin:0 auto .9rem;text-align:center}
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
.ob-streak{background:linear-gradient(120deg,#f5f3ff,#fce7f3,#fff7ed);border:1px solid #ddd6fe;border-radius:12px;padding:.7rem 1rem;text-align:center;font-size:.88rem;font-weight:600;color:#5b21b6;margin-top:1rem}
.onboard-foot{display:flex;align-items:center;justify-content:space-between;margin-top:1.3rem;padding-top:1rem;border-top:1px solid var(--border)}
.ob-dots{display:flex;gap:5px;align-items:center}
.ob-dot{width:7px;height:7px;border-radius:50%;background:var(--border);transition:all .25s}
.ob-dot.on{background:linear-gradient(90deg,var(--p),#DDA7A5);width:22px;border-radius:4px}
.ob-next{background:linear-gradient(135deg,var(--p-dark),var(--p));color:#fff;border:none;border-radius:50px;padding:.6rem 1.6rem;font-size:.92rem;font-weight:700;cursor:pointer;transition:all .12s;font-family:inherit;letter-spacing:.04em;box-shadow:0 5px 0 var(--p-dark),0 7px 14px rgba(140,74,80,.28)}
.ob-next:hover{transform:translateY(-2px);box-shadow:0 7px 0 var(--p-dark),0 10px 18px rgba(140,74,80,.32)}
.ob-next:active{transform:translateY(3px);box-shadow:0 2px 0 var(--p-dark),0 3px 8px rgba(140,74,80,.2)}
/* ── Onboarding Language Picker ── */
.ob-lang-label{font-size:.8rem;font-weight:700;color:var(--muted);text-align:center;margin:.6rem 0 .5rem;letter-spacing:.04em;text-transform:uppercase}
.ob-lang-grid{display:grid;grid-template-columns:1fr 1fr;gap:.45rem;margin-bottom:.8rem}
.ob-lang-opt{background:var(--surface2);border:2px solid var(--border);border-radius:12px;padding:.5rem .6rem;font-size:.83rem;font-weight:600;cursor:pointer;transition:all .18s;text-align:center;color:var(--text);user-select:none;-webkit-tap-highlight-color:transparent}
.ob-lang-opt:hover{background:var(--p-light);border-color:var(--p-mid);color:var(--p)}
.ob-lang-opt.selected{background:linear-gradient(135deg,var(--p-light),var(--p-mid));border-color:var(--p);color:var(--p);font-weight:700;box-shadow:0 2px 8px rgba(140,74,80,.18)}
/* ── Slide 4: Exam Date ── */
.ob-date-presets{display:grid;grid-template-columns:repeat(2,1fr);gap:.45rem;margin:.5rem 0 .7rem}
.ob-date-opt{background:var(--surface2);border:2px solid var(--border);border-radius:12px;padding:.55rem .5rem;font-size:.92rem;font-weight:700;cursor:pointer;transition:all .18s;text-align:center;color:var(--text);user-select:none}
.ob-date-opt:hover{background:var(--p-light);border-color:var(--p-mid);color:var(--p)}
.ob-date-opt.selected{background:linear-gradient(135deg,var(--p-light),var(--p-mid));border-color:var(--p);color:var(--p);box-shadow:0 2px 8px rgba(140,74,80,.18)}
.ob-date-divider{text-align:center;font-size:.76rem;color:var(--muted);margin:.3rem 0;font-weight:600;letter-spacing:.04em}
.ob-date-field{width:100%;border:2px solid var(--border);border-radius:12px;padding:.52rem .8rem;font-size:.92rem;font-family:var(--font);color:var(--text);background:var(--surface2);outline:none;transition:border-color .15s}
.ob-date-field:focus{border-color:var(--p);background:#fff}
.ob-date-result{text-align:center;font-size:.88rem;font-weight:600;color:var(--p);margin-top:.6rem;min-height:1.4rem;padding:.3rem .8rem;border-radius:8px;background:var(--p-light)}
.ob-skip-link{display:block;text-align:center;font-size:.78rem;color:var(--muted);margin-top:.55rem;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.ob-skip-link:hover{color:var(--p)}
/* ── Slide 5: Skill Focus ── */
.ob-skill-grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin:.5rem 0 .7rem}
.ob-skill-opt{background:var(--surface2);border:2px solid var(--border);border-radius:14px;padding:.7rem .5rem;cursor:pointer;transition:all .2s;text-align:center;user-select:none}
.ob-skill-opt:hover{border-color:var(--p-mid)}
.ob-skill-opt.selected{border-color:var(--p);background:linear-gradient(135deg,var(--p-light),var(--p-mid))}
.ob-skill-ico{font-size:1.6rem;margin-bottom:.25rem}
.ob-skill-name{font-size:.82rem;font-weight:700;color:var(--text)}
.ob-skill-sub{font-size:.7rem;color:var(--muted);margin-top:.1rem}
.ob-skill-opt.selected .ob-skill-name{color:var(--p)}
.ob-skill-hint{text-align:center;font-size:.78rem;color:var(--muted);min-height:1.2rem;transition:opacity .5s}
.ob-skill-hint.warn{color:var(--danger);font-weight:600}

/* ── Mobile ── */
@media(max-width:480px){
  body{font-size:19px}
  .stat-num{font-size:1.45rem}
  .nav-tabs .nav-link{padding:.42rem .7rem;font-size:.8rem}
  .card-header{font-size:.9rem}
  #startBtn,#stopBtn{padding:.55rem 1.2rem;font-size:.88rem}
  .ob-slide{min-height:260px}
}
@media(min-width:768px){
  body{font-size:21px}
  .card-header{font-size:1rem}
  .nav-tabs .nav-link{font-size:.9rem;padding:.5rem 1rem}
  .stat-num{font-size:1.8rem}
}
/* ── Phase Test ── */
#phaseTestOverlay{position:fixed;inset:0;z-index:5000;background:rgba(30,8,18,.82);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;padding:1rem;overflow-y:auto}
.pt-card{background:#fff;border-radius:24px;max-width:560px;width:100%;padding:1.6rem 1.4rem 1.4rem;max-height:88vh;overflow-y:auto;position:relative}
.pt-header{text-align:center;margin-bottom:1.2rem}
.pt-icon{font-size:2.4rem;margin-bottom:.4rem}
.pt-title{font-size:1.25rem;font-weight:900;color:var(--p-dark);margin:.2rem 0 .4rem}
.pt-summary{font-size:.88rem;color:var(--muted);line-height:1.6}
.pt-section{margin:1.1rem 0;border-top:1px solid var(--border);padding-top:1rem}
.pt-sec-title{font-size:.95rem;font-weight:800;color:var(--p);margin-bottom:.7rem}
.pt-scenario{font-size:.82rem;color:var(--muted);background:var(--surface2);border-radius:10px;padding:.6rem .8rem;margin-bottom:.6rem;line-height:1.5}
.pt-article{font-size:.85rem;color:var(--text);background:var(--surface2);border-radius:10px;padding:.7rem .9rem;margin-bottom:.7rem;line-height:1.65}
.pt-q{margin-bottom:.9rem}
.pt-q-text{font-size:.88rem;font-weight:600;margin-bottom:.4rem;line-height:1.5;color:var(--text)}
.pt-dialogue{font-size:.82rem;margin-bottom:.7rem}
.pt-line{display:flex;gap:.5rem;margin:.3rem 0;align-items:flex-start}
.pt-line.pt-nurse .pt-spk{background:var(--p-light);color:var(--p-dark)}
.pt-line.pt-patient .pt-spk{background:#f0fdf4;color:#166534}
.pt-spk{font-size:.7rem;font-weight:700;border-radius:6px;padding:.15rem .45rem;white-space:nowrap;min-width:52px;text-align:center}
.pt-submit{width:100%;padding:.8rem;background:linear-gradient(135deg,var(--p-dark),var(--p));color:#fff;border:none;border-radius:14px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:1rem;box-shadow:0 4px 0 var(--p-dark)}
.pt-submit:hover{opacity:.92}
.pt-score-row{display:flex;align-items:center;gap:.8rem;margin:.55rem 0;font-size:.9rem}
.pt-score-row>span:first-child{min-width:70px;font-weight:600}
.pt-score-bar-wrap{flex:1;height:10px;background:var(--border);border-radius:6px;overflow:hidden}
.pt-score-bar{height:100%;border-radius:6px;transition:width .7s ease}
.pt-score-row>span:last-child{min-width:28px;font-weight:700;text-align:right}
.pt-result-title{font-size:1.3rem;font-weight:900;text-align:center;color:var(--p-dark);margin-bottom:1rem}
.pt-result-summary{text-align:center;font-size:1rem;font-weight:700;color:var(--p);margin:.6rem 0}
.pt-weak-notice{background:#fef2f2;border:1px solid #fca5a5;border-radius:12px;padding:.7rem 1rem;font-size:.88rem;line-height:1.6;margin-top:.7rem}
.pt-strong-notice{background:#f0fdf4;border:1px solid #86efac;border-radius:12px;padding:.7rem 1rem;font-size:.88rem;text-align:center;margin-top:.7rem}
.pt-loading{text-align:center;padding:2rem 1rem}
.pt-spin{font-size:2.5rem;animation:xpBounce .6s ease infinite alternate}
/* ── Feature Tour ── */
#tourOverlay{position:fixed;inset:0;z-index:3000;background:rgba(35,8,18,.6);backdrop-filter:blur(1px);transition:opacity .3s}
#tourSvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
.tour-label{position:absolute;color:#fff;line-height:1.6;text-align:center;pointer-events:none;text-shadow:0 2px 10px rgba(0,0,0,.6);font-size:.95rem;font-weight:600}
.tour-deco{position:absolute;color:rgba(255,255,255,.75);pointer-events:none}
#tourDismiss{position:absolute;left:50%;bottom:20%;transform:translateX(-50%);background:rgba(255,255,255,.14);border:1.5px solid rgba(255,255,255,.58);color:#fff;border-radius:24px;padding:.65rem 2.2rem;font-size:.95rem;cursor:pointer;backdrop-filter:blur(8px);pointer-events:all;white-space:nowrap;transition:background .2s}
#tourDismiss:hover{background:rgba(255,255,255,.26)}
/* ── XP Complete Animation ── */
#xpOverlay{position:fixed;inset:0;z-index:4000;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
#xpOverlay.show{animation:xpFadeIn .35s ease forwards}
#xpOverlay.hide{animation:xpFadeOut .4s ease forwards}
.xp-bg{position:absolute;inset:0;background:rgba(30,8,18,.78);backdrop-filter:blur(6px)}
.xp-center{position:relative;z-index:1;text-align:center}
.xp-icon{font-size:3.5rem;animation:xpBounce .55s ease infinite alternate}
.xp-title{color:#fff;font-size:1.6rem;font-weight:900;letter-spacing:-.01em;margin:.6rem 0 .3rem;text-shadow:0 3px 12px rgba(0,0,0,.5)}
.xp-sub{color:rgba(255,255,255,.8);font-size:1rem}
.xp-float{position:absolute;color:#fff;font-weight:900;font-size:1.2rem;pointer-events:none;animation:xpFloat 1.8s ease forwards}
.xp-bar-wrap{width:220px;height:14px;background:rgba(255,255,255,.18);border-radius:8px;margin:1rem auto 0;overflow:hidden}
.xp-bar-fill{height:100%;border-radius:8px;animation:xpBarFill 1.6s .3s ease forwards;width:0}
@keyframes xpFadeIn{from{opacity:0}to{opacity:1}}
@keyframes xpFadeOut{from{opacity:1}to{opacity:0}}
@keyframes xpBounce{from{transform:scale(1) rotate(-5deg)}to{transform:scale(1.2) rotate(5deg)}}
@keyframes xpFloat{0%{transform:translateY(0);opacity:1}100%{transform:translateY(-90px);opacity:0}}
@keyframes xpBarFill{from{width:0}to{width:100%}}
</style>
</head>
<body>

<!-- ── Onboarding Overlay ── -->
<!-- Phase Test Modal -->
<div id="phaseTestOverlay">
  <div class="pt-card">
    <div id="ptLoading" class="pt-loading">
      <div class="pt-spin">🎓</div>
      <div style="margin-top:.8rem;font-weight:700;color:var(--p-dark)" id="ptLoadText">正在準備階段測驗...</div>
    </div>
    <div id="ptContent" style="display:none">
      <div class="pt-header">
        <div class="pt-icon">🎓</div>
        <div class="pt-title" id="ptTitle"></div>
        <div class="pt-summary" id="ptSummary"></div>
      </div>
      <div class="pt-section">
        <div class="pt-sec-title">🌸 詞彙</div>
        <div id="ptVocabQs"></div>
      </div>
      <div class="pt-section">
        <div class="pt-sec-title">🎵 聽力</div>
        <div class="pt-scenario" id="ptListenScenario"></div>
        <div class="pt-dialogue" id="ptListenDialogue"></div>
        <div id="ptListenQs"></div>
      </div>
      <div class="pt-section">
        <div class="pt-sec-title">🫖 閱讀</div>
        <div class="pt-article" id="ptReadArticle"></div>
        <div id="ptReadQs"></div>
      </div>
      <button class="pt-submit" onclick="submitPhaseTest()">提交測驗 ↝</button>
    </div>
    <div id="ptResults" style="display:none">
      <div class="pt-result-title">🎉 Phase 結業測驗結果</div>
      <div id="ptResultScores"></div>
      <div class="pt-result-summary" id="ptResultSummary"></div>
      <div id="ptWeakAreas"></div>
      <button class="pt-submit" onclick="dismissPhaseTest()">開始今日課程 ↝</button>
    </div>
  </div>
</div>

<div id="onboardOverlay" class="onboard-overlay" style="display:none">
  <div class="onboard-card">
    <button class="onboard-skip" onclick="closeOnboard()" title="Skip">✕</button>

    <!-- Slide 0: Welcome -->
    <div class="ob-slide" id="ob-s0">
      <div class="ob-art">
        <div class="ob-ring ob-ring-1"></div>
        <div class="ob-ring ob-ring-2"></div>
        <div class="ob-ring ob-ring-3"></div>
        <div class="ob-emoji">🌸</div>
      </div>
      <h2 class="ob-h1" id="ob-title"></h2>
      <p class="ob-sub" id="ob-tagline"></p>
    </div>

    <!-- Slide 1: Daily Practice -->
    <div class="ob-slide" id="ob-s1" style="display:none">
      <h3 class="ob-h2" id="ob-s2title"></h3>
      <div class="ob-tab-row"><div class="ob-tab-ico">🌸</div><div><div class="ob-tab-name">Vocabulary</div><div class="ob-tab-desc">3 clinical terms · flip cards · 🔊 pronunciation</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">🫖</div><div><div class="ob-tab-name">Reading</div><div class="ob-tab-desc">OET Part C article · 3 MCQ (comprehension, vocab, inference)</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">🎵</div><div><div class="ob-tab-name">Listening</div><div class="ob-tab-desc">Nurse–patient dialogue · live highlight · 3 questions</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">💌</div><div><div class="ob-tab-name">Speaking</div><div class="ob-tab-desc">OET role-play · instant scoring · keyword tracking</div></div></div>
      <div class="ob-tab-row"><div class="ob-tab-ico">🌹</div><div><div class="ob-tab-name">Writing</div><div class="ob-tab-desc">OET referral letter practice · instant grading</div></div></div>
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
        <div class="ob-test"><div class="ob-test-ico">🫖</div><div class="ob-test-name">Reading</div><div class="ob-test-info">60 min · 3 parts</div></div>
        <div class="ob-test"><div class="ob-test-ico">🎵</div><div class="ob-test-name">Listening</div><div class="ob-test-info">40 min · 3 parts</div></div>
        <div class="ob-test"><div class="ob-test-ico">💌</div><div class="ob-test-name">Speaking</div><div class="ob-test-info">20 min · 2 role-plays</div></div>
        <div class="ob-test"><div class="ob-test-ico">🌹</div><div class="ob-test-name">Writing</div><div class="ob-test-info">45 min · referral letter</div></div>
      </div>
    </div>

    <!-- Slide 3: Language Pick + Start -->
    <div class="ob-slide" id="ob-s3" style="display:none">
      <div class="ob-start-ico">🌍</div>
      <h2 class="ob-h1" id="ob-s4title"></h2>
      <p class="ob-sub mb-2" id="ob-s4sub"></p>
      <div class="ob-lang-label" id="ob-lang-label">選擇你的學習語言</div>
      <div class="ob-lang-grid">
        <div class="ob-lang-opt" data-lang="zh-TW" onclick="obPickLang(this)">🇹🇼 繁體中文</div>
        <div class="ob-lang-opt" data-lang="zh-CN" onclick="obPickLang(this)">🇨🇳 简体中文</div>
        <div class="ob-lang-opt" data-lang="ja"    onclick="obPickLang(this)">🇯🇵 日本語</div>
        <div class="ob-lang-opt" data-lang="ko"    onclick="obPickLang(this)">🇰🇷 한국어</div>
        <div class="ob-lang-opt" data-lang="th"    onclick="obPickLang(this)">🇹🇭 ภาษาไทย</div>
        <div class="ob-lang-opt" data-lang="vi"    onclick="obPickLang(this)">🇻🇳 Tiếng Việt</div>
        <div class="ob-lang-opt" style="grid-column:1/-1" data-lang="id" onclick="obPickLang(this)">🇮🇩 Bahasa Indonesia</div>
      </div>
      <div class="ob-streak" id="ob-s4streak"></div>
      <div id="ob-lang-hint" style="color:#e05a7a;font-size:.82rem;margin-top:.4rem;min-height:1.2rem;text-align:center;transition:opacity .5s;opacity:0"></div>
    </div>

    <!-- Slide 4: Exam Date -->
    <div class="ob-slide" id="ob-s4" style="display:none">
      <div class="ob-start-ico" style="font-size:2.5rem;margin:1rem 0 .8rem;text-align:center">📅</div>
      <h2 class="ob-h1" id="ob-s5title"></h2>
      <p class="ob-sub mb-2" id="ob-s5sub"></p>
      <div class="ob-date-presets" id="ob-date-presets">
        <div class="ob-date-opt" data-days="30"  onclick="obPickDate(this)"><div>1</div><div id="ob-month-label" style="font-size:.72rem;font-weight:500;opacity:.75">month</div></div>
        <div class="ob-date-opt" data-days="60"  onclick="obPickDate(this)"><div>2</div><div style="font-size:.72rem;font-weight:500;opacity:.75">months</div></div>
        <div class="ob-date-opt" data-days="90"  onclick="obPickDate(this)"><div>3</div><div style="font-size:.72rem;font-weight:500;opacity:.75">months</div></div>
        <div class="ob-date-opt" data-days="180" onclick="obPickDate(this)"><div>6</div><div style="font-size:.72rem;font-weight:500;opacity:.75">months</div></div>
      </div>
      <div class="ob-date-divider" id="ob-or-label">── or pick a date ──</div>
      <input type="date" id="ob-date-input" class="ob-date-field" oninput="obCustomDate(this)" />
      <div class="ob-date-result" id="ob-date-result" style="display:none"></div>
      <div id="ob-date-hint" style="color:#e05a7a;font-size:.82rem;margin-top:.3rem;min-height:1.2rem;text-align:center;transition:opacity .5s;opacity:0"></div>
      <span class="ob-skip-link" id="ob-skip-date" onclick="obSkipDate()"></span>
    </div>

    <!-- Slide 5: Skill Focus -->
    <div class="ob-slide" id="ob-s5" style="display:none">
      <h2 class="ob-h1" id="ob-s6title"></h2>
      <p class="ob-sub mb-2" id="ob-s6sub"></p>
      <div class="ob-skill-grid">
        <div class="ob-skill-opt selected" data-skill="reading" onclick="obToggleSkill(this)">
          <div class="ob-skill-ico">🫖</div>
          <div class="ob-skill-name">Reading</div>
          <div class="ob-skill-sub">Part A / B / C</div>
        </div>
        <div class="ob-skill-opt selected" data-skill="listening" onclick="obToggleSkill(this)">
          <div class="ob-skill-ico">🎵</div>
          <div class="ob-skill-name">Listening</div>
          <div class="ob-skill-sub">3 parts · 42 Q</div>
        </div>
        <div class="ob-skill-opt selected" data-skill="speaking" onclick="obToggleSkill(this)">
          <div class="ob-skill-ico">💌</div>
          <div class="ob-skill-name">Speaking</div>
          <div class="ob-skill-sub">2 role-plays</div>
        </div>
        <div class="ob-skill-opt selected" data-skill="writing" onclick="obToggleSkill(this)">
          <div class="ob-skill-ico">🌹</div>
          <div class="ob-skill-name">Writing</div>
          <div class="ob-skill-sub">Referral letter</div>
        </div>
      </div>
      <div class="ob-skill-hint" id="ob-skill-hint"></div>
    </div>

    <!-- Footer nav -->
    <div class="onboard-foot">
      <div class="ob-dots" id="ob-dots">
        <div class="ob-dot on"></div>
        <div class="ob-dot"></div>
        <div class="ob-dot"></div>
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
        <div class="fw-black" style="font-size:1rem;line-height:1.25;letter-spacing:-.01em">不再卡關 B 級！</div>
        <div style="font-size:.75rem;opacity:.82;font-weight:600">OET 聽說讀寫全攻略 · 目標 365+</div>
      </div>
      <div class="text-end d-flex flex-column align-items-end gap-1">
        <!-- Language badge (locked after onboarding) -->
        <div class="lang-badge" id="langBadge">
          <span id="langFlag"></span> <span id="langName"></span>
        </div>
        <div id="resetBtn" onclick="confirmReset()" style="font-size:.62rem;opacity:.45;cursor:pointer;color:inherit;text-decoration:none;padding:1px 4px;border-radius:4px" title="重設進度">⚙ 重設</div>
        <div>
          <div style="font-size:1.75rem;font-weight:800;line-height:1">🔥 {{ streak }}</div>
          <div style="font-size:.7rem;opacity:.8">連續天數</div>
        </div>
      </div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div id="statDay" class="stat-box"><div class="stat-num">{{ day }}</div><div class="stat-label">Day / 270</div></div></div>
      <div class="col-4"><div id="statPhase" class="stat-box"><div class="stat-num">P{{ phase }}</div><div class="stat-label">{{ phase_name }}</div></div></div>
      <div class="col-4"><div class="stat-box"><div class="stat-num">{{ total }}</div><div class="stat-label">已完成天</div></div></div>
    </div>
    <div id="progressHero" class="progress-hero"><div class="progress-hero-bar" style="width:{{ pct }}%"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:.72rem;opacity:.75;margin-top:3px">
      <div id="examCountdownHero" style="display:none">📅 <span id="examDaysText"></span></div>
      <div style="margin-left:auto">{{ pct }}% 完成</div>
    </div>
  </div>
</div>

<div class="container-sm mt-3">
  <div id="missedBanner" class="warn-box mb-3" style="display:none"></div>
  {% if show_incomplete %}
  <div class="incomplete-warn mb-3" id="incompleteWarn">
    <div class="fw-bold mb-1" style="font-size:.95rem">😴 上次課程未完成</div>
    <div style="font-size:.85rem;margin-bottom:.5rem">
      未完成項目：{% for t in last_incomplete %}<span class="incomplete-tab-badge">{{ {'vocab':'🌸 詞彙','read':'🫖 閱讀','listen':'🎵 聽力','speak':'💌 口說','write':'🌹 寫作'}[t] }}</span>{% endfor %}
    </div>
    <div style="font-size:.82rem;color:#7c3aed">✨ 今日課程已針對未完成項目自動加強調整</div>
  </div>
  {% endif %}

  <div id="loadingBox" class="loading-screen">
    <div class="loader-wrap">
      <div class="loader-ring"></div>
      <div class="loader-icon">🌸</div>
    </div>
    <div class="loader-dots"><i></i><i></i><i></i></div>
    <div id="loadingText"></div>
    <div id="loadingSubText"></div>
  </div>

  <div id="lessonBox" style="display:none">
    <div id="encouragement" class="mb-3"></div>

    <ul class="nav nav-tabs mb-3">
      <li class="nav-item"><button class="nav-link active" data-tab="vocab">🌸 詞彙</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="read">🫖 閱讀</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="listen">🎵 聽力</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="speak">💌 口說</button></li>
      <li class="nav-item"><button class="nav-link" data-tab="write">🌹 寫作</button></li>
    </ul>

    <!-- 詞彙 -->
    <div id="tab-vocab" class="tab-pane">
      <div class="card">
        <div class="card-header">🌸 今日詞彙 <span class="badge ms-1" style="background:var(--p-light);color:var(--p);font-size:.72rem" id="vocabHintBadge">翻完所有卡片即完成 ✨</span></div>
        <div class="card-body" id="vocabContent"></div>
      </div>
    </div>

    <!-- 閱讀 -->
    <div id="tab-read" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">🫖 閱讀測驗 <span class="badge ms-1" style="background:var(--p-light);color:var(--p);font-size:.72rem">3 題</span></div>
        <div class="card-body">
          <div class="fw-semibold small mb-2" id="readTitle"></div>
          <div id="readArticle"></div>
          <div id="readQs"></div>
          <button class="btn btn-primary btn-sm mt-3" id="readSubmitBtn" onclick="checkReading()">✓ Submit</button>
          <div id="readResults" style="display:none" class="mt-3"></div>
        </div>
      </div>
    </div>

    <!-- 聽力 -->
    <div id="tab-listen" class="tab-pane" style="display:none">
      <div class="card">
        <div class="card-header">🎵 聽力練習</div>
        <div class="card-body">
          <div class="info-box mb-3 small" id="listenScenario"></div>
          <div class="d-flex gap-2 mb-3 flex-wrap">
            <button class="btn btn-primary px-4" id="playBtn" onclick="playListening()">▶ 播放對話</button>
            <button style="display:none" id="showDialogueBtn" class="btn btn-outline-secondary btn-sm" onclick="toggleDialogue()">📄 對話文字</button>
          </div>
          <div id="dialogueText" style="display:none;background:#f8fafc" class="mb-3 p-3 rounded-3">
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
        <div class="card-header">💌 口說練習</div>
        <div class="card-body">
          <div class="info-box mb-3">
            <div class="small fw-semibold mb-1" style="color:var(--p)">情境</div>
            <div id="speakScenario" class="small"></div>
          </div>
          <div class="mb-2 small fw-semibold text-muted">你的任務</div>
          <div id="speakTask" class="mb-3 small"></div>
          <div class="mb-2 small fw-semibold text-muted">🌿 重點用語 <span class="fw-normal" style="color:var(--muted)">(🌸說到 🥀漏掉)</span></div>
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
        <div class="card-header">🌹 寫作練習</div>
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
  <div class="container-sm">
    {% if done %}
    <div class="text-center fw-semibold py-1 small" style="color:var(--success)" id="statusDoneMsg">🌸 今日課程已完成！明天繼續 🌷</div>
    {% elif tired %}
    <div class="text-center text-muted py-1 small" id="statusTiredMsg">🌙 今天休息，明天繼續加油！（不算完成）</div>
    {% else %}
    <!-- Progress pills (vocab/read/listen/speak/write) — shown until all 5 done -->
    <div id="tabProgress">
      <div class="d-flex gap-2 justify-content-center mb-1">
        <div class="tab-prog-pill" id="prog-vocab">🌸<span class="prog-check">✓</span></div>
        <div class="tab-prog-pill" id="prog-read">🫖<span class="prog-check">✓</span></div>
        <div class="tab-prog-pill" id="prog-listen">🎵<span class="prog-check">✓</span></div>
        <div class="tab-prog-pill" id="prog-speak">💌<span class="prog-check">✓</span></div>
        <div class="tab-prog-pill" id="prog-write">🌹<span class="prog-check">✓</span></div>
      </div>
      <div class="text-center small" style="color:var(--muted)" id="tabProgressText">0 / 5 完成</div>
    </div>
    <!-- Complete actions (hidden until all 5 done) -->
    <div id="tabCompleteActions" style="display:none" class="d-flex gap-2">
      <button class="btn btn-success flex-fill" onclick="markComplete()" style="border-radius:12px;font-weight:700">🌸 完成今日課程</button>
      <button class="btn btn-outline-secondary" id="tiredBtnEl" onclick="markTired()" style="border-radius:12px;white-space:nowrap">🌙 很累</button>
    </div>
    {% endif %}
  </div>
</div>

<script>
const TODAY_DONE = {{ 'true' if done else 'false' }};
const MISSED_DAYS = {{ missed_count }};
let lesson = null, recognition = null, finalTranscript = '';
let scoreHistory = [];
let tabsDone = { vocab: false, read: false, listen: false, speak: false, write: false };
let vocabFlipped = new Set();

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
let currentLang = localStorage.getItem('oet_lang') || 'zh-TW';

const UI = {
  'zh-TW': {loading:'正在生成今日課程…',loadSub:'首次約需 10 秒',hint:'按「開始錄音」（需 Chrome / Edge）',rec:'🔴 錄音中…',done:'錄音完成，可按評分',scoring:'評分中…',rescore:'🔄 重新評分',grading:'批改中…',regrade:'✏️ 重新批改',results:'批改結果',attempt:'第{n}次',noRec:'請先錄音',noWrite:'請先輸入答案',notAll:'請先回答所有題目',noBrowser:'請使用 Chrome 或 Edge',tiredBtn:'🌙 很累',tiredConfirm:'🌙 確定休息？再點一次',tiredDone:'🌙 今天先充電（未完成，明天你一定更棒 💪）',completeDone:'🌸 今日課程已完成！明天繼續 🌷',vocabHint:'翻完所有卡片即完成 ✨',missedBack:'你已缺席 {n} 天，連續天數已重置。沒關係，今天重新出發！'},
  'zh-CN': {loading:'正在生成今日课程…',loadSub:'首次约需 10 秒',hint:'按「开始录音」（需 Chrome / Edge）',rec:'🔴 录音中…',done:'录音完成，可按评分',scoring:'评分中…',rescore:'🔄 重新评分',grading:'批改中…',regrade:'✏️ 重新批改',results:'批改结果',attempt:'第{n}次',noRec:'请先录音',noWrite:'请先输入答案',notAll:'请先回答所有题目',noBrowser:'请使用 Chrome 或 Edge',tiredBtn:'🌙 很累',tiredConfirm:'🌙 确认休息？再点一次',tiredDone:'🌙 今天先充电（未完成，明天你一定更棒 💪）',completeDone:'🌸 今日课程已完成！明天继续 🌷',vocabHint:'翻完所有卡片即完成 ✨',missedBack:'你已缺席 {n} 天，连续天数已重置。没关系，今天重新出发！'},
  'ja':    {loading:'本日のレッスンを生成中…',loadSub:'初回は約10秒',hint:'「録音開始」を押す (Chrome/Edge)',rec:'🔴 録音中…',done:'録音完了 — 採点できます',scoring:'採点中…',rescore:'🔄 再採点',grading:'添削中…',regrade:'✏️ 再添削',results:'採点結果',attempt:'{n}回目',noRec:'先に録音してください',noWrite:'先に答えを入力してください',notAll:'全問に解答してください',noBrowser:'Chrome または Edge を使用してください',tiredBtn:'🌙 疲れた',tiredConfirm:'🌙 本当に休憩？もう一度タップ',tiredDone:'🌙 今日はしっかり充電（未完了，明日また頑張ろう 💪）',completeDone:'🌸 本日のレッスン完了！また明日 🌷',vocabHint:'全カードをめくると完了 ✨',missedBack:'{n}日間お休みしました — 連続記録はリセットされました。今日また一歩から始めましょう！'},
  'ko':    {loading:'오늘 레슨을 생성 중…',loadSub:'처음에는 약 10초 소요',hint:'「녹음 시작」누르기 (Chrome/Edge)',rec:'🔴 녹음 중…',done:'녹음 완료 — 채점 가능',scoring:'채점 중…',rescore:'🔄 재채점',grading:'첨삭 중…',regrade:'✏️ 재첨삭',results:'채점 결과',attempt:'{n}번째',noRec:'먼저 녹음해 주세요',noWrite:'먼저 답을 입력해 주세요',notAll:'모든 문제를 풀어 주세요',noBrowser:'Chrome 또는 Edge를 사용해 주세요',tiredBtn:'🌙 힘들어요',tiredConfirm:'🌙 정말 쉬실 건가요? 한 번 더',tiredDone:'🌙 오늘은 충전（미완료，내일 더 힘차게 💪）',completeDone:'🌸 오늘 레슨 완료！내일도 화이팅 🌷',vocabHint:'카드를 모두 뒤집으면 완료 ✨',missedBack:'{n}일 동안 쉬셨네요 — 연속 기록이 초기화됐습니다. 괜찮아요, 오늘부터 다시 시작해요！'},
  'th':    {loading:'กำลังสร้างบทเรียนวันนี้…',loadSub:'ครั้งแรกใช้เวลา ~10 วินาที',hint:'กด「เริ่มอัดเสียง」(Chrome/Edge)',rec:'🔴 กำลังอัดเสียง…',done:'อัดเสร็จ — กดให้คะแนน',scoring:'กำลังให้คะแนน…',rescore:'🔄 ให้คะแนนใหม่',grading:'กำลังตรวจ…',regrade:'✏️ ตรวจใหม่',results:'ผลคะแนน',attempt:'ครั้งที่ {n}',noRec:'กรุณาอัดเสียงก่อน',noWrite:'กรุณาพิมพ์คำตอบก่อน',notAll:'กรุณาตอบทุกข้อ',noBrowser:'ใช้ Chrome หรือ Edge',tiredBtn:'🌙 เหนื่อย',tiredConfirm:'🌙 พักจริงๆ ใช่ไหม? แตะอีกครั้ง',tiredDone:'🌙 ชาร์จพลังวันนี้（ยังไม่เสร็จ，พรุ่งนี้สู้ต่อ 💪）',completeDone:'🌸 เสร็จแล้ววันนี้！พรุ่งนี้เจอกัน 🌷',vocabHint:'พลิกครบทุกใบแล้วเสร็จ ✨',missedBack:'คุณขาด {n} วัน — สถิติต่อเนื่องรีเซ็ตแล้ว ไม่เป็นไร วันนี้เริ่มใหม่ได้เลย！'},
  'vi':    {loading:'Đang tạo bài học hôm nay…',loadSub:'Lần đầu ~10 giây',hint:'Nhấn「Bắt đầu ghi âm」(Chrome/Edge)',rec:'🔴 Đang ghi âm…',done:'Xong — nhấn chấm điểm',scoring:'Đang chấm…',rescore:'🔄 Chấm lại',grading:'Đang sửa…',regrade:'✏️ Sửa lại',results:'Kết quả',attempt:'Lần {n}',noRec:'Vui lòng ghi âm trước',noWrite:'Vui lòng nhập câu trả lời',notAll:'Hãy trả lời tất cả câu hỏi',noBrowser:'Dùng Chrome hoặc Edge',tiredBtn:'🌙 Mệt rồi',tiredConfirm:'🌙 Nghỉ hôm nay? Nhấn lần nữa',tiredDone:'🌙 Nạp năng lượng hôm nay（Chưa xong，mai bứt phá 💪）',completeDone:'🌸 Hoàn thành hôm nay！Hẹn gặp ngày mai 🌷',vocabHint:'Lật hết thẻ là xong ✨',missedBack:'Bạn đã nghỉ {n} ngày — chuỗi ngày học đã được đặt lại. Không sao, hôm nay bắt đầu lại！'},
  'id':    {loading:'Membuat pelajaran hari ini…',loadSub:'Pertama kali ~10 detik',hint:'Tekan「Mulai Rekam」(Chrome/Edge)',rec:'🔴 Merekam…',done:'Selesai — tekan nilai',scoring:'Menilai…',rescore:'🔄 Nilai ulang',grading:'Mengoreksi…',regrade:'✏️ Koreksi ulang',results:'Hasil',attempt:'Ke-{n}',noRec:'Rekam dulu',noWrite:'Isi jawaban dulu',notAll:'Jawab semua soal dulu',noBrowser:'Gunakan Chrome atau Edge',tiredBtn:'🌙 Lelah',tiredConfirm:'🌙 Yakin istirahat? Ketuk lagi',tiredDone:'🌙 Isi daya hari ini（Belum selesai，besok semangat 💪）',completeDone:'🌸 Pelajaran hari ini selesai！Sampai besok 🌷',vocabHint:'Balik semua kartu untuk selesai ✨',missedBack:'Kamu absen {n} hari — catatan beruntun direset. Tidak apa-apa, mulai lagi hari ini！'},
};
function t(key, n) {
  const d = UI[currentLang] || UI['zh-TW'];
  return (d[key] || key).replace('{n}', n ?? '');
}
function showMissedBanner() {
  if (!MISSED_DAYS) return;
  const msg = t('missedBack', MISSED_DAYS);
  const el = document.getElementById('missedBanner');
  if (el) { el.textContent = '⚠️ ' + msg; el.style.display = ''; }
}

// ── Toast ──
function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast-msg' + (type === 'error' ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

// ── Onboarding handwriting fonts per language ──
const OB_FONTS = {
  'zh-TW': "'Ma Shan Zheng', cursive",
  'zh-CN': "'Ma Shan Zheng', cursive",
  'ja':    "'Hachi Maru Pop', cursive",
  'ko':    "'Gaegu', cursive",
  'th':    "'Sriracha', cursive",
  'vi':    "'Caveat', cursive",
  'id':    "'Caveat', cursive",
};
// ── Feature Tour ──
const TOUR_I18N = {
  'zh-TW': {day:'今天是第幾天',phase:'學習的第幾階段',bar:'學習經驗條',tabs:'點這裡<br>切換練習項目',tired:'今天太累？<br>點這裡',dismiss:'知道啦！開始練習 ↝'},
  'zh-CN': {day:'今天是第几天',phase:'学习的第几阶段',bar:'学习经验条',tabs:'点这里<br>切换练习项目',tired:'今天太累？<br>点这里',dismiss:'知道啦！开始练习 ↝'},
  'ja':    {day:'今日は何日目',phase:'現在の学習フェーズ',bar:'学習経験バー',tabs:'ここをタップして<br>練習を切り替え',tired:'疲れた日は<br>ここを押してね',dismiss:'わかった！始める ↝'},
  'ko':    {day:'오늘은 몇 번째 날',phase:'학습 단계',bar:'학습 경험 바',tabs:'여기서<br>연습 전환',tired:'너무 피곤해요？<br>여기 누르세요',dismiss:'알겠어요！시작 ↝'},
  'th':    {day:'วันที่เท่าไหร่แล้ว',phase:'ระดับการเรียนรู้',bar:'แถบประสบการณ์',tabs:'แตะที่นี่<br>สลับแบบฝึก',tired:'เหนื่อยวันนี้？<br>กดที่นี่',dismiss:'เข้าใจแล้ว！เริ่มเลย ↝'},
  'vi':    {day:'Hôm nay là ngày thứ mấy',phase:'Giai đoạn học',bar:'Thanh kinh nghiệm',tabs:'Nhấn đây<br>chuyển bài tập',tired:'Mệt hôm nay？<br>Nhấn đây',dismiss:'Hiểu rồi！Bắt đầu ↝'},
  'id':    {day:'Hari keberapa hari ini',phase:'Fase belajar',bar:'Bilah pengalaman',tabs:'Ketuk di sini<br>ganti latihan',tired:'Lelah hari ini？<br>Tekan ini',dismiss:'Mengerti！Mulai ↝'},
};
function _tourLine(svg, x1, y1, x2, y2, cx, cy) {
  const p = document.createElementNS('http://www.w3.org/2000/svg','path');
  p.setAttribute('d', `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`);
  p.setAttribute('fill','none'); p.setAttribute('stroke','rgba(255,255,255,.65)');
  p.setAttribute('stroke-width','1.5'); p.setAttribute('stroke-dasharray','5,4');
  p.setAttribute('stroke-linecap','round'); svg.appendChild(p);
}
function _tourLabel(ov, font, x, y, rot, html) {
  const d = document.createElement('div');
  d.className = 'tour-label';
  d.style.cssText = `font-family:${font};left:${x}px;top:${y}px;transform:rotate(${rot}deg)`;
  d.innerHTML = html; ov.appendChild(d); return d;
}
function _tourDeco(ov, x, y, ch, size) {
  const d = document.createElement('div');
  d.className = 'tour-deco';
  d.style.cssText = `left:${x}px;top:${y}px;font-size:${size}`;
  d.textContent = ch; ov.appendChild(d);
}
function showTour() {
  if (localStorage.getItem('oet_tour_v1')) return;
  if (!localStorage.getItem('oet_onboard_v2')) return;
  const font = OB_FONTS[currentLang] || OB_FONTS['zh-TW'];
  const txt = TOUR_I18N[currentLang] || TOUR_I18N['zh-TW'];
  const dayEl   = document.getElementById('statDay');
  const phaseEl = document.getElementById('statPhase');
  const barEl   = document.getElementById('progressHero');
  const tabsEl  = document.querySelector('.nav-tabs');
  const tiredEl = document.getElementById('tiredBtnEl');
  if (!dayEl || !phaseEl || !barEl || !tabsEl) return;
  const dR  = dayEl.getBoundingClientRect();
  const phR = phaseEl.getBoundingClientRect();
  const bR  = barEl.getBoundingClientRect();
  const tR  = tabsEl.getBoundingClientRect();
  const vw = window.innerWidth;

  const ov  = document.createElement('div'); ov.id = 'tourOverlay';
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.id = 'tourSvg'; ov.appendChild(svg);

  // ① Day box — label above-left, arrow points down to box
  const dLx = Math.max(4, dR.left), dLy = Math.max(4, dR.top - 58);
  _tourLabel(ov, font, dLx, dLy, -3, txt.day);
  _tourLine(svg, dLx+50, dLy+36, dR.left+dR.width/2, dR.top, dLx+50, dLy+36+(dR.top-dLy-36)/2-10);
  _tourDeco(ov, dLx+90, dLy-8, '✦', '.85rem');

  // ② Phase box — label above-right, arrow points down to box
  const phLx = Math.min(vw-140, phR.right-130), phLy = Math.max(4, phR.top - 58);
  _tourLabel(ov, font, phLx, phLy, 3, txt.phase);
  _tourLine(svg, phLx+60, phLy+36, phR.left+phR.width/2, phR.top, phLx+60, phLy+36+(phR.top-phLy-36)/2-10);
  _tourDeco(ov, phLx-12, phLy+4, '◇', '.8rem');

  // ③ Progress bar — label below-right, arrow points left to bar end
  const bLx = Math.min(vw-145, bR.right-145), bLy = bR.bottom + 14;
  _tourLabel(ov, font, bLx, bLy, -2, txt.bar);
  _tourLine(svg, bLx+30, bLy+2, Math.min(bR.right-10, bLx+25), bR.top+bR.height/2, bLx+20, bR.top+bR.height/2);
  _tourDeco(ov, bLx+105, bLy+8, '♡', '.85rem');

  // ④ Tabs — label below-right of tabs, arrow points up into tabs
  const tLx = Math.min(vw-150, tR.right-150), tLy = tR.bottom + 50;
  _tourLabel(ov, font, tLx, tLy, -4, txt.tabs);
  _tourLine(svg, tLx+55, tLy, tR.left+tR.width*0.55, tR.bottom+2, tLx+55, tLy+(tR.bottom-tLy)/2);
  _tourDeco(ov, tLx+110, tLy-14, '✦', '.9rem');

  // ⑤ Tired button — label above-left, arrow points down-right to button
  if (tiredEl) {
    const tiR2 = tiredEl.getBoundingClientRect();
    const tiLx = Math.max(4, tiR2.left-6), tiLy = Math.max(tLy+60, tiR2.top - 92);
    _tourLabel(ov, font, tiLx, tiLy, 3, txt.tired);
    _tourLine(svg, tiLx+58, tiLy+40, tiR2.left+tiR2.width/2, tiR2.top, tiR2.left+tiR2.width/2, tiLy+40+(tiR2.top-tiLy-40)/2);
    _tourDeco(ov, tiLx-10, tiLy+16, '♡', '.8rem');
  }

  // Dismiss button
  const db = document.createElement('button');
  db.id = 'tourDismiss'; db.style.fontFamily = font;
  db.innerHTML = txt.dismiss; db.onclick = closeTour;
  ov.appendChild(db);

  document.body.appendChild(ov);
}
function closeTour() {
  const el = document.getElementById('tourOverlay');
  if (el) { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }
  localStorage.setItem('oet_tour_v1', '1');
}

// ── Onboarding i18n ──
const OB = {
  'zh-TW': {title:'OET 聽說讀寫全攻略',tagline:'每日 30 分鐘　從臨床到考場',s2:'每日五大練習',s3:'OET 考試攻略',target:'目標：Band B = 350 分以上',s4:'選擇母語',s4sub:'之後語言無法更改，請確認選取 ✨',streak:'🔥 連續天數從今天開始',s5title:'考試日期',s5sub:'依剩餘天數自動安排最有效率的課程計畫',daysLeft:'天後考試',skipDate:'略過，稍後設定',s6title:'選擇加強項目',s6sub:'選一到多項 — 每日針對這些項目加強出題與建議',skillHint:'請至少選擇一項',langHint:'請先選擇你的語言 ↑',dateHint:'請選擇考試日期，或點「略過」',next:'下一步 ↝',start:'開始今日課程 ↝'},
  'zh-CN': {title:'OET 听说读写全攻略',tagline:'每日 30 分钟　从临床到考场',s2:'每日五大练习',s3:'OET 考试攻略',target:'目标：Band B = 350 分以上',s4:'选择母语',s4sub:'之后语言无法更改，请确认选取 ✨',streak:'🔥 连续天数从今天开始',s5title:'考试日期',s5sub:'依剩余天数自动安排最高效的课程计划',daysLeft:'天后考试',skipDate:'跳过，稍后设置',s6title:'选择加强项目',s6sub:'选一到多项 — 每日针对这些项目加强练习',skillHint:'请至少选择一项',langHint:'请先选择你的语言 ↑',dateHint:'请选择考试日期，或点「跳过」',next:'下一步 ↝',start:'开始今日课程 ↝'},
  'ja':    {title:'OET 4技能完全攻略',tagline:'毎日 30分　臨床から試験へ',s2:'毎日の5練習',s3:'OET 試験マップ',target:'目標：Band B = 350点以上',s4:'言語を選択',s4sub:'選択後は変更できません。ご確認ください ✨',streak:'🔥 連続日数は今日からスタート',s5title:'試験日',s5sub:'残り日数に合わせて最適な学習計画を自動調整',daysLeft:'日後に試験',skipDate:'スキップ — 後で設定',s6title:'強化したいスキル',s6sub:'1つ以上選択 — 毎日そのスキルを重点的に出題',skillHint:'少なくとも1つ選んでください',langHint:'言語を選択してください ↑',dateHint:'試験日を選ぶか「スキップ」をクリック',next:'次へ ↝',start:'今日のレッスンを始める ↝'},
  'ko':    {title:'OET 4기능 완전 정복',tagline:'매일 30분　임상에서 시험까지',s2:'매일 5가지 연습',s3:'OET 시험 가이드',target:'목표: Band B = 350점 이상',s4:'언어 선택',s4sub:'선택 후에는 변경할 수 없으니 확인해 주세요 ✨',streak:'🔥 연속 일수 오늘부터 시작',s5title:'시험 날짜',s5sub:'남은 일수에 맞춰 최적의 학습 계획을 자동 설계',daysLeft:'일 후 시험',skipDate:'건너뛰기 — 나중에 설정',s6title:'강화할 영역 선택',s6sub:'1개 이상 선택 — 매일 해당 영역을 집중 훈련',skillHint:'최소 1개를 선택하세요',langHint:'언어를 선택해 주세요 ↑',dateHint:'시험 날짜를 선택하거나 건너뛰기를 클릭하세요',next:'다음 ↝',start:'오늘 수업 시작 ↝'},
  'th':    {title:'OET ครบ 4 ทักษะ',tagline:'30 นาทีต่อวัน　จากคลินิกสู่ห้องสอบ',s2:'5 หัวข้อฝึกประจำวัน',s3:'แผนที่สอบ OET',target:'เป้าหมาย: Band B = 350+ คะแนน',s4:'เลือกภาษา',s4sub:'ไม่สามารถเปลี่ยนได้หลังเลือก กรุณายืนยัน ✨',streak:'🔥 เริ่มนับวันต่อเนื่องวันนี้',s5title:'วันสอบ',s5sub:'ระบบจะจัดแผนบทเรียนให้เหมาะกับเวลาที่เหลืออัตโนมัติ',daysLeft:'วันถึงวันสอบ',skipDate:'ข้ามไป — ตั้งภายหลัง',s6title:'เลือกทักษะที่ต้องการเสริม',s6sub:'เลือก 1 ข้อขึ้นไป — ทุกวันจะเน้นฝึกในส่วนที่เลือก',skillHint:'กรุณาเลือกอย่างน้อย 1 ข้อ',langHint:'กรุณาเลือกภาษาก่อน ↑',dateHint:'เลือกวันสอบหรือคลิก "ข้ามไป"',next:'ถัดไป ↝',start:'เริ่มบทเรียนวันนี้ ↝'},
  'vi':    {title:'OET Toàn diện 4 kỹ năng',tagline:'30 phút mỗi ngày　từ lâm sàng đến thi cử',s2:'5 bài tập hàng ngày',s3:'Bản đồ thi OET',target:'Mục tiêu: Band B = 350+ điểm',s4:'Chọn ngôn ngữ',s4sub:'Không thể thay đổi sau khi chọn, vui lòng xác nhận ✨',streak:'🔥 Chuỗi ngày bắt đầu từ hôm nay',s5title:'Ngày thi',s5sub:'Hệ thống tự động sắp xếp bài học phù hợp với thời gian còn lại',daysLeft:'ngày đến kỳ thi',skipDate:'Bỏ qua — Đặt sau',s6title:'Chọn kỹ năng cần tăng cường',s6sub:'Chọn 1 hoặc nhiều — mỗi ngày tập trung vào kỹ năng đã chọn',skillHint:'Vui lòng chọn ít nhất 1',langHint:'Vui lòng chọn ngôn ngữ trước ↑',dateHint:'Chọn ngày thi hoặc nhấn "Bỏ qua"',next:'Tiếp theo ↝',start:'Bắt đầu bài hôm nay ↝'},
  'id':    {title:'OET Kuasai 4 Keterampilan',tagline:'30 menit sehari　dari klinis ke ujian',s2:'5 latihan harian',s3:'Peta Ujian OET',target:'Target: Band B = 350+ poin',s4:'Pilih Bahasa',s4sub:'Tidak dapat diubah setelah dipilih, harap konfirmasi ✨',streak:'🔥 Hari berturut-turut mulai hari ini',s5title:'Tanggal Ujian',s5sub:'Sistem otomatis merancang pelajaran sesuai sisa waktu Anda',daysLeft:'hari menuju ujian',skipDate:'Lewati — Atur nanti',s6title:'Pilih Keterampilan yang Ingin Ditingkatkan',s6sub:'Pilih 1 atau lebih — setiap hari fokus pada keterampilan pilihan',skillHint:'Pilih setidaknya 1',langHint:'Silakan pilih bahasa Anda ↑',dateHint:'Pilih tanggal ujian atau klik "Lewati"',next:'Selanjutnya ↝',start:'Mulai pelajaran hari ini ↝'},
};

let obSlide = 0;
const OB_TOTAL = 6;
let obExamDate = null;
let obFocus = ['reading','listening','speaking','writing'];

function showOnboard() {
  document.getElementById('onboardOverlay').style.display = 'flex';
  setObSlide(0);
}
function closeOnboard() {
  const el = document.getElementById('onboardOverlay');
  el.style.animation = 'obFadeOut .28s ease forwards';
  setTimeout(() => { el.style.display = 'none'; el.style.animation = ''; }, 290);
  // Save settings
  if (obExamDate) localStorage.setItem('oet_exam_date', obExamDate);
  localStorage.setItem('oet_focus', JSON.stringify(obFocus));
  localStorage.setItem('oet_onboard_v2', '1');
  updateExamCountdown();
  // Re-fetch lesson with updated lang/exam_date/focus (may differ from page-load defaults)
  initLangUI();
  document.getElementById('loadingBox').style.display = 'block';
  document.getElementById('lessonBox').style.display = 'none';
  fetch(buildLessonUrl()).then(r => r.json()).then(l => { lesson = l; renderLesson(l); }).catch(() => {});
}
let obLangPicked = false;
let obDateConfirmed = false;
function nextOnboard() {
  const o = OB[currentLang] || OB['zh-TW'];
  // Slide 3: must pick a language
  if (obSlide === 3) {
    if (!obLangPicked) {
      const h = document.getElementById('ob-lang-hint');
      h.textContent = o.langHint || '請選擇一種語言 ↑'; h.style.opacity = '1';
      setTimeout(() => h.style.opacity = '0', 1800);
      return;
    }
  }
  // Slide 4: must pick a date or explicitly skip
  if (obSlide === 4) {
    if (!obDateConfirmed) {
      const h = document.getElementById('ob-date-hint');
      h.textContent = o.dateHint || '請選擇考試日期，或點「略過」'; h.style.opacity = '1';
      setTimeout(() => h.style.opacity = '0', 1800);
      return;
    }
  }
  // Slide 5 (last): must pick at least one skill
  if (obSlide === OB_TOTAL - 1) {
    if (obFocus.length === 0) {
      const hint = document.getElementById('ob-skill-hint');
      hint.textContent = o.skillHint; hint.classList.add('warn');
      setTimeout(() => hint.classList.remove('warn'), 1500);
      return;
    }
    closeOnboard(); return;
  }
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
    document.querySelectorAll('.ob-lang-opt').forEach(el => {
      el.classList.toggle('selected', el.dataset.lang === currentLang);
    });
  } else if (n === 4) {
    document.getElementById('ob-s5title').textContent = o.s5title;
    document.getElementById('ob-s5sub').textContent = o.s5sub;
    document.getElementById('ob-skip-date').textContent = o.skipDate;
    // Set min date to today
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('ob-date-input').min = today;
    if (obExamDate) _refreshDateResult(obExamDate, o);
  } else if (n === 5) {
    document.getElementById('ob-s6title').textContent = o.s6title;
    document.getElementById('ob-s6sub').textContent = o.s6sub;
    document.getElementById('ob-skill-hint').textContent = o.skillHint;
    // Reflect current obFocus state
    document.querySelectorAll('.ob-skill-opt').forEach(el => {
      el.classList.toggle('selected', obFocus.includes(el.dataset.skill));
    });
  }
}
// ── Language picker (slide 3) ──────────────────────────────────────
function obPickLang(el) {
  document.querySelectorAll('.ob-lang-opt').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  obLangPicked = true;
  currentLang = el.dataset.lang;
  localStorage.setItem('oet_lang', currentLang);
  initLangUI();
  const o = OB[currentLang] || OB['zh-TW'];
  document.getElementById('ob-s4title').textContent = o.s4;
  document.getElementById('ob-s4sub').textContent = o.s4sub;
  document.getElementById('ob-s4streak').textContent = o.streak;
  document.getElementById('ob-btn').textContent = o.next; // slide 3 is not last
}
// ── Exam date picker (slide 4) ─────────────────────────────────────
function _refreshDateResult(isoDate, o) {
  const days = Math.ceil((new Date(isoDate) - new Date()) / 86400000);
  const r = document.getElementById('ob-date-result');
  if (days > 0) {
    r.textContent = '📅 ' + days + ' ' + (o||OB[currentLang]||OB['zh-TW']).daysLeft;
    r.style.display = 'block';
  }
}
function obPickDate(el) {
  document.querySelectorAll('.ob-date-opt').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  obDateConfirmed = true;
  const days = parseInt(el.dataset.days);
  const d = new Date(); d.setDate(d.getDate() + days);
  obExamDate = d.toISOString().split('T')[0];
  document.getElementById('ob-date-input').value = obExamDate;
  localStorage.setItem('oet_exam_date', obExamDate);
  _refreshDateResult(obExamDate);
}
function obCustomDate(input) {
  if (!input.value) return;
  document.querySelectorAll('.ob-date-opt').forEach(o => o.classList.remove('selected'));
  obExamDate = input.value;
  obDateConfirmed = true;
  localStorage.setItem('oet_exam_date', obExamDate);
  _refreshDateResult(obExamDate);
}
function obSkipDate() {
  obDateConfirmed = true;
  obExamDate = null;
  localStorage.removeItem('oet_exam_date');
  document.querySelectorAll('.ob-date-opt').forEach(o => o.classList.remove('selected'));
  document.getElementById('ob-date-input').value = '';
  document.getElementById('ob-date-result').style.display = 'none';
  setObSlide(5);
}
// ── Skill focus picker (slide 5) ───────────────────────────────────
function obToggleSkill(el) {
  const skill = el.dataset.skill;
  const on = obFocus.includes(skill);
  if (on && obFocus.length === 1) {
    const hint = document.getElementById('ob-skill-hint');
    const o = OB[currentLang] || OB['zh-TW'];
    hint.textContent = o.skillHint; hint.classList.add('warn');
    setTimeout(() => hint.classList.remove('warn'), 1200);
    return;
  }
  if (on) { obFocus = obFocus.filter(s => s !== skill); el.classList.remove('selected'); }
  else { obFocus.push(skill); el.classList.add('selected'); }
  localStorage.setItem('oet_focus', JSON.stringify(obFocus));
}

function getClientDate() {
  // Returns local date as YYYY-MM-DD (respects user's system timezone)
  return new Date().toLocaleDateString('en-CA');
}
function getSettings() {
  const lang = localStorage.getItem('oet_lang') || currentLang || 'zh-TW';
  const examDate = localStorage.getItem('oet_exam_date') || null;
  let focus = null;
  try { focus = JSON.parse(localStorage.getItem('oet_focus')); } catch(e) {}
  return { lang, examDate, focus };
}
function buildLessonUrl() {
  const s = getSettings();
  const p = new URLSearchParams({ lang: s.lang, client_date: getClientDate() });
  if (s.examDate) p.set('exam_date', s.examDate);
  if (s.focus) p.set('focus', JSON.stringify(s.focus));
  if (MISSED_DAYS > 0) p.set('missed_days', MISSED_DAYS);
  return '/api/lesson?' + p.toString();
}
function updateExamCountdown() {
  const ed = localStorage.getItem('oet_exam_date');
  if (!ed) return;
  const days = Math.ceil((new Date(ed) - new Date()) / 86400000);
  const hero = document.getElementById('examCountdownHero');
  const txt = document.getElementById('examDaysText');
  if (hero && txt && days > 0) {
    txt.textContent = days + ' days to exam';
    hero.style.display = 'block';
  }
}
function initLangUI() {
  const info = LANGS[currentLang] || LANGS['zh-TW'];
  document.getElementById('langFlag').textContent = info.flag;
  document.getElementById('langName').textContent = info.name;
  const lt = document.getElementById('loadingText');
  const ls = document.getElementById('loadingSubText');
  const rs = document.getElementById('recordStatus');
  if (lt) lt.textContent = t('loading');
  if (ls) ls.textContent = t('loadSub');
  if (rs && !rs.textContent) rs.textContent = t('hint');
  // Bottom bar status messages
  const doneMsg = document.getElementById('statusDoneMsg');
  if (doneMsg) doneMsg.textContent = t('completeDone');
  const tiredMsg = document.getElementById('statusTiredMsg');
  if (tiredMsg) tiredMsg.textContent = t('tiredDone');
  // Tired button
  const tiredBtnEl = document.getElementById('tiredBtnEl');
  if (tiredBtnEl && !tiredBtnEl._confirming) tiredBtnEl.textContent = t('tiredBtn');
  // Vocab hint badge
  const vhb = document.getElementById('vocabHintBadge');
  if (vhb && !tabsDone.vocab) vhb.textContent = t('vocabHint');
}

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

// ── Phase Test ──────────────────────────────────────────────────────────────
let phaseTestData = null;
let phaseTestPhase = null;

async function checkPhaseTest() {
  try {
    const r = await fetch('/api/phase-status');
    const d = await r.json();
    if (d.pending_phase) {
      phaseTestPhase = d.pending_phase;
      document.getElementById('phaseTestOverlay').style.display = 'flex';
      const { lang, examDate } = getSettings();
      const tr = await fetch(`/api/phase-test?phase=${d.pending_phase}&lang=${lang}${examDate ? '&exam_date='+examDate : ''}`);
      if (!tr.ok) { document.getElementById('phaseTestOverlay').style.display = 'none'; return; }
      phaseTestData = await tr.json();
      renderPhaseTest(phaseTestData);
    }
  } catch(e) { console.error('checkPhaseTest', e); }
}

function renderPhaseTest(data) {
  document.getElementById('ptLoading').style.display = 'none';
  document.getElementById('ptContent').style.display = 'block';
  document.getElementById('ptTitle').textContent = data.title || `Phase ${data.phase} 結業測驗`;
  document.getElementById('ptSummary').textContent = data.summary_intro || '';
  document.getElementById('ptVocabQs').innerHTML = _ptRenderQs(data.vocab_questions, 'ptv');
  document.getElementById('ptListenScenario').textContent = data.listening.scenario || '';
  document.getElementById('ptListenDialogue').innerHTML = (data.listening.dialogue || [])
    .map(l => `<div class="pt-line ${l.speaker==='Nurse'?'pt-nurse':'pt-patient'}"><span class="pt-spk">${l.speaker}</span><span style="font-size:.82rem">${l.text}</span></div>`).join('');
  document.getElementById('ptListenQs').innerHTML = _ptRenderQs(data.listening.questions, 'ptl');
  document.getElementById('ptReadArticle').textContent = data.reading.article || '';
  document.getElementById('ptReadQs').innerHTML = _ptRenderQs(data.reading.questions, 'ptr');
}

function _ptRenderQs(qs, prefix) {
  return (qs || []).map((q, i) => `
    <div class="pt-q">
      <div class="pt-q-text">${i+1}. ${q.q}</div>
      ${(q.options || []).map(o => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="${prefix}${i}" id="${prefix}${i}${o[0]}" value="${o[0]}">
          <label class="form-check-label" for="${prefix}${i}${o[0]}">${o}</label>
        </div>`).join('')}
    </div>`).join('');
}

let _phaseTestSaved = false;
async function submitPhaseTest() {
  if (!phaseTestData) return;
  const scores = {vocab:0, listening:0, reading:0};
  (phaseTestData.vocab_questions || []).forEach((q,i) => {
    const s = document.querySelector(`input[name="ptv${i}"]:checked`);
    if (s && s.value === q.answer) scores.vocab++;
  });
  (phaseTestData.listening.questions || []).forEach((q,i) => {
    const s = document.querySelector(`input[name="ptl${i}"]:checked`);
    if (s && s.value === q.answer) scores.listening++;
  });
  (phaseTestData.reading.questions || []).forEach((q,i) => {
    const s = document.querySelector(`input[name="ptr${i}"]:checked`);
    if (s && s.value === q.answer) scores.reading++;
  });
  document.getElementById('ptContent').style.display = 'none';
  document.getElementById('ptResults').style.display = 'block';
  const total = scores.vocab + scores.listening + scores.reading;
  const skillNames = {vocab:'🌸 詞彙', listening:'🎵 聽力', reading:'🫖 閱讀'};
  document.getElementById('ptResultScores').innerHTML = Object.entries(scores).map(([k,v]) => `
    <div class="pt-score-row">
      <span>${skillNames[k]}</span>
      <div class="pt-score-bar-wrap"><div class="pt-score-bar" style="width:${Math.round(v/3*100)}%;background:${v>=2?'#10b981':'#ef4444'}"></div></div>
      <span>${v}/3</span>
    </div>`).join('');
  document.getElementById('ptResultSummary').textContent = `總分 ${total} / 9`;
  const weak = Object.entries(scores).filter(([k,v]) => v<2).map(([k]) => k);
  const weakNames = weak.map(w => skillNames[w]).join('、');
  document.getElementById('ptWeakAreas').innerHTML = weak.length
    ? `<div class="pt-weak-notice">📌 需加強：<strong>${weakNames}</strong><br>後續課程將針對這些項目加強難度與練習量。</div>`
    : `<div class="pt-strong-notice">🌟 全部優秀！繼續保持！</div>`;
  // Save and await before enabling dismiss
  try {
    await fetch('/api/phase-test/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phase: phaseTestPhase, scores, lang: getSettings().lang})
    });
    _phaseTestSaved = true;
  } catch(e) { _phaseTestSaved = true; } // allow dismiss even on error
}

function dismissPhaseTest() {
  // Reload page so lesson regenerates with updated weak_areas
  if (_phaseTestSaved) { location.reload(); }
  else { document.getElementById('phaseTestOverlay').style.display = 'none'; }
}

window.onload = async () => {
  updateExamCountdown();
  checkPhaseTest();
  try {
    const resp = await fetch(buildLessonUrl());
    if (!resp.ok) throw new Error('伺服器錯誤 ' + resp.status + '，請稍後重試');
    lesson = await resp.json();
    renderLesson(lesson);
  } catch(e) {
    document.getElementById('loadingBox').innerHTML =
      '<div class="danger-box m-3">⚠️ 載入失敗：' + e.message + '<br><button class="btn btn-primary mt-2" onclick="location.reload()">重試</button></div>';
  }
};

function markTabDone(tab) {
  if (tabsDone[tab]) return;
  tabsDone[tab] = true;
  const pill = document.getElementById('prog-' + tab);
  if (pill) pill.classList.add('done');
  const count = Object.values(tabsDone).filter(Boolean).length;
  const txt = document.getElementById('tabProgressText');
  const tabNames = {vocab:'🌸 詞彙', read:'🫖 閱讀', listen:'🎵 聽力', speak:'💌 口說', write:'🌹 寫作'};
  if (count < 5) {
    const remaining = Object.keys(tabsDone).filter(k => !tabsDone[k]).map(k => tabNames[k]);
    if (txt) txt.textContent = count + ' / 5 完成  ·  待完成：' + remaining.join('、');
  } else {
    if (txt) txt.textContent = '✅ 5 / 5 完成！';
  }
  if (count >= 5) {
    setTimeout(() => {
      const prog = document.getElementById('tabProgress');
      const acts = document.getElementById('tabCompleteActions');
      if (prog) prog.style.display = 'none';
      if (acts) acts.style.display = 'flex';
      toast('🎉 5/5 全部完成！點「完成今日課程」記錄今天！');
    }, 400);
  }
}
function renderLesson(l) {
  document.getElementById('loadingBox').style.display = 'none';
  document.getElementById('lessonBox').style.display = 'block';
  document.getElementById('bottomBar').style.display = 'block';
  if (!localStorage.getItem('oet_tour_v1')) setTimeout(showTour, 650);
  vocabFlipped.clear();
  document.getElementById('encouragement').textContent = l.encouragement || '今天也要加油！';
  // Render compensation tips if present
  if (l.compensation && l.compensation.none !== true) {
    const c = l.compensation;
    if (c.speak_tip) {
      const s = document.getElementById('speakPhrases');
      if (s) s.insertAdjacentHTML('beforebegin', `<div class="warn-box mb-2" style="border-left-color:var(--p)">⭐ 補強提示：${c.speak_tip}</div>`);
    }
    if (c.write_tip) {
      const w = document.getElementById('writeTip');
      if (w) w.insertAdjacentHTML('afterend', `<div class="info-box mt-2 mb-2" style="font-size:.88rem">⭐ 補強示範：${c.write_tip}</div>`);
    }
  }

  // Vocab flip cards
  document.getElementById('vocabContent').innerHTML = l.vocabulary.map((v,i) => `
    <div class="flip-card" onclick="flipVocabCard(this,${i})"  data-idx="${i}">
      <div class="flip-card-inner">
        <div class="flip-card-front">
          <span class="flip-hint">點我翻面 →</span>
          <div class="d-flex align-items-center gap-2 mb-2">
            <span style="font-size:1.45rem;font-weight:900;color:var(--p);letter-spacing:-.01em">${v.word}</span>
            <button class="speak-btn" onclick="event.stopPropagation();speakWord('${v.word.replace(/'/g,"\\'")}')">🔊</button>
          </div>
          <div class="ipa mb-2">${v.ipa || ''}</div>
          <div style="font-size:.9rem;color:#5D8A6E;line-height:1.55;background:#EEF6F1;border-radius:8px;padding:.45rem .6rem;border-left:3px solid #8DC4A8">🌿 ${v.tip}</div>
        </div>
        <div class="flip-card-back">
          <div class="d-flex justify-content-between mb-2">
            <span style="font-size:1.15rem;font-weight:800;color:#2e8f61">${v.native || v.zh || ''}</span>
            <span class="flip-hint">← 點我翻回</span>
          </div>
          <div style="font-size:.92rem;font-style:italic;color:#4A3B32;line-height:1.6">"${v.example}"</div>
        </div>
      </div>
    </div>
  `).join('');

  // Listening
  document.getElementById('listenScenario').innerHTML = '🎵 ' + l.listening.scenario;
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
  if (l.writing) {
    document.getElementById('writeTip').textContent = l.writing.tip || '';
    document.getElementById('writeBefore').textContent = l.writing.before || '';
    document.getElementById('writeAfter').textContent = l.writing.after || '';
    document.getElementById('writeTask').textContent = l.writing.task || '';
  }
}

function flipVocabCard(el, idx) {
  el.classList.toggle('flipped');
  if (el.classList.contains('flipped')) {
    vocabFlipped.add(idx);
    const total = lesson ? lesson.vocabulary.length : 3;
    const badge = document.getElementById('vocabHintBadge');
    if (vocabFlipped.size >= total) {
      markTabDone('vocab');
      if (badge) { badge.textContent = '✅ 全部翻完！'; badge.style.background = '#dcfce7'; badge.style.color = '#059669'; }
    } else {
      if (badge) badge.textContent = t('vocabHint').replace('✨', '') + '（' + vocabFlipped.size + '/' + total + '）✨';
    }
  }
}

function renderReadQuestions(qs, prefix) {
  prefix = prefix || 'rq';
  return (qs || []).map((q, i) => `
    <div class="read-q">
      <div style="font-size:.88rem;font-weight:600;margin-bottom:.6rem;line-height:1.5">${i+1}. ${q.q}</div>
      ${q.options.map(o => `
        <div class="form-check">
          <input class="form-check-input" type="radio" name="${prefix}${i}" id="${prefix}${i}${o[0]}" value="${o[0]}">
          <label class="form-check-label" for="${prefix}${i}${o[0]}">${o}</label>
        </div>`).join('')}
    </div>`).join('');
}

function checkReading() {
  if (!lesson || !lesson.reading) { toast(t('notAll'), 'error'); return; }
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
  markTabDone('read');
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
      document.getElementById('listenQs').innerHTML = renderReadQuestions(lesson.listening.questions, 'q');
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


function showAnswers() {
  document.getElementById('listenAnswers').innerHTML =
    '<div class="fw-semibold small mb-2">解答</div>' +
    lesson.listening.questions.map(q =>
      `<div class="mb-2 small feedback-box"><span class="badge bg-success me-1">${q.answer}</span>${q.explanation}</div>`
    ).join('');
  document.getElementById('listenAnswers').style.display = 'block';
  document.getElementById('showAnswerBtn').style.display = 'none';
  markTabDone('listen');
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
  if (!lesson || !lesson.speaking) return text;
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
  const sb = document.getElementById('startBtn');
  sb.disabled = true; sb.classList.add('recording');
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
  const sb = document.getElementById('startBtn');
  sb.disabled = false; sb.classList.remove('recording');
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
    const evalResp = await fetch('/api/evaluate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({spoken, scenario: lesson.speaking.scenario, sample: lesson.speaking.sample, lang: currentLang})
    });
    if (!evalResp.ok) throw new Error('評分失敗 ' + evalResp.status);
    const f = await evalResp.json();
    scoreHistory.push(f.score);
    const best = Math.max(...scoreHistory);
    document.getElementById('scoreHistory').innerHTML =
      scoreHistory.map((s,i) => `<span class="score-pill ${s===best?'best':''}">${t('attempt',i+1)} ${s}⭐</span>`).join('');
    const stars = '⭐'.repeat(f.score) + '☆'.repeat(5 - f.score);
    const cr = f.criteria || {};
    const crHtml = cr.intelligibility ? `
      <div class="mb-2" style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;font-size:.85rem">
        <div class="p-2 rounded-2" style="background:#FFF5F4;border:1px solid #E8C8C6"><b>🗣 Intelligibility</b> ${cr.intelligibility.score}/5<br><span style="color:#4A3B32">${cr.intelligibility.comment}</span></div>
        <div class="p-2 rounded-2" style="background:#EEF6F1;border:1px solid #8DC4A8"><b>🌊 Fluency</b> ${cr.fluency.score}/5<br><span style="color:#4A3B32">${cr.fluency.comment}</span></div>
        <div class="p-2 rounded-2" style="background:#FFEEF2;border:1px solid #E8C8C6"><b>🤝 Appropriateness</b> ${cr.appropriateness.score}/5<br><span style="color:#4A3B32">${cr.appropriateness.comment}</span></div>
        <div class="p-2 rounded-2" style="background:#FFF0E6;border:1px solid #D4C4B0"><b>📖 Grammar+Vocab</b> ${cr.grammar_vocab.score}/5<br><span style="color:#4A3B32">${cr.grammar_vocab.comment}</span></div>
      </div>` : '';
    document.getElementById('speakFeedback').innerHTML = `
      <div class="fw-bold mb-2" style="font-size:1.15rem;color:var(--p)">${stars} ${f.score}/5 · Band ${f.band||'B'}</div>
      ${crHtml}
      <div class="mb-2" style="font-size:.9rem"><b style="color:var(--success)">✅ 做得好：</b>${f.good}</div>
      <div class="mb-2" style="font-size:.9rem"><b style="color:var(--p)">🌱 改進點：</b>${f.improve}</div>
      <div class="mb-2" style="font-size:.88rem"><b>🌸 建議用語：</b>${f.vocabulary}</div>
      <div class="mt-2 p-2 rounded-2" style="font-size:.88rem;background:var(--p-light);color:var(--p-dark);border-left:3px solid var(--p)">🎯 <b>考場提示：</b>${f.oet_tip}</div>
    `;
    document.getElementById('speakFeedback').style.display = 'block';
    markTabDone('speak');
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
    const writeResp = await fetch('/api/evaluate-writing', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ answer, task: document.getElementById('writeTask').textContent, tip: document.getElementById('writeTip').textContent, lang: currentLang })
    });
    if (!writeResp.ok) throw new Error('批改失敗 ' + writeResp.status);
    const f = await writeResp.json();
    const stars = '⭐'.repeat(f.score) + '☆'.repeat(5-f.score);
    const wc = f.criteria || {};
    const wcHtml = wc.purpose ? `
      <div class="mb-2" style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem;font-size:.82rem">
        <div class="p-2 rounded-2" style="background:#f5f3ff"><b>🎯 Purpose</b> ${wc.purpose.score}/5<br>${wc.purpose.comment}</div>
        <div class="p-2 rounded-2" style="background:#f0fdf4"><b>📋 Content</b> ${wc.content.score}/5<br>${wc.content.comment}</div>
        <div class="p-2 rounded-2" style="background:#fff1f2"><b>✂️ Conciseness</b> ${wc.conciseness.score}/5<br>${wc.conciseness.comment}</div>
        <div class="p-2 rounded-2" style="background:#fff7ed"><b>🖋 Genre Style</b> ${wc.genre_style.score}/5<br>${wc.genre_style.comment}</div>
      </div>` : '';
    document.getElementById('writeFeedback').innerHTML = `
      <div class="fw-bold mb-1" style="font-size:1.1rem">${stars} ${f.score}/5 · Band ${f.oet_band||'B'}</div>
      ${wcHtml}
      <div class="mb-1 small">📝 ${f.grammar}</div>
      <div class="mb-1 small">📖 ${f.vocabulary}</div>
      <div class="mb-2 small">🏗️ ${f.structure}</div>
      <div class="p-2 rounded-2 small" style="background:#f0fdf4;border-left:3px solid #10b981">
        <div class="fw-semibold mb-1">改寫示範</div>
        <div class="fst-italic">${f.rewrite}</div>
      </div>
      <div class="mt-2 small text-muted">💬 ${f.summary}</div>
    `;
    document.getElementById('writeFeedback').style.display = 'block';
    markTabDone('write');
    btn.textContent = t('regrade');
  } catch(e) {
    toast('⚠️ ' + e.message, 'error');
    btn.textContent = t('regrade');
  }
  btn.disabled = false;
}

const XP_ANIMS = [
  {icon:'🌸', color:'#e879a0', bar:'linear-gradient(90deg,#f9a8d4,#ec4899)', msg:'今天辛苦了！'},
  {icon:'⭐', color:'#f59e0b', bar:'linear-gradient(90deg,#fde68a,#f59e0b)', msg:'滿分收工！'},
  {icon:'💪', color:'#8b5cf6', bar:'linear-gradient(90deg,#c4b5fd,#7c3aed)', msg:'持續突破自己！'},
  {icon:'🎉', color:'#10b981', bar:'linear-gradient(90deg,#6ee7b7,#059669)', msg:'完美的一天！'},
  {icon:'🌟', color:'#3b82f6', bar:'linear-gradient(90deg,#93c5fd,#2563eb)', msg:'離 Band B 又更近了！'},
];
async function markComplete() {
  await fetch('/api/complete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({client_date: getClientDate()})});
  const a = XP_ANIMS[Math.floor(Math.random() * XP_ANIMS.length)];
  const font = OB_FONTS[currentLang] || OB_FONTS['zh-TW'];
  const ov = document.createElement('div'); ov.id = 'xpOverlay'; ov.classList.add('show');
  ov.innerHTML = `<div class="xp-bg"></div>
    <div class="xp-center" style="font-family:${font}">
      <div class="xp-icon">${a.icon}</div>
      <div class="xp-title" style="color:#fff">+ XP UP！</div>
      <div class="xp-sub">${a.msg}</div>
      <div class="xp-bar-wrap"><div class="xp-bar-fill" style="background:${a.bar}"></div></div>
    </div>`;
  // Floating +XP numbers
  for (let i = 0; i < 6; i++) {
    const f = document.createElement('div'); f.className = 'xp-float';
    f.style.cssText = `left:${10+Math.random()*80}%;top:${20+Math.random()*55}%;animation-delay:${i*0.22}s;font-family:${font};color:rgba(255,255,255,.9)`;
    f.textContent = ['+XP','✦','+10','🌸','+EXP','💫'][i];
    ov.appendChild(f);
  }
  document.body.appendChild(ov);
  setTimeout(() => {
    ov.classList.remove('show'); ov.classList.add('hide');
    setTimeout(() => location.reload(), 420);
  }, 2400);
}
let _resetPending = false, _resetTimer = null;
function confirmReset() {
  const btn = document.getElementById('resetBtn');
  if (!_resetPending) {
    _resetPending = true;
    btn.textContent = '⚠ 確定重設？再點一次';
    btn.style.opacity = '1'; btn.style.color = '#e05a7a';
    _resetTimer = setTimeout(() => { _resetPending = false; btn.textContent = '⚙ 重設'; btn.style.opacity = '.45'; btn.style.color = ''; }, 4000);
  } else {
    clearTimeout(_resetTimer); _resetPending = false;
    fetch('/api/reset', {method:'POST'}).then(() => { localStorage.clear(); location.reload(); });
  }
}
let tiredConfirmPending = false;
let tiredConfirmTimer = null;
function markTired() {
  const btn = document.getElementById('tiredBtnEl');
  if (!tiredConfirmPending) {
    tiredConfirmPending = true;
    if (btn) { btn._confirming = true; btn.textContent = t('tiredConfirm'); btn.style.color = 'var(--danger)'; }
    tiredConfirmTimer = setTimeout(() => {
      tiredConfirmPending = false;
      if (btn) { btn._confirming = false; btn.textContent = t('tiredBtn'); btn.style.color = ''; }
    }, 4000);
  } else {
    clearTimeout(tiredConfirmTimer);
    tiredConfirmPending = false;
    if (btn) { btn._confirming = false; }
    fetch('/api/tired', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tabs_done: tabsDone, lang: currentLang, client_date: getClientDate() })
    }).then(() => location.reload());
  }
}

// Ensure server uses local timezone date — redirect once if client_date param is missing/stale
(function() {
  const clientDate = getClientDate();
  const params = new URLSearchParams(window.location.search);
  if (params.get('client_date') !== clientDate) {
    window.location.replace('/?client_date=' + clientDate);
  }
})();

// Run immediately — script is at end of body so DOM is ready
initLangUI();
showMissedBanner();
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
