import http.server
import socketserver
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import os
import sys
import sqlite3
import re

PORT = int(os.environ.get("PORT", 8080))
DB_FILE = os.environ.get("DB_FILE", "minhan_crm.db")

# API KEY Gemini của bạn (Hỗ trợ biến môi trường khi Deploy lên Cloud)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JikBBroNMN9zu3AJi4tZwo1XHXcP85R_WsYixksbW-3w")

# ==========================================
# 1. KHỞI TẠO CƠ SỞ DỮ LIỆU TRỐNG 100% (KHÔNG BỊA SỐ LIỆU)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    # Bảng 1: Hồ sơ cơ hội (Bắt đầu hoàn toàn trống = 0)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS opportunities (
        id TEXT PRIMARY KEY,
        created_at TEXT,
        customer_name TEXT,
        company_name TEXT,
        contact_info TEXT,
        raw_request TEXT,
        extracted_json TEXT,
        total_amount REAL,
        discount_pct REAL,
        lead_time_days INTEGER,
        status TEXT,
        escalation_reasons TEXT,
        assigned_rep TEXT,
        draft_email TEXT
    )
    """)

    # Bảng 2: Danh mục Sản phẩm, Giá & Tồn kho do người dùng tự nhập
    cur.execute("""
    CREATE TABLE IF NOT EXISTS catalog (
        sku TEXT PRIMARY KEY,
        name TEXT,
        keywords TEXT,
        unit_price REAL,
        available_stock INTEGER,
        lead_time_out_of_stock INTEGER
    )
    """)

    # Bảng 3: Danh sách Nhân viên Kinh doanh do người dùng tự nhập
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sales_reps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        role TEXT
    )
    """)

    # Bảng 4: Chính sách chiết khấu & phân quyền do người dùng tự thiết lập (Mặc định = 0 khi chưa nhập)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS policies (
        key TEXT PRIMARY KEY,
        value REAL
    )
    """)

    conn.commit()
    conn.close()

init_db()

# ==========================================
# 2. CÁC HÀM ĐỌC / GHI DỮ LIỆU NGƯỜI DÙNG NHẬP
# ==========================================
def get_all_catalog():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM catalog ORDER BY sku ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_all_reps():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sales_reps ORDER BY id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_policies():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM policies")
    rows = {r["key"]: r["value"] for r in cur.fetchall()}
    conn.close()
    return {
        "max_rep_discount_pct": rows.get("max_rep_discount_pct", 0.0),
        "max_rep_deal_value": rows.get("max_rep_deal_value", 0.0),
        "default_in_stock_lead_days": int(rows.get("default_in_stock_lead_days", 0))
    }

_rep_rr_index = 0
def assign_next_rep():
    global _rep_rr_index
    reps = get_all_reps()
    if not reps:
        return {"name": "Chưa gán (Chưa nhập DS NVKD)", "email": "", "role": ""}
    rep = reps[_rep_rr_index % len(reps)]
    _rep_rr_index += 1
    return rep

def match_user_catalog(product_keyword, catalog_list):
    """So khớp sản phẩm khách yêu cầu với Bảng giá do người dùng đã nhập"""
    kw = (product_keyword or "").lower().strip()
    if not kw or not catalog_list:
        return None
    for item in catalog_list:
        sku_low = item["sku"].lower()
        name_low = item["name"].lower()
        user_kws = [k.strip().lower() for k in (item.get("keywords") or "").split(",") if k.strip()]
        if sku_low in kw or kw in name_low or name_low in kw:
            return item
        for uk in user_kws:
            if uk and uk in kw:
                return item
        # Kiểm tra trùng các từ chính
        words = [w for w in re.split(r'\s+', kw) if len(w) >= 3]
        if words and all(w in (name_low + " " + " ".join(user_kws) + " " + sku_low) for w in words):
            return item
    return None

# ==========================================
# 3. AI AGENT BÓC TÁCH & ĐỐI CHIẾU DỮ LIỆU THẬT
# ==========================================
def call_gemini_extract(text, catalog_list):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
    catalog_hint = ""
    if catalog_list:
        catalog_hint = "Danh mục sản phẩm hiện có trong hệ thống:\n" + "\n".join(
            [f"- Mã SKU: {c['sku']} | Tên: {c['name']} | Từ khóa: {c.get('keywords','')}" for c in catalog_list]
        )

    prompt = f"""Bạn là AI Tiếp nhận Yêu cầu B2B cho Công ty Minh An.
{catalog_hint}

Hãy đọc nội dung yêu cầu của khách hàng dưới đây và bóc tách dữ liệu trung thực 100% theo những gì khách viết (KHÔNG tự bịa thêm số liệu nếu khách không nêu):
\"\"\"{text}\"\"\"

