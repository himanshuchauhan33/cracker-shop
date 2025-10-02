import os
import json
import sqlite3
import stripe
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
from flask import Flask, render_template, session, redirect, url_for, request, flash
from flask_session import Session

# Load environment variables
load_dotenv()

# ----------------- Flask Setup -----------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
Session(app)

# ----------------- Stripe Setup -----------------
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ----------------- Database Setup -----------------
DB_PATH = "orders.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            delivery_type TEXT,
            items TEXT,
            total REAL,
            paid INTEGER DEFAULT 0
        )
    """
    )
    conn.commit()
    conn.close()


init_db()

# ----------------- Load Products -----------------
try:
    with open("products.json", "r", encoding="utf-8") as f:
        PRODUCTS = json.load(f)
except FileNotFoundError:
    PRODUCTS = []
except json.JSONDecodeError as e:
    print(f"Error parsing products.json: {e}")
    PRODUCTS = []


# ----------------- Utility Functions -----------------
def get_product(pid):
    for p in PRODUCTS:
        if p["id"] == int(pid):
            return p
    return None


@app.context_processor
def inject_publishable_key():
    return dict(STRIPE_PUBLISHABLE=STRIPE_PUBLISHABLE)


# ----------------- Routes -----------------
@app.route("/")
def index():
    return render_template("index.html", products=PRODUCTS)


@app.route("/add-to-cart/<int:pid>")
def add_to_cart(pid):
    prod = get_product(pid)
    if not prod:
        flash("Product not found", "error")
        return redirect(url_for("index"))
    cart = session.get("cart", {})
    cart[str(pid)] = cart.get(str(pid), 0) + 1
    session["cart"] = cart
    flash(f"Added {prod['name']} to cart", "success")
    return redirect(url_for("index"))


@app.route("/cart")
def cart():
    cart = session.get("cart", {})
    items = []
    total = 0
    for pid, qty in cart.items():
        p = get_product(pid)
        if p:
            p_copy = p.copy()
            p_copy["qty"] = qty
            p_copy["subtotal"] = p_copy["price"] * qty
            items.append(p_copy)
            total += p_copy["subtotal"]
    return render_template("cart.html", items=items, total=total)


@app.route("/update-cart", methods=["POST"])
def update_cart():
    data = request.form
    cart = {}
    for key in data:
        if key.startswith("qty-"):
            pid = key.split("-", 1)[1]
            try:
                qty = int(data.get(key, 0))
            except ValueError:
                qty = 0
            if qty > 0:
                cart[pid] = qty
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty", "error")
        return redirect(url_for("index"))
    
    items = []
    total = 0
    for pid, qty in cart.items():
        p = get_product(pid)
        if p:
            items.append(
                {"id": p["id"], "name": p["name"], "price": p["price"], "qty": qty}
            )
            total += p["price"] * qty

    if request.method == "GET":
        return render_template("checkout.html", items=items, total=total)

    # POST: Save order
    name = request.form.get("name")
    email = request.form.get("email")
    phone = request.form.get("phone")
    address = request.form.get("address")
    delivery_type = request.form.get("delivery_type")
    pay_method = request.form.get("pay_method")

    if not name or not phone:
        flash("Please enter name and phone", "error")
        return redirect(url_for("checkout"))

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO orders (customer_name,email,phone,address,delivery_type,items,total,paid) VALUES (?,?,?,?,?,?,?,?)",
            (name, email, phone, address, delivery_type, json.dumps(items), total, 0),
        )
        order_id = c.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        flash(f"Database error: {e}", "error")
        return redirect(url_for("checkout"))

    session.pop("cart", None)

    send_order_email(email, name, order_id, items, total, delivery_type)

    if pay_method == "stripe" and STRIPE_SECRET:
        try:
            line_items = []
            for it in items:
                line_items.append(
                    {
                        "price_data": {
                            "currency": "inr",
                            "product_data": {"name": it["name"]},
                            "unit_amount": int(it["price"] * 100),
                        },
                        "quantity": it["qty"],
                    }
                )
            session_stripe = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=line_items,
                mode="payment",
                success_url=url_for("success", _external=True)
                + f"?order_id={order_id}",
                cancel_url=url_for("checkout", _external=True),
            )
            return redirect(session_stripe.url, code=303)
        except stripe.error.StripeError as e:
            flash(f"Payment processing error: {e}", "error")
            return redirect(url_for("checkout"))

    return redirect(url_for("success", order_id=order_id))


@app.route("/success")
def success():
    order_id = request.args.get("order_id")
    return render_template("success.html", order_id=order_id)


@app.route("/admin")
def admin():
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    p = request.args.get("p")
    if p != admin_pass:
        return render_template("admin.html", orders=None, denied=True)
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT id,customer_name,email,phone,address,delivery_type,items,total,paid FROM orders ORDER BY id DESC"
        )
        rows = c.fetchall()
        conn.close()
        orders = []
        for r in rows:
            orders.append(
                {
                    "id": r[0],
                    "customer_name": r[1],
                    "email": r[2],
                    "phone": r[3],
                    "address": r[4],
                    "delivery_type": r[5],
                    "items": json.loads(r[6]),
                    "total": r[7],
                    "paid": r[8],
                }
            )
        return render_template("admin.html", orders=orders, denied=False)
    except sqlite3.Error as e:
        print(f"Database error in admin: {e}")
        return render_template("admin.html", orders=None, denied=False)


# ----------------- Email Sending -----------------
def send_order_email(to_email, customer_name, order_id, items, total, delivery_type):
    EMAIL_USER = os.getenv("EMAIL_USER")
    EMAIL_PASS = os.getenv("EMAIL_PASS")
    if not EMAIL_USER or not EMAIL_PASS or not to_email:
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"Order Confirmation #{order_id} - Cracker Shop"
        msg["From"] = EMAIL_USER
        msg["To"] = to_email
        body = f"Hello {customer_name},\n\nThank you for your order.\nOrder ID: {order_id}\nDelivery: {delivery_type}\nTotal: {total}\n\nItems:\n"
        for it in items:
            body += f"- {it['name']} x{it['qty']} @ {it['price']}\n"
        body += "\nWe will contact you soon.\n\nRegards,\nCracker Shop"
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_USER, EMAIL_PASS)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print("Email send failed:", e)
        return False
        
@app.context_processor
def inject_globals():
    return dict(
        STRIPE_PUBLISHABLE=STRIPE_PUBLISHABLE,
        WHATSAPP_NUMBER=os.getenv("WHATSAPP_NUMBER", "91XXXXXXXXXX")  # fallback number
    )


# ----------------- Run App -----------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
