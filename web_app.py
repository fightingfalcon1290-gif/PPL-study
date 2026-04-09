"""
PPL Study Tool - Flask Web Application
FAA PPLライセンス取得のための学習ツール
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for
import json, os, base64, io, datetime, uuid, time, csv, re, sys
from PIL import Image
import anthropic

# ─── PyInstaller 対応パスヘルパー ─────────────────────────────────
def _res(rel):
    """同梱リソースのパス（読み取り専用: templates, static, textbook）"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def _dat(rel=''):
    """書き込み可能データのパス（exe隣: config.json, data/）"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel) if rel else base

app = Flask(__name__,
            template_folder=_res('templates'),
            static_folder=_res('static'))
app.secret_key = 'ppl-study-tool-2026'

# ─── 定数 ─────────────────────────────────────────────────────────
CATEGORIES = {
    'A': '飛行機の部品',
    'B': '空気力学',
    'C': '性能',
    'D': '重量重心',
    'E': '計器',
    'F': 'エンジン',
    'G': '空港',
    'H': '空域',
    'I': '飛行',
    'J': 'チャート',
    'K': '航法',
    'L': '気象理論',
    'M': '気象データ',
    'N': '生理学',
    'O': '航空法規',
}

BASE_DIR      = _dat()
DATA_DIR      = _dat('data')
RECORDS_FILE  = _dat('data/learning_records.json')
QUIZLET_FILE  = _dat('data/quizlet.csv')
CHARTS_DIR    = _dat('data/charts')
CONFIG_FILE   = _dat('config.json')
TEXTBOOK_FILE = _res('textbook.txt')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHARTS_DIR, exist_ok=True)

# ─── グローバル状態（単一ユーザー前提） ────────────────────────────
pending_questions = []   # [{id, image_b64, result, created_at}]
processing_status = {'running': False, 'progress': 0, 'total': 0, 'log': []}

# ─── データ永続化 ─────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'api_key': ''}

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_records():
    if os.path.exists(RECORDS_FILE):
        with open(RECORDS_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'questions': [], 'oral_records': []}

def save_records(data):
    with open(RECORDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 保存のたびにバックグラウンドでGitHubへプッシュ
    import threading
    threading.Thread(target=_git_push, daemon=True).start()

def load_quizlet():
    """QuizletのCSVを読み込む（複数フォーマット対応）"""
    if not os.path.exists(QUIZLET_FILE):
        return []
    rows = []
    try:
        with open(QUIZLET_FILE, encoding='utf-8-sig') as f:
            content = f.read()
        delimiter = '\t' if '\t' in content.split('\n')[0] else ','
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)
        for i, row in enumerate(reader):
            if len(row) >= 2:
                rows.append({'id': i, 'term': row[0].strip(), 'definition': row[1].strip()})
        return rows
    except Exception as e:
        print(f"Quizlet load error: {e}")
        return []

def load_textbook():
    """教科書テキストを読み込む"""
    if os.path.exists(TEXTBOOK_FILE):
        with open(TEXTBOOK_FILE, encoding='utf-8') as f:
            return f.read()
    return ''

def get_textbook_section(category_key):
    """指定カテゴリの教科書セクションを返す（最大4000文字）"""
    text = load_textbook()
    if not text:
        return ''
    # "A. ", "B. " 等のセクション見出しで分割
    sections = re.split(r'\n(?=[A-O]\. [A-Z])', text)
    for section in sections:
        if section.strip().startswith(category_key + '.'):
            return section[:4000]
    return ''

def srs_next_date(q):
    """エビングハウス忘却曲線に基づく次回復習日を返す"""
    history = q.get('result_history', [])
    if not history:
        return datetime.date.min  # 未学習 → 即出題
    last_entry = history[-1]
    try:
        last_date = datetime.date.fromisoformat(last_entry['date'][:10])
    except Exception:
        return datetime.date.min
    # 末尾から連続正解数を数える
    streak = 0
    for h in reversed(history):
        if h.get('result') == 'correct':
            streak += 1
        else:
            break
    intervals = [1, 3, 7, 14, 30]
    if streak == 0:
        interval = 1
    else:
        interval = intervals[min(streak - 1, len(intervals) - 1)]
    return last_date + datetime.timedelta(days=interval)

def calc_study_streak(records):
    """連続学習日数を計算"""
    qs = records.get('questions', [])
    oral = records.get('oral_records', [])
    dates = set()
    for q in qs:
        for h in q.get('result_history', []):
            try:
                dates.add(datetime.date.fromisoformat(h['date'][:10]))
            except Exception:
                pass
    for r in oral:
        for h in r.get('history', []):
            try:
                dates.add(datetime.date.fromisoformat(h['date'][:10]))
            except Exception:
                pass
    if not dates:
        return 0
    today = datetime.date.today()
    streak = 0
    d = today
    while d in dates:
        streak += 1
        d -= datetime.timedelta(days=1)
    # 昨日から始まっても連続とみなす
    if streak == 0:
        d = today - datetime.timedelta(days=1)
        while d in dates:
            streak += 1
            d -= datetime.timedelta(days=1)
    return streak

def calc_category_trends(records):
    """カテゴリ別・週次正答率の推移を計算（直近8週）"""
    qs = records.get('questions', [])
    today = datetime.date.today()
    weeks = []
    for i in range(7, -1, -1):
        week_end = today - datetime.timedelta(weeks=i)
        weeks.append(week_end)

    trends = {}
    for cat in CATEGORIES:
        cat_qs = [q for q in qs if q.get('category') == cat]
        if not cat_qs:
            continue
        series = []
        for week_end in weeks:
            # その週末までの全履歴で正答率を計算
            correct = total = 0
            for q in cat_qs:
                for h in q.get('result_history', []):
                    try:
                        d = datetime.date.fromisoformat(h['date'][:10])
                    except Exception:
                        continue
                    if d <= week_end:
                        total += 1
                        if h.get('result') == 'correct':
                            correct += 1
            rate = round(correct / total * 100, 1) if total > 0 else None
            series.append(rate)
        if any(v is not None for v in series):
            trends[cat] = {'name': CATEGORIES[cat], 'data': series}
    labels = [(today - datetime.timedelta(weeks=i)).strftime('%-m/%-d') for i in range(7, -1, -1)]
    return {'labels': labels, 'trends': trends}

def calc_accuracy(history):
    if not history:
        return None
    # correct のみ正解扱い（unsure・incorrect は不正解）
    correct = sum(1 for h in history if h.get('result') == 'correct')
    return round(correct / len(history) * 100, 1)

def accuracy_label(rate):
    if rate is None:
        return '未学習'
    if rate <= 50:
        return '🔴'
    if rate <= 79:
        return '🟡'
    return '🟢'

# ─── ダッシュボード統計 ────────────────────────────────────────────
def calculate_stats(records):
    qs = records.get('questions', [])
    oral = records.get('oral_records', [])
    total = len(qs)
    if total == 0:
        return {'total': 0, 'accuracy': 0, 'oral_total': len(oral),
                'weak_categories': [], 'pending': len(pending_questions)}
    correct = sum(1 for q in qs
                  if q.get('result_history') and q['result_history'][-1]['result'] == 'correct')
    accuracy = round(correct / total * 100, 1)

    cat_stats = {}
    for q in qs:
        cat = q.get('category', '?')
        if cat not in cat_stats:
            cat_stats[cat] = {'correct': 0, 'total': 0}
        hist = q.get('result_history', [])
        if hist:
            cat_stats[cat]['total'] += 1
            if hist[-1]['result'] == 'correct':
                cat_stats[cat]['correct'] += 1

    weak = []
    for cat, s in cat_stats.items():
        if s['total'] > 0:
            rate = round(s['correct'] / s['total'] * 100, 1)
            weak.append({'category': cat, 'name': CATEGORIES.get(cat, cat),
                         'rate': rate, 'label': accuracy_label(rate)})
    weak.sort(key=lambda x: x['rate'])

    return {'total': total, 'accuracy': accuracy, 'oral_total': len(oral),
            'weak_categories': weak[:5], 'pending': len(pending_questions)}

# ─── Claude API ───────────────────────────────────────────────────
def get_claude_client():
    cfg = load_config()
    return anthropic.Anthropic(api_key=cfg.get('api_key', ''))

HAIKU_MODEL = 'claude-haiku-4-5-20251001'

def analyze_screenshot_with_claude(image_b64: str, needs_explanation: bool, quizlet_terms: list,
                                    chart_b64: str = None) -> dict:
    """スクリーンショットをClaudeで解析。Pass1:OCR、Pass2:解説生成"""
    client = get_claude_client()
    cat_list = '\n'.join(f'{k}: {v}' for k, v in CATEGORIES.items())
    quizlet_sample = ', '.join(q['term'] for q in quizlet_terms[:30])

    # ── Pass 1: OCR（画像解析） ──
    chart_note = "1枚目が問題、2枚目がチャート（航空図）です。" if chart_b64 else ""
    ocr_prompt = f"""FAA PPL試験問題のスクリーンショットです。{chart_note}