Trả về đúng chuỗi JSON (không dùng ```json):
{{
    "customer_name": "Tên người liên hệ (hoặc null nếu không có)",
    "company_name": "Tên công ty/tổ chức (hoặc null nếu không có)",
    "contact_info": "Số điện thoại hoặc email (hoặc null nếu không có)",
    "items": [
        {{"product_keyword": "tên sản phẩm khách yêu cầu", "matched_sku": "Mã SKU khớp trong danh mục hoặc null", "quantity": số lượng nguyên hoặc null}}
    ],
    "budget": số tiền ngân sách khách nêu (số nguyên) hoặc null,
    "desired_deadline_days": số ngày khách muốn nhận hàng (số nguyên) hoặc null,
    "discount_requested_pct": phần trăm chiết khấu khách đề nghị (số thực) hoặc 0.0,
    "is_sufficient_info": true nếu khách đã nêu rõ tên sản phẩm và số lượng > 0, ngược lại false,
    "missing_fields": ["liệt kê các thông tin còn thiếu nếu is_sufficient_info là false"]
}}"""

    data = {"contents": [{"parts": [{"text": prompt}]}]}
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        raw_output = res["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw_output = re.sub(r"^```json\s*", "", raw_output)
        raw_output = re.sub(r"^```\s*", "", raw_output)
        raw_output = re.sub(r"\s*```$", "", raw_output)
        return json.loads(raw_output)

def fallback_extract(text, catalog_list):
    """Bóc tách dự phòng dựa trên đúng văn bản người dùng nhập và catalog hiện có"""
    text_lower = text.lower()
    items = []
    for cat in catalog_list:
        kws = [cat["name"].lower(), cat["sku"].lower()] + [k.strip().lower() for k in (cat.get("keywords") or "").split(",") if k.strip()]
        for k in kws:
            if k and k in text_lower:
                m = re.search(r'(\d+)\s*(?:bộ|cái|chiếc|máy|license|gói|x)?\s*' + re.escape(k), text_lower)
                qty = int(m.group(1)) if m else None
                items.append({"product_keyword": cat["name"], "matched_sku": cat["sku"], "quantity": qty})
                break

    discount_m = re.search(r'chiết khấu\s*(\d+(?:\.\d+)?)\s*%', text_lower) or re.search(r'giảm\s*(\d+(?:\.\d+)?)\s*%', text_lower)
    discount = float(discount_m.group(1)) if discount_m else 0.0

    deadline_m = re.search(r'(\d+)\s*ngày', text_lower)
    deadline_days = int(deadline_m.group(1)) if deadline_m else None

    valid_items = [i for i in items if i.get("quantity") and i["quantity"] > 0]
    missing = []
    if not valid_items:
        missing.append("Tên sản phẩm cụ thể và số lượng cần mua")
    if deadline_days is None:
        missing.append("Thời hạn giao hàng / triển khai dự kiến")

    return {
        "customer_name": None,
        "company_name": None,
        "contact_info": None,
        "items": items,
        "budget": None,
        "desired_deadline_days": deadline_days,
        "discount_requested_pct": discount,
        "is_sufficient_info": len(valid_items) > 0,
        "missing_fields": missing
    }

def process_and_save_request(raw_text):
    catalog_list = get_all_catalog()
    policies = get_policies()
    assigned_rep = assign_next_rep()

    try:
        extracted = call_gemini_extract(raw_text, catalog_list)
    except Exception as e:
        print(f"Fallback extract ({e})")
        extracted = fallback_extract(raw_text, catalog_list)

    # Đếm thứ tự hồ sơ thật
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM opportunities")
    seq_num = cur.fetchone()[0] + 1
    conn.close()

    deal_id = f"OPP-{datetime.now().strftime('%m%d')}-{seq_num:03d}"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    valid_items = [i for i in extracted.get("items", []) if i.get("quantity") and int(i.get("quantity", 0)) > 0]

    # 1. Nếu thiếu thông tin sản phẩm hoặc số lượng
    if not extracted.get("is_sufficient_info", True) or not valid_items:
        missing_list = extracted.get("missing_fields") or ["Tên sản phẩm/thiết bị cụ thể và số lượng yêu cầu"]
        missing_str = "\n".join([f"  • {m}" for m in missing_list])
        draft_email = f"""Kính gửi: {extracted.get('customer_name') or 'Quý khách hàng'} {('- ' + extracted.get('company_name')) if extracted.get('company_name') else ''},

Công ty Minh An đã tiếp nhận yêu cầu của Quý khách.
Tuy nhiên, để lập báo giá chính xác và kiểm tra tồn kho khả dụng, kính mong Quý khách bổ sung các thông tin còn thiếu sau:
{missing_str}

Người phụ trách hồ sơ: {assigned_rep['name']} {('(' + assigned_rep['email'] + ')') if assigned_rep.get('email') else ''}
Trân trọng!"""

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            deal_id, created_at,
            extracted.get("customer_name") or "Chưa rõ tên KH",
            extracted.get("company_name") or "Chưa rõ tên Công ty",
            extracted.get("contact_info") or "Chưa có liên hệ",
            raw_text, json.dumps(extracted, ensure_ascii=False),
            0.0, 0.0, 0, "NEEDS_CLARIFICATION",
            "Thiếu thông tin: " + ", ".join(missing_list),
            assigned_rep["name"], draft_email
        ))
        conn.commit()
        conn.close()
        return {"deal_id": deal_id, "status": "NEEDS_CLARIFICATION"}

    # 2. Nếu đủ thông tin -> Đối chiếu với Bảng giá & Tồn kho do người dùng đã nhập
    proposal_items = []
    total_list_price = 0.0
    max_lead_time_days = policies["default_in_stock_lead_days"]
    stock_notes = []
    unmatched_items = []

    catalog_by_sku = {c["sku"].upper(): c for c in catalog_list}

    for it in valid_items:
        qty = int(it.get("quantity", 0))
        matched_cat = None
        if it.get("matched_sku") and it["matched_sku"].upper() in catalog_by_sku:
            matched_cat = catalog_by_sku[it["matched_sku"].upper()]
        else:
            matched_cat = match_user_catalog(it.get("product_keyword", ""), catalog_list)

        if matched_cat:
            unit_price = float(matched_cat["unit_price"])
            subtotal = unit_price * qty
            total_list_price += subtotal
            avail = int(matched_cat["available_stock"])

            if avail >= qty:
                stock_status = f"Đủ tồn kho (Tồn: {avail}, Cần: {qty})"
            else:
                deficit = qty - avail
                lead_out = int(matched_cat["lead_time_out_of_stock"])
                if lead_out > max_lead_time_days:
                    max_lead_time_days = lead_out
                stock_status = f"Thiếu {deficit} (Tồn: {avail}, Cần nhập thêm {lead_out} ngày)"
                stock_notes.append(f"{matched_cat['name']}: thiếu {deficit} (cần {lead_out} ngày)")

            proposal_items.append({
                "sku": matched_cat["sku"],
                "name": matched_cat["name"],
                "qty": qty,
                "unit_price": unit_price,
                "subtotal": subtotal,
                "stock_status": stock_status
            })
        else:
            unmatched_items.append(f"{it.get('product_keyword')} (SL: {qty})")
            proposal_items.append({
                "sku": "CHƯA CÓ TRONG BẢNG GIÁ",
                "name": it.get("product_keyword"),
                "qty": qty,
                "unit_price": 0.0,
                "subtotal": 0.0,
                "stock_status": "Sản phẩm chưa được nhập trong Bảng giá & Kho"
            })

    # 3. Tính chiết khấu & Kiểm tra Chính sách Phân quyền do người dùng thiết lập
    discount_pct = float(extracted.get("discount_requested_pct") or 0.0)
    discount_val = total_list_price * (discount_pct / 100.0)
    final_amount = total_list_price - discount_val

    requires_approval = False
    escalation_reasons = []

    if unmatched_items:
        requires_approval = True
        escalation_reasons.append("Có hạng mục chưa có trong Bảng giá: " + ", ".join(unmatched_items))

    if stock_notes:
        requires_approval = True
        escalation_reasons.append("Tồn kho không đủ: " + "; ".join(stock_notes))

    if policies["max_rep_discount_pct"] > 0 and discount_pct > policies["max_rep_discount_pct"]:
        requires_approval = True
        escalation_reasons.append(f"Chiết khấu đề nghị {discount_pct}% vượt trần NVKD ({policies['max_rep_discount_pct']}%)")
    elif policies["max_rep_discount_pct"] == 0 and discount_pct > 0:
        requires_approval = True
        escalation_reasons.append(f"Khách yêu cầu chiết khấu {discount_pct}% (Chính sách hiện tại yêu cầu duyệt mọi chiết khấu > 0%)")

    if policies["max_rep_deal_value"] > 0 and final_amount > policies["max_rep_deal_value"]:
        requires_approval = True
        escalation_reasons.append(f"Tổng giá trị {final_amount:,.0f} ₫ vượt hạn mức NVKD ({policies['max_rep_deal_value']:,.0f} ₫)")

    desired_days = extracted.get("desired_deadline_days")
    if desired_days is not None and int(desired_days) < max_lead_time_days:
        requires_approval = True
        escalation_reasons.append(f"Khách yêu cầu giao trong {desired_days} ngày, nhưng thời gian đáp ứng thực tế là {max_lead_time_days} ngày")

    status = "WAITING_APPROVAL" if requires_approval else "READY_TO_SEND"

    # 4. Soạn thảo bản nháp phản hồi dựa trên đúng số liệu thật
    table_lines = "\n".join([
        f"  • [{x['sku']}] {x['name']} | SL: {x['qty']} | Đơn giá: {x['unit_price']:,.0f} ₫ | Thành tiền: {x['subtotal']:,.0f} ₫ ({x['stock_status']})"
        for x in proposal_items
    ])

    draft_email = f"""Kính gửi: {extracted.get('customer_name') or 'Quý khách hàng'} {('- ' + extracted.get('company_name')) if extracted.get('company_name') else ''},

Công ty Minh An xin gửi Bảng dự thảo báo giá cho Hồ sơ cơ hội [{deal_id}]:

1. CHI TIẾT HẠNG MỤC & GIÁ TRỊ:
{table_lines}

- Tổng tiền theo bảng giá: {total_list_price:,.0f} VNĐ
- Mức chiết khấu ({discount_pct}%): -{discount_val:,.0f} VNĐ
=> TỔNG GIÁ TRỊ ĐỀ XUẤT: {final_amount:,.0f} VNĐ

2. THỜI GIAN ĐÁP ỨNG & TỒN KHO:
- Thời gian triển khai/giao hàng dự kiến: {max_lead_time_days} ngày làm việc.
{"- Lưu ý tồn kho: " + "; ".join(stock_notes) if stock_notes else "- Tình trạng kho: Đáp ứng đủ theo bảng kiểm tra."}

Nhân viên phụ trách: {assigned_rep['name']} {('(' + assigned_rep['email'] + ')') if assigned_rep.get('email') else ''}
Trân trọng!"""

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        deal_id, created_at,
        extracted.get("customer_name") or "Khách hàng",
        extracted.get("company_name") or "Khách hàng Doanh nghiệp",
        extracted.get("contact_info") or "Chưa có SĐT/Email",
        raw_text, json.dumps(extracted, ensure_ascii=False),
        final_amount, discount_pct, max_lead_time_days,
        status, "; ".join(escalation_reasons),
        assigned_rep["name"], draft_email
    ))
    conn.commit()
    conn.close()

    return {"deal_id": deal_id, "status": status}


