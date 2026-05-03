"""
PickOS PDF 解析伺服器
部署平台：Render.com (免費方案)
功能：接收 PDF → pdfplumber 解析表格 → 回傳 JSON
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
    return jsonify({'status': 'PickOS Parser running', 'version': '2.0'})

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
    """
    使用 pdfplumber extract_tables() 解析撿貨單 PDF。
    比座標解析法可靠，直接讀取 PDF 表格向量線條。

    表格欄位 (5欄):
      [0] 序号   [1] 库位\nsku名称   [2] sku
      [3] barcode\nE-barcode         [4] 数\n量
    """
    results = []
    meta = {}
    pick_id = ''

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pi, page in enumerate(pdf.pages):
            # ── 第一頁抓 meta ──
            if pi == 0:
                words = page.extract_words()
                txt = ' '.join(w['text'] for w in words)
                m = re.search(r'(PICK[A-Z0-9]+)', txt)
                if m: pick_id = m.group(1)
                for pat, key in [
                    (r'总订单数[：:]\s*(\d+)',       'orders'),
                    (r'总sku种类数[：:]\s*(\d+)',    'skuCount'),
                    (r'总件数[：:]\s*(\d+)',          'totalQty'),
                    (r'总重量\(kg\)[：:]([\d.]+)',   'weight'),
                ]:
                    m = re.search(pat, txt)
                    if m: meta[key] = m.group(1)

            # ── 解析表格 ──
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 5:
                        continue

                    # 序號欄
                    seq_str = (row[0] or '').strip().replace('\n', '')
                    if not re.match(r'^\d+$', seq_str):
                        continue
                    seq = int(seq_str)

                    # 庫位 + 品名："RA11-50-1\n办公椅"
                    loc = name = ''
                    if row[1]:
                        parts = row[1].strip().split('\n')
                        loc  = parts[0].strip()
                        name = ' '.join(p.strip() for p in parts[1:] if p.strip())

                    # SKU (可能換行拼接)
                    sku = (row[2] or '').replace('\n', '').strip()

                    # Barcode + E-barcode
                    barcode = ebarcode = ''
                    if row[3]:
                        bc = row[3].strip().split('\n')
                        barcode  = bc[0].strip().rstrip(',') if bc else ''
                        ebarcode = bc[1].strip().rstrip(',') if len(bc) > 1 else ''

                    # 數量
                    qty = 1
                    if row[4]:
                        m2 = re.match(r'^(\d+)', str(row[4]).strip())
                        if m2: qty = int(m2.group(1))

                    if not name: name = sku
                    if sku == barcode: sku = ''  # 去除重複

                    if seq > 0 and loc and barcode:
                        results.append({
                            'seq':      seq,
                            'loc':      loc,
                            'name':     name,
                            'sku':      sku,
                            'barcode':  barcode,
                            'ebarcode': ebarcode,
                            'qty':      qty
                        })

    if not pick_id or not results:
        raise ValueError(f'無法解析 (pickId={pick_id}, items={len(results)})')

    return {'pickId': pick_id, 'meta': meta, 'items': results}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, debug=False)
