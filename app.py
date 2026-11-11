import json
from decimal import Decimal
from datetime import date, datetime

from openai import OpenAI
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_file,
    Response,
    flash,
    jsonify
)
import re
from dotenv import load_dotenv
from jinja2 import TemplateNotFound
from functools import wraps
import pymysql
import os
import csv
from io import StringIO, BytesIO

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


# =========================
# Basic Helpers
# =========================

def safe_render(template_name, **context):
    """
    Helpful during development: if a template is missing,
    show a placeholder page instead of crashing.
    Remove this later if you want stricter behavior.
    """
    try:
        return render_template(template_name, **context)
    except TemplateNotFound:
        pretty_keys = sorted(context.keys())
        html = f"""
        <html>
        <head>
            <title>Template Missing - {template_name}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    background: #f7f9fc;
                    padding: 40px;
                    color: #222;
                }}
                .box {{
                    max-width: 900px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 16px;
                    padding: 24px;
                    box-shadow: 0 8px 24px rgba(0,0,0,.08);
                }}
                code {{
                    background: #f3f4f6;
                    padding: 2px 6px;
                    border-radius: 6px;
                }}
                pre {{
                    background: #0f172a;
                    color: #e2e8f0;
                    padding: 16px;
                    border-radius: 12px;
                    overflow-x: auto;
                }}
            </style>
        </head>
        <body>
            <div class="box">
                <h1>{template_name} not created yet</h1>
                <p>This route is working. You can now build this template.</p>
                <p><strong>Expected context variables:</strong></p>
                <ul>
                    {''.join(f'<li><code>{k}</code></li>' for k in pretty_keys)}
                </ul>
                <p><strong>Preview of context:</strong></p>
                <pre>{context}</pre>
                <p><a href="/dashboard">Back to Dashboard</a></p>
            </div>
        </body>
        </html>
        """
        return html
    
# =========================
# DeepSeek Configuration
# =========================

def has_deepseek_enabled():
    """检查是否配置了 DeepSeek 的 API Key"""
    return bool(os.environ.get("DEEPSEEK_API_KEY"))

def deepseek_client():
    """初始化并返回 DeepSeek 的客户端（兼容 OpenAI SDK）"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY environment variable is not set.")
    
    # 借助 openai 库调用 DeepSeek 接口
    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com"  # 确保指引到 DeepSeek 的官方接口
    )


def blank_to_none(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


def parse_int_or_none(value):
    value = blank_to_none(value)
    if value is None:
        return None
    return int(value)


def parse_float_or_none(value):
    value = blank_to_none(value)
    if value is None:
        return None
    return float(value)


def normalize_text(value):
    return (value or "").strip()


# =========================
# Database Helpers
# =========================

def get_ca_path():
    raw_path = os.getenv("TIDB_CA_PATH", "").strip()
    if not raw_path:
        return None
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.join(BASE_DIR, raw_path)


def get_db_connection():
    ca_path = get_ca_path()

    if ca_path and not os.path.exists(ca_path):
        raise RuntimeError(f"CA certificate file not found: {ca_path}")

    ssl_config = {"ca": ca_path} if ca_path else None

    return pymysql.connect(
        host=os.getenv("TIDB_HOST"),
        port=int(os.getenv("TIDB_PORT", 4000)),
        user=os.getenv("TIDB_USER"),
        password=os.getenv("TIDB_PASSWORD"),
        database=os.getenv("TIDB_DB_NAME"),
        cursorclass=pymysql.cursors.DictCursor,
        ssl=ssl_config,
        autocommit=False,
    )


def query_all(sql, params=None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchall()
    finally:
        conn.close()


def query_one(sql, params=None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchone()
    finally:
        conn.close()


def execute_sql(sql, params=None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            conn.commit()
            return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DONOR_TYPE_OPTIONS = ["Individual", "Corporate"]
GENDER_OPTIONS = ["Female", "Male", "Other", "Prefer not to say"]
MARITAL_STATUS_OPTIONS = ["Single", "Married", "Divorced", "Widowed", "Prefer not to say"]
STAFF_TYPE_OPTIONS = ["Full-time", "Part-time", "Volunteer", "Contract"]
DONATION_FREQUENCY_OPTIONS = ["One-time", "Monthly", "Quarterly", "Annually", "Other"]
RECEIVABLE_PAYMENT_METHOD_OPTIONS = [
    "Cash", "Credit Card", "Bank Transfer", "PayPal", "Online Banking", "Cheque", "Other"
]


def parse_bool_flag(value):
    return 1 if str(value).strip() in {"1", "true", "True", "yes", "Yes", "on"} else 0


def parse_nonnegative_int(value, default=0):
    try:
        parsed = int(str(value).strip())
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError, AttributeError):
        return default


def serialize_rows(rows):
    result = []
    for row in rows or []:
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, Decimal):
                clean_row[k] = float(v)
            elif isinstance(v, (datetime, date)):
                clean_row[k] = v.isoformat()
            else:
                clean_row[k] = v
        result.append(clean_row)
    return result


def canonical_child_value(value):
    if value in [1, True, "1", "true", "True", "yes", "Yes"]:
        return 1
    if value in [0, False, "0", "false", "False", "no", "No"]:
        return 0
    return None


def execute_transaction(callback):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            result = callback(cursor)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================
# Auth / Permission Helpers
# =========================

def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if session.get("role_name") not in allowed_roles:
                return "403 Forbidden", 403
            return view_func(*args, **kwargs)
        return wrapper
    return decorator


def common_context():
    return {
        "current_user": session.get("staff_name", session.get("username", "User")),
        "role_name": session.get("role_name", ""),
    }


# =========================
# Debug
# =========================

@app.route("/db-test")
def db_test():
    try:
        ca_path = get_ca_path()
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DATABASE() AS db_name;")
                row = cursor.fetchone()
        finally:
            conn.close()

        return {
            "status": "ok",
            "database": row["db_name"] if row else None,
            "ca_path": ca_path,
            "ca_exists": os.path.exists(ca_path) if ca_path else False
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "ca_path": get_ca_path()
        }, 500


# =========================
# Auth Routes
# =========================

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = normalize_text(request.form.get("username"))
        password = normalize_text(request.form.get("password"))

        try:
            user = query_one("""
                SELECT
                    u.user_id,
                    u.username,
                    u.role_id,
                    r.role_name,
                    s.staff_id,
                    s.staff_name
                FROM UserAccount u
                JOIN Role r ON u.role_id = r.role_id
                JOIN Staff s ON u.staff_id = s.staff_id
                WHERE u.username = %s
                  AND u.password = %s
                  AND u.account_status = 'Active'
            """, (username, password))
        except Exception as e:
            return safe_render("login.html", error=f"Database error: {e}")

        if user:
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["role_id"] = user["role_id"]
            session["role_name"] = user["role_name"]
            session["staff_id"] = user["staff_id"]
            session["staff_name"] = user["staff_name"]
            return redirect(url_for("dashboard"))

        return safe_render("login.html", error="Invalid username or password.")

    return safe_render("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = normalize_text(request.form.get("username"))
        password = normalize_text(request.form.get("password"))
        role_id = parse_int_or_none(request.form.get("role_id"))

        if not username or not password or not role_id:
            return safe_render("signup.html", error="Please fill in all fields.")

        try:
            existing = query_one(
                "SELECT user_id FROM UserAccount WHERE username = %s",
                (username,)
            )
            if existing:
                return safe_render("signup.html", error="Username already exists.")

            def tx(cursor):
                cursor.execute("""
                    INSERT INTO Staff (
                        staff_name, phone, email, position, staff_type, is_paid, salary, date_of_birth
                    ) VALUES (%s, NULL, NULL, 'New Staff', 'Staff', 0, NULL, NULL)
                """, (username,))
                new_staff_id = cursor.lastrowid

                cursor.execute("""
                    INSERT INTO UserAccount (staff_id, role_id, username, password, account_status)
                    VALUES (%s, %s, %s, %s, 'Active')
                """, (new_staff_id, role_id, username, password))

            execute_transaction(tx)
            return safe_render("login.html", success="Account created successfully. Please log in.")
        except Exception as e:
            return safe_render("signup.html", error=f"Registration failed: {e}")

    return safe_render("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# Dashboard
# =========================

@app.route("/dashboard")
@login_required
def dashboard():
    search_query = normalize_text(request.args.get("search"))

    try:
        total_donors = query_one("SELECT COUNT(*) AS total FROM Donor")["total"]
        total_receivables = query_one("SELECT COUNT(*) AS total FROM Receivable")["total"]
        total_receivable_amount = query_one("SELECT COALESCE(SUM(amount), 0) AS total FROM Receivable")["total"]
        total_gift_distributions = query_one("SELECT COUNT(*) AS total FROM GiftDistribution")["total"]
        total_events = query_one("SELECT COUNT(*) AS total FROM Event")["total"]
        total_inventory_items = query_one("SELECT COALESCE(SUM(quantities), 0) AS total FROM Inventory")["total"]
        unpaid_invoice_count = query_one("""
            SELECT COUNT(*) AS total
            FROM Invoice
            WHERE bill_status = 'Pending'
        """)["total"]
        total_payment_amount = query_one("SELECT COALESCE(SUM(amount), 0) AS total FROM Payment")["total"]

        gender_stats = query_all("""
            SELECT gender AS label, COUNT(*) AS value
            FROM Donor
            WHERE gender IS NOT NULL AND gender <> ''
            GROUP BY gender
            ORDER BY value DESC
        """)

        donor_type_stats = query_all("""
            SELECT donor_type AS label, COUNT(*) AS value
            FROM Donor
            WHERE donor_type IS NOT NULL AND donor_type <> ''
            GROUP BY donor_type
            ORDER BY value DESC
        """)

        if search_query:
            donors = query_all("""
                SELECT donor_id, donor_name, email, phone, city, donor_type
                FROM Donor
                WHERE donor_name LIKE %s
                ORDER BY donor_name
            """, (f"%{search_query}%",))
        else:
            donors = query_all("""
                SELECT donor_id, donor_name, email, phone, city, donor_type
                FROM Donor
                ORDER BY donor_id DESC
                LIMIT 20
            """)

        return safe_render(
            "dashboard.html",
            total=total_donors,
            total_donors=total_donors,
            total_receivables=total_receivables,
            total_receivable_amount=total_receivable_amount,
            total_gift_distributions=total_gift_distributions,
            total_events=total_events,
            total_inventory_items=total_inventory_items,
            unpaid_invoice_count=unpaid_invoice_count,
            total_payment_amount=total_payment_amount,
            donors=donors,
            recent_donors=donors,
            gender_stats=gender_stats,
            donor_type_stats=donor_type_stats,
            search_query=search_query,
            **common_context(),
        )
    except Exception as e:
        return f"Dashboard error: {e}", 500


# =========================
# Donor Module
# =========================

def get_donor_form_payload():
    donor_name = normalize_text(request.form.get("donor_name"))
    gender = blank_to_none(request.form.get("gender"))
    marital_status = blank_to_none(request.form.get("marital_status"))
    feedback = blank_to_none(request.form.get("feedback"))
    email = blank_to_none(request.form.get("email"))
    phone = blank_to_none(request.form.get("phone"))
    date_of_birth = blank_to_none(request.form.get("date_of_birth"))
    city = blank_to_none(request.form.get("city"))
    donor_type = blank_to_none(request.form.get("donor_type"))
    location = blank_to_none(request.form.get("location"))
    country = blank_to_none(request.form.get("country"))
    has_child = parse_bool_flag(request.form.get("has_child"))
    number_of_child = parse_nonnegative_int(request.form.get("number_of_child"), default=0)

    if has_child == 0:
        number_of_child = 0

    

    return (
        donor_name,
        gender,
        marital_status,
        feedback,
        email,
        phone,
        date_of_birth,
        city,
        donor_type,
        location,
        country,
        has_child,
        number_of_child,
    )


@app.route("/donors")
@login_required
def donor_list():
    search_query = normalize_text(request.args.get("search"))
    donor_type_filter = normalize_text(request.args.get("donor_type"))

    # 1. 动态查询是否收到过礼物
    donors = query_all("""
        SELECT
            d.donor_id,
            d.donor_name,
            d.gender,
            d.email,
            d.phone,
            d.city,
            d.donor_type,
            d.country,
            IF(EXISTS(SELECT 1 FROM GiftDistribution gd WHERE gd.donor_id = d.donor_id), 1, 0) AS received_gift
        FROM Donor d
        WHERE (%s = ''
               OR d.donor_name LIKE %s
               OR d.email LIKE %s
               OR d.phone LIKE %s
               OR d.city LIKE %s
               OR d.country LIKE %s)
          AND (%s = '' OR d.donor_type = %s)
        ORDER BY d.donor_id DESC
    """, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        donor_type_filter,
        donor_type_filter,
    ))

    # 2. 删除了原本对婚姻和孩子字段的统计，避免 SQL 报错。保留了 total 和 female 统计。
    donor_overview = query_one("""
        SELECT
            COUNT(*) AS total_donors,
            SUM(CASE WHEN LOWER(COALESCE(gender, '')) = 'female' THEN 1 ELSE 0 END) AS female_donors
        FROM Donor
    """) or {}

    staff_overlap = query_one("""
        SELECT COUNT(DISTINCT d.donor_id) AS overlap_count
        FROM Donor d
        JOIN Staff s
          ON (
                (d.email IS NOT NULL AND s.email IS NOT NULL
                 AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email)))
                OR
                (
                    LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.staff_name))
                    AND d.phone IS NOT NULL
                    AND s.phone IS NOT NULL
                    AND REPLACE(REPLACE(d.phone, '-', ''), ' ', '') =
                        REPLACE(REPLACE(s.phone, '-', ''), ' ', '')
                )
             )
    """) or {}

    # Supplier 如果也是 donor，按公司 donor 匹配，不按 contact_person
    supplier_overlap = query_one("""
        SELECT COUNT(DISTINCT d.donor_id) AS overlap_count
        FROM Donor d
        JOIN Supplier s
          ON (
                LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.supplier_name))
                OR
                (
                    d.email IS NOT NULL AND s.email IS NOT NULL
                    AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email))
                )
             )
        WHERE LOWER(COALESCE(d.donor_type, '')) = 'corporate'
    """) or {}

    return safe_render(
        "donor_list.html",
        donors=donors,
        donor_overview=donor_overview or {},
        staff_overlap_count=(staff_overlap or {}).get("overlap_count", 0),
        supplier_overlap_count=(supplier_overlap or {}).get("overlap_count", 0),
        search_query=search_query,
        donor_type_filter=donor_type_filter,
        donor_type_options=DONOR_TYPE_OPTIONS,
        **common_context(),
    )


@app.route("/donor/<int:donor_id>")
@login_required
def donor_profile(donor_id):
    donor_info = query_one("""
        SELECT
            donor_id,
            donor_name,
            gender,
            marital_status,
            has_child,
            number_of_child,
            feedback,
            email,
            phone,
            date_of_birth,
            city,
            donor_type,
            location,
            country
        FROM Donor
        WHERE donor_id = %s
    """, (donor_id,))

    if not donor_info:
        return "Donor not found", 404

    receivables = query_all("""
        SELECT
            receivable_id,
            donation_date,
            amount,
            donation_frequency,
            payment_method,
            receipt_number,
            receipt_issue_date,
            is_event_related
        FROM Receivable
        WHERE donor_id = %s
        ORDER BY donation_date DESC, receivable_id DESC
    """, (donor_id,))

    event_participations = query_all("""
        SELECT
            e.event_id,
            e.event_name,
            e.event_date,
            ed.participation_status
        FROM EventDonor ed
        JOIN Event e ON ed.event_id = e.event_id
        WHERE ed.donor_id = %s
        ORDER BY e.event_date DESC
    """, (donor_id,))

    gift_distributions = query_all("""
        SELECT
            gd.distribution_id,
            gd.distribution_date,
            gd.quantity,
            gd.distribution_reason,
            g.gift_name,
            g.gift_category,
            g.gift_value
        FROM GiftDistribution gd
        JOIN Gift g ON gd.gift_id = g.gift_id
        WHERE gd.donor_id = %s
        ORDER BY gd.distribution_date DESC
    """, (donor_id,))

    donation_summary = query_one("""
        SELECT
            COUNT(*) AS total_donations,
            COALESCE(SUM(amount), 0) AS total_donated
        FROM Receivable
        WHERE donor_id = %s
    """, (donor_id,))

    matched_staff = query_all("""
        SELECT DISTINCT
            s.staff_id,
            s.staff_name,
            s.email,
            s.phone,
            s.position
        FROM Staff s
        JOIN Donor d
          ON d.donor_id = %s
         AND (
                (d.email IS NOT NULL AND s.email IS NOT NULL
                 AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email)))
                OR
                (
                    LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.staff_name))
                    AND d.phone IS NOT NULL
                    AND s.phone IS NOT NULL
                    AND REPLACE(REPLACE(d.phone, '-', ''), ' ', '') =
                        REPLACE(REPLACE(s.phone, '-', ''), ' ', '')
                )
             )
        ORDER BY s.staff_id
    """, (donor_id,))

    matched_suppliers = query_all("""
        SELECT DISTINCT
            s.supplier_id,
            s.supplier_name,
            s.contact_person,
            s.email,
            s.phone_number,
            s.supplier_type
        FROM Supplier s
        JOIN Donor d
          ON d.donor_id = %s
         AND LOWER(COALESCE(d.donor_type, '')) = 'corporate'
         AND (
                LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.supplier_name))
                OR
                (
                    d.email IS NOT NULL AND s.email IS NOT NULL
                    AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email))
                )
             )
        ORDER BY s.supplier_id
    """, (donor_id,))

    donor_metrics = {
        "total_donations": (donation_summary or {}).get("total_donations", 0),
        "total_donated": float((donation_summary or {}).get("total_donated", 0) or 0),
        "total_events": len(event_participations),
        "total_gifts": sum(int(row.get("quantity", 0) or 0) for row in gift_distributions),
    }

    return safe_render(
        "donor_profile.html",
        donor_info=donor_info,
        receivables=receivables,
        event_participations=event_participations,
        gift_distributions=gift_distributions,
        donor_metrics=donor_metrics,
        matched_staff=matched_staff,
        matched_suppliers=matched_suppliers,
        **common_context(),
    )

@app.route("/donor/new", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def donor_new():
    if request.method == "POST":
        payload = get_donor_form_payload()

        new_id = execute_sql("""
            INSERT INTO Donor (
                donor_name,
                gender,
                marital_status,
                feedback,
                email,
                phone,
                date_of_birth,
                city,
                donor_type,
                location,
                country,
                has_child,
                number_of_child
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, payload)

        return redirect(url_for("donor_profile", donor_id=new_id))

    return safe_render(
        "donor_form.html",
        donor_info=None,
        form_mode="create",
        donor_type_options=DONOR_TYPE_OPTIONS,
        gender_options=GENDER_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        **common_context(),
    )