# ==========================================
# 4. GIAO DIỆN EXTEJ DASHBOARD (SỐ LIỆU 100% DO NGƯỜI DÙNG NHẬP)
# ==========================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MinhAn B2B — Hệ Thống AI Agent (Dữ Liệu Thực Tế)</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-body: #F5F7FB;
            --bg-surface: #FFFFFF;
            --bg-subtle: #F8FAFC;
            --text-main: #181D31;
            --text-secondary: #6E7A91;
            --text-muted: #9BA5B7;
            --border-light: #EEF1F6;
            --border-dashed: #DCE2EC;
            --orange-primary: #F7931A;
            --orange-light: #FFF4E5;
            --orange-gradient: linear-gradient(135deg, #FF9924 0%, #F27816 100%);
            --orange-shadow: 0 8px 20px -4px rgba(247, 147, 26, 0.42);
            --coral-primary: #F26464;
            --coral-gradient: linear-gradient(135deg, #FF7676 0%, #EC5252 100%);
            --green-pos: #12B886;
            --green-bg: #E6FCF5;
            --card-shadow: 0 10px 30px -5px rgba(24, 29, 49, 0.04), 0 2px 6px -1px rgba(24, 29, 49, 0.02);
            --card-hover-shadow: 0 18px 35px -5px rgba(24, 29, 49, 0.08);
            --radius-xl: 20px;
            --radius-lg: 16px;
            --radius-md: 12px;
            --transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }

        body.dark-mode {
            --bg-body: #0F1322;
            --bg-surface: #171C30;
            --bg-subtle: #1E253D;
            --text-main: #F3F5F9;
            --text-secondary: #94A3B8;
            --text-muted: #64748B;
            --border-light: #262F4D;
            --border-dashed: #334155;
            --card-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.35);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background-color: var(--bg-body); color: var(--text-main); display: flex; min-height: 100vh; overflow-x: hidden; transition: var(--transition); }

        /* SIDEBAR */
        .sidebar {
            width: 270px;
            background: var(--bg-surface);
            border-right: 1px solid var(--border-light);
            display: flex;
            flex-direction: column;
            padding: 24px 18px;
            position: fixed;
            top: 0; bottom: 0; left: 0;
            z-index: 100;
        }
        .brand-box { display: flex; align-items: center; justify-content: space-between; padding: 0 8px 24px 8px; }
        .brand-logo { display: flex; align-items: center; gap: 12px; text-decoration: none; }
        .logo-hex {
            width: 40px; height: 40px;
            background: linear-gradient(135deg, #FF4B4B 0%, #F7931A 100%);
            border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 6px 16px rgba(255, 75, 75, 0.3);
        }
        .brand-name { font-size: 22px; font-weight: 800; color: var(--text-main); letter-spacing: -0.5px; }

        .nav-group-label {
            font-size: 11px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.8px; color: var(--text-muted);
            padding: 14px 12px 8px 12px;
        }
        .nav-list { list-style: none; display: flex; flex-direction: column; gap: 4px; }
        .nav-item {
            display: flex; align-items: center; justify-content: space-between;
            padding: 11px 14px; border-radius: 12px;
            color: var(--text-secondary); font-size: 13.5px; font-weight: 600;
            cursor: pointer; transition: var(--transition);
        }
        .nav-item-left { display: flex; align-items: center; gap: 11px; }
        .nav-item-right { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text-muted); }
        .nav-item:hover:not(.active) { background: var(--bg-subtle); color: var(--text-main); transform: translateX(3px); }
        .nav-item.active { background: var(--orange-gradient); color: #FFFFFF; box-shadow: var(--orange-shadow); }
        .nav-item.active .nav-item-right { color: #FFFFFF; }

        .sidebar-footer { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border-light); }

        /* MAIN CONTENT */
        .main-wrapper { margin-left: 270px; flex: 1; display: flex; flex-direction: column; min-width: 0; }
        .topbar {
            height: 74px; background: var(--bg-surface);
            border-bottom: 1px solid var(--border-light);
            padding: 0 32px; display: flex; align-items: center; justify-content: space-between;
            position: sticky; top: 0; z-index: 90;
        }
        .search-box { position: relative; width: 400px; }
        .search-box input {
            width: 100%; background: var(--bg-body); border: 1px solid transparent;
            padding: 10px 16px 10px 38px; border-radius: 12px; font-size: 13.5px; color: var(--text-main); outline: none;
        }
        .search-box input:focus { background: var(--bg-surface); border-color: var(--orange-primary); }
        .search-icon { position: absolute; left: 13px; top: 50%; transform: translateY(-50%); color: var(--text-muted); }

        .topbar-actions { display: flex; align-items: center; gap: 16px; }
        .theme-switch {
            width: 48px; height: 26px; background: var(--bg-body); border: 1px solid var(--border-light);
            border-radius: 99px; padding: 3px; cursor: pointer; display: flex; align-items: center;
        }
        .theme-switch-thumb { width: 18px; height: 18px; border-radius: 50%; background: var(--orange-gradient); transition: var(--transition); }
        body.dark-mode .theme-switch-thumb { transform: translateX(22px); }

        .content-area { padding: 26px 32px 48px 32px; max-width: 1500px; width: 100%; margin: 0 auto; }
        .page-title-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 22px; }
        .page-title { font-size: 25px; font-weight: 800; letter-spacing: -0.5px; }

        /* ROW 1: EXTEJ TOP CARDS */
        .top-cards-row {
            display: grid;
            grid-template-columns: 1.25fr 68px 1.1fr 1.1fr;
            gap: 20px;
            margin-bottom: 24px;
        }
        .extej-card {
            background: var(--bg-surface);
            border-radius: var(--radius-xl);
            padding: 22px 24px;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--border-light);
            display: flex; flex-direction: column; justify-content: space-between;
            transition: var(--transition);
        }
        .extej-card:hover { transform: translateY(-2px); box-shadow: var(--card-hover-shadow); }
        .card-top-meta { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .card-label-group { display: flex; align-items: center; gap: 8px; font-size: 14.5px; font-weight: 700; color: var(--text-secondary); }
        .currency-badge { font-size: 11.5px; font-weight: 700; color: var(--text-secondary); background: var(--bg-subtle); padding: 4px 10px; border-radius: 8px; }
        .big-balance-val { font-size: 30px; font-weight: 800; color: var(--text-main); margin: 4px 0 16px 0; }

        .segmented-bar { height: 6px; width: 100%; border-radius: 99px; background: var(--bg-subtle); display: flex; overflow: hidden; gap: 2px; margin-bottom: 12px; }
        .seg-piece { height: 100%; transition: width 0.4s ease; }
        .legend-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
        .legend-item { display: flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 600; color: var(--text-secondary); }
        .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

        .profit-footer { display: flex; align-items: flex-end; justify-content: space-between; padding-top: 8px; border-top: 1px dashed var(--border-light); }
        .profit-label { font-size: 11.5px; color: var(--text-muted); }
        .profit-value { font-size: 14px; font-weight: 700; color: var(--green-pos); }

        .dashed-add-card {
            border: 1.5px dashed var(--border-dashed);
            border-radius: var(--radius-xl);
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            cursor: pointer; color: var(--text-muted); font-size: 11.5px; font-weight: 700;
            transition: var(--transition);
        }
        .dashed-add-card:hover { border-color: var(--orange-primary); color: var(--orange-primary); background: var(--orange-light); }

        .pill-btn-group { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
        .pill-btn {
            border: none; padding: 9px 12px; border-radius: 10px;
            font-size: 12.5px; font-weight: 700; color: #FFFFFF;
            cursor: pointer; transition: var(--transition); text-align: center;
        }
        .pill-btn.orange { background: var(--orange-gradient); box-shadow: 0 4px 12px rgba(247, 147, 26, 0.25); }
        .pill-btn.coral { background: var(--coral-gradient); box-shadow: 0 4px 12px rgba(242, 100, 100, 0.25); }
        .pill-btn.subtle { background: var(--bg-subtle); color: var(--text-main); border: 1px solid var(--border-light); }
        .pill-btn:hover { filter: brightness(1.05); transform: translateY(-1px); }

        /* ROW 2: DATA INPUT WORKSPACE & CHART */
        .middle-grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 22px; margin-bottom: 24px; }
        .section-card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .section-card-title { font-size: 16.5px; font-weight: 800; color: var(--text-main); }

        .timeframe-tabs { display: flex; gap: 4px; background: var(--bg-subtle); padding: 4px; border-radius: 10px; }
        .tf-tab {
            border: none; background: transparent; color: var(--text-secondary);
            font-size: 12px; font-weight: 700; padding: 6px 12px; border-radius: 8px; cursor: pointer;
        }
        .tf-tab.active { background: var(--orange-gradient); color: #FFFFFF; }

        .ai-composer-textarea {
            width: 100%; height: 125px; background: var(--bg-subtle);
            border: 1.5px solid var(--border-light); border-radius: var(--radius-md);
            padding: 12px 14px; font-size: 13.5px; color: var(--text-main); outline: none; resize: vertical;
        }
        .ai-composer-textarea:focus { background: var(--bg-surface); border-color: var(--orange-primary); }

        .form-input {
            width: 100%; background: var(--bg-subtle); border: 1.5px solid var(--border-light);
            border-radius: 10px; padding: 9px 12px; font-size: 13px; color: var(--text-main); outline: none;
        }
        .form-input:focus { background: var(--bg-surface); border-color: var(--orange-primary); }

        /* ROW 3: MY CARDS OPPORTUNITY LIST */
        .my-cards-section {
            background: var(--bg-surface); border-radius: var(--radius-xl);
            padding: 22px 24px; box-shadow: var(--card-shadow); border: 1px solid var(--border-light);
        }
        .opp-cards-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 18px; margin-top: 14px;
        }
        .opp-credit-card {
            background: var(--bg-surface); border: 1.5px solid var(--border-light);
            border-radius: var(--radius-lg); padding: 20px; position: relative;
            transition: var(--transition); cursor: pointer; display: flex; flex-direction: column; justify-content: space-between; min-height: 200px;
        }
        .opp-credit-card:hover { transform: translateY(-3px); border-color: var(--orange-primary); box-shadow: var(--card-hover-shadow); }
        .occ-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .double-circle-logo { display: flex; align-items: center; }
        .dc-left { width: 22px; height: 22px; border-radius: 50%; background: #EB001B; }
        .dc-right { width: 22px; height: 22px; border-radius: 50%; background: #F79E1B; margin-left: -9px; opacity: 0.9; }

        .occ-check-badge { padding: 4px 10px; border-radius: 99px; font-size: 11px; font-weight: 700; }
        .occ-check-badge.waiting { background: #FFF4E5; color: #D96B00; }
        .occ-check-badge.ready { background: #E8F0FE; color: #1D4ED8; }
        .occ-check-badge.sent { background: #E6FCF5; color: #0CA678; }
        .occ-check-badge.clarify { background: #FFE8E8; color: #E03131; }

        .occ-number { font-size: 17px; font-weight: 800; margin-bottom: 4px; }
        .occ-company { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px; }
        .occ-bottom-meta { display: grid; grid-template-columns: 1.2fr 0.8fr 1fr; gap: 8px; padding-top: 10px; border-top: 1px solid var(--border-light); }
        .occ-meta-label { font-size: 10.5px; color: var(--text-muted); }
        .occ-meta-val { font-size: 12px; font-weight: 700; }

        /* MODAL */
        .modal-backdrop {
            position: fixed; inset: 0; background: rgba(15, 19, 34, 0.55); backdrop-filter: blur(4px);
            display: none; align-items: center; justify-content: center; z-index: 999; padding: 20px;
        }
        .modal-backdrop.open { display: flex; }
        .modal-dialog {
            background: var(--bg-surface); border-radius: var(--radius-xl);
            max-width: 780px; width: 100%; max-height: 90vh; overflow-y: auto;
            padding: 26px; box-shadow: 0 24px 48px rgba(0,0,0,0.22); border: 1px solid var(--border-light);
        }

        .data-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
        .data-table th, .data-table td { padding: 10px 12px; border-bottom: 1px solid var(--border-light); text-align: left; }
        .data-table th { color: var(--text-muted); font-size: 11.5px; text-transform: uppercase; }

        .toast-box {
            position: fixed; bottom: 24px; right: 24px; background: #181D31; color: #FFF;
            padding: 12px 18px; border-radius: 12px; font-size: 13px; font-weight: 600;
            transform: translateY(100px); opacity: 0; transition: var(--transition); z-index: 1200;
        }
        .toast-box.show { transform: translateY(0); opacity: 1; }
    </style>
</head>
<body>

    <!-- SIDEBAR -->
    <aside class="sidebar">
        <div class="brand-box">
            <a href="#" class="brand-logo" onclick="setActiveNav(document.querySelector('#mainNav .nav-item'), 'ALL')">
                <div class="logo-hex">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.3"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path></svg>
                </div>
                <span class="brand-name">MinhAn</span>
            </a>
        </div>

        <div class="nav-group-label">QUẢN LÝ HỒ SƠ CƠ HỘI</div>
        <ul class="nav-list" id="mainNav">
            <li class="nav-item active" onclick="setActiveNav(this, 'ALL')">
                <div class="nav-item-left"><span>📂 Tất cả Hồ sơ Cơ hội</span></div>
                <div class="nav-item-right"><span id="navCountAll">0</span> <span>›</span></div>
            </li>
            <li class="nav-item" onclick="setActiveNav(this, 'WAITING_APPROVAL')">
                <div class="nav-item-left"><span>🛡️ Chờ Trưởng phòng duyệt</span></div>
                <div class="nav-item-right"><span id="navCountApprove">0</span> <span>›</span></div>
            </li>
            <li class="nav-item" onclick="setActiveNav(this, 'READY_TO_SEND')">
                <div class="nav-item-left"><span>✅ Sẵn sàng gửi báo giá</span></div>
                <div class="nav-item-right"><span id="navCountReady">0</span> <span>›</span></div>
            </li>
            <li class="nav-item" onclick="setActiveNav(this, 'NEEDS_CLARIFICATION')">
                <div class="nav-item-left"><span>❓ Cần hỏi bổ sung KH</span></div>
                <div class="nav-item-right"><span id="navCountClarify">0</span> <span>›</span></div>
            </li>
            <li class="nav-item" onclick="setActiveNav(this, 'SENT')">
                <div class="nav-item-left"><span>📤 Đã gửi & Theo dõi</span></div>
                <div class="nav-item-right"><span id="navCountSent">0</span> <span>›</span></div>
            </li>
        </ul>

        <div class="nav-group-label" style="margin-top:10px;">TỰ NHẬP DỮ LIỆU GỐC (CẤU HÌNH)</div>
        <ul class="nav-list">
            <li class="nav-item" onclick="openModal('catalogModal')">
                <div class="nav-item-left"><span>📦 1. Nhập Bảng giá & Tồn kho</span></div>
                <div class="nav-item-right"><span id="navCountSku">0 SKU</span> <span>›</span></div>
            </li>
            <li class="nav-item" onclick="openModal('policyModal')">
                <div class="nav-item-left"><span>⚖️ 2. Nhập Chính sách Duyệt</span></div>
                <div class="nav-item-right"><span>›</span></div>
            </li>
            <li class="nav-item" onclick="openModal('repsModal')">
                <div class="nav-item-left"><span>👥 3. Nhập Danh sách NVKD</span></div>
                <div class="nav-item-right"><span id="navCountReps">0 NV</span> <span>›</span></div>
            </li>
        </ul>

        <div class="sidebar-footer">
            <button class="pill-btn coral" style="width:100%;" onclick="resetAllData()">🗑️ Xóa Trắng Toàn Bộ Dữ Liệu</button>
        </div>
    </aside>

    <!-- MAIN WORKSPACE -->
    <div class="main-wrapper">
        <header class="topbar">
            <div class="search-box">
                <span class="search-icon">🔍</span>
                <input type="text" id="searchInput" placeholder="Tìm kiếm trong các hồ sơ bạn đã nhập..." oninput="renderAll()">
            </div>

            <div class="topbar-actions">
                <button class="pill-btn subtle" onclick="openModal('catalogModal')">+ Nhập Bảng Giá & Kho</button>
                <button class="pill-btn subtle" onclick="openModal('policyModal')">+ Thiết Lập Phân Quyền</button>
                <button class="pill-btn subtle" onclick="openModal('repsModal')">+ Nhập NVKD</button>
                <div class="theme-switch" onclick="document.body.classList.toggle('dark-mode')" title="Đổi giao diện Sáng/Tối">
                    <div class="theme-switch-thumb"></div>
                </div>
            </div>
        </header>

        <main class="content-area">
            <div class="page-title-bar">
                <h1 class="page-title">Workspace Xử Lý Cơ Hội B2B (Dữ liệu thực do bạn nhập)</h1>
                <span class="currency-badge" style="color:var(--green-pos);">● Không dùng số liệu giả định</span>
            </div>

            <!-- ROW 1: TOP SUMMARY CARDS (BẮT ĐẦU TỪ 0) -->
            <section class="top-cards-row">
                <!-- CARD 1: BALANCE -->
                <div class="extej-card">
                    <div>
                        <div class="card-top-meta">
                            <div class="card-label-group"><span>💼 Tổng Giá Trị Cơ Hội Đã Nhập</span></div>
                            <span class="currency-badge">VNĐ</span>
                        </div>
                        <div class="big-balance-val" id="totalPipelineValue">0 ₫</div>

                        <div class="segmented-bar">
                            <div class="seg-piece" id="segApprove" style="width: 0%; background: #F7931A;"></div>
                            <div class="seg-piece" id="segReady" style="width: 0%; background: #627EEA;"></div>
                            <div class="seg-piece" id="segSent" style="width: 0%; background: #26A17B;"></div>
                            <div class="seg-piece" id="segClarify" style="width: 0%; background: #EC4899;"></div>
                        </div>

                        <div class="legend-row">
                            <div class="legend-item"><span class="legend-dot" style="background:#F7931A"></span>Chờ duyệt (<span id="lgApprove">0</span>)</div>
                            <div class="legend-item"><span class="legend-dot" style="background:#627EEA"></span>Sẵn sàng (<span id="lgReady">0</span>)</div>
                            <div class="legend-item"><span class="legend-dot" style="background:#26A17B"></span>Đã gửi (<span id="lgSent">0</span>)</div>
                            <div class="legend-item"><span class="legend-dot" style="background:#EC4899"></span>Làm rõ (<span id="lgClarify">0</span>)</div>
                        </div>
                    </div>

                    <div class="profit-footer">
                        <div>
                            <div class="profit-label">Tổng số hồ sơ bạn đã tạo</div>
                            <div class="profit-value" id="totalDealsText">0 hồ sơ</div>
                        </div>
                        <div style="font-size:12px; color:var(--text-muted);" id="catalogStatusText">Kho: 0 mã sản phẩm</div>
                    </div>
                </div>

                <!-- DASHED + ADD -->
                <div class="dashed-add-card" onclick="document.getElementById('rawInput').focus()">
                    <span style="font-size:20px;">+</span>
                    <span>NHẬP MỚI</span>
                </div>

                <!-- CARD 2: LATEST DEAL 1 -->
                <div class="extej-card" id="featuredSlot1">
                    <!-- Rendered dynamically from user data (or empty state) -->
                </div>

                <!-- CARD 3: LATEST DEAL 2 -->
                <div class="extej-card" id="featuredSlot2">
                    <!-- Rendered dynamically from user data (or empty state) -->
                </div>
            </section>

            <!-- ROW 2: DIRECT INPUT STUDIO & REAL-TIME CHART -->
            <section class="middle-grid">
                <!-- LEFT: TRẠM NHẬP YÊU CẦU KHÁCH HÀNG -->
                <div class="extej-card">
                    <div>
                        <div class="section-card-header">
                            <div class="section-card-title">🤖 Nhập Yêu Cầu Khách Hàng Để AI Xử Lý</div>
                            <span style="font-size:12px; color:var(--text-muted);">Đối chiếu 100% với Bảng giá & Kho bạn nhập</span>
                        </div>

                        <textarea id="rawInput" class="ai-composer-textarea" placeholder="Nhập trực tiếp nội dung email/tin nhắn của khách hàng vào đây (Ví dụ: Công ty X cần mua 10 máy ..., yêu cầu chiết khấu ...%, giao trong ... ngày)..."></textarea>
                    </div>

                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:14px; gap:10px; flex-wrap:wrap;">
                        <div style="display:flex; gap:8px;">
                            <button class="pill-btn subtle" onclick="openModal('catalogModal')">📦 Bảng giá (<span id="btnSkuCount">0</span>)</button>
                            <button class="pill-btn subtle" onclick="openModal('policyModal')">⚖️ Chính sách duyệt</button>
                            <button class="pill-btn subtle" onclick="openModal('repsModal')">👥 NVKD (<span id="btnRepCount">0</span>)</button>
                        </div>
                        <button class="pill-btn orange" id="btnRunAgent" style="padding:11px 22px; font-size:13.5px;" onclick="submitRequest()">
                            🚀 Chạy AI Phân Tích Yêu Cầu Này
                        </button>
                    </div>
                </div>

                <!-- RIGHT: BIỂU ĐỒ GIÁ TRỊ THỰC TẾ TỪ CÁC HỒ SƠ ĐÃ NHẬP -->
                <div class="extej-card">
                    <div>
                        <div class="section-card-header">
                            <div class="section-card-title">Biểu đồ Giá trị theo Hồ sơ bạn đã nhập</div>
                            <span class="currency-badge" id="chartCountBadge">0 hồ sơ</span>
                        </div>

                        <div style="position:relative; height:195px; width:100%; display:flex; align-items:center; justify-content:center;" id="chartContainer">
                            <!-- Sẽ vẽ đường biểu đồ thật khi có dữ liệu, hoặc hiện trạng thái 0 khi chưa nhập -->
                        </div>
                    </div>
                </div>
            </section>

            <!-- ROW 3: DANH SÁCH HỒ SƠ CƠ HỘI -->
            <section class="my-cards-section">
                <div class="section-card-header">
                    <div class="section-card-title" id="galleryTitle">Danh sách Hồ sơ Cơ hội của bạn (0)</div>
                </div>

                <div id="oppCardsGrid" class="opp-cards-grid">
                    <!-- Sẽ hiển thị các thẻ cơ hội do người dùng tạo -->
                </div>
            </section>
        </main>
    </div>

    <!-- ==========================================
         MODAL 1: TỰ NHẬP BẢNG GIÁ & TỒN KHO
         ========================================== -->
    <div class="modal-backdrop" id="catalogModal" onclick="if(event.target===this) closeModal('catalogModal')">
        <div class="modal-dialog">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                <h3 style="font-size:18px; font-weight:800;">📦 Tự Nhập Danh Mục Sản Phẩm, Bảng Giá & Tồn Kho</h3>
                <button class="pill-btn subtle" onclick="closeModal('catalogModal')">Đóng ✕</button>
            </div>
            <p style="font-size:12.5px; color:var(--text-secondary); margin-bottom:14px;">
                Nhập các sản phẩm thực tế của bạn. Khi khách hàng gửi yêu cầu, AI sẽ tra cứu đúng đơn giá và tồn kho bạn khai báo tại đây:
            </p>

            <div style="display:grid; grid-template-columns: 1fr 1.6fr 1.2fr; gap:10px; margin-bottom:10px;">
                <input type="text" id="catSku" class="form-input" placeholder="Mã SKU (VD: DELL-5440)">
                <input type="text" id="catName" class="form-input" placeholder="Tên sản phẩm (VD: Laptop Dell Latitude 5440)">
                <input type="text" id="catKeywords" class="form-input" placeholder="Từ khóa nhận diện (VD: dell, latitude)">
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr 1fr auto; gap:10px; margin-bottom:16px;">
                <input type="number" id="catPrice" class="form-input" placeholder="Đơn giá VNĐ (VD: 19500000)">
                <input type="number" id="catStock" class="form-input" placeholder="Tồn kho khả dụng (VD: 25)">
                <input type="number" id="catLead" class="form-input" placeholder="Số ngày nhập nếu hết hàng (VD: 14)">
                <button class="pill-btn orange" onclick="addCatalogItem()">+ Lưu Sản Phẩm</button>
            </div>

            <table class="data-table">
                <thead>
                    <tr>
                        <th>Mã SKU</th>
                        <th>Tên sản phẩm</th>
                        <th>Từ khóa</th>
                        <th>Đơn giá</th>
                        <th>Tồn kho</th>
                        <th>Ngày nhập thêm</th>
                        <th>Xóa</th>
                    </tr>
                </thead>
                <tbody id="catalogTableBody"></tbody>
            </table>
        </div>
    </div>

    <!-- ==========================================
         MODAL 2: TỰ THIẾT LẬP CHÍNH SÁCH CHIẾT KHẤU & PHÂN QUYỀN
         ========================================== -->
    <div class="modal-backdrop" id="policyModal" onclick="if(event.target===this) closeModal('policyModal')">
        <div class="modal-dialog" style="max-width:560px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                <h3 style="font-size:18px; font-weight:800;">⚖️ Tự Thiết Lập Chính Sách Chiết Khấu & Phân Quyền</h3>
                <button class="pill-btn subtle" onclick="closeModal('policyModal')">Đóng ✕</button>
            </div>
            <p style="font-size:12.5px; color:var(--text-secondary); margin-bottom:16px;">
                Nếu yêu cầu của khách vượt quá các ngưỡng bạn nhập dưới đây, hệ thống sẽ tự động chuyển sang trạng thái <strong>Chờ Trưởng phòng duyệt</strong>:
            </p>

            <div style="display:flex; flex-direction:column; gap:12px;">
                <div>
                    <label style="font-size:12px; font-weight:700; display:block; margin-bottom:5px;">1. Trần chiết khấu tối đa NVKD được tự quyết (%):</label>
                    <input type="number" step="0.1" id="polMaxDiscount" class="form-input" placeholder="VD: 5">
                </div>
                <div>
                    <label style="font-size:12px; font-weight:700; display:block; margin-bottom:5px;">2. Hạn mức tổng giá trị đơn hàng tối đa NVKD tự quyết (VNĐ):</label>
                    <input type="number" id="polMaxValue" class="form-input" placeholder="VD: 150000000">
                </div>
                <div>
                    <label style="font-size:12px; font-weight:700; display:block; margin-bottom:5px;">3. Thời gian giao hàng tiêu chuẩn khi sẵn kho (Số ngày):</label>
                    <input type="number" id="polDefaultLead" class="form-input" placeholder="VD: 2">
                </div>
                <button class="pill-btn orange" style="margin-top:8px; padding:12px;" onclick="savePolicies()">💾 Lưu Chính Sách Phân Quyền</button>
            </div>
        </div>
    </div>

    <!-- ==========================================
         MODAL 3: TỰ NHẬP DANH SÁCH NHÂN VIÊN KINH DOANH
         ========================================== -->
    <div class="modal-backdrop" id="repsModal" onclick="if(event.target===this) closeModal('repsModal')">
        <div class="modal-dialog" style="max-width:640px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                <h3 style="font-size:18px; font-weight:800;">👥 Tự Nhập Danh Sách Nhân Viên Kinh Doanh (NVKD)</h3>
                <button class="pill-btn subtle" onclick="closeModal('repsModal')">Đóng ✕</button>
            </div>
            <p style="font-size:12.5px; color:var(--text-secondary); margin-bottom:14px;">
                Hệ thống sẽ tự động chia đều (Round-Robin) các yêu cầu mới cho danh sách nhân viên bạn nhập tại đây:
            </p>

            <div style="display:grid; grid-template-columns: 1.2fr 1.2fr 1fr auto; gap:10px; margin-bottom:16px;">
                <input type="text" id="repName" class="form-input" placeholder="Họ tên NVKD">
                <input type="text" id="repEmail" class="form-input" placeholder="Email / SĐT">
                <input type="text" id="repRole" class="form-input" placeholder="Chức danh (VD: NVKD B2B)">
                <button class="pill-btn orange" onclick="addSalesRep()">+ Thêm NVKD</button>
            </div>

            <table class="data-table">
                <thead>
                    <tr>
                        <th>Họ và tên</th>
                        <th>Email / Liên hệ</th>
                        <th>Chức danh</th>
                        <th>Xóa</th>
                    </tr>
                </thead>
                <tbody id="repsTableBody"></tbody>
            </table>
        </div>
    </div>

    <!-- ==========================================
         MODAL 4: CHI TIẾT HỒ SƠ & PHÊ DUYỆT
         ========================================== -->
    <div class="modal-backdrop" id="dealModal" onclick="if(event.target===this) closeModal('dealModal')">
        <div class="modal-dialog">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                <div>
                    <span class="currency-badge" id="modalDealId" style="color:var(--orange-primary);font-weight:800;"></span>
                    <h3 style="font-size:18px; font-weight:800; margin-top:6px;" id="modalCompanyName"></h3>
                </div>
                <button class="pill-btn subtle" onclick="closeModal('dealModal')">Đóng ✕</button>
            </div>

            <div id="modalEscalationBox" style="display:none; background:#FFF4E5; border:1px solid #FFD099; padding:12px 16px; border-radius:12px; margin-bottom:14px; font-size:13px; color:#B45309;">
                <strong>⚠️ Lý do cần kiểm tra / phê duyệt:</strong>
                <div id="modalEscalationText" style="margin-top:4px;"></div>
            </div>

            <div style="margin-bottom:14px;">
                <label style="font-size:12px; font-weight:700; color:var(--text-secondary); display:block; margin-bottom:6px;">
                    Bản nháp Phản hồi / Báo giá (Có thể chỉnh sửa trực tiếp):
                </label>
                <textarea id="modalDraftEmail" class="ai-composer-textarea" style="height:210px; font-family:monospace; font-size:12.5px;"></textarea>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                <span style="font-size:12.5px; color:var(--text-secondary);" id="modalRepInfo"></span>
                <div style="display:flex; gap:8px;" id="modalActionButtons"></div>
            </div>
        </div>
    </div>

    <div class="toast-box" id="toastBox"><span id="toastText"></span></div>

<script>
let STATE = {
    opportunities: [],
    catalog: [],
    reps: [],
    policies: { max_rep_discount_pct: 0, max_rep_deal_value: 0, default_in_stock_lead_days: 0 }
};
let CURRENT_FILTER = 'ALL';

function showToast(msg) {
    const box = document.getElementById("toastBox");
    document.getElementById("toastText").innerText = msg;
    box.classList.add("show");
    setTimeout(() => box.classList.remove("show"), 3000);
}

function openModal(id) { document.getElementById(id).classList.add("open"); }
function closeModal(id) { document.getElementById(id).classList.remove("open"); }

function setActiveNav(el, filterStatus) {
    document.querySelectorAll("#mainNav .nav-item").forEach(item => item.classList.remove("active"));
    if (el) el.classList.add("active");
    CURRENT_FILTER = filterStatus;
    renderAll();
}

async function loadStateFromServer() {
    const res = await fetch("/api/state");
    STATE = await res.json();
    renderAll();
    renderConfigTables();
}

function renderConfigTables() {
    // 1. Bảng giá & Kho
    document.getElementById("navCountSku").innerText = STATE.catalog.length + " SKU";
    document.getElementById("btnSkuCount").innerText = STATE.catalog.length;
    document.getElementById("catalogStatusText").innerText = `Kho: ${STATE.catalog.length} mã sản phẩm`;

    const catBody = document.getElementById("catalogTableBody");
    if (STATE.catalog.length === 0) {
        catBody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:18px;">Chưa có sản phẩm nào. Hãy nhập sản phẩm đầu tiên của bạn ở trên.</td></tr>`;
    } else {
        catBody.innerHTML = STATE.catalog.map(c => `
            <tr>
                <td><strong>${c.sku}</strong></td>
                <td>${c.name}</td>
                <td>${c.keywords || ''}</td>
                <td>${Number(c.unit_price).toLocaleString('vi-VN')} ₫</td>
                <td>${c.available_stock}</td>
                <td>${c.lead_time_out_of_stock} ngày</td>
                <td><button class="pill-btn coral" style="padding:4px 8px; font-size:11px;" onclick="deleteCatalogItem('${c.sku}')">Xóa</button></td>
            </tr>
        `).join("");
    }

    // 2. Chính sách
    document.getElementById("polMaxDiscount").value = STATE.policies.max_rep_discount_pct;
    document.getElementById("polMaxValue").value = STATE.policies.max_rep_deal_value;
    document.getElementById("polDefaultLead").value = STATE.policies.default_in_stock_lead_days;

    // 3. NVKD
    document.getElementById("navCountReps").innerText = STATE.reps.length + " NV";
    document.getElementById("btnRepCount").innerText = STATE.reps.length;
    const repBody = document.getElementById("repsTableBody");
    if (STATE.reps.length === 0) {
        repBody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:18px;">Chưa có NVKD nào. Hãy nhập tên NVKD ở trên.</td></tr>`;
    } else {
        repBody.innerHTML = STATE.reps.map(r => `
            <tr>
                <td><strong>${r.name}</strong></td>
                <td>${r.email || ''}</td>
                <td>${r.role || ''}</td>
                <td><button class="pill-btn coral" style="padding:4px 8px; font-size:11px;" onclick="deleteSalesRep(${r.id})">Xóa</button></td>
            </tr>
        `).join("");
    }
}

function renderAll() {
    const deals = STATE.opportunities || [];
    const query = (document.getElementById("searchInput").value || "").toLowerCase().trim();

    let totalVal = 0;
    let cntApprove = 0, cntReady = 0, cntSent = 0, cntClarify = 0;

    deals.forEach(d => {
        totalVal += Number(d.total_amount) || 0;
        if (d.status === "WAITING_APPROVAL") cntApprove++;
        else if (d.status === "READY_TO_SEND") cntReady++;
        else if (d.status === "SENT") cntSent++;
        else cntClarify++;
    });

    // Hiển thị đúng số thật (0 nếu chưa có)
    document.getElementById("totalPipelineValue").innerText = totalVal.toLocaleString("vi-VN") + " ₫";
    document.getElementById("totalDealsText").innerText = deals.length + " hồ sơ";

    document.getElementById("navCountAll").innerText = deals.length;
    document.getElementById("navCountApprove").innerText = cntApprove;
    document.getElementById("navCountReady").innerText = cntReady;
    document.getElementById("navCountClarify").innerText = cntClarify;
    document.getElementById("navCountSent").innerText = cntSent;

    document.getElementById("lgApprove").innerText = cntApprove;
    document.getElementById("lgReady").innerText = cntReady;
    document.getElementById("lgSent").innerText = cntSent;
    document.getElementById("lgClarify").innerText = cntClarify;

    if (deals.length === 0) {
        document.getElementById("segApprove").style.width = "0%";
        document.getElementById("segReady").style.width = "0%";
        document.getElementById("segSent").style.width = "0%";
        document.getElementById("segClarify").style.width = "0%";
    } else {
        document.getElementById("segApprove").style.width = ((cntApprove / deals.length) * 100) + "%";
        document.getElementById("segReady").style.width = ((cntReady / deals.length) * 100) + "%";
        document.getElementById("segSent").style.width = ((cntSent / deals.length) * 100) + "%";
        document.getElementById("segClarify").style.width = ((cntClarify / deals.length) * 100) + "%";
    }

    // 2 Thẻ Featured trên cùng
    renderFeaturedSlot("featuredSlot1", deals[0], 1);
    renderFeaturedSlot("featuredSlot2", deals[1], 2);

    // Biểu đồ thực tế
    renderRealChart(deals);

    // Danh sách thẻ cơ hội bên dưới
    const filtered = deals.filter(d => {
        const matchStatus = (CURRENT_FILTER === 'ALL') || (d.status === CURRENT_FILTER);
        const matchQuery = !query ||
            (d.id && d.id.toLowerCase().includes(query)) ||
            (d.company_name && d.company_name.toLowerCase().includes(query)) ||
            (d.customer_name && d.customer_name.toLowerCase().includes(query)) ||
            (d.raw_request && d.raw_request.toLowerCase().includes(query));
        return matchStatus && matchQuery;
    });

    document.getElementById("galleryTitle").innerText = `Danh sách Hồ sơ Cơ hội của bạn (${filtered.length})`;
    const grid = document.getElementById("oppCardsGrid");

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div style="grid-column: 1 / -1; text-align:center; padding: 38px 20px; border: 1.5px dashed var(--border-dashed); border-radius: 16px; color: var(--text-muted);">
                <div style="font-size: 15px; font-weight: 700; color: var(--text-secondary); margin-bottom: 6px;">Chưa có hồ sơ cơ hội nào (Số liệu = 0)</div>
                <div style="font-size: 13px;">Hãy tự nhập Bảng giá, Chính sách và Yêu cầu khách hàng ở trên để hệ thống bắt đầu tính toán.</div>
            </div>
        `;
        return;
    }

    grid.innerHTML = filtered.map(d => {
        let badgeClass = "ready", badgeText = "✓ Sẵn sàng gửi";
        if (d.status === "WAITING_APPROVAL") { badgeClass = "waiting"; badgeText = "⏳ Chờ TPKD duyệt"; }
        else if (d.status === "SENT") { badgeClass = "sent"; badgeText = "✓ Đã gửi báo giá"; }
        else if (d.status === "NEEDS_CLARIFICATION") { badgeClass = "clarify"; badgeText = "! Cần hỏi rõ KH"; }

        const amtText = Number(d.total_amount) > 0 ? Number(d.total_amount).toLocaleString("vi-VN") + " ₫" : "0 ₫ (Chưa đủ TT)";

        return `
            <div class="opp-credit-card" onclick="openDealModal('${d.id}')">
                <div>
                    <div class="occ-top">
                        <div class="double-circle-logo"><span class="dc-left"></span><span class="dc-right"></span></div>
                        <span class="occ-check-badge ${badgeClass}">${badgeText}</span>
                    </div>
                    <div class="occ-number">${amtText}</div>
                    <div class="occ-company">${d.company_name || d.customer_name} • <span style="color:var(--orange-primary)">${d.id}</span></div>
                </div>
                <div class="occ-bottom-meta">
                    <div>
                        <div class="occ-meta-label">Phụ trách</div>
                        <div class="occ-meta-val">${d.assigned_rep}</div>
                    </div>
                    <div>
                        <div class="occ-meta-label">Chiết khấu</div>
                        <div class="occ-meta-val">${d.discount_pct || 0}%</div>
                    </div>
                    <div style="text-align:right;">
                        <div class="occ-meta-label">Tiến độ</div>
                        <div class="occ-meta-val" style="color:var(--orange-primary);">${d.lead_time_days ? d.lead_time_days + ' ngày' : '--'}</div>
                    </div>
                </div>
            </div>
        `;
    }).join("");
}

function renderFeaturedSlot(elId, deal, indexNum) {
    const box = document.getElementById(elId);
    if (!deal) {
        box.innerHTML = `
            <div>
                <div class="card-top-meta">
                    <div class="card-label-group"><span>Hồ sơ gần nhất #${indexNum}</span></div>
                    <span class="currency-badge">Trống</span>
                </div>
                <div style="font-size:22px; font-weight:800; color:var(--text-muted); margin:12px 0 6px 0;">0 ₫</div>
                <div style="font-size:12.5px; color:var(--text-muted);">Chưa có dữ liệu. Hãy nhập yêu cầu khách hàng để hiển thị.</div>
            </div>
            <div class="pill-btn-group">
                <button class="pill-btn subtle" style="grid-column: span 3;" onclick="document.getElementById('rawInput').focus()">+ Nhập Yêu Cầu Ngay</button>
            </div>
        `;
        return;
    }

    box.innerHTML = `
        <div>
            <div class="card-top-meta">
                <div class="card-label-group"><span style="color:var(--text-main);font-weight:800;">${deal.company_name || deal.customer_name}</span></div>
                <span class="currency-badge" style="color:var(--orange-primary);">${deal.id}</span>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:baseline; margin:6px 0;">
                <div style="font-size:22px; font-weight:800;">${Number(deal.total_amount).toLocaleString('vi-VN')} ₫</div>
                <div style="font-size:12px; color:var(--text-muted);">CK: ${deal.discount_pct || 0}%</div>
            </div>
            <div style="font-size:12px; color:var(--text-secondary);">Phụ trách: ${deal.assigned_rep} • Trạng thái: ${deal.status}</div>
        </div>
        <div class="pill-btn-group">
            <button class="pill-btn orange" onclick="updateStatus('${deal.id}', 'READY_TO_SEND')">Duyệt</button>
            <button class="pill-btn orange" onclick="openDealModal('${deal.id}')">Chi Tiết</button>
            <button class="pill-btn coral" onclick="deleteDeal('${deal.id}')">Xóa</button>
        </div>
    `;
}

function renderRealChart(deals) {
    const container = document.getElementById("chartContainer");
    document.getElementById("chartCountBadge").innerText = deals.length + " hồ sơ";

    if (deals.length === 0) {
        container.innerHTML = `
            <div style="text-align:center; color:var(--text-muted); font-size:13px;">
                <div style="font-size:24px; font-weight:800; color:var(--text-secondary); margin-bottom:4px;">0 ₫</div>
                Chưa có dữ liệu biểu đồ. Biểu đồ sẽ tự động vẽ theo giá trị các hồ sơ bạn nhập.
            </div>
        `;
        return;
    }

    // Vẽ SVG thực tế từ mảng deals (theo thứ tự thời gian từ cũ đến mới)
    const chronological = [...deals].reverse();
    const values = chronological.map(d => Number(d.total_amount) || 0);
    const maxVal = Math.max(...values, 1);

    const width = 600, height = 160;
    const pts = values.map((v, i) => {
        const x = values.length === 1 ? width / 2 : (i / (values.length - 1)) * (width - 40) + 20;
        const y = height - 20 - ((v / maxVal) * (height - 45));
        return { x, y, v, id: chronological[i].id };
    });

    const linePoints = pts.map(p => `${p.x},${p.y}`).join(" ");
    const areaPoints = `${pts[0].x},${height - 15} ` + linePoints + ` ${pts[pts.length - 1].x},${height - 15}`;

    container.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="100%">
            <defs>
                <linearGradient id="realGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="#F7931A" stop-opacity="0.32"/>
                    <stop offset="100%" stop-color="#F7931A" stop-opacity="0.0"/>
                </linearGradient>
            </defs>
            <polygon points="${areaPoints}" fill="url(#realGrad)"/>
            <polyline fill="none" stroke="#F7931A" stroke-width="3" points="${linePoints}"/>
            ${pts.map(p => `
                <circle cx="${p.x}" cy="${p.y}" r="5" fill="#FFF" stroke="#F7931A" stroke-width="3"/>
                <text x="${p.x}" y="${p.y - 10}" text-anchor="middle" font-size="10" font-weight="700" fill="var(--text-main)">${p.v.toLocaleString('vi-VN')}đ</text>
            `).join("")}
        </svg>
    `;
}

// ==========================================
// CÁC HÀM THAO TÁC DỮ LIỆU TRỰC TIẾP
// ==========================================
async function addCatalogItem() {
    const sku = document.getElementById("catSku").value.trim().toUpperCase();
    const name = document.getElementById("catName").value.trim();
    const keywords = document.getElementById("catKeywords").value.trim();
    const unit_price = Number(document.getElementById("catPrice").value || 0);
    const available_stock = Number(document.getElementById("catStock").value || 0);
    const lead_time_out_of_stock = Number(document.getElementById("catLead").value || 0);

    if (!sku || !name) return showToast("⚠️ Vui lòng nhập Mã SKU và Tên sản phẩm!");

    await fetch("/api/catalog", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sku, name, keywords, unit_price, available_stock, lead_time_out_of_stock })
    });

    document.getElementById("catSku").value = "";
    document.getElementById("catName").value = "";
    document.getElementById("catKeywords").value = "";
    document.getElementById("catPrice").value = "";
    document.getElementById("catStock").value = "";
    document.getElementById("catLead").value = "";

    await loadStateFromServer();
    showToast(`✅ Đã lưu sản phẩm ${sku} vào Bảng giá & Kho!`);
}

async function deleteCatalogItem(sku) {
    await fetch("/api/catalog_delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sku })
    });
    await loadStateFromServer();
    showToast(`🗑️ Đã xóa mã ${sku}`);
}

async function savePolicies() {
    const max_rep_discount_pct = Number(document.getElementById("polMaxDiscount").value || 0);
    const max_rep_deal_value = Number(document.getElementById("polMaxValue").value || 0);
    const default_in_stock_lead_days = Number(document.getElementById("polDefaultLead").value || 0);

    await fetch("/api/policies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_rep_discount_pct, max_rep_deal_value, default_in_stock_lead_days })
    });
    await loadStateFromServer();
    closeModal('policyModal');
    showToast("✅ Đã lưu Chính sách Chiết khấu & Phân quyền!");
}

async function addSalesRep() {
    const name = document.getElementById("repName").value.trim();
    const email = document.getElementById("repEmail").value.trim();
    const role = document.getElementById("repRole").value.trim();
    if (!name) return showToast("⚠️ Vui lòng nhập họ tên NVKD!");

    await fetch("/api/reps", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, role })
    });
    document.getElementById("repName").value = "";
    document.getElementById("repEmail").value = "";
    document.getElementById("repRole").value = "";
    await loadStateFromServer();
    showToast(`✅ Đã thêm NVKD: ${name}`);
}