以下を抽出・判定してください：

【問題文】問題文を完全に抽出。
【選択肢】全選択肢を1行ずつ（例: A. text）
【正解記号】正解の記号1文字のみ（A/B/C/D）
【正解】正解の選択肢テキスト（全文）。
【カテゴリ】1文字のみ（{', '.join(CATEGORIES.keys())}から選択）。カテゴリ一覧：
{cat_list}
【単元】カテゴリ内の単元名（15字以内）。
【オーラル関連】次のトピックと関連あるか（yes/no）：{quizlet_sample[:200]}

読み取れない場合は「読み取れず」と記載。"""

    content = [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': image_b64}},
    ]
    if chart_b64:
        content.append(
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': chart_b64}}
        )
    content.append({'type': 'text', 'text': ocr_prompt})

    response1 = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1500,
        messages=[{'role': 'user', 'content': content}],
    )
    text1 = response1.content[0].text

    def extract(label, next_label=None, src=None):
        t = src or text1
        tag = f'【{label}】'
        if tag not in t:
            return ''
        after = t.split(tag)[1]
        if next_label and f'【{next_label}】' in after:
            return after.split(f'【{next_label}】')[0].strip()
        return after.strip()

    question_text  = extract('問題文', '選択肢')
    choices_raw    = extract('選択肢', '正解記号')
    correct_letter = extract('正解記号', '正解').strip().upper()[:1]
    answer_text    = re.sub(r'^\*{0,2}\(?[A-D]\)?[.):\s]\*{0,2}\s*', '', extract('正解', 'カテゴリ'), flags=re.IGNORECASE).strip('* \n')
    raw_cat        = extract('カテゴリ', '単元').strip().upper()
    unit           = extract('単元', 'オーラル関連')
    oral_raw       = extract('オーラル関連')

    choices = [c.strip() for c in choices_raw.strip().splitlines() if c.strip()]
    category = raw_cat[0] if raw_cat and raw_cat[0] in CATEGORIES else 'H'
    oral_related = 'yes' in oral_raw.lower()

    result = {
        'question_text':  question_text,
        'answer_text':    answer_text,
        'choices':        choices,
        'correct_letter': correct_letter,
        'category':       category,
        'category_name':  CATEGORIES.get(category, ''),
        'unit':           unit,
        'oral_related':   oral_related,
        'explanation':    {'why': '', 'memory_tip': '', 'confusion_points': '', 'connections': '', 'common_mistakes': ''},
    }

    if not needs_explanation:
        return result

    # ── Pass 2: 解説生成（5セクション）──
    tb_section = get_textbook_section(category)
    tb_ctx = f"\n\n【教科書抜粋】\n{tb_section[:1000]}" if tb_section else ""

    exp_prompt = f"""FAA PPL試験問題の解説を日本語で作成してください。{tb_ctx}