@app.route("/donor/<int:donor_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def donor_edit(donor_id):
    donor_info = query_one("SELECT * FROM Donor WHERE donor_id = %s", (donor_id,))
    if not donor_info:
        return "Donor not found", 404

    if request.method == "POST":
        payload = get_donor_form_payload()

        execute_sql("""
            UPDATE Donor
            SET donor_name = %s,
                gender = %s,
                marital_status = %s,
                feedback = %s,
                email = %s,
                phone = %s,
                date_of_birth = %s,
                city = %s,
                donor_type = %s,
                location = %s,
                country = %s,
                has_child = %s,
                number_of_child = %s
            WHERE donor_id = %s
        """, payload + (donor_id,))

        return redirect(url_for("donor_profile", donor_id=donor_id))

    return safe_render(
        "donor_form.html",
        donor_info=donor_info,
        form_mode="edit",
        donor_type_options=DONOR_TYPE_OPTIONS,
        gender_options=GENDER_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        **common_context(),
    )

# =========================
# Receivable Module
# =========================

def get_receivable_form_payload():
    donor_id = parse_int_or_none(request.form.get("donor_id"))
    donation_date = blank_to_none(request.form.get("donation_date"))
    amount = parse_float_or_none(request.form.get("amount")) or 0
    donation_frequency = blank_to_none(request.form.get("donation_frequency"))
    payment_method = blank_to_none(request.form.get("payment_method"))
    receipt_number = blank_to_none(request.form.get("receipt_number"))
    receipt_issue_date = blank_to_none(request.form.get("receipt_issue_date"))
    is_event_related = parse_bool_flag(request.form.get("is_event_related"))

    return (
        donor_id,
        donation_date,
        amount,
        donation_frequency,
        payment_method,
        receipt_number,
        receipt_issue_date,
        is_event_related,
    )


@app.route("/receivables")
@role_required("Administrator", "Finance", "Staff")
def receivable_list():
    search_query = normalize_text(request.args.get("search"))
    event_related_filter = normalize_text(request.args.get("event_related"))

    receivables = query_all("""
        SELECT
            r.receivable_id,
            r.donor_id,
            d.donor_name,
            r.donation_date,
            r.amount,
            r.donation_frequency,
            r.payment_method,
            r.receipt_number,
            r.receipt_issue_date,
            r.is_event_related
        FROM Receivable r
        JOIN Donor d ON r.donor_id = d.donor_id
        WHERE (%s = ''
               OR d.donor_name LIKE %s
               OR COALESCE(r.receipt_number, '') LIKE %s
               OR COALESCE(r.payment_method, '') LIKE %s)
          AND (
                %s = ''
                OR (%s = '1' AND r.is_event_related = 1)
                OR (%s = '0' AND r.is_event_related = 0)
          )
        ORDER BY r.donation_date DESC, r.receivable_id DESC
    """, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        event_related_filter,
        event_related_filter,
        event_related_filter,
    ))

    return safe_render(
        "receivable_list.html",
        receivables=receivables,
        search_query=search_query,
        event_related_filter=event_related_filter,
        **common_context(),
    )