async function deleteSalesRep(id) {
    await fetch("/api/reps_delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id })
    });
    await loadStateFromServer();
}

async function submitRequest() {
    const text = document.getElementById("rawInput").value.trim();
    if (!text) return showToast("⚠️ Hãy nhập nội dung yêu cầu của khách hàng!");

    const btn = document.getElementById("btnRunAgent");
    btn.disabled = true;
    btn.innerText = "⏳ AI đang phân tích dữ liệu của bạn...";

    try {
        const res = await fetch("/api/process", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ raw_text: text })
        });
        const data = await res.json();
        document.getElementById("rawInput").value = "";
        await loadStateFromServer();
        showToast(`✅ Đã tạo hồ sơ ${data.deal_id}`);
        openDealModal(data.deal_id);
    } catch (e) {
        showToast("❌ Lỗi: " + e.message);
    } finally {
        btn.disabled = false;
        btn.innerText = "🚀 Chạy AI Phân Tích Yêu Cầu Này";
    }
}

function openDealModal(dealId) {
    const deal = STATE.opportunities.find(d => d.id === dealId);
    if (!deal) return;

    document.getElementById("modalDealId").innerText = `${deal.id} • Trạng thái: ${deal.status}`;
    document.getElementById("modalCompanyName").innerText = `${deal.company_name || deal.customer_name} (${Number(deal.total_amount).toLocaleString("vi-VN")} ₫)`;
    document.getElementById("modalDraftEmail").value = deal.draft_email || "";
    document.getElementById("modalRepInfo").innerText = `Phụ trách: ${deal.assigned_rep} • Tạo lúc: ${deal.created_at}`;

    const escBox = document.getElementById("modalEscalationBox");
    if (deal.escalation_reasons) {
        escBox.style.display = "block";
        document.getElementById("modalEscalationText").innerText = deal.escalation_reasons;
    } else {
        escBox.style.display = "none";
    }

    const btns = document.getElementById("modalActionButtons");
    btns.innerHTML = `
        <button class="pill-btn coral" onclick="deleteDeal('${deal.id}')">🗑️ Xóa Hồ Sơ</button>
        ${deal.status === 'WAITING_APPROVAL' ? `<button class="pill-btn orange" onclick="updateStatus('${deal.id}', 'READY_TO_SEND')">👍 Trưởng Phòng Duyệt</button>` : ''}
        ${deal.status !== 'SENT' ? `<button class="pill-btn orange" onclick="updateStatus('${deal.id}', 'SENT')">📤 Xác Nhận Đã Gửi KH</button>` : ''}
    `;
    openModal("dealModal");
}

