"""
PickOS PDF 解析伺服器 v3.0
部署平台：Render.com
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
    return jsonify({'status': 'PickOS Parser running', 'version': '3.0'})

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
    try:
        result = parse_pdf(f.read())
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def parse_pdf(data: bytes) -> dict:
    results = []
    meta = {}
    pick_id = ''

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pi, page in enumerate(pdf.pages):
            if pi == 0:
                # Use full text for meta + delivery method
                full_text = page.extract_text() or ''
                words = page.extract_words()
                words_txt = ' '.join(w['text'] for w in words)

                m = re.search(r'(PICK[A-Z0-9]+)', words_txt)
                if m: pick_id = m.group(1)

                for pat, key in [
                    (r'总订单数[：:]\s*(\d+)',      'orders'),
                    (r'总sku种类数[：:]\s*(\d+)',   'skuCount'),
                    (r'总件数[：:]\s*(\d+)',         'totalQty'),
                    (r'总重量\(kg\)[：:]([\d.]+)',  'weight'),
                ]:
                    m = re.search(pat, words_txt)
                    if m: meta[key] = m.group(1)

                # ── Delivery method (coordinate-based) ──────────────────
                # PDF layout splits delivery across multiple lines and the
                # text parser inserts 出库增值 in the middle of the value.
                # Use word x/y positions to collect only the delivery cell.
                SKIP_KEYWORDS = {
                    '总订单数','总sku','总件数','总重量','总体积',
                    '播种单','自动打包','⾃动打包','低库位','高库位',
                    '⾼库位','单品单件','出库增值','出库','增值',':','：'
                }
                words_pos = page.extract_words()
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
                    # Group into rows then join
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
                    # Fallback: full-text regex
                    dm = re.search(
                        r'提货[⽅方]式[：:\s]+([\s\S]+?)出库增值',
                        full_text, re.DOTALL
                    )
                    if dm:
                        delivery = re.sub(r'\s+', ' ', dm.group(1)).strip().rstrip(':： ')
                        if delivery:
                            meta['delivery'] = delivery

            tables = page.extract_tables()
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

    if not pick_id or not results:
        raise ValueError(f'無法解析 (pickId={pick_id}, items={len(results)})')

    return {'pickId': pick_id, 'meta': meta, 'items': results}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, debug=False)