@app.route("/receivable/new", methods=["GET", "POST"])
@role_required("Administrator", "Finance", "Staff")
def receivable_new():
    donors = query_all("""
        SELECT donor_id, donor_name
        FROM Donor
        ORDER BY donor_name
    """)

    selected_donor_id = parse_int_or_none(request.args.get("donor_id"))

    if request.method == "POST":
        payload = get_receivable_form_payload()

        donor_id = payload[0]
        donation_date = payload[1]
        amount = payload[2]

        if not donor_id or not donation_date or amount <= 0:
            return safe_render(
                "receivable_form.html",
                receivable_info=None,
                donors=donors,
                selected_donor_id=selected_donor_id,
                form_mode="create",
                donation_frequency_options=DONATION_FREQUENCY_OPTIONS,
                receivable_payment_method_options=RECEIVABLE_PAYMENT_METHOD_OPTIONS,
                error="Please fill in donor, donation date, and a valid amount.",
                **common_context(),
            )

        new_id = execute_sql("""
            INSERT INTO Receivable (
                donor_id,
                donation_date,
                amount,
                donation_frequency,
                payment_method,
                receipt_number,
                receipt_issue_date,
                is_event_related
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, payload)

        # 如果是从 donor 页面点进来的，保存后回 donor 主页
        donor_id_from_form = payload[0]
        if donor_id_from_form:
            return redirect(url_for("donor_profile", donor_id=donor_id_from_form))

        return redirect(url_for("receivable_list"))

    return safe_render(
        "receivable_form.html",
        receivable_info=None,
        donors=donors,
        selected_donor_id=selected_donor_id,
        form_mode="create",
        donation_frequency_options=DONATION_FREQUENCY_OPTIONS,
        receivable_payment_method_options=RECEIVABLE_PAYMENT_METHOD_OPTIONS,
        **common_context(),
    )


@app.route("/receivable/<int:receivable_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Finance", "Staff")
def receivable_edit(receivable_id):
    receivable_info = query_one("""
        SELECT *
        FROM Receivable
        WHERE receivable_id = %s
    """, (receivable_id,))
    if not receivable_info:
        return "Receivable not found", 404

    donors = query_all("""
        SELECT donor_id, donor_name
        FROM Donor
        ORDER BY donor_name
    """)

    if request.method == "POST":
        payload = get_receivable_form_payload()

        donor_id = payload[0]
        donation_date = payload[1]
        amount = payload[2]

        if not donor_id or not donation_date or amount <= 0:
            return safe_render(
                "receivable_form.html",
                receivable_info=receivable_info,
                donors=donors,
                selected_donor_id=receivable_info.get("donor_id"),
                form_mode="edit",
                donation_frequency_options=DONATION_FREQUENCY_OPTIONS,
                receivable_payment_method_options=RECEIVABLE_PAYMENT_METHOD_OPTIONS,
                error="Please fill in donor, donation date, and a valid amount.",
                **common_context(),
            )

        execute_sql("""
            UPDATE Receivable
            SET donor_id = %s,
                donation_date = %s,
                amount = %s,
                donation_frequency = %s,
                payment_method = %s,
                receipt_number = %s,
                receipt_issue_date = %s,
                is_event_related = %s
            WHERE receivable_id = %s
        """, payload + (receivable_id,))

        return redirect(url_for("donor_profile", donor_id=payload[0]))

    return safe_render(
        "receivable_form.html",
        receivable_info=receivable_info,
        donors=donors,
        selected_donor_id=receivable_info.get("donor_id"),
        form_mode="edit",
        donation_frequency_options=DONATION_FREQUENCY_OPTIONS,
        receivable_payment_method_options=RECEIVABLE_PAYMENT_METHOD_OPTIONS,
        **common_context(),
    )


@app.route("/donor/<int:donor_id>/receivable/new", methods=["GET"])
@role_required("Administrator", "Finance", "Staff")
def donor_receivable_new(donor_id):
    donor = query_one("SELECT donor_id FROM Donor WHERE donor_id = %s", (donor_id,))
    if not donor:
        return "Donor not found", 404
    return redirect(url_for("receivable_new", donor_id=donor_id))

# =========================
# Staff Module
# =========================

def get_staff_form_payload():
    staff_name = normalize_text(request.form.get("staff_name"))
    phone = blank_to_none(request.form.get("phone"))
    email = blank_to_none(request.form.get("email"))
    position = blank_to_none(request.form.get("position"))
    staff_type = blank_to_none(request.form.get("staff_type"))

    is_paid = parse_bool_flag(request.form.get("is_paid"))
    salary_raw = blank_to_none(request.form.get("salary"))
    salary = salary_raw if salary_raw else None

    date_of_birth = blank_to_none(request.form.get("date_of_birth"))
    gender = blank_to_none(request.form.get("gender"))
    marital_status = blank_to_none(request.form.get("marital_status"))
    has_child = parse_bool_flag(request.form.get("has_child"))
    number_of_child = parse_nonnegative_int(request.form.get("number_of_child"), default=0)

    if has_child == 0:
        number_of_child = 0

    city = blank_to_none(request.form.get("city"))
    location = blank_to_none(request.form.get("location"))
    country = blank_to_none(request.form.get("country"))

    return (
        staff_name,
        phone,
        email,
        position,
        staff_type,
        is_paid,
        salary,
        date_of_birth,
        gender,
        marital_status,
        has_child,
        number_of_child,
        city,
        location,
        country,
    )


@app.route("/staff")
@login_required
def staff_list():
    search_query = normalize_text(request.args.get("search"))
    staff_type_filter = normalize_text(request.args.get("staff_type"))

    staff_rows = query_all("""
        SELECT
            staff_id,
            staff_name,
            email,
            phone,
            position,
            staff_type,
            is_paid,
            city,
            country
        FROM Staff
        WHERE (%s = ''
               OR staff_name LIKE %s
               OR email LIKE %s
               OR phone LIKE %s
               OR position LIKE %s
               OR city LIKE %s)
          AND (%s = '' OR staff_type = %s)
        ORDER BY staff_id DESC
    """, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        staff_type_filter,
        staff_type_filter,
    ))

    staff_type_options_rows = query_all("""
        SELECT DISTINCT staff_type
        FROM Staff
        WHERE staff_type IS NOT NULL AND staff_type <> ''
        ORDER BY staff_type
    """)
    staff_type_options = [row["staff_type"] for row in staff_type_options_rows]

    return safe_render(
        "staff.html",
        staff_rows=staff_rows,
        search_query=search_query,
        staff_type_filter=staff_type_filter,
        staff_type_options=staff_type_options,
        **common_context(),
    )


@app.route("/staff/new", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def staff_new():
    if request.method == "POST":
        payload = get_staff_form_payload()

        new_id = execute_sql("""
            INSERT INTO Staff (
                staff_name,
                phone,
                email,
                position,
                staff_type,
                is_paid,
                salary,
                date_of_birth,
                gender,
                marital_status,
                has_child,
                number_of_child,
                city,
                location,
                country
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, payload)

        return redirect(url_for("staff_profile", staff_id=new_id))

    return safe_render(
        "staff_form.html",
        staff_info=None,
        form_mode="create",
        gender_options=GENDER_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        staff_type_options=STAFF_TYPE_OPTIONS,
        **common_context(),
    )


@app.route("/staff/<int:staff_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def staff_edit(staff_id):
    staff_info = query_one("SELECT * FROM Staff WHERE staff_id = %s", (staff_id,))
    if not staff_info:
        return "Staff not found", 404

    if request.method == "POST":
        payload = get_staff_form_payload()

        execute_sql("""
            UPDATE Staff
            SET staff_name = %s,
                phone = %s,
                email = %s,
                position = %s,
                staff_type = %s,
                is_paid = %s,
                salary = %s,
                date_of_birth = %s,
                gender = %s,
                marital_status = %s,
                has_child = %s,
                number_of_child = %s,
                city = %s,
                location = %s,
                country = %s
            WHERE staff_id = %s
        """, payload + (staff_id,))

        return redirect(url_for("staff_profile", staff_id=staff_id))

    return safe_render(
        "staff_form.html",
        staff_info=staff_info,
        form_mode="edit",
        gender_options=GENDER_OPTIONS,
        marital_status_options=MARITAL_STATUS_OPTIONS,
        staff_type_options=STAFF_TYPE_OPTIONS,
        **common_context(),
    )


@app.route("/staff/<int:staff_id>")
@login_required
def staff_profile(staff_id):
    staff_info = query_one("""
        SELECT
            staff_id,
            staff_name,
            phone,
            email,
            position,
            staff_type,
            is_paid,
            salary,
            date_of_birth,
            gender,
            marital_status,
            has_child,
            number_of_child,
            city,
            location,
            country
        FROM Staff
        WHERE staff_id = %s
    """, (staff_id,))

    if not staff_info:
        return "Staff not found", 404

    event_assignments = query_all("""
        SELECT
            e.event_id,
            e.event_name,
            e.event_date,
            es.tasks,
            es.hours_worked
        FROM EventStaff es
        JOIN Event e ON es.event_id = e.event_id
        WHERE es.staff_id = %s
        ORDER BY e.event_date DESC
    """, (staff_id,))

    payment_records = query_all("""
        SELECT
            payment_id,
            invoice_id,
            payment_date,
            payment_type,
            amount,
            payment_method
        FROM Payment
        WHERE staff_id = %s
        ORDER BY payment_date DESC, payment_id DESC
    """, (staff_id,))

    matched_donors = query_all("""
        SELECT DISTINCT
            d.donor_id,
            d.donor_name,
            d.donor_type,
            d.email,
            d.phone,
            d.city
        FROM Donor d
        JOIN Staff s
          ON s.staff_id = %s
         AND (
                (d.email IS NOT NULL AND s.email IS NOT NULL
                 AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email)))
                OR
                (
                    LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.staff_name))
                    AND d.phone IS NOT NULL
                    AND s.phone IS NOT NULL
                    AND REPLACE(REPLACE(d.phone, '-', ''), ' ', '') =
                        REPLACE(REPLACE(s.phone, '-', ''), ' ', '')
                )
             )
        ORDER BY d.donor_id
    """, (staff_id,))

    staff_metrics = {
        "total_events": len(event_assignments),
        "total_payments": len(payment_records),
        "total_payment_amount": sum(float(row.get("amount", 0) or 0) for row in payment_records),
        "matched_donor_count": len(matched_donors),
    }

    return safe_render(
        "staff_profile.html",
        staff_info=staff_info,
        event_assignments=event_assignments,
        payment_records=payment_records,
        matched_donors=matched_donors,
        staff_metrics=staff_metrics,
        **common_context(),
    )

# =========================
# Event Module
# =========================

@app.route("/events")
@role_required("Administrator", "Event Coordinator")
def event_list():
    search_query = normalize_text(request.args.get("search"))

    events = query_all("""
        SELECT event_id, event_name, event_date, event_street, city, location, country
        FROM Event
        WHERE (%s = '' OR event_name LIKE %s)
        ORDER BY event_date DESC, event_id DESC
    """, (search_query, f"%{search_query}%"))

    return safe_render(
        "event_list.html",
        events=events,
        search_query=search_query,
        **common_context(),
    )


@app.route("/event/<int:event_id>")
@role_required("Administrator", "Event Coordinator")
def event_detail(event_id):
    event_info = query_one("""
        SELECT event_id, event_name, event_date, event_street, city, location, country
        FROM Event
        WHERE event_id = %s
    """, (event_id,))
    if not event_info:
        return "Event not found", 404

    event_donors = query_all("""
        SELECT
            d.donor_id,
            d.donor_name,
            d.email,
            d.phone,
            d.city,
            d.donor_type,
            ed.participation_status
        FROM EventDonor ed
        JOIN Donor d ON ed.donor_id = d.donor_id
        WHERE ed.event_id = %s
        ORDER BY d.donor_name
    """, (event_id,))

    event_staff = query_all("""
        SELECT
            s.staff_id,
            s.staff_name,
            s.email,
            s.position,
            es.tasks,
            es.hours_worked
        FROM EventStaff es
        JOIN Staff s ON es.staff_id = s.staff_id
        WHERE es.event_id = %s
        ORDER BY s.staff_name
    """, (event_id,))

    return safe_render(
        "event_detail.html",
        event_info=event_info,
        event_donors=event_donors,
        event_staff=event_staff,
        **common_context(),
    )


@app.route("/event/new", methods=["GET", "POST"])
@role_required("Administrator", "Event Coordinator")
def event_new():
    if request.method == "POST":
        new_id = execute_sql("""
            INSERT INTO Event (event_name, event_date, event_street, city, location, country)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            normalize_text(request.form.get("event_name")),
            blank_to_none(request.form.get("event_date")),
            blank_to_none(request.form.get("event_street")),
            blank_to_none(request.form.get("city")),
            blank_to_none(request.form.get("location")),
            blank_to_none(request.form.get("country")),
        ))
        return redirect(url_for("event_detail", event_id=new_id))

    return safe_render(
        "event_form.html",
        event_info=None,
        form_mode="create",
        **common_context(),
    )


@app.route("/event/<int:event_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Event Coordinator")
def event_edit(event_id):
    event_info = query_one("SELECT * FROM Event WHERE event_id = %s", (event_id,))
    if not event_info:
        return "Event not found", 404

    if request.method == "POST":
        execute_sql("""
            UPDATE Event
            SET event_name = %s,
                event_date = %s,
                event_street = %s,
                city = %s,
                location = %s,
                country = %s
            WHERE event_id = %s
        """, (
            normalize_text(request.form.get("event_name")),
            blank_to_none(request.form.get("event_date")),
            blank_to_none(request.form.get("event_street")),
            blank_to_none(request.form.get("city")),
            blank_to_none(request.form.get("location")),
            blank_to_none(request.form.get("country")),
            event_id,
        ))
        return redirect(url_for("event_detail", event_id=event_id))

    return safe_render(
        "event_form.html",
        event_info=event_info,
        form_mode="edit",
        **common_context(),
    )


@app.route("/event/<int:event_id>/assign-donor", methods=["GET", "POST"])
@role_required("Administrator", "Event Coordinator")
def event_assign_donor(event_id):
    event_info = query_one("SELECT * FROM Event WHERE event_id = %s", (event_id,))
    if not event_info:
        return "Event not found", 404

    if request.method == "POST":
        donor_id = parse_int_or_none(request.form.get("donor_id"))
        participation_status = normalize_text(request.form.get("participation_status")) or "Registered"

        if donor_id:
            execute_sql("""
                INSERT INTO EventDonor (event_id, donor_id, participation_status)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE participation_status = VALUES(participation_status)
            """, (event_id, donor_id, participation_status))

        return redirect(url_for("event_assign_donor", event_id=event_id))

    event_donors = query_all("""
        SELECT
            d.donor_id,
            d.donor_name,
            d.email,
            d.phone,
            d.city,
            ed.participation_status
        FROM EventDonor ed
        JOIN Donor d ON ed.donor_id = d.donor_id
        WHERE ed.event_id = %s
        ORDER BY d.donor_name
    """, (event_id,))

    available_donors = query_all("""
        SELECT donor_id, donor_name
        FROM Donor
        WHERE donor_id NOT IN (
            SELECT donor_id
            FROM EventDonor
            WHERE event_id = %s
        )
        ORDER BY donor_name
    """, (event_id,))

    return safe_render(
        "event_assign_donor.html",
        event_info=event_info,
        event_donors=event_donors,
        available_donors=available_donors,
        participation_status_options=["Registered", "Attended", "Cancelled"],
        **common_context(),
    )


@app.route("/event/<int:event_id>/assign-staff", methods=["GET", "POST"])
@role_required("Administrator", "Event Coordinator")
def event_assign_staff_page(event_id):
    event_info = query_one("SELECT * FROM Event WHERE event_id = %s", (event_id,))
    if not event_info:
        return "Event not found", 404

    if request.method == "POST":
        staff_id = parse_int_or_none(request.form.get("staff_id"))
        tasks = blank_to_none(request.form.get("tasks"))
        hours_worked = parse_float_or_none(request.form.get("hours_worked")) or 0.0

        if staff_id:
            execute_sql("""
                INSERT INTO EventStaff (event_id, staff_id, tasks, hours_worked)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    tasks = VALUES(tasks),
                    hours_worked = VALUES(hours_worked)
            """, (event_id, staff_id, tasks, hours_worked))

        return redirect(url_for("event_assign_staff_page", event_id=event_id))

    event_staff = query_all("""
        SELECT
            s.staff_id,
            s.staff_name,
            s.email,
            s.position,
            es.tasks,
            es.hours_worked
        FROM EventStaff es
        JOIN Staff s ON es.staff_id = s.staff_id
        WHERE es.event_id = %s
        ORDER BY s.staff_name
    """, (event_id,))

    available_staff = query_all("""
        SELECT staff_id, staff_name
        FROM Staff
        WHERE staff_id NOT IN (
            SELECT staff_id
            FROM EventStaff
            WHERE event_id = %s
        )
        ORDER BY staff_name
    """, (event_id,))

    return safe_render(
        "event_assign_staff.html",
        event_info=event_info,
        event_staff=event_staff,
        available_staff=available_staff,
        **common_context(),
    )


# =========================
# Procurement / Inventory / Gift / Invoice
# =========================

@app.route("/suppliers")
@role_required("Administrator", "Finance", "Staff")
def supplier_list():
    search_query = normalize_text(request.args.get("search")).lower()
    supplier_type = normalize_text(request.args.get("supplier_type"))

    suppliers = query_all("""
        SELECT supplier_id, supplier_name, contact_person, phone_number, email, address, supplier_type
        FROM Supplier
        WHERE (%s = ''
               OR LOWER(supplier_name) LIKE %s
               OR LOWER(COALESCE(contact_person, '')) LIKE %s
               OR LOWER(COALESCE(email, '')) LIKE %s)
          AND (%s = '' OR supplier_type = %s)
        ORDER BY supplier_name
    """, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        supplier_type,
        supplier_type,
    ))

    return safe_render(
        "supplier_list.html",
        suppliers=suppliers,
        search_query=request.args.get("search", ""),
        supplier_type=supplier_type,
        **common_context(),
    )


@app.route("/suppliers/new", methods=["GET", "POST"])
@role_required("Administrator", "Finance")
def supplier_new():
    if request.method == "POST":
        execute_sql("""
            INSERT INTO Supplier (
                supplier_name, contact_person, phone_number, email, address, supplier_type
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            normalize_text(request.form.get("supplier_name")),
            blank_to_none(request.form.get("contact_person")),
            blank_to_none(request.form.get("phone_number")),
            blank_to_none(request.form.get("email")),
            blank_to_none(request.form.get("address")),
            blank_to_none(request.form.get("supplier_type")),
        ))
        return redirect(url_for("supplier_list"))

    return safe_render(
        "supplier_form.html",
        supplier_info=None,
        form_mode="create",
        **common_context(),
    )


@app.route("/inventory")
@role_required("Administrator", "Finance", "Staff")
def inventory_list():
    search_query = normalize_text(request.args.get("search")).lower()
    supplier_id = normalize_text(request.args.get("supplier_id"))
    sort = normalize_text(request.args.get("sort"))

    order_clause = "i.item_id DESC"
    if sort == "item_name":
        order_clause = "i.item_name ASC"
    elif sort == "quantities":
        order_clause = "i.quantities ASC"
    elif sort == "unit_cost":
        order_clause = "i.unit_cost ASC"

    sql = f"""
        SELECT
            i.item_id,
            i.supplier_id,
            i.quantities,
            i.item_name,
            i.unit_cost,
            s.supplier_name
        FROM Inventory i
        JOIN Supplier s ON i.supplier_id = s.supplier_id
        WHERE (%s = ''
               OR LOWER(i.item_name) LIKE %s
               OR LOWER(COALESCE(s.supplier_name, '')) LIKE %s)
          AND (%s = '' OR CAST(i.supplier_id AS CHAR) = %s)
        ORDER BY {order_clause}
    """

    inventory_items = query_all(sql, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        supplier_id,
        supplier_id,
    ))

    suppliers = query_all("""
        SELECT supplier_id, supplier_name
        FROM Supplier
        ORDER BY supplier_name
    """)

    return safe_render(
        "inventory_list.html",
        inventory_items=inventory_items,
        suppliers=suppliers,
        search_query=request.args.get("search", ""),
        supplier_id=supplier_id,
        sort=sort,
        **common_context(),
    )


@app.route("/inventory/new", methods=["GET", "POST"])
@role_required("Administrator", "Finance", "Staff")
def inventory_new():
    suppliers = query_all("SELECT supplier_id, supplier_name FROM Supplier ORDER BY supplier_name")

    if request.method == "POST":
        execute_sql("""
            INSERT INTO Inventory (supplier_id, quantities, item_name, unit_cost)
            VALUES (%s, %s, %s, %s)
        """, (
            parse_int_or_none(request.form.get("supplier_id")),
            parse_int_or_none(request.form.get("quantities")) or 0,
            normalize_text(request.form.get("item_name")),
            parse_float_or_none(request.form.get("unit_cost")) or 0,
        ))
        return redirect(url_for("inventory_list"))

    return safe_render(
        "inventory_form.html",
        inventory_info=None,
        form_mode="create",
        suppliers=suppliers,
        **common_context(),
    )


@app.route("/inventory/<int:item_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Finance", "Staff")
def inventory_edit(item_id):
    inventory_info = query_one("SELECT * FROM Inventory WHERE item_id = %s", (item_id,))
    if not inventory_info:
        return "Inventory item not found", 404

    suppliers = query_all("SELECT supplier_id, supplier_name FROM Supplier ORDER BY supplier_name")

    if request.method == "POST":
        execute_sql("""
            UPDATE Inventory
            SET supplier_id = %s,
                quantities = %s,
                item_name = %s,
                unit_cost = %s
            WHERE item_id = %s
        """, (
            parse_int_or_none(request.form.get("supplier_id")),
            parse_int_or_none(request.form.get("quantities")) or 0,
            normalize_text(request.form.get("item_name")),
            parse_float_or_none(request.form.get("unit_cost")) or 0,
            item_id,
        ))
        return redirect(url_for("inventory_list"))

    return safe_render(
        "inventory_form.html",
        inventory_info=inventory_info,
        form_mode="edit",
        suppliers=suppliers,
        **common_context(),
    )


@app.route("/gifts")
@role_required("Administrator", "Staff")
def gift_list():
    search_query = normalize_text(request.args.get("search")).lower()
    gift_category = normalize_text(request.args.get("gift_category"))

    gifts = query_all("""
        SELECT
            g.gift_id,
            g.gift_name,
            g.gift_category,
            g.gift_value,
            g.item_id,
            COALESCE(i.item_name, '-') AS item_name,
            COALESCE(i.quantities, 0) AS inventory_quantity,
            COALESCE(s.supplier_name, '-') AS supplier_name,
            COALESCE(dist.total_distributed, 0) AS total_distributed,
            COALESCE(dist.donor_records, '') AS donor_records
        FROM Gift g
        LEFT JOIN Inventory i ON g.item_id = i.item_id
        LEFT JOIN Supplier s ON i.supplier_id = s.supplier_id
        LEFT JOIN (
            SELECT
                gd.gift_id,
                SUM(gd.quantity) AS total_distributed,
                GROUP_CONCAT(
                    CONCAT(
                        d.donor_name,
                        ' | Qty: ', gd.quantity,
                        ' | Date: ', COALESCE(CAST(gd.distribution_date AS CHAR), '-'),
                        ' | Reason: ', COALESCE(gd.distribution_reason, '-')
                    )
                    SEPARATOR '||'
                ) AS donor_records
            FROM GiftDistribution gd
            JOIN Donor d ON gd.donor_id = d.donor_id
            GROUP BY gd.gift_id
        ) dist ON g.gift_id = dist.gift_id
        WHERE (%s = ''
               OR LOWER(g.gift_name) LIKE %s
               OR LOWER(COALESCE(g.gift_category, '')) LIKE %s
               OR LOWER(COALESCE(i.item_name, '')) LIKE %s
               OR LOWER(COALESCE(s.supplier_name, '')) LIKE %s)
          AND (%s = '' OR g.gift_category = %s)
        ORDER BY g.gift_id DESC
    """, (
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        gift_category,
        gift_category,
    ))

    return safe_render(
        "gift_list.html",
        gifts=gifts,
        search_query=request.args.get("search", ""),
        gift_category=gift_category,
        **common_context(),
    )


@app.route("/gifts/new", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def gift_new():
    inventory_items = query_all("""
        SELECT
            i.item_id,
            i.item_name,
            i.quantities,
            i.unit_cost,
            COALESCE(s.supplier_name, '-') AS supplier_name
        FROM Inventory i
        LEFT JOIN Supplier s ON i.supplier_id = s.supplier_id
        ORDER BY i.item_name
    """)

    suppliers = query_all("""
        SELECT supplier_id, supplier_name
        FROM Supplier
        ORDER BY supplier_name
    """)

    if request.method == "POST":
        item_id = parse_int_or_none(request.form.get("item_id"))
        supplier_id = parse_int_or_none(request.form.get("supplier_id"))
        gift_name = normalize_text(request.form.get("gift_name"))
        gift_category = blank_to_none(request.form.get("gift_category"))
        gift_value = parse_float_or_none(request.form.get("gift_value")) or 0
        quantity_added = parse_int_or_none(request.form.get("quantity_added")) or 0

        if not gift_name:
            return safe_render(
                "gift_form.html",
                inventory_items=inventory_items,
                suppliers=suppliers,
                form_mode="create",
                error="Please enter a gift name.",
                **common_context(),
            )

        if quantity_added < 0:
            quantity_added = 0

        def tx(cursor):
            final_item_id = item_id

            if final_item_id:
                cursor.execute("""
                    UPDATE Inventory
                    SET quantities = quantities + %s
                    WHERE item_id = %s
                """, (quantity_added, final_item_id))
            else:
                if not supplier_id:
                    raise ValueError("Please select a supplier when creating a new inventory item automatically.")

                cursor.execute("""
                    INSERT INTO Inventory (supplier_id, quantities, item_name, unit_cost)
                    VALUES (%s, %s, %s, %s)
                """, (
                    supplier_id,
                    quantity_added,
                    gift_name,
                    gift_value,
                ))
                final_item_id = cursor.lastrowid

            cursor.execute("""
                SELECT gift_id
                FROM Gift
                WHERE item_id = %s
                  AND LOWER(TRIM(gift_name)) = LOWER(TRIM(%s))
                LIMIT 1
            """, (final_item_id, gift_name))

            existing_gift = cursor.fetchone()

            if existing_gift:
                cursor.execute("""
                    UPDATE Gift
                    SET gift_category = %s,
                        gift_value = %s
                    WHERE gift_id = %s
                """, (
                    gift_category,
                    gift_value,
                    existing_gift["gift_id"],
                ))
            else:
                cursor.execute("""
                    INSERT INTO Gift (item_id, gift_name, gift_category, gift_value)
                    VALUES (%s, %s, %s, %s)
                """, (
                    final_item_id,
                    gift_name,
                    gift_category,
                    gift_value,
                ))

        try:
            execute_transaction(tx)
        except Exception as e:
            return safe_render(
                "gift_form.html",
                inventory_items=inventory_items,
                suppliers=suppliers,
                form_mode="create",
                error=str(e),
                **common_context(),
            )

        return redirect(url_for("gift_list"))

    return safe_render(
        "gift_form.html",
        inventory_items=inventory_items,
        suppliers=suppliers,
        form_mode="create",
        **common_context(),
    )

@app.route("/gifts/distribute", methods=["GET", "POST"])
@role_required("Administrator", "Staff")
def gift_distribute():
    gifts = query_all("""
        SELECT
            g.gift_id,
            g.gift_name,
            g.gift_category,
            i.quantities AS inventory_quantity
        FROM Gift g
        JOIN Inventory i ON g.item_id = i.item_id
        ORDER BY g.gift_name
    """)

    donors = query_all("""
        SELECT donor_id, donor_name, email
        FROM Donor
        ORDER BY donor_name
    """)

    if request.method == "POST":
        gift_id = parse_int_or_none(request.form.get("gift_id"))
        donor_id = parse_int_or_none(request.form.get("donor_id"))
        quantity = parse_int_or_none(request.form.get("quantity")) or 1
        distribution_date = blank_to_none(request.form.get("distribution_date"))
        distribution_reason = blank_to_none(request.form.get("distribution_reason"))

        if not gift_id or not donor_id:
            return safe_render(
                "gift_distribution_form.html",
                gifts=gifts,
                donors=donors,
                error="Please select both gift and donor.",
                **common_context(),
            )

        if quantity <= 0:
            quantity = 1

        def tx(cursor):
            cursor.execute("""
                SELECT
                    g.gift_id,
                    g.item_id,
                    i.quantities
                FROM Gift g
                JOIN Inventory i ON g.item_id = i.item_id
                WHERE g.gift_id = %s
                FOR UPDATE
            """, (gift_id,))
            gift_row = cursor.fetchone()

            if not gift_row:
                raise ValueError("Gift not found.")

            if int(gift_row["quantities"] or 0) < quantity:
                raise ValueError("Not enough inventory quantity for this gift distribution.")

            cursor.execute("""
                INSERT INTO GiftDistribution (
                    gift_id,
                    donor_id,
                    receivable_id,
                    distribution_date,
                    quantity,
                    distribution_reason
                ) VALUES (%s, %s, NULL, %s, %s, %s)
            """, (
                gift_id,
                donor_id,
                distribution_date,
                quantity,
                distribution_reason,
            ))

            cursor.execute("""
                UPDATE Inventory
                SET quantities = quantities - %s
                WHERE item_id = %s
            """, (
                quantity,
                gift_row["item_id"],
            ))

        try:
            execute_transaction(tx)
        except Exception as e:
            return safe_render(
                "gift_distribution_form.html",
                gifts=gifts,
                donors=donors,
                error=str(e),
                **common_context(),
            )

        return redirect(url_for("gift_list"))

    return safe_render(
        "gift_distribution_form.html",
        gifts=gifts,
        donors=donors,
        **common_context(),
    )
@app.route("/gifts/<int:gift_id>")
@role_required("Administrator", "Staff")
def gift_detail(gift_id):
    gift_info = query_one("""
        SELECT
            g.gift_id,
            g.gift_name,
            g.gift_category,
            g.gift_value,
            g.item_id,
            COALESCE(i.item_name, '-') AS item_name,
            COALESCE(i.quantities, 0) AS inventory_quantity,
            COALESCE(s.supplier_name, '-') AS supplier_name
        FROM Gift g
        LEFT JOIN Inventory i ON g.item_id = i.item_id
        LEFT JOIN Supplier s ON i.supplier_id = s.supplier_id
        WHERE g.gift_id = %s
    """, (gift_id,))

    if not gift_info:
        return "Gift not found", 404

    distribution_records = query_all("""
        SELECT
            gd.distribution_id,
            gd.distribution_date,
            gd.quantity,
            gd.distribution_reason,
            d.donor_id,
            d.donor_name,
            d.email,
            d.phone,
            d.city,
            d.donor_type
        FROM GiftDistribution gd
        JOIN Donor d ON gd.donor_id = d.donor_id
        WHERE gd.gift_id = %s
        ORDER BY gd.distribution_date DESC, gd.distribution_id DESC
    """, (gift_id,))

    total_distributed = sum(int(r.get("quantity", 0) or 0) for r in distribution_records)

    return safe_render(
        "gift_detail.html",
        gift_info=gift_info,
        distribution_records=distribution_records,
        total_distributed=total_distributed,
        **common_context(),
    )
@app.route("/gifts/<int:gift_id>/delete", methods=["POST"])
@role_required("Administrator", "Staff")
def gift_delete(gift_id):
    def tx(cursor):
        # Delete gift distribution records first
        cursor.execute("""
            DELETE FROM GiftDistribution
            WHERE gift_id = %s
        """, (gift_id,))

        # Delete the gift record
        cursor.execute("""
            DELETE FROM Gift
            WHERE gift_id = %s
        """, (gift_id,))

    execute_transaction(tx)
    return redirect(url_for("gift_list"))


@app.route("/invoices")
@role_required("Administrator", "Finance")
def invoice_list():
    status_filter = normalize_text(request.args.get("bill_status"))
    search_query = normalize_text(request.args.get("search")).lower()

    invoices = query_all("""
        SELECT
            i.invoice_id,
            i.item_id,
            i.event_id,
            i.issue_date,
            i.due_date,
            i.quantity_billed,
            i.bill_amount,
            i.bill_type,
            i.bill_status,
            inv.item_name,
            COALESCE(e.event_name, '-') AS event_name
        FROM Invoice i
        JOIN Inventory inv ON i.item_id = inv.item_id
        LEFT JOIN Event e ON i.event_id = e.event_id
        WHERE (%s = '' OR i.bill_status = %s)
          AND (%s = ''
               OR CAST(i.invoice_id AS CHAR) LIKE %s
               OR LOWER(COALESCE(inv.item_name, '')) LIKE %s
               OR LOWER(COALESCE(i.bill_type, '')) LIKE %s
               OR LOWER(COALESCE(e.event_name, '')) LIKE %s)
        ORDER BY i.invoice_id DESC
    """, (
        status_filter,
        status_filter,
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
    ))

    return safe_render(
        "invoice_list.html",
        invoices=invoices,
        status_filter=status_filter,
        search_query=request.args.get("search", ""),
        **common_context(),
    )


@app.route("/invoice/<int:invoice_id>")
@role_required("Administrator", "Finance")
def invoice_detail(invoice_id):
    invoice_info = query_one("""
        SELECT *
        FROM Invoice
        WHERE invoice_id = %s
    """, (invoice_id,))
    if not invoice_info:
        return "Invoice not found", 404

    item_info = query_one("""
        SELECT item_id, supplier_id, quantities, item_name, unit_cost
        FROM Inventory
        WHERE item_id = %s
    """, (invoice_info["item_id"],))

    event_info = None
    if invoice_info["event_id"]:
        event_info = query_one("""
            SELECT event_id, event_name, city, location, country
            FROM Event
            WHERE event_id = %s
        """, (invoice_info["event_id"],))

    related_payments = query_all("""
        SELECT payment_id, payment_date, payment_type, amount, payment_method
        FROM Payment
        WHERE invoice_id = %s
        ORDER BY payment_date DESC, payment_id DESC
    """, (invoice_id,))

    return safe_render(
        "invoice_detail.html",
        invoice_info=invoice_info,
        item_info=item_info,
        event_info=event_info,
        related_payments=related_payments,
        **common_context(),
    )


@app.route("/invoice/new", methods=["GET", "POST"])
@role_required("Administrator", "Finance")
def invoice_new():
    inventory_items = query_all("SELECT item_id, item_name FROM Inventory ORDER BY item_name")
    events = query_all("SELECT event_id, event_name FROM Event ORDER BY event_name")

    if request.method == "POST":
        new_id = execute_sql("""
            INSERT INTO Invoice (
                item_id, event_id, issue_date, due_date,
                quantity_billed, bill_amount, bill_type, bill_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            parse_int_or_none(request.form.get("item_id")),
            parse_int_or_none(request.form.get("event_id")),
            blank_to_none(request.form.get("issue_date")),
            blank_to_none(request.form.get("due_date")),
            parse_int_or_none(request.form.get("quantity_billed")) or 0,
            parse_float_or_none(request.form.get("bill_amount")) or 0,
            blank_to_none(request.form.get("bill_type")),
            blank_to_none(request.form.get("bill_status")),
        ))
        return redirect(url_for("invoice_detail", invoice_id=new_id))

    return safe_render(
        "invoice_form.html",
        invoice_info=None,
        form_mode="create",
        inventory_items=inventory_items,
        events=events,
        bill_status_options=["Paid", "Pending", "Overdue"],
        **common_context(),
    )


@app.route("/invoice/<int:invoice_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Finance")
def invoice_edit(invoice_id):
    invoice_info = query_one("SELECT * FROM Invoice WHERE invoice_id = %s", (invoice_id,))
    if not invoice_info:
        return "Invoice not found", 404

    inventory_items = query_all("SELECT item_id, item_name FROM Inventory ORDER BY item_name")
    events = query_all("SELECT event_id, event_name FROM Event ORDER BY event_name")

    if request.method == "POST":
        execute_sql("""
            UPDATE Invoice
            SET item_id = %s,
                event_id = %s,
                issue_date = %s,
                due_date = %s,
                quantity_billed = %s,
                bill_amount = %s,
                bill_type = %s,
                bill_status = %s
            WHERE invoice_id = %s
        """, (
            parse_int_or_none(request.form.get("item_id")),
            parse_int_or_none(request.form.get("event_id")),
            blank_to_none(request.form.get("issue_date")),
            blank_to_none(request.form.get("due_date")),
            parse_int_or_none(request.form.get("quantity_billed")) or 0,
            parse_float_or_none(request.form.get("bill_amount")) or 0,
            blank_to_none(request.form.get("bill_type")),
            blank_to_none(request.form.get("bill_status")),
            invoice_id,
        ))
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    return safe_render(
        "invoice_form.html",
        invoice_info=invoice_info,
        form_mode="edit",
        inventory_items=inventory_items,
        events=events,
        bill_status_options=["Paid", "Pending", "Overdue"],
        **common_context(),
    )


# =========================
# Payment + Reports
# =========================

@app.route("/payments")
@role_required("Administrator", "Finance")
def payment_list():
    payment_type_filter = normalize_text(request.args.get("payment_type"))
    search_query = normalize_text(request.args.get("search")).lower()

    payments = query_all("""
        SELECT
            p.payment_id,
            p.invoice_id,
            p.staff_id,
            p.payment_date,
            p.payment_type,
            p.amount,
            p.payment_method,
            COALESCE(s.staff_name, '-') AS staff_name,
            CASE
                WHEN p.invoice_id IS NULL THEN '-'
                ELSE CONCAT('Invoice #', p.invoice_id)
            END AS invoice_label
        FROM Payment p
        LEFT JOIN Staff s ON p.staff_id = s.staff_id
        WHERE (%s = '' OR p.payment_type = %s)
          AND (%s = ''
               OR CAST(p.payment_id AS CHAR) LIKE %s
               OR LOWER(COALESCE(p.payment_type, '')) LIKE %s
               OR LOWER(COALESCE(p.payment_method, '')) LIKE %s
               OR LOWER(COALESCE(s.staff_name, '')) LIKE %s
               OR CAST(COALESCE(p.invoice_id, '') AS CHAR) LIKE %s)
        ORDER BY p.payment_id DESC
    """, (
        payment_type_filter,
        payment_type_filter,
        search_query,
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
        f"%{search_query}%",
    ))

    return safe_render(
        "payment_list.html",
        payments=payments,
        payment_type_filter=payment_type_filter,
        search_query=request.args.get("search", ""),
        **common_context(),
    )


@app.route("/payment/new", methods=["GET", "POST"])
@role_required("Administrator", "Finance")
def payment_new():
    invoice_list = query_all("""
        SELECT invoice_id
        FROM Invoice
        ORDER BY invoice_id DESC
    """)
    staff_list = query_all("""
        SELECT staff_id, staff_name
        FROM Staff
        ORDER BY staff_name
    """)

    if request.method == "POST":
        execute_sql("""
            INSERT INTO Payment (
                invoice_id, staff_id, payment_date, payment_type, amount, payment_method
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            parse_int_or_none(request.form.get("invoice_id")),
            parse_int_or_none(request.form.get("staff_id")),
            blank_to_none(request.form.get("payment_date")),
            blank_to_none(request.form.get("payment_type")),
            parse_float_or_none(request.form.get("amount")) or 0,
            blank_to_none(request.form.get("payment_method")),
        ))
        return redirect(url_for("payment_list"))

    return safe_render(
        "payment_form.html",
        payment_info=None,
        form_mode="create",
        invoice_list=invoice_list,
        staff_list=staff_list,
        payment_type_options=["Salary", "Invoice Settlement", "Volunteer Reimbursement", "Reimbursement"],
        payment_method_options=["Bank Transfer", "Credit Card", "Cash", "PayPal", "Online Banking"],
        **common_context(),
    )


@app.route("/payment/<int:payment_id>/edit", methods=["GET", "POST"])
@role_required("Administrator", "Finance")
def payment_edit(payment_id):
    payment_info = query_one("SELECT * FROM Payment WHERE payment_id = %s", (payment_id,))
    if not payment_info:
        return "Payment not found", 404

    invoice_list = query_all("""
        SELECT invoice_id
        FROM Invoice
        ORDER BY invoice_id DESC
    """)
    staff_list = query_all("""
        SELECT staff_id, staff_name
        FROM Staff
        ORDER BY staff_name
    """)

    if request.method == "POST":
        execute_sql("""
            UPDATE Payment
            SET invoice_id = %s,
                staff_id = %s,
                payment_date = %s,
                payment_type = %s,
                amount = %s,
                payment_method = %s
            WHERE payment_id = %s
        """, (
            parse_int_or_none(request.form.get("invoice_id")),
            parse_int_or_none(request.form.get("staff_id")),
            blank_to_none(request.form.get("payment_date")),
            blank_to_none(request.form.get("payment_type")),
            parse_float_or_none(request.form.get("amount")) or 0,
            blank_to_none(request.form.get("payment_method")),
            payment_id,
        ))
        return redirect(url_for("payment_list"))

    return safe_render(
        "payment_form.html",
        payment_info=payment_info,
        form_mode="edit",
        invoice_list=invoice_list,
        staff_list=staff_list,
        payment_type_options=["Salary", "Invoice Settlement", "Volunteer Reimbursement", "Reimbursement"],
        payment_method_options=["Bank Transfer", "Credit Card", "Cash", "PayPal", "Online Banking"],
        **common_context(),
    )


@app.route("/reports")
@role_required("Administrator", "Finance", "Event Coordinator")
def reports():
    donor_summary = query_one("""
        SELECT
            COUNT(*) AS total_donors,
            SUM(CASE WHEN gender = 'Female' AND marital_status = 'Married' THEN 1 ELSE 0 END) AS married_female_donors,
            SUM(CASE WHEN has_child = 1 THEN 1 ELSE 0 END) AS donors_with_children,
            ROUND(AVG(COALESCE(number_of_child, 0)), 2) AS avg_children_per_donor
        FROM Donor
    """) or {}

    invoice_summary = query_one("""
        SELECT
            COUNT(*) AS total_invoices,
            SUM(CASE WHEN bill_status = 'Pending' THEN 1 ELSE 0 END) AS pending_invoices,
            COALESCE(SUM(bill_amount), 0) AS total_invoice_amount
        FROM Invoice
    """) or {}

    payment_summary = query_one("""
        SELECT
            COUNT(*) AS total_payments,
            COALESCE(SUM(amount), 0) AS total_payment_amount,
            COALESCE(SUM(CASE WHEN invoice_id IS NOT NULL THEN amount ELSE 0 END), 0) AS invoice_payment_amount,
            COALESCE(SUM(CASE WHEN staff_id IS NOT NULL THEN amount ELSE 0 END), 0) AS payroll_payment_amount
        FROM Payment
    """) or {}

    receivable_summary = query_one("""
        SELECT
            COUNT(*) AS total_receivables,
            COALESCE(SUM(amount), 0) AS total_receivable_amount
        FROM Receivable
    """) or {}

    staff_overlap = query_one("""
        SELECT COUNT(DISTINCT d.donor_id) AS overlap_count
        FROM Donor d
        JOIN Staff s
          ON (
                (d.email IS NOT NULL AND s.email IS NOT NULL
                 AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email)))
                OR
                (
                    LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.staff_name))
                    AND d.phone IS NOT NULL
                    AND s.phone IS NOT NULL
                    AND REPLACE(REPLACE(d.phone, '-', ''), ' ', '') =
                        REPLACE(REPLACE(s.phone, '-', ''), ' ', '')
                )
             )
    """) or {}

    supplier_overlap = query_one("""
        SELECT COUNT(DISTINCT d.donor_id) AS overlap_count
        FROM Donor d
        JOIN Supplier s
          ON (
                (d.email IS NOT NULL AND s.email IS NOT NULL
                 AND LOWER(TRIM(d.email)) = LOWER(TRIM(s.email)))
                OR
                (
                    LOWER(TRIM(d.donor_name)) = LOWER(TRIM(s.contact_person))
                    AND d.phone IS NOT NULL
                    AND s.phone_number IS NOT NULL
                    AND REPLACE(REPLACE(d.phone, '-', ''), ' ', '') =
                        REPLACE(REPLACE(s.phone_number, '-', ''), ' ', '')
                )
             )
    """) or {}

    total_receivable_amount = float(receivable_summary.get("total_receivable_amount", 0) or 0)
    total_invoice_amount = float(invoice_summary.get("total_invoice_amount", 0) or 0)
    total_payment_amount = float(payment_summary.get("total_payment_amount", 0) or 0)
    invoice_payment_amount = float(payment_summary.get("invoice_payment_amount", 0) or 0)
    payroll_payment_amount = float(payment_summary.get("payroll_payment_amount", 0) or 0)

    estimated_transfer_amount = round(total_receivable_amount * 0.90, 2)
    retained_amount = round(total_receivable_amount * 0.10, 2)
    outstanding_invoice_amount = round(max(total_invoice_amount - invoice_payment_amount, 0), 2)
    sustainability_gap = round(retained_amount - total_payment_amount, 2)

    report_summary = {
        "total_donors": int(donor_summary.get("total_donors", 0) or 0),
        "married_female_donors": int(donor_summary.get("married_female_donors", 0) or 0),
        "donors_with_children": int(donor_summary.get("donors_with_children", 0) or 0),
        "avg_children_per_donor": float(donor_summary.get("avg_children_per_donor", 0) or 0),

        "staff_overlap_count": int(staff_overlap.get("overlap_count", 0) or 0),
        "supplier_overlap_count": int(supplier_overlap.get("overlap_count", 0) or 0),

        "total_invoices": int(invoice_summary.get("total_invoices", 0) or 0),
        "pending_invoices": int(invoice_summary.get("pending_invoices", 0) or 0),
        "total_invoice_amount": total_invoice_amount,

        "total_payments": int(payment_summary.get("total_payments", 0) or 0),
        "total_payment_amount": total_payment_amount,
        "invoice_payment_amount": invoice_payment_amount,
        "payroll_payment_amount": payroll_payment_amount,

        "total_receivables": int(receivable_summary.get("total_receivables", 0) or 0),
        "total_receivable_amount": total_receivable_amount,

        "estimated_transfer_amount": estimated_transfer_amount,
        "retained_amount": retained_amount,
        "outstanding_invoice_amount": outstanding_invoice_amount,
        "sustainability_gap": sustainability_gap,
    }

    donor_type_stats = query_all("""
        SELECT donor_type AS label, COUNT(*) AS value
        FROM Donor
        WHERE donor_type IS NOT NULL AND donor_type <> ''
        GROUP BY donor_type
        ORDER BY COUNT(*) DESC
    """)

    gender_stats = query_all("""
        SELECT gender AS label, COUNT(*) AS value
        FROM Donor
        WHERE gender IS NOT NULL AND gender <> ''
        GROUP BY gender
        ORDER BY COUNT(*) DESC
    """)

    marital_status_stats = query_all("""
        SELECT marital_status AS label, COUNT(*) AS value
        FROM Donor
        WHERE marital_status IS NOT NULL AND marital_status <> ''
        GROUP BY marital_status
        ORDER BY COUNT(*) DESC
    """)

    children_stats = query_all("""
        SELECT
            CASE WHEN has_child = 1 THEN 'Has Children' ELSE 'No Children' END AS label,
            COUNT(*) AS value
        FROM Donor
        GROUP BY has_child
        ORDER BY value DESC
    """)

    monthly_receivable_stats = query_all("""
    SELECT
        DATE_FORMAT(donation_date, '%%Y-%%m') AS label,
        COALESCE(SUM(amount), 0) AS value
    FROM Receivable
    GROUP BY DATE_FORMAT(donation_date, '%%Y-%%m')
    ORDER BY label
    """)

    invoice_status_stats = query_all("""
        SELECT
            COALESCE(NULLIF(bill_status, ''), 'Unknown') AS label,
            COUNT(*) AS value
        FROM Invoice
        GROUP BY COALESCE(NULLIF(bill_status, ''), 'Unknown')
        ORDER BY value DESC
    """)

    finance_comparison_stats = [
        {"label": "Donation", "value": total_receivable_amount},
        {"label": "Invoice", "value": total_invoice_amount},
        {"label": "Payment", "value": total_payment_amount},
        {"label": "Retained 10%", "value": retained_amount},
    ]

    overlap_stats = [
        {"label": "Staff Also Donors", "value": report_summary["staff_overlap_count"]},
        {"label": "Supplier Contacts Also Donors", "value": report_summary["supplier_overlap_count"]},
    ]

    return safe_render(
        "reports.html",
        report_summary=report_summary,
        donor_type_stats=donor_type_stats,
        gender_stats=gender_stats,
        marital_status_stats=marital_status_stats,
        children_stats=children_stats,
        monthly_receivable_stats=monthly_receivable_stats,
        invoice_status_stats=invoice_status_stats,
        finance_comparison_stats=finance_comparison_stats,
        overlap_stats=overlap_stats,
        **common_context(),
    )

# =========================
# AI Assistant / Text-to-SQL Module (终极版)
# =========================

def generate_sql_from_deepseek(question, history=None):
    """Use DeepSeek to convert natural language into exactly one valid SQL SELECT query."""
    if history is None:
        history = []

    client = deepseek_client()

    system_prompt = """
You are an expert Text-to-SQL translator for a TiDB/MySQL database.

Your job:
- Convert the user's question into exactly ONE valid SQL SELECT query.
- Use ONLY the schema below.
- Return ONLY the SQL query text.
- No explanation.
- No markdown.
- No code fences.
- No prefix like SQL:
- No semicolon.
- If the request is outside the database scope, return exactly: UNSUPPORTED_QUERY

REAL DATABASE SCHEMA:

Donor(
    donor_id,
    donor_name,
    gender,
    marital_status,
    has_child,
    number_of_child,
    feedback,
    email,
    phone,
    date_of_birth,
    city,
    donor_type,
    location,
    country
)

Staff(
    staff_id,
    staff_name,
    phone,
    email,
    position,
    staff_type,
    is_paid,
    salary,
    date_of_birth,
    gender,
    marital_status,
    has_child,
    number_of_child,
    city,
    location,
    country
)

Receivable(
    receivable_id,
    donor_id,
    donation_date,
    amount,
    donation_frequency,
    payment_method,
    receipt_number,
    receipt_issue_date
)

Event(
    event_id,
    event_name,
    event_date,
    event_street,
    city,
    location,
    country
)

EventDonor(
    event_id,
    donor_id,
    participation_status
)

EventStaff(
    event_id,
    staff_id,
    tasks,
    hours_worked
)

Supplier(
    supplier_id,
    supplier_name,
    contact_person,
    phone_number,
    email,
    address,
    supplier_type
)

Inventory(
    item_id,
    supplier_id,
    quantities,
    item_name,
    unit_cost
)

Gift(
    gift_id,
    item_id,
    gift_name,
    gift_category,
    gift_value
)

GiftDistribution(
    distribution_id,
    donor_id,
    receivable_id,
    gift_id,
    distribution_date,
    quantity,
    distribution_reason
)

Invoice(
    invoice_id,
    item_id,
    event_id,
    issue_date,
    due_date,
    quantity_billed,
    bill_amount,
    bill_type,
    bill_status
)

Payment(
    payment_id,
    invoice_id,
    staff_id,
    payment_date,
    payment_type,
    amount,
    payment_method
)

STRICT RULES:
1. Output exactly ONE SQL SELECT query or UNSUPPORTED_QUERY.
2. Never output INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE.
3. Never output two queries.
4. Never output natural-language explanation.
5. Use exact table names and exact column names from the schema.
6. Prefer readable result columns, for example donor_name, email, phone, city, event_name.
7. For fuzzy text search, prefer:
   INSTR(LOWER(column_name), LOWER('keyword')) > 0
8. For "how many", use COUNT(*) or COUNT(DISTINCT ...).
9. For "total" money, use SUM(...).
10. For "who attended at least one event", use EventDonor.
11. For "received gifts", use GiftDistribution.
12. For "joined an event", use EventDonor JOIN Event.
13. For "worked on an event", use EventStaff JOIN Staff JOIN Event.
14. If the question asks for contact details, include email and phone when those fields exist.
15. If the user asks a follow-up like "list them", "show them", "their email", "those donors", "which of them", or "tell me who attended at least one event", you MUST use the conversation history to infer who "them" refers to.
16. When the follow-up refers to a previously filtered donor group, preserve that earlier filter in the new SQL.
17. When a follow-up asks for extra information about the same people, keep the same WHERE condition and expand the SELECT columns.
18. When a follow-up asks for whether they attended any event, use:
   CASE WHEN EXISTS (
       SELECT 1 FROM EventDonor ed WHERE ed.donor_id = d.donor_id
   ) THEN 'Yes' ELSE 'No' END
19. If a request contains multiple related requirements, combine them into ONE SELECT query whenever possible.
20. Default to LIMIT 100 for non-aggregate result sets if no smaller limit is clearly implied.

IMPORTANT FOLLOW-UP EXAMPLES:

Conversation:
User: how many donors are single?
Assistant meaning: filter is donor.marital_status = 'Single'
User: list them
SQL:
SELECT donor_id, donor_name, gender, city
FROM Donor
WHERE LOWER(COALESCE(marital_status, '')) = 'single'
ORDER BY donor_name
LIMIT 100

Conversation:
User: how many donors are single?
Assistant meaning: filter is donor.marital_status = 'Single'
User: list them
Assistant meaning: same single-donor group
User: give me their email and phone number
SQL:
SELECT donor_id, donor_name, email, phone, city
FROM Donor
WHERE LOWER(COALESCE(marital_status, '')) = 'single'
ORDER BY donor_name
LIMIT 100

Conversation:
User: how many donors are single?
Assistant meaning: filter is donor.marital_status = 'Single'
User: list them
Assistant meaning: same single-donor group
User: give me their email and phone number. And tell me who attended at least one event.
SQL:
SELECT
    d.donor_id,
    d.donor_name,
    d.email,
    d.phone,
    d.city,
    CASE
        WHEN EXISTS (
            SELECT 1
            FROM EventDonor ed
            WHERE ed.donor_id = d.donor_id
        ) THEN 'Yes'
        ELSE 'No'
    END AS attended_at_least_one_event
FROM Donor d
WHERE LOWER(COALESCE(d.marital_status, '')) = 'single'
ORDER BY d.donor_name
LIMIT 100

Conversation:
User: list female donors
Assistant meaning: donor.gender = 'Female'
User: which of them have children?
SQL:
SELECT donor_id, donor_name, email, phone, city, number_of_child
FROM Donor
WHERE LOWER(COALESCE(gender, '')) = 'female'
  AND has_child = 1
ORDER BY donor_name
LIMIT 100

GENERAL EXAMPLES:

User: How many donors are there?
SQL:
SELECT COUNT(*) AS total_donors
FROM Donor

User: List all female donors
SQL:
SELECT donor_id, donor_name, gender, email, phone, city
FROM Donor
WHERE LOWER(COALESCE(gender, '')) = 'female'
ORDER BY donor_name
LIMIT 100

User: Which donors have children?
SQL:
SELECT donor_id, donor_name, number_of_child, city
FROM Donor
WHERE has_child = 1
ORDER BY donor_name
LIMIT 100

User: Who donated the most money?
SQL:
SELECT d.donor_id, d.donor_name, SUM(r.amount) AS total_donated
FROM Donor d
JOIN Receivable r ON d.donor_id = r.donor_id
GROUP BY d.donor_id, d.donor_name
ORDER BY total_donated DESC
LIMIT 1

User: How many donors received gifts?
SQL:
SELECT COUNT(DISTINCT donor_id) AS donor_count
FROM GiftDistribution

User: List donors who received gifts
SQL:
SELECT DISTINCT d.donor_id, d.donor_name, d.email, d.phone, d.city
FROM GiftDistribution gd
JOIN Donor d ON gd.donor_id = d.donor_id
ORDER BY d.donor_name
LIMIT 100

User: Show donors who joined an event after 2026-01-01
SQL:
SELECT DISTINCT d.donor_id, d.donor_name, e.event_name, e.event_date
FROM EventDonor ed
JOIN Donor d ON ed.donor_id = d.donor_id
JOIN Event e ON ed.event_id = e.event_id
WHERE e.event_date > '2026-01-01'
ORDER BY e.event_date DESC, d.donor_name
LIMIT 100

User: What inventory items contain bookmark?
SQL:
SELECT item_id, item_name, quantities, unit_cost
FROM Inventory
WHERE INSTR(LOWER(item_name), 'bookmark') > 0
ORDER BY item_name
LIMIT 100

User: Which supplier provides notebooks?
SQL:
SELECT DISTINCT s.supplier_id, s.supplier_name, s.contact_person, s.email
FROM Inventory i
JOIN Supplier s ON i.supplier_id = s.supplier_id
WHERE INSTR(LOWER(i.item_name), 'notebook') > 0
ORDER BY s.supplier_name
LIMIT 100

User: What events are in Seattle?
SQL:
SELECT event_id, event_name, event_date, event_street, city, location, country
FROM Event
WHERE INSTR(LOWER(COALESCE(city, '')), 'seattle') > 0
   OR INSTR(LOWER(COALESCE(location, '')), 'seattle') > 0
   OR INSTR(LOWER(COALESCE(country, '')), 'seattle') > 0
ORDER BY event_date DESC
LIMIT 100

User: Which staff worked on charity gala?
SQL:
SELECT DISTINCT s.staff_id, s.staff_name, s.position, es.tasks, es.hours_worked
FROM EventStaff es
JOIN Staff s ON es.staff_id = s.staff_id
JOIN Event e ON es.event_id = e.event_id
WHERE INSTR(LOWER(e.event_name), 'charity gala') > 0
ORDER BY s.staff_name
LIMIT 100

User: Show pending invoices
SQL:
SELECT invoice_id, item_id, event_id, due_date, bill_amount, bill_status
FROM Invoice
WHERE LOWER(COALESCE(bill_status, '')) = 'pending'
ORDER BY due_date ASC
LIMIT 100

User: Show payroll payments
SQL:
SELECT payment_id, staff_id, payment_date, payment_type, amount, payment_method
FROM Payment
WHERE INSTR(LOWER(COALESCE(payment_type, '')), 'salary') > 0
ORDER BY payment_date DESC
LIMIT 100

User: What is Ethan's favorite food?
SQL:
UNSUPPORTED_QUERY
"""

    messages = [{"role": "system", "content": system_prompt}]

    # Keep a short recent history window, because follow-up reference resolution matters here
    for msg in history[-8:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": str(content)})

    messages.append({"role": "user", "content": question})

    response = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=messages,
        max_tokens=350,
        temperature=0.0
    )

    sql = response.choices[0].message.content.strip()

    if sql.lower().startswith("```sql"):
        sql = sql[6:]
    elif sql.startswith("```"):
        sql = sql[3:]
    if sql.endswith("```"):
        sql = sql[:-3]

    sql = sql.strip()

    # Extra cleanup for accidental prefixes
    if sql.upper().startswith("SQL:"):
        sql = sql[4:].strip()

    return sql


def execute_ai_sql(question, history=None):
    """Execute AI-generated SQL and return the result in English only."""
    try:
        sql = generate_sql_from_deepseek(question, history)

        if sql == "UNSUPPORTED_QUERY":
            return {
                "answer": "Sorry, this question is outside the scope of the current database.",
                "sql": "UNSUPPORTED_QUERY",
                "rows": [],
                "error": False
            }

        if ";" in sql:
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            if statements:
                sql = statements[0]

        if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
            return {
                "answer": "Security block: the AI generated a non-SELECT statement, so it was not executed.",
                "sql": sql,
                "rows": [],
                "error": True
            }

        forbidden_patterns = [
            r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b",
            r"\bALTER\b", r"\bTRUNCATE\b", r"\bCREATE\b", r"\bREPLACE\b"
        ]
        for pattern in forbidden_patterns:
            if re.search(pattern, sql, re.IGNORECASE):
                return {
                    "answer": "Security block: a potentially dangerous SQL statement was detected and has been prevented from running.",
                    "sql": sql,
                    "rows": [],
                    "error": True
                }

        # Add a row limit for detail queries without LIMIT
        if not re.search(r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE):
            if not re.search(r"\bCOUNT\s*\(", sql, re.IGNORECASE) and not re.search(r"\bSUM\s*\(", sql, re.IGNORECASE):
                sql = sql.rstrip() + " LIMIT 100"

        rows = query_all(sql)
        clean_rows = serialize_rows(rows)

        client = deepseek_client()
        nl_prompt = f"""
You are a helpful database assistant.

User question:
{question}

SQL used:
{sql}

Database result rows:
{json.dumps(clean_rows, ensure_ascii=False)}

Instructions:
1. Answer ONLY based on the database result above.
2. Always answer in English.
3. If rows are empty, clearly say that no matching records were found.
4. If the user asked to list people/items/events, present them clearly in a readable English list.
5. If there is only one aggregate result like a count or total, answer directly with that number in English.
6. Do not mention raw JSON.
7. Do not mention internal prompt rules.
"""

        response = client.chat.completions.create(
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            messages=[{"role": "user", "content": nl_prompt}],
            max_tokens=500,
            temperature=0.2
        )

        friendly_answer = response.choices[0].message.content.strip()

        return {
            "answer": friendly_answer,
            "sql": sql,
            "rows": clean_rows,
            "error": False
        }

    except Exception as e:
        return {
            "answer": f"Sorry, there was a problem while running the query: {str(e)}",
            "sql": locals().get("sql", "N/A"),
            "rows": [],
            "error": True
        }

@app.route("/assistant")
@login_required
def assistant_page():
    return safe_render("qa_chat.html", **common_context())

@app.route("/assistant/ask", methods=["POST"])
@login_required
def api_chat():
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}
        
    question = data.get("question") or data.get("query") or data.get("message") or ""
    history = data.get("history", []) # 💡 新增：获取历史记录
    
    question = str(question).strip()
    
    if not question:
        return jsonify({"answer": "Please ask a question."}), 400

    if not has_deepseek_enabled():
        return jsonify({"answer": "DeepSeek API is not enabled."})

    # 💡 修改：将 history 传给执行函数
    result = execute_ai_sql(question, history) 
    
    return jsonify({
        "answer": result.get("answer"),
        "sql": result.get("sql"),
        "rows": result.get("rows", []),
        "mode": "Text-to-SQL",
        "debug_error": result.get("error")
    })

# 注意：上面这部分代码之后，应该是你的 # ========================= Export 部分的代码（如果有的话）。

# ==========================================
# 2. 替换原有的 export 路由为【万能导出接口】(已修复字段不匹配)
# ==========================================
@app.route("/export/<entity>")
@login_required
def export_csv(entity):
    """
    根据传入的 entity 参数，导出对应的 CSV。
    """
    output = StringIO()
    writer = csv.writer(output)

    if entity == "donors":
        data = query_all("SELECT donor_id, donor_name, gender, email, phone, city, donor_type, country FROM Donor ORDER BY donor_id DESC")
        filename = "donors.csv"
    
    elif entity == "staff":
        # 修复点：严格匹配你 staff_list 路由里的字段，去掉了可能引发格式错误的字段
        data = query_all("SELECT staff_id, staff_name, email, phone, position, staff_type, is_paid, city, country FROM Staff ORDER BY staff_id DESC")
        filename = "staff.csv"
        
    elif entity == "events":
        data = query_all("SELECT event_id, event_name, event_date, location, event_type, target_audience FROM Event ORDER BY event_date DESC, event_id DESC")
        filename = "events.csv"
        
    elif entity == "suppliers":
        data = query_all("SELECT supplier_id, supplier_name, contact_person, email, phone_number, address, supplier_type FROM Supplier ORDER BY supplier_id DESC")
        filename = "suppliers.csv"
        
    elif entity == "inventory":
        # 修复点：把 inventory_id 改为 item_id，unit_price 改为 unit_cost，并关联供应商名称
        data = query_all("""
            SELECT 
                i.item_id, 
                i.item_name, 
                i.quantities, 
                i.unit_cost, 
                s.supplier_name 
            FROM Inventory i
            LEFT JOIN Supplier s ON i.supplier_id = s.supplier_id
            ORDER BY i.item_id DESC
        """)
        filename = "inventory.csv"
        
    elif entity == "payments":
        data = query_all("SELECT payment_id, invoice_id, staff_id, payment_date, payment_type, amount, payment_method FROM Payment ORDER BY payment_date DESC, payment_id DESC")
        filename = "payments.csv"

    else:
        return "Export entity not supported", 400

    # 写入 CSV 头部和数据
    if data:
        writer.writerow(data[0].keys()) # 写入列名
        for row in data:
            writer.writerow(row.values()) # 写入具体数据
    else:
        writer.writerow(["No data found"])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


if __name__ == "__main__":
    app.run(debug=True)