問題：{question_text}
正解：{answer_text}

以下の5セクションを必ず全て含めてください。各セクションは必ず1〜2文、合計200字以内に収めること：

【なぜそうなるのか】
【覚え方】
【混同注意】
【つながり】
【間違えやすい点】"""

    REQUIRED_SECTIONS = ['なぜそうなるのか', '覚え方', '混同注意', 'つながり', '間違えやすい点']

    def call_explanation_api():
        r = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=800,
            messages=[{'role': 'user', 'content': exp_prompt}],
        )
        return r.content[0].text

    def parse_explanation(text):
        return {
            'why':              extract('なぜそうなるのか', '覚え方', src=text),
            'memory_tip':       extract('覚え方', '混同注意', src=text),
            'confusion_points': extract('混同注意', 'つながり', src=text),
            'connections':      extract('つながり', '間違えやすい点', src=text),
            'common_mistakes':  extract('間違えやすい点', None, src=text),
        }

    def needs_retry(text, parsed):
        # 完全に空か極端に短い場合のみリトライ
        return not text or len(text) < 80

    text2 = call_explanation_api()
    parsed2 = parse_explanation(text2)
    if needs_retry(text2, parsed2):
        text2 = call_explanation_api()
        parsed2 = parse_explanation(text2)

    parsed2['textbook_ref'] = {
        'category': category,
        'category_name': CATEGORIES.get(category, ''),
        'excerpt': tb_section[:500] if tb_section else '',
    }
    result['explanation'] = parsed2
    return result

def process_with_claude(pending: list, quizlet_terms: list) -> list:
    """ペンディング問題をClaudeで一括処理"""
    results = []
    total = len(pending)
    processing_status['total'] = total
    processing_status['log'] = []

    # IDをループ前に1回だけ取得し、各問題で+1ずつ増やす
    records_data = load_records()
    next_id = max((q['id'] for q in records_data['questions']), default=0) + 1

    for i, pq in enumerate(pending):
        processing_status['progress'] = i + 1
        msg = f"問題 {i+1}/{total} を処理中..."
        processing_status['log'].append(msg)
        print(msg)

        # 迷った・不正解 → 解説生成あり
        needs_explanation = pq.get('result') in ('incorrect', 'unsure')
        chart_b64 = pq.get('chart_b64')
        try:
            info = analyze_screenshot_with_claude(
                pq['image_b64'], needs_explanation, quizlet_terms, chart_b64=chart_b64
            )
        except Exception as e:
            print(f"Claude error for question {i+1}: {e}")
            info = {
                'question_text': f'解析エラー: {e}',
                'answer_text': '', 'category': 'H', 'category_name': '空域',
                'unit': '不明', 'oral_related': False,
                'explanation': {'why': '', 'memory_tip': '', 'confusion_points': '', 'connections': '', 'common_mistakes': ''},
            }

        new_id = next_id + i

        chart_filename = None
        if chart_b64:
            chart_filename = f'{new_id}.png'
            chart_path = os.path.join(CHARTS_DIR, chart_filename)
            with open(chart_path, 'wb') as f:
                f.write(base64.b64decode(chart_b64))

        entry = {
            'id': new_id,
            **info,
            'chart_filename': chart_filename,
            'result_history': [{
                'date': datetime.datetime.now().isoformat(),
                'result': pq.get('result', 'incorrect'),
            }],
            'created_at': datetime.datetime.now().isoformat(),
        }
        entry['accuracy_rate'] = calc_accuracy(entry['result_history'])
        results.append(entry)

    return results

# ─── ページルート ─────────────────────────────────────────────────
@app.route('/')
def index():
    records = load_records()
    stats = calculate_stats(records)
    return render_template('index.html', stats=stats, categories=CATEGORIES)

@app.route('/record')
def record():
    return render_template('record.html',
                           pending=pending_questions,
                           categories=CATEGORIES)

@app.route('/today')
def today():
    records = load_records()
    streak  = calc_study_streak(records)
    return render_template('today.html', streak=streak)

@app.route('/api/today/questions', methods=['GET'])
def api_today_questions():
    records = load_records()
    qs      = records.get('questions', [])
    today_d = datetime.date.today()

    # 期限切れ（due <= today）
    due = [q for q in qs if srs_next_date(q) <= today_d]
    # 期限切れが10未満なら正答率の低い順で補充
    if len(due) < 10:
        not_due = [q for q in qs if q not in due]
        not_due.sort(key=lambda q: calc_accuracy(q.get('result_history', [])) or 0)
        due += not_due[:10 - len(due)]

    # 10問に絞る（最も期限が古い順）
    due.sort(key=lambda q: srs_next_date(q))
    selected = due[:10]

    result = []
    for q in selected:
        exp = q.get('explanation', {})
        next_d = srs_next_date(q)
        overdue = (today_d - next_d).days if next_d <= today_d else 0
        result.append({
            'id':             q['id'],
            'question_text':  q.get('question_text', ''),
            'answer_text':    q.get('answer_text', ''),
            'choices':        q.get('choices', []),
            'correct_letter': q.get('correct_letter', ''),
            'category':       q.get('category', ''),
            'category_name':  q.get('category_name', ''),
            'unit':           q.get('unit', ''),
            'overdue_days':   overdue,
            'accuracy':       calc_accuracy(q.get('result_history', [])),
            'explanation': {
                'why':              exp.get('why', ''),
                'memory_tip':       exp.get('memory_tip', ''),
                'confusion_points': exp.get('confusion_points', ''),
                'connections':      exp.get('connections', ''),
                'common_mistakes':  exp.get('common_mistakes', ''),
                'textbook_ref':     exp.get('textbook_ref'),
            },
        })
    return jsonify({'ok': True, 'questions': result, 'total': len(result)})

@app.route('/api/stats/detail', methods=['GET'])
def api_stats_detail():
    records = load_records()
    return jsonify({
        'ok':     True,
        'streak': calc_study_streak(records),
        'trends': calc_category_trends(records),
    })

@app.route('/review')
def review():
    records = load_records()
    cats = sorted(set(q.get('category', '') for q in records['questions'] if q.get('category')))
    return render_template('review.html', categories=CATEGORIES, available_cats=cats)

@app.route('/oral')
def oral():
    quizlet = load_quizlet()
    has_quizlet = len(quizlet) > 0
    return render_template('oral.html', categories=CATEGORIES,
                           has_quizlet=has_quizlet, quizlet_count=len(quizlet))

@app.route('/textbook')
@app.route('/textbook/<category>')
def textbook(category=None):
    records  = load_records()
    active   = (category or 'A').upper()
    all_qs   = records.get('questions', [])

    cat_summaries = {}
    for key in CATEGORIES:
        qs = [q for q in all_qs if q.get('category') == key]
        hist = [h for q in qs for h in q.get('result_history', [])]
        cat_summaries[key] = {
            'count': len(qs),
            'rate':  calc_accuracy(hist),
        }

    cat_qs = [q for q in all_qs if q.get('category') == active]
    sorted_qs = sorted(cat_qs,
                       key=lambda q: q.get('accuracy_rate') if q.get('accuracy_rate') is not None else -1)

    weak_qs = [q for q in sorted_qs
               if q.get('accuracy_rate') is not None and q['accuracy_rate'] <= 50]

    # NEWフラグ（当日追加された問題のみ）
    today_str = datetime.date.today().isoformat()
    new_cat_counts = {}
    for q in all_qs:
        q['is_new'] = q.get('created_at', '')[:10] == today_str
        if q['is_new']:
            new_cat_counts[q.get('category', '')] = new_cat_counts.get(q.get('category', ''), 0) + 1

    return render_template('textbook.html',
                           categories=CATEGORIES,
                           active_category=active,
                           cat_summaries=cat_summaries,
                           sorted_qs=sorted_qs,
                           weak_qs=weak_qs,
                           accuracy_label=accuracy_label,
                           new_cat_counts=new_cat_counts)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    cfg = load_config()
    msg = ''
    if request.method == 'POST':
        api_key = request.form.get('api_key', '').strip()
        if api_key:
            cfg['api_key'] = api_key
            save_config(cfg)
            msg = 'APIキーを保存しました。'
    has_key = bool(cfg.get('api_key'))
    has_quizlet = os.path.exists(QUIZLET_FILE)
    return render_template('settings.html', has_key=has_key, msg=msg,
                           has_quizlet=has_quizlet, quizlet_path=QUIZLET_FILE)

# ─── API: 記録モード ──────────────────────────────────────────────
def _do_screenshot():
    """スクリーンショットを撮って base64 文字列を返す"""
    import pyautogui
    shot = pyautogui.screenshot()
    buf  = io.BytesIO()
    shot.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

@app.route('/api/screenshot', methods=['POST'])
def take_screenshot():
    """問題スクリーンショットを撮影して新規ペンディングに追加"""
    try:
        delay = int((request.get_json(force=True, silent=True) or {}).get('delay', 3))
        time.sleep(delay)
        img_b64 = _do_screenshot()
        temp_id = str(uuid.uuid4())[:8]
        pending_questions.append({
            'id': temp_id,
            'image_b64': img_b64,
            'result': None,
            'created_at': datetime.datetime.now().isoformat(),
        })
        return jsonify({'ok': True, 'id': temp_id, 'image': img_b64,
                        'count': len(pending_questions)})
    except BaseException as e:
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'})

@app.route('/api/screenshot-chart', methods=['POST'])
def take_chart_screenshot():
    """チャートスクショを撮って既存ペンディング問題に添付"""
    try:
        body    = request.get_json(force=True, silent=True) or {}
        q_id    = body.get('id')
        img_b64 = _do_screenshot()
        for q in pending_questions:
            if q['id'] == q_id:
                q['chart_b64'] = img_b64
                return jsonify({'ok': True, 'image': img_b64})
        return jsonify({'ok': False, 'error': f'question {q_id} not found'})
    except BaseException as e:
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'})

@app.route('/api/attach-chart', methods=['POST'])
def attach_chart():
    """スクショ済み画像をペンディング問題にチャートとして添付（pyautogui不使用）"""
    try:
        body    = request.json or {}
        q_id    = body.get('id')
        img_b64 = body.get('image')
        if not q_id or not img_b64:
            return jsonify({'ok': False, 'error': 'id または image が不足しています'})
        for q in pending_questions:
            if q['id'] == q_id:
                q['chart_b64'] = img_b64
                return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': f'pending question not found (id={q_id})'})
    except BaseException as e:
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'})

@app.route('/api/chart/<filename>')
def serve_chart(filename):
    from flask import send_from_directory
    return send_from_directory(CHARTS_DIR, filename)

@app.route('/api/record-result', methods=['POST'])
def api_record_result():
    data   = request.json
    q_id   = data.get('id')
    result = data.get('result')  # 'correct' | 'unsure' | 'incorrect'
    for q in pending_questions:
        if q['id'] == q_id:
            q['result'] = result
            answered = sum(1 for q in pending_questions if q['result'])
            return jsonify({'ok': True, 'count': len(pending_questions), 'answered': answered})
    return jsonify({'ok': False, 'error': 'not found'})

@app.route('/api/pending', methods=['GET'])
def api_pending():
    items = [{'id': q['id'], 'result': q['result'],
              'created_at': q['created_at'], 'has_chart': bool(q.get('chart_b64'))}
             for q in pending_questions]
    answered = sum(1 for q in pending_questions if q['result'])
    return jsonify({'items': items, 'count': len(pending_questions), 'answered': answered})

@app.route('/api/pending/<q_id>', methods=['DELETE'])
def api_delete_pending(q_id):
    for i, q in enumerate(pending_questions):
        if q['id'] == q_id:
            pending_questions.pop(i)
            return jsonify({'ok': True})
    return jsonify({'ok': False})

@app.route('/api/process', methods=['POST'])
def api_process():
    global processing_status
    if processing_status['running']:
        return jsonify({'ok': False, 'error': '処理中です。しばらくお待ちください。'})
    if not pending_questions:
        return jsonify({'ok': False, 'error': 'ペンディング問題がありません。'})

    cfg = load_config()
    if not cfg.get('api_key'):
        return jsonify({'ok': False, 'error': 'APIキーが設定されていません。設定画面で登録してください。'})

    unanswered = [q for q in pending_questions if not q['result']]
    if unanswered:
        return jsonify({'ok': False, 'error': f'{len(unanswered)}問の結果が未入力です。'})

    processing_status = {'running': True, 'progress': 0,
                         'total': len(pending_questions), 'log': []}

    import threading
    def do_process():
        global processing_status
        try:
            quizlet = load_quizlet()
            results = process_with_claude(list(pending_questions), quizlet)
            records = load_records()
            records['questions'].extend(results)
            save_records(records)
            pending_questions.clear()
            processing_status['log'].append(f'✅ {len(results)}問を教科書に追記しました。')
        except Exception as e:
            processing_status['log'].append(f'❌ エラー: {e}')
        finally:
            processing_status['running'] = False

    threading.Thread(target=do_process, daemon=True).start()
    return jsonify({'ok': True, 'total': processing_status['total']})

@app.route('/api/process-status', methods=['GET'])
def api_process_status():
    return jsonify(processing_status)

# ─── API: 復習モード ──────────────────────────────────────────────
@app.route('/api/review/next', methods=['GET'])
def api_review_next():
    mode     = request.args.get('mode', 'weak')
    category = request.args.get('category', '')
    records  = load_records()
    qs = records.get('questions', [])

    if not qs:
        return jsonify({'ok': False, 'error': '問題がありません。記録モードで問題を追加してください。'})

    if category:
        qs = [q for q in qs if q.get('category') == category]
    if not qs:
        return jsonify({'ok': False, 'error': 'このカテゴリに問題がありません。'})

    import random
    if mode == 'new':
        # created_at の新しい順（直近7日以内を優先、なければ全体から新しい順）
        from datetime import timezone
        now = datetime.datetime.now(timezone.utc)
        def created_dt(q):
            s = q.get('created_at', '')
            try:
                dt = datetime.datetime.fromisoformat(s)
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            except Exception:
                return datetime.datetime(2000, 1, 1, tzinfo=timezone.utc)
        recent = [q for q in qs if (now - created_dt(q)).days <= 7]
        pool = recent if recent else qs
        pool = sorted(pool, key=created_dt, reverse=True)
        q = random.choice(pool[:max(1, len(pool))])
    elif mode == 'weak':
        def sort_key(q):
            hist = q.get('result_history', [])
            acc  = calc_accuracy(hist) or 0.0
            if not hist:
                return (1, acc)
            last = hist[-1]['result']
            if last == 'incorrect':
                return (0, acc)
            elif last == 'unsure':
                return (1, acc)
            else:
                return (2, acc)
        qs_sorted = sorted(qs, key=sort_key)
        pool = qs_sorted[:max(1, len(qs_sorted) // 3 + 1)]
        q = random.choice(pool)
    elif mode == 'random':
        q = random.choice(qs)
    else:
        q = random.choice(qs)

    exp = q.get('explanation', {})
    today_str = datetime.date.today().isoformat()
    is_new = q.get('created_at', '')[:10] == today_str
    return jsonify({
        'ok': True,
        'id':             q['id'],
        'question_text':  q.get('question_text', ''),
        'answer_text':    q.get('answer_text', ''),
        'choices':        q.get('choices', []),
        'correct_letter': q.get('correct_letter', ''),
        'category':       q.get('category', ''),
        'category_name':  q.get('category_name', ''),
        'unit':           q.get('unit', ''),
        'oral_related':   q.get('oral_related', False),
        'is_new':         is_new,
        'created_at':     q.get('created_at', ''),
        'explanation': {
            'why':              exp.get('why', ''),
            'memory_tip':       exp.get('memory_tip', ''),
            'confusion_points': exp.get('confusion_points', ''),
            'connections':      exp.get('connections', ''),
            'common_mistakes':  exp.get('common_mistakes', ''),
            'textbook_ref':     exp.get('textbook_ref'),
        },
        'accuracy': calc_accuracy(q.get('result_history', [])),
    })

@app.route('/api/review/result', methods=['POST'])
def api_review_result():
    data   = request.json
    q_id   = data.get('id')
    result = data.get('result')  # 'correct' | 'unsure' | 'incorrect'

    records = load_records()
    for q in records['questions']:
        if q['id'] == q_id:
            if 'result_history' not in q:
                q['result_history'] = []
            q['result_history'].append({
                'date': datetime.datetime.now().isoformat(),
                'result': result,
            })
            q['accuracy_rate'] = calc_accuracy(q['result_history'])
            save_records(records)
            return jsonify({'ok': True, 'accuracy': q['accuracy_rate']})

    return jsonify({'ok': False, 'error': 'not found'})

# ─── API: オーラルモード ──────────────────────────────────────────
@app.route('/api/oral/next', methods=['GET'])
def api_oral_next():
    mode = request.args.get('mode', 'random')
    quizlet = load_quizlet()
    if not quizlet:
        return jsonify({'ok': False, 'error': 'Quizlet CSVがありません。設定画面でファイルを配置してください。'})

    records = load_records()
    oral_records = {r['quizlet_id']: r for r in records.get('oral_records', [])}

    import random
    if mode == 'weak':
        def oral_sort(q):
            r = oral_records.get(q['id'])
            if not r:
                return 0.0
            return calc_accuracy(r.get('history', [])) or 0.0
        pool = sorted(quizlet, key=oral_sort)
        pool = pool[:max(1, len(pool) // 3 + 1)]
        q = random.choice(pool)
    else:
        q = random.choice(quizlet)

    oral_rec = oral_records.get(q['id'], {})
    accuracy = calc_accuracy(oral_rec.get('history', []))

    return jsonify({
        'ok': True,
        'id': q['id'],
        'term': q['term'],
        'definition': q['definition'],
        'accuracy': accuracy,
    })

@app.route('/api/oral/explain', methods=['POST'])
def api_oral_explain():
    data = request.json
    term = data.get('term', '')
    definition = data.get('definition', '')

    cfg = load_config()
    if not cfg.get('api_key'):
        return jsonify({'ok': False, 'error': 'APIキーが未設定です'})

    try:
        client = get_claude_client()
        prompt = f"""FAA PPL口述試験（オーラル試験）の対策をしています。

