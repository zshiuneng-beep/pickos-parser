"""
PickOS PDF 解析伺服器 v3.1
部署平台：Render.com
更新：
- 支援 PDF/A-1b 格式（PDFCreator 輸出）
- 加強 PICK 號碼辨識，減少「無法辨識」情況
- 多重解析策略，提高成功率
"""
from flask import Flask, request, jsonify
import pdfplumber, re, io

app = Flask(__name__)

@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, apikey'
    return resp

@app.route('/')
def home():
    return jsonify({'status': 'PickOS Parser running', 'version': '3.1'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/parse', methods=['POST', 'OPTIONS'])
def parse_route():
    if request.method == 'OPTIONS':
        return '', 204
    if 'file' not in request.files:
        return jsonify({'error': '未收到 PDF 檔案'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'error': '請上傳 PDF 格式'}), 400

    # 接收前端偵測到的 pickId 作為備用
    hint_id = request.form.get('pickId', '').strip()

    try:
        result = parse_pdf(f.read(), hint_id=hint_id)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def clean_text(text: str) -> str:
    """清理文字：移除多餘空白、統一全半形"""
    if not text:
        return ''
    # 統一全形冒號
    text = text.replace('：', ':')
    # 移除多餘空白
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_all_text(pdf) -> str:
    """從所有頁面提取全部文字，PDF/A 相容"""
    all_text = []
    for page in pdf.pages:
        # 方法1：標準提取
        t = page.extract_text(x_tolerance=3, y_tolerance=3)
        if t:
            all_text.append(t)
        else:
            # 方法2：從 words 重組（PDF/A-1b 有時需要）
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if words:
                all_text.append(' '.join(w['text'] for w in words))
    return '\n'.join(all_text)


def detect_pick_id(text: str, words_txt: str, hint_id: str = '') -> str:
    """
    多重策略偵測 PICK 號碼，優先順序：
    1. 前端傳入的 hint_id
    2. 完整 PICK 格式（PICK + 10位以上數字）
    3. 寬鬆 PICK 格式（PICK + 8位以上數字）
    4. 從合併文字中搜尋
    5. 從原始文字搜尋（處理空格插入問題）
    """
    # 1. 前端 hint 優先（前端已成功偵測）
    if hint_id and re.match(r'PICK\d{8,}', hint_id, re.IGNORECASE):
        return hint_id.upper()

    search_targets = [words_txt, text]

    for target in search_targets:
        if not target:
            continue
        # 2. 標準格式
        m = re.search(r'\b(PICK\d{10,})\b', target, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        # 3. 寬鬆格式
        m = re.search(r'\b(PICK\d{8,})\b', target, re.IGNORECASE)
        if m:
            return m.group(1).upper()

    # 4. 處理 PDF/A 空格插入問題（如 "P I C K 2 2 2 2 ..."）
    # 移除所有空格後再搜尋
    no_space = re.sub(r'\s', '', words_txt or text or '')
    m = re.search(r'(PICK\d{8,})', no_space, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # 5. 從文字中找連續數字組合（PICK 後面可能被斷行）
    m = re.search(r'PICK[\s\n]*(\d[\d\s]{7,})', words_txt or '', re.IGNORECASE)
    if m:
        digits = re.sub(r'\s', '', m.group(1))
        candidate = 'PICK' + digits
        if len(candidate) >= 12:
            return candidate.upper()

    return ''


def parse_pdf(data: bytes, hint_id: str = '') -> dict:
    results = []
    meta = {}
    pick_id = ''

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pi, page in enumerate(pdf.pages):
            # ── 提取文字（相容 PDF/A）──────────────────────────────
            full_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ''
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            words_txt = ' '.join(w['text'] for w in words)

            # 如果標準提取失敗，用 words 重組
            if not full_text and words_txt:
                full_text = words_txt

            if pi == 0:
                # ── 偵測 PICK 號碼 ──────────────────────────────────
                pick_id = detect_pick_id(full_text, words_txt, hint_id)

                # ── 提取 meta 資訊 ──────────────────────────────────
                combined = clean_text(words_txt + ' ' + full_text)

                for pat, key in [
                    (r'总订单数:?\s*(\d+)',      'orders'),
                    (r'总sku种类数:?\s*(\d+)',   'skuCount'),
                    (r'总件数:?\s*(\d+)',         'totalQty'),
                    (r'总重量\(kg\):?([\d.]+)',  'weight'),
                    (r'总体积\(m[³3]\):?([\d.]+)', 'volume'),
                ]:
                    m = re.search(pat, combined)
                    if m:
                        meta[key] = m.group(1)

                # ── 提貨方式（座標法 + fallback）───────────────────
                SKIP_KEYWORDS = {
                    '总订单数','总sku','总件数','总重量','总体积',
                    '播种单','自动打包','⾃动打包','低库位','高库位',
                    '⾼库位','单品单件','出库增值','出库','增值',':','：'
                }
                words_pos = page.extract_words(x_tolerance=3, y_tolerance=3)
                label_w = next((w for w in words_pos if '提货' in w['text']), None)
                if label_w:
                    label_top = label_w['top']
                    label_x1  = label_w['x1']
                    chuKu_x   = next(
                        (w['x0'] for w in words_pos
                         if '出库' in w['text'] and abs(w['top'] - label_top) < 20),
                        999
                    )
                    dw = [
                        w for w in words_pos
                        if w['x0'] >= label_x1
                        and w['x0'] < chuKu_x - 5
                        and w['top'] >= label_top - 5
                        and w['top'] < label_top + 60
                        and not any(k in w['text'] for k in SKIP_KEYWORDS)
                    ]
                    dw.sort(key=lambda w: (round(w['top']/4)*4, w['x0']))
                    rows_d, cur_y, cur_r = [], -99, []
                    for w in dw:
                        y = round(w['top']/4)*4
                        if abs(y - cur_y) > 5:
                            if cur_r: rows_d.append(' '.join(cur_r))
                            cur_r = [w['text']]; cur_y = y
                        else:
                            cur_r.append(w['text'])
                    if cur_r: rows_d.append(' '.join(cur_r))
                    delivery = ' '.join(rows_d).strip().rstrip(':： ')
                    if delivery:
                        meta['delivery'] = delivery
                else:
                    # Fallback regex
                    dm = re.search(
                        r'提货[⽅方]式[：:\s]+([\s\S]+?)(?:出库增值|$)',
                        full_text, re.DOTALL
                    )
                    if dm:
                        delivery = re.sub(r'\s+', ' ', dm.group(1)).strip().rstrip(':： ')
                        if delivery:
                            meta['delivery'] = delivery

            # ── 解析表格（多重策略）────────────────────────────────
            tables = page.extract_tables({
                'vertical_strategy': 'lines',
                'horizontal_strategy': 'lines',
                'intersection_tolerance': 5,
            })

            # 如果線條策略失敗，嘗試文字策略（PDF/A 常見）
            if not tables or all(len(t) == 0 for t in tables):
                tables = page.extract_tables({
                    'vertical_strategy': 'text',
                    'horizontal_strategy': 'text',
                    'intersection_tolerance': 5,
                })

            for table in tables:
                for row in table:
                    if not row or len(row) < 5:
                        continue
                    seq_str = (row[0] or '').strip().replace('\n', '')
                    if not re.match(r'^\d+$', seq_str):
                        continue
                    seq = int(seq_str)

                    loc = name = ''
                    if row[1]:
                        parts = row[1].strip().split('\n')
                        loc  = parts[0].strip()
                        name = ' '.join(p.strip() for p in parts[1:] if p.strip())

                    sku = (row[2] or '').replace('\n', '').strip()

                    barcode = ebarcode = ''
                    if row[3]:
                        bc = row[3].strip().split('\n')
                        barcode  = bc[0].strip().rstrip(',')
                        ebarcode = bc[1].strip().rstrip(',') if len(bc) > 1 else ''

                    qty = 1
                    if row[4]:
                        m2 = re.match(r'^(\d+)', str(row[4]).strip())
                        if m2: qty = int(m2.group(1))

                    if not name: name = sku
                    if sku == barcode: sku = ''

                    if seq > 0 and loc and barcode:
                        results.append({
                            'seq': seq, 'loc': loc, 'name': name,
                            'sku': sku, 'barcode': barcode,
                            'ebarcode': ebarcode, 'qty': qty
                        })

    # ── 最後嘗試：用 hint_id 補救 pick_id ─────────────────────────
    if not pick_id and hint_id:
        pick_id = hint_id

    if not pick_id or not results:
        raise ValueError(f'無法解析 (pickId={pick_id}, items={len(results)})')

    return {'pickId': pick_id, 'meta': meta, 'items': results}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, debug=False)