async function updateStatus(id, status) {
    const draft_email = document.getElementById("modalDraftEmail") ? document.getElementById("modalDraftEmail").value : null;
    await fetch("/api/update_status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, status, draft_email })
    });
    closeModal("dealModal");
    await loadStateFromServer();
    showToast("✅ Đã cập nhật trạng thái hồ sơ!");
}

async function deleteDeal(id) {
    await fetch("/api/deal_delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id })
    });
    closeModal("dealModal");
    await loadStateFromServer();
    showToast(`🗑️ Đã xóa hồ sơ ${id}`);
}

async function resetAllData() {
    if (!confirm("Bạn có chắc chắn muốn xóa trắng toàn bộ Hồ sơ, Bảng giá, Chính sách và NVKD về 0 không?")) return;
    await fetch("/api/reset_all", { method: "POST" });
    await loadStateFromServer();
    showToast("🔄 Đã đưa toàn bộ hệ thống về 0!");
}

window.onload = () => {
    loadStateFromServer();
};
</script>
</body>
</html>
"""

# ==========================================
# 5. HTTP SERVER HANDLERS
# ==========================================
class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/api/state":
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM opportunities ORDER BY rowid DESC")
            opportunities = [dict(r) for r in cur.fetchall()]
            conn.close()

            payload = {
                "opportunities": opportunities,
                "catalog": get_all_catalog(),
                "reps": get_all_reps(),
                "policies": get_policies()
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/healthz":
            self._send_json({"status": "ok"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body) if body else {}

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()

        if self.path == "/api/process":
            conn.close()
            result = process_and_save_request(data.get("raw_text", ""))
            self._send_json(result)
            return

        elif self.path == "/api/catalog":
            cur.execute("""
                INSERT OR REPLACE INTO catalog (sku, name, keywords, unit_price, available_stock, lead_time_out_of_stock)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                data.get("sku"), data.get("name"), data.get("keywords", ""),
                float(data.get("unit_price", 0)), int(data.get("available_stock", 0)), int(data.get("lead_time_out_of_stock", 0))
            ))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/catalog_delete":
            cur.execute("DELETE FROM catalog WHERE sku = ?", (data.get("sku"),))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/policies":
            for k in ["max_rep_discount_pct", "max_rep_deal_value", "default_in_stock_lead_days"]:
                if k in data:
                    cur.execute("INSERT OR REPLACE INTO policies (key, value) VALUES (?, ?)", (k, float(data[k])))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/reps":
            cur.execute("INSERT INTO sales_reps (name, email, role) VALUES (?, ?, ?)",
                        (data.get("name"), data.get("email", ""), data.get("role", "")))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/reps_delete":
            cur.execute("DELETE FROM sales_reps WHERE id = ?", (data.get("id"),))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/update_status":
            if data.get("draft_email"):
                cur.execute("UPDATE opportunities SET status = ?, draft_email = ? WHERE id = ?",
                            (data.get("status"), data.get("draft_email"), data.get("id")))
            else:
                cur.execute("UPDATE opportunities SET status = ? WHERE id = ?", (data.get("status"), data.get("id")))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/deal_delete":
            cur.execute("DELETE FROM opportunities WHERE id = ?", (data.get("id"),))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        elif self.path == "/api/reset_all":
            cur.execute("DELETE FROM opportunities")
            cur.execute("DELETE FROM catalog")
            cur.execute("DELETE FROM sales_reps")
            cur.execute("DELETE FROM policies")
            conn.commit()
            conn.close()
            self._send_json({"success": True})
            return

        conn.close()
        self._send_json({"error": "Not found"})

    def _send_json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

def run_server():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server_address = ('0.0.0.0', PORT)
    with socketserver.ThreadingTCPServer(server_address, RequestHandler) as httpd:
        print(f"=====================================================")
        print(f"✅ MinhAn Server (Production & Localhost Ready) đang chạy:")
        print(f"👉 Truy cập: http://localhost:{PORT}")
        print(f"=====================================================")
        sys.stdout.flush()
        httpd.serve_forever()

if __name__ == '__main__':
    run_server()