質問: {term}
答え: {definition}

以下の形式で日本語でアドバイスをください。各セクションは簡潔にまとめること：

【オーラル用キーワード（英語）】
試験で使う重要な英単語・フレーズを3〜5個、箇条書きで。

【日本語解説】
この概念を日本語で100字以内で端的に説明。

【英語構文アドバイス】
DPEへの回答で使える英語表現を2行以内で。

【模範回答例文（英語）】
2文以内の自然な英語回答例。"""

        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = response.content[0].text

        def extract(label, next_label=None):
            if f'【{label}】' not in text:
                return ''
            after = text.split(f'【{label}】')[1]
            if next_label and f'【{next_label}】' in after:
                return after.split(f'【{next_label}】')[0].strip()
            return after.strip()

        result = {
            'ok': True,
            'keywords':    extract('オーラル用キーワード（英語）', '日本語解説'),
            'explanation': extract('日本語解説', '英語構文アドバイス'),
            'advice':      extract('英語構文アドバイス', '模範回答例文（英語）'),
            'example':     extract('模範回答例文（英語）', None),
        }

        # 解説をoral_recordsに保存
        q_id = data.get('id')
        if q_id is not None:
            records = load_records()
            records.setdefault('oral_records', [])
            saved = False
            for r in records['oral_records']:
                if r.get('quizlet_id') == int(q_id):
                    r['term']        = term
                    r['definition']  = definition
                    r['explanation'] = {k: result[k] for k in ('keywords','explanation','advice','example')}
                    r['explained_at']= datetime.datetime.now().isoformat()
                    saved = True
                    break
            if not saved:
                records['oral_records'].append({
                    'quizlet_id':   int(q_id),
                    'term':         term,
                    'definition':   definition,
                    'explanation':  {k: result[k] for k in ('keywords','explanation','advice','example')},
                    'explained_at': datetime.datetime.now().isoformat(),
                    'history':      [],
                })
            save_records(records)

        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/oral/explanations', methods=['GET'])
def api_oral_explanations():
    records = load_records()
    oral = records.get('oral_records', [])
    result = [r for r in oral if r.get('explanation')]
    result.sort(key=lambda r: r.get('explained_at', ''), reverse=True)
    return jsonify({'ok': True, 'items': result})

@app.route('/api/oral/result', methods=['POST'])
def api_oral_result():
    data   = request.json
    q_id   = int(data.get('id', 0))
    result = data.get('result')

    records = load_records()
    if 'oral_records' not in records:
        records['oral_records'] = []

    found = False
    for r in records['oral_records']:
        if r['quizlet_id'] == q_id:
            r['history'].append({'date': datetime.datetime.now().isoformat(), 'result': result})
            found = True
            break
    if not found:
        records['oral_records'].append({
            'quizlet_id': q_id,
            'history': [{'date': datetime.datetime.now().isoformat(), 'result': result}],
        })

    save_records(records)
    return jsonify({'ok': True})

# ─── API: データ管理 ──────────────────────────────────────────────
@app.route('/api/question/<int:q_id>', methods=['GET'])
def api_get_question(q_id):
    records = load_records()
    for q in records['questions']:
        if q['id'] == q_id:
            return jsonify({'ok': True, 'question': q})
    return jsonify({'ok': False, 'error': 'not found'})

@app.route('/api/question/<int:q_id>', methods=['PUT'])
def api_update_question(q_id):
    data    = request.json
    records = load_records()
    for q in records['questions']:
        if q['id'] == q_id:
            if 'question_text' in data: q['question_text'] = data['question_text']
            if 'answer_text'   in data: q['answer_text']   = data['answer_text']
            if 'unit'          in data: q['unit']          = data['unit']
            if 'category' in data:
                q['category']      = data['category'].upper()
                q['category_name'] = CATEGORIES.get(q['category'], '')
            if 'explanation' in data:
                q.setdefault('explanation', {})
                q['explanation'].update(data['explanation'])
            save_records(records)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'not found'})

@app.route('/api/question/<int:q_id>', methods=['DELETE'])
def api_delete_question(q_id):
    records = load_records()
    qs = records.get('questions', [])
    new_qs = [q for q in qs if q.get('id') != q_id]
    if len(new_qs) == len(qs):
        return jsonify({'ok': False, 'error': 'not found'})
    records['questions'] = new_qs
    save_records(records)
    return jsonify({'ok': True})

@app.route('/api/stats', methods=['GET'])
def api_stats():
    records = load_records()
    stats = calculate_stats(records)
    return jsonify(stats)

@app.route('/api/quizlet-upload', methods=['POST'])
def api_quizlet_upload():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'ファイルがありません'})
    f = request.files['file']
    if not f.filename.endswith('.csv'):
        return jsonify({'ok': False, 'error': '.csvファイルを選択してください'})
    f.save(QUIZLET_FILE)
    quizlet = load_quizlet()
    return jsonify({'ok': True, 'count': len(quizlet)})

# ─── テンプレートフィルタ ──────────────────────────────────────────
@app.template_filter('md')
def md_filter(text):
    if not text:
        return ''
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    lines = text.split('\n')
    html_lines = []
    for line in lines:
        if line.strip().startswith('- '):
            html_lines.append('<li>' + line.strip()[2:] + '</li>')
        elif re.match(r'^\d+\.\s+', line.strip()):
            m = re.match(r'^\d+\.\s+(.+)', line.strip())
            if m:
                html_lines.append('<li>' + m.group(1) + '</li>')
            else:
                html_lines.append(line)
        else:
            html_lines.append(line)
    result = '\n'.join(html_lines)
    result = result.replace('\n', '<br>')
    return result

@app.template_filter('accuracy_color')
def accuracy_color_filter(rate):
    if rate is None:
        return '#6c7086'
    if rate <= 50:
        return '#f38ba8'
    if rate <= 79:
        return '#f9e2af'
    return '#a6e3a1'

GITHUB_REPO   = "fightingfalcon1290-gif/PPL-study"
GITHUB_BRANCH = "main"


def _github_raw(path):
    import requests
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{path}"
    r = requests.get(url, timeout=10)
    return r if r.status_code == 200 else None


def _git_push():
    """学習記録をGitHubにプッシュする"""
    import subprocess
    base = _dat()
    try:
        subprocess.run(['git', 'add', 'data/learning_records.json'], cwd=base, check=True, capture_output=True)
        result = subprocess.run(['git', 'commit', '-m', 'sync: update learning records'],
                                cwd=base, capture_output=True, text=True)
        if 'nothing to commit' in result.stdout:
            return  # 変更なし
        subprocess.run(['git', 'push'], cwd=base, check=True, capture_output=True)
        print("  [sync] 学習記録をGitHubにプッシュしました")
    except Exception as e:
        print(f"  [sync] プッシュ失敗: {e}")


def sync_from_github():
    """GitHubから最新データを取得、学習記録は問題数が多い方を採用"""
    import requests

    # quizlet.csv / oral_questions.tsv は常に上書き取得
    for remote_path, local_path in [
        ("data/quizlet.csv",   _dat("data/quizlet.csv")),
        ("oral_questions.tsv", _dat("oral_questions.tsv")),
    ]:
        try:
            r = _github_raw(remote_path)
            if r:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(r.content)
                print(f"  [sync] 更新: {remote_path}")
        except Exception as e:
            print(f"  [sync] {remote_path} の取得失敗: {e}")

    # learning_records.json: 問題数が多い方を採用
    # 候補パス: data/以下、親フォルダ、さらに上の階層も確認
    canonical_path = _dat("data/learning_records.json")
    candidate_paths = [
        canonical_path,
        os.path.join(os.path.dirname(_dat()), "learning_records.json"),
        os.path.join(os.path.dirname(os.path.dirname(_dat())), "learning_records.json"),
    ]
    best_data  = None
    best_count = 0
    for p in candidate_paths:
        if os.path.exists(p):
            try:
                with open(p, encoding='utf-8') as f:
                    d = json.load(f)
                cnt = len(d.get('questions', []))
                if cnt > best_count:
                    best_count = cnt
                    best_data  = d
                    print(f"  [sync] ローカル候補: {p} ({cnt}問)")
            except Exception:
                pass

    # 最大問題数ファイルを正規パスにコピー
    if best_data is not None and best_count > 0:
        if not os.path.exists(canonical_path) or \
           best_count > len(json.load(open(canonical_path, encoding='utf-8')).get('questions', [])):
            os.makedirs(os.path.dirname(canonical_path), exist_ok=True)
            with open(canonical_path, 'w', encoding='utf-8') as f:
                json.dump(best_data, f, ensure_ascii=False, indent=2)
            print(f"  [sync] 最大問題数ファイルをdata/にコピー: {best_count}問")

    local_path  = canonical_path
    local_count = best_count

    try:
        r = _github_raw("data/learning_records.json")
        if r:
            remote_data  = r.json()
            remote_count = len(remote_data.get('questions', []))
            if remote_count > local_count:
                with open(local_path, 'w', encoding='utf-8') as f:
                    json.dump(remote_data, f, ensure_ascii=False, indent=2)
                print(f"  [sync] 学習記録を更新: リモート{remote_count}問 > ローカル{local_count}問")
            elif local_count > remote_count:
                print(f"  [sync] ローカルが最新: {local_count}問 > リモート{remote_count}問 → プッシュします")
                _git_push()
            else:
                print(f"  [sync] 学習記録は同じ ({local_count}問)")
        elif local_count > 0:
            # リモートにまだ存在しない場合はプッシュ
            print(f"  [sync] 学習記録をGitHubに初回プッシュします ({local_count}問)")
            _git_push()
    except Exception as e:
        print(f"  [sync] 学習記録の同期失敗: {e}")


if __name__ == '__main__':
    import threading, webbrowser, atexit

    print("=" * 50)
    print("PPL学習ツール 起動中...")
    print("GitHubから最新データを取得中...")
    sync_from_github()
    print("ブラウザで http://localhost:8080 を開いてください")
    print("終了するには このウィンドウを閉じてください")
    print("=" * 50)

    # 終了時にも学習記録をプッシュ
    atexit.register(_git_push)

    def _open_browser():
        time.sleep(1.5)
        webbrowser.open('http://localhost:8080')
    threading.Thread(target=_open_browser, daemon=True).start()

    is_frozen = getattr(sys, 'frozen', False)
    app.run(debug=not is_frozen, host='0.0.0.0', port=8080, use_reloader=False)
