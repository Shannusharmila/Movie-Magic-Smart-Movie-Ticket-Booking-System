# ============================================================
#  TamilTix — Main Flask Application
#  
#  SMART MODE:
#    - If AWS credentials are set in .env → uses DynamoDB + SNS
#    - If AWS credentials are NOT set     → uses local JSON files
#      (data/ folder) so the app works immediately on localhost
#
#  Run: python app.py
#  URL: http://localhost:5000
# ============================================================

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash)
from werkzeug.security import generate_password_hash, check_password_hash
import os, uuid, json
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

# ── Flask ────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "tamiltix-local-secret-2025")

# ── AWS Config ───────────────────────────────────────────────
REGION   = os.getenv("AWS_REGION",              "ap-south-1")
KEY_ID   = os.getenv("AWS_ACCESS_KEY_ID",       "")
SECRET   = os.getenv("AWS_SECRET_ACCESS_KEY",   "")
TOKEN    = os.getenv("AWS_SESSION_TOKEN",       "") or None
TBL_USR  = os.getenv("DYNAMODB_TABLE_USERS",    "tamiltix_users")
TBL_BKG  = os.getenv("DYNAMODB_TABLE_BOOKINGS", "tamiltix_bookings")
SNS_ARN  = os.getenv("SNS_TOPIC_ARN",           "")

# ── Detect if real AWS credentials are configured ────────────
AWS_READY = (
    KEY_ID and
    KEY_ID != "PASTE_YOUR_ACCESS_KEY_HERE" and
    SECRET and
    SECRET != "PASTE_YOUR_SECRET_KEY_HERE"
)

# ── Local JSON storage paths (used when AWS not configured) ──
DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ── AWS Clients (only if credentials are present) ────────────
users_tbl    = None
bookings_tbl = None
sns_client   = None

if AWS_READY:
    try:
        import boto3
        from boto3.dynamodb.conditions import Key as DynamoKey
        _kw = dict(region_name=REGION,
                   aws_access_key_id=KEY_ID,
                   aws_secret_access_key=SECRET)
        if TOKEN:
            _kw["aws_session_token"] = TOKEN
        _db          = boto3.resource("dynamodb", **_kw)
        users_tbl    = _db.Table(TBL_USR)
        bookings_tbl = _db.Table(TBL_BKG)
        sns_client   = boto3.client("sns", **_kw)
        print("[AWS] Connected to DynamoDB and SNS successfully.")
    except Exception as e:
        print(f"[AWS] Connection failed: {e}")
        print("[AWS] Falling back to local JSON storage.")
        AWS_READY = False
else:
    print("[LOCAL] AWS credentials not set — using local JSON storage.")
    print("[LOCAL] Fill .env with your AWS credentials to switch to DynamoDB.")


# ════════════════════════════════════════════════════════════
#  LOCAL JSON STORAGE FUNCTIONS
#  Used automatically when AWS is not configured
# ════════════════════════════════════════════════════════════

def local_read(filepath):
    """Read JSON file, return empty dict if not exists."""
    try:
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def local_write(filepath, data):
    """Write data to JSON file."""
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

def local_get_user(email):
    users = local_read(USERS_FILE)
    return users.get(email)

def local_save_user(user):
    users = local_read(USERS_FILE)
    users[user["email"]] = user
    local_write(USERS_FILE, users)

def local_update_login_count(email, count):
    users = local_read(USERS_FILE)
    if email in users:
        users[email]["login_count"] = count
        local_write(USERS_FILE, users)

def local_save_booking(booking):
    bookings = local_read(BOOKINGS_FILE)
    bookings[booking["booking_id"]] = booking
    local_write(BOOKINGS_FILE, bookings)

def local_get_user_bookings(email):
    bookings = local_read(BOOKINGS_FILE)
    return [b for b in bookings.values() if b.get("user_email") == email]

def local_get_occupied_seats(show_key):
    bookings = local_read(BOOKINGS_FILE)
    occupied = []
    for b in bookings.values():
        if b.get("show_key") == show_key:
            occupied.extend(b.get("seats", []))
    return occupied


# ════════════════════════════════════════════════════════════
#  UNIFIED DB FUNCTIONS
#  Automatically uses DynamoDB or local JSON based on AWS_READY
# ════════════════════════════════════════════════════════════

def db_get_user(email):
    if AWS_READY:
        resp = users_tbl.get_item(Key={"email": email})
        return resp.get("Item")
    return local_get_user(email)

def db_save_user(user):
    if AWS_READY:
        users_tbl.put_item(Item=user)
    else:
        local_save_user(user)

def db_update_login_count(email, count):
    if AWS_READY:
        users_tbl.update_item(
            Key={"email": email},
            UpdateExpression="SET login_count = :lc",
            ExpressionAttributeValues={":lc": count}
        )
    else:
        local_update_login_count(email, count)

def db_save_booking(booking):
    if AWS_READY:
        bookings_tbl.put_item(Item=booking)
    else:
        local_save_booking(booking)

def db_get_user_bookings(email):
    if AWS_READY:
        from boto3.dynamodb.conditions import Key as DynamoKey
        resp = bookings_tbl.query(
            IndexName="user-email-index",
            KeyConditionExpression=DynamoKey("user_email").eq(email)
        )
        return resp.get("Items", [])
    return local_get_user_bookings(email)

def db_get_occupied_seats(show_key):
    if AWS_READY:
        from boto3.dynamodb.conditions import Key as DynamoKey
        resp = bookings_tbl.query(
            IndexName="seat-index",
            KeyConditionExpression=DynamoKey("show_key").eq(show_key)
        )
        occupied = []
        for item in resp.get("Items", []):
            occupied.extend(item.get("seats", []))
        return occupied
    return local_get_occupied_seats(show_key)


# ════════════════════════════════════════════════════════════
#  SNS EMAIL CONFIRMATION
# ════════════════════════════════════════════════════════════

def send_confirmation_email(booking):
    msg = f"""
========================================
  TamilTix — Booking Confirmed!
========================================
  Booking ID  : {booking['booking_id']}
  Movie       : {booking['movie_name']}
  Theatre     : {booking['theater']}
  Date        : {booking['show_date']}
  Showtime    : {booking['show_time']}
  Seats       : {', '.join(booking['seats'])}
  Total Paid  : Rs.{booking['total_amount']}
  Payment     : {booking['payment_method']}
========================================
"""
    if AWS_READY and sns_client and SNS_ARN and SNS_ARN != "PASTE_YOUR_SNS_TOPIC_ARN_HERE":
        try:
            sns_client.publish(
                TopicArn = SNS_ARN,
                Message  = msg,
                Subject  = f"TamilTix Booking Confirmed — {booking['movie_name']} [{booking['booking_id']}]"
            )
            print(f"[SNS] Email sent for {booking['booking_id']}")
        except Exception as e:
            print(f"[SNS] Failed: {e}")
    else:
        print("\n[LOCAL] Booking Confirmation:")
        print(msg)


# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if "user_email" not in session:
            flash("Please sign in first.", "warning")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrap

def calc_price(price_per_seat, count):
    base  = price_per_seat * count
    fee   = count * 8
    gst   = round((base + fee) * 0.18)
    return {"base": base, "fee": fee, "gst": gst, "total": base + fee + gst}


# ════════════════════════════════════════════════════════════
#  MOVIES DATA
# ════════════════════════════════════════════════════════════

MOVIES = [
    {"id":"1","name":"Amaran","year":2024,"genre":"Biography","rating":"8.5",
     "lang":"Tamil","price":230,"duration":"169 min","cast":"Sivakarthikeyan, Sai Pallavi",
     "poster":"https://image.tmdb.org/t/p/w500/nGxUxi3PoCIiAVJSBBSqNZBCFhZ.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/3/3b/Amaran_film_poster.jpg",
     "poster_local":"/static/posters/amaran.jpg",
     "desc":"True story of Major Mukund Varadarajan, a decorated Indian Army officer.",
     "theaters":[
         {"name":"Rohini Silver Screens","city":"Chennai","dist":"3.2 km","amen":"Dolby Atmos · 4K"},
         {"name":"Kasi Theatre","city":"Chennai","dist":"6.8 km","amen":"DTS · 2K"},
         {"name":"SPI Cinemas","city":"Coimbatore","dist":"2.1 km","amen":"Dolby · 4K"}]},

    {"id":"2","name":"Vettaiyan","year":2024,"genre":"Action","rating":"7.8",
     "lang":"Tamil, Telugu","price":240,"duration":"172 min",
     "cast":"Rajinikanth, Amitabh Bachchan, Fahadh Faasil",
     "poster":"https://image.tmdb.org/t/p/w500/ugFqGjBGZkMxPeXb1RG1bBi6SQL.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/a/ab/Vettaiyan_film_poster.jpg",
     "poster_local":"/static/posters/vettaiyan.jpg",
     "desc":"A fierce police officer wages war against a powerful criminal empire.",
     "theaters":[
         {"name":"Vettri Cinemas","city":"Chennai","dist":"2.1 km","amen":"IMAX · 4K"},
         {"name":"Meenakshi Theatre","city":"Madurai","dist":"2.7 km","amen":"DTS"}]},

    {"id":"3","name":"The Greatest of All Time","year":2024,"genre":"Action","rating":"6.9",
     "lang":"Tamil, Telugu, Hindi","price":250,"duration":"175 min","cast":"Vijay, Prashanth, Sneha",
     "poster":"https://image.tmdb.org/t/p/w500/okiQZMOOmfFh6wiFqvUqxDUMBNT.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/a/a7/The_Greatest_of_All_Time_film_poster.jpg",
     "poster_local":"/static/posters/goat.jpg",
     "desc":"A top agent battles against time and his own son in this sci-fi thriller.",
     "theaters":[
         {"name":"Rohini Silver Screens","city":"Chennai","dist":"3.2 km","amen":"Dolby Atmos · 4K"},
         {"name":"INOX Forum","city":"Coimbatore","dist":"2.2 km","amen":"Dolby · 4K"},
         {"name":"Galaxy Cinemas","city":"Trichy","dist":"1.8 km","amen":"Standard"}]},

    {"id":"4","name":"Raayan","year":2024,"genre":"Crime","rating":"7.5",
     "lang":"Tamil","price":210,"duration":"155 min","cast":"Dhanush, Selvaraghavan, S.J. Suryah",
     "poster":"https://image.tmdb.org/t/p/w500/e7z53YjrXGIkTrlFQpj7a1PYH4W.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/0/05/Raayan_film_poster.jpg",
     "poster_local":"/static/posters/raayan.jpg",
     "desc":"A street leader is pulled into brutal underworld conflict.",
     "theaters":[
         {"name":"SPI Cinemas PVR","city":"Chennai","dist":"4.5 km","amen":"Dolby Atmos · 4K"},
         {"name":"Devi Cinemas","city":"Madurai","dist":"5.1 km","amen":"Standard"}]},

    {"id":"5","name":"Thug Life","year":2025,"genre":"Action","rating":"7.2",
     "lang":"Tamil","price":260,"duration":"158 min","cast":"Kamal Haasan, Silambarasan, Trisha",
     "poster":"https://image.tmdb.org/t/p/w500/zC1a4McZfGBGNYcENxhNrKz9H9S.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/9/91/Thug_Life_film_poster.jpg",
     "poster_local":"/static/posters/thuglife.jpg",
     "desc":"A gangster's final job unravels into a deadly web of old scores.",
     "theaters":[
         {"name":"Rohini Silver Screens","city":"Chennai","dist":"3.2 km","amen":"Dolby Atmos · 4K"},
         {"name":"Vettri Cinemas","city":"Chennai","dist":"2.1 km","amen":"IMAX · 4K"},
         {"name":"Albert Theatre","city":"Chennai","dist":"5.6 km","amen":"DTS"}]},

    {"id":"6","name":"Good Bad Ugly","year":2025,"genre":"Comedy","rating":"7.6",
     "lang":"Tamil, Telugu","price":235,"duration":"152 min","cast":"Ajith Kumar, Trisha, Arjun Das",
     "poster":"https://image.tmdb.org/t/p/w500/9vQgPGkmfhKZvW4xRvQ6iF8QEGJ.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/a/a7/Good_Bad_Ugly_film_poster.jpg",
     "poster_local":"/static/posters/goodbadugly.jpg",
     "desc":"An unlikely trio clash and team up to survive a chaotic underworld.",
     "theaters":[
         {"name":"SPI Cinemas PVR","city":"Chennai","dist":"4.5 km","amen":"Dolby Atmos · 4K"},
         {"name":"Sri Murugan Cinemas","city":"Coimbatore","dist":"1.5 km","amen":"Standard"}]},

    {"id":"7","name":"Coolie","year":2025,"genre":"Action","rating":"6.8",
     "lang":"Tamil, Telugu, Hindi","price":255,"duration":"163 min",
     "cast":"Rajinikanth, Upendra, Nagarjuna",
     "poster":"https://image.tmdb.org/t/p/w500/hHkQ3XJsqhqNJGQSRX4DLMnYtTx.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/7/7e/Coolie_film_poster.jpg",
     "poster_local":"/static/posters/coolie.jpg",
     "desc":"A railway coolie with a hidden past takes on a powerful criminal syndicate.",
     "theaters":[
         {"name":"Vettri Cinemas","city":"Chennai","dist":"2.1 km","amen":"IMAX · 4K"},
         {"name":"Meenakshi Theatre","city":"Madurai","dist":"2.7 km","amen":"DTS"},
         {"name":"Ananda Theatre","city":"Trichy","dist":"3.0 km","amen":"Standard"}]},

    {"id":"8","name":"Vidaamuyarchi","year":2025,"genre":"Thriller","rating":"7.3",
     "lang":"Tamil","price":220,"duration":"148 min","cast":"Ajith Kumar, Trisha, Regina Cassandra",
     "poster":"https://image.tmdb.org/t/p/w500/kHakvSTKFNJ9zqT2w8cTwE8CvzT.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/8/8e/Vidaamuyarchi_film_poster.jpg",
     "poster_local":"/static/posters/vidaamuyarchi.jpg",
     "desc":"A man searching for his wife unravels a terrifying trafficking conspiracy.",
     "theaters":[
         {"name":"Kasi Theatre","city":"Chennai","dist":"6.8 km","amen":"Dolby Atmos"},
         {"name":"KK Cinemas","city":"Tiruvannamalai","dist":"1.1 km","amen":"Standard"}]},

    {"id":"9","name":"Retro","year":2025,"genre":"Drama","rating":"7.9",
     "lang":"Tamil","price":200,"duration":"145 min","cast":"Suriya, Pooja Hegde",
     "poster":"https://image.tmdb.org/t/p/w500/kfkStSmQAMaHfv4uaQJuNyE0Eqn.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/8/8a/Retro_2025_film_poster.jpg",
     "poster_local":"/static/posters/retro.jpg",
     "desc":"A soulful drama tracing a man's journey through love, loss, and time.",
     "theaters":[
         {"name":"AVM Rajeswari","city":"Chennai","dist":"7.2 km","amen":"Standard"},
         {"name":"Ganesh Theatre","city":"Vellore","dist":"3.5 km","amen":"Standard"}]},

    {"id":"10","name":"Kuberaa","year":2025,"genre":"Crime","rating":"7.4",
     "lang":"Tamil, Telugu","price":215,"duration":"160 min",
     "cast":"Dhanush, Nagarjuna, Rashmika Mandanna",
     "poster":"https://image.tmdb.org/t/p/w500/qzGZ7DeSaXkBawCGjlBNpZJNQSK.jpg",
     "poster_alt":"https://upload.wikimedia.org/wikipedia/en/7/7d/Kuberaa_film_poster.jpg",
     "poster_local":"/static/posters/kuberaa.jpg",
     "desc":"A slum dweller finds a stash of money — but power and greed have other plans.",
     "theaters":[
         {"name":"SPI Cinemas PVR","city":"Chennai","dist":"4.5 km","amen":"Dolby Atmos · 4K"},
         {"name":"Padmavathi Talkies","city":"Chennai","dist":"9.1 km","amen":"Standard"}]},
]

SHOWTIMES = ["10:00 AM", "12:30 PM", "3:15 PM", "6:00 PM", "9:00 PM", "11:55 PM"]


# ════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════

@app.route("/")
def index():
    if "user_email" in session:
        return redirect(url_for("home"))
    return render_template("index.html")


# ── Register ─────────────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name  = request.form.get("name",     "").strip()
        email = request.form.get("email",    "").strip().lower()
        pwd   = request.form.get("password", "")
        mob   = request.form.get("mobile",   "").strip()
        city  = request.form.get("city",     "").strip()

        # Validation
        if not all([name, email, pwd, mob, city]):
            flash("Please fill all fields.", "error")
            return redirect(url_for("register"))
        if len(pwd) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))

        # Check if email already exists
        try:
            existing = db_get_user(email)
            if existing:
                flash("This email is already registered. Please sign in.", "error")
                return redirect(url_for("register"))
        except Exception as e:
            flash(f"Database error: {e}", "error")
            return redirect(url_for("register"))

        # Create and save the new user
        try:
            new_user = {
                "email":       email,
                "name":        name,
                "password":    generate_password_hash(pwd),
                "mobile":      mob,
                "city":        city,
                "login_count": 0,
                "created_at":  datetime.utcnow().isoformat()
            }
            db_save_user(new_user)
            print(f"[{'AWS' if AWS_READY else 'LOCAL'}] User created: {email}")
        except Exception as e:
            flash(f"Could not create account: {e}", "error")
            return redirect(url_for("register"))

        # Auto-login after registration
        session["user_email"]  = email
        session["user_name"]   = name
        session["user_city"]   = city
        session["login_count"] = 1
        flash(f"Welcome to TamilTix, {name}! 🎉", "success")
        return redirect(url_for("home"))

    return render_template("register.html")


# ── Login ────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email",    "").strip().lower()
        pwd   = request.form.get("password", "")

        if not email or not pwd:
            flash("Please enter your email and password.", "error")
            return redirect(url_for("login"))

        # Fetch user
        try:
            user = db_get_user(email)
        except Exception as e:
            flash(f"Database error: {e}", "error")
            return redirect(url_for("login"))

        if not user or not check_password_hash(user["password"], pwd):
            flash("Incorrect email or password. Please try again.", "error")
            return redirect(url_for("login"))

        # Update login count
        new_count = int(user.get("login_count", 0)) + 1
        try:
            db_update_login_count(email, new_count)
        except Exception as e:
            print(f"[WARNING] Could not update login count: {e}")

        session["user_email"]  = email
        session["user_name"]   = user["name"]
        session["user_city"]   = user.get("city", "")
        session["login_count"] = new_count
        flash(f"Welcome back, {user['name'].split()[0]}! 👋", "success")
        return redirect(url_for("home"))

    return render_template("login.html")


# ── Logout ───────────────────────────────────────────────────
@app.route("/logout")
@login_required
def logout():
    name = session.get("user_name", "")
    session.clear()
    flash(f"Goodbye, {name.split()[0]}! See you soon. 👋", "info")
    return redirect(url_for("index"))


# ── Home / Browse Movies ─────────────────────────────────────
@app.route("/home")
@login_required
def home():
    genre = request.args.get("genre", "")
    lang  = request.args.get("lang",  "")
    q     = request.args.get("q",     "").lower()

    filtered = MOVIES
    if genre:   filtered = [m for m in filtered if m["genre"] == genre]
    if lang:    filtered = [m for m in filtered if m["lang"].startswith(lang)]
    if q:       filtered = [m for m in filtered if
                            q in m["name"].lower() or
                            q in m["genre"].lower() or
                            q in m["cast"].lower()]

    genres = sorted(set(m["genre"] for m in MOVIES))
    return render_template("home.html",
                           movies=filtered, genres=genres,
                           filter_genre=genre, filter_lang=lang, filter_q=q)


# ── Booking Page ─────────────────────────────────────────────
@app.route("/book/<movie_id>")
@login_required
def book(movie_id):
    movie = next((m for m in MOVIES if m["id"] == movie_id), None)
    if not movie:
        flash("Movie not found.", "error")
        return redirect(url_for("home"))
    return render_template("booking.html", movie=movie, showtimes=SHOWTIMES)


# ── API: Occupied Seats ───────────────────────────────────────
@app.route("/api/seats")
@login_required
def api_seats():
    movie_id = request.args.get("movie_id")
    theater  = request.args.get("theater")
    date     = request.args.get("date")
    time     = request.args.get("time")

    if not all([movie_id, theater, date, time]):
        return jsonify({"occupied": []}), 400

    show_key = f"{movie_id}#{theater}#{date}#{time}"

    try:
        occupied = db_get_occupied_seats(show_key)
        return jsonify({"occupied": occupied})
    except Exception as e:
        print(f"[ERROR] Seat query: {e}")
        return jsonify({"occupied": []})


# ── Checkout ─────────────────────────────────────────────────
@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    if request.method == "POST":
        seats = request.form.getlist("seats")
        if not seats:
            flash("No seats selected. Please go back and select seats.", "warning")
            return redirect(url_for("home"))

        session["booking_draft"] = {
            "movie_id":   request.form.get("movie_id"),
            "movie_name": request.form.get("movie_name"),
            "poster":     request.form.get("poster"),
            "theater":    request.form.get("theater"),
            "show_date":  request.form.get("show_date"),
            "show_time":  request.form.get("show_time"),
            "seats":      seats,
            "price":      int(request.form.get("price", 0)),
        }

    draft = session.get("booking_draft")
    if not draft or not draft.get("seats"):
        flash("No seats selected. Please start your booking again.", "warning")
        return redirect(url_for("home"))

    pricing = calc_price(draft["price"], len(draft["seats"]))
    return render_template("checkout.html", draft=draft, pricing=pricing)


# ── Confirm Booking ───────────────────────────────────────────
@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    draft = session.get("booking_draft")
    if not draft:
        flash("Session expired. Please rebook.", "warning")
        return redirect(url_for("home"))

    name    = request.form.get("name",    "").strip()
    email   = request.form.get("email",   "").strip()
    mobile  = request.form.get("mobile",  "").strip()
    age     = request.form.get("age",     "").strip()
    payment = request.form.get("payment", "")

    if not all([name, email, mobile, age, payment]):
        flash("Please fill in all attendee and payment details.", "error")
        return redirect(url_for("checkout"))

    pricing    = calc_price(draft["price"], len(draft["seats"]))
    booking_id = "TT" + uuid.uuid4().hex.upper()[:10]

    booking = {
        "booking_id":      booking_id,
        "user_email":      session["user_email"],
        "movie_id":        draft["movie_id"],
        "movie_name":      draft["movie_name"],
        "poster":          draft["poster"],
        "theater":         draft["theater"],
        "show_date":       draft["show_date"],
        "show_time":       draft["show_time"],
        "seats":           draft["seats"],
        "show_key":        f"{draft['movie_id']}#{draft['theater']}#{draft['show_date']}#{draft['show_time']}",
        "attendee_name":   name,
        "attendee_email":  email,
        "attendee_mobile": mobile,
        "attendee_age":    age,
        "payment_method":  payment,
        "base_amount":     pricing["base"],
        "conv_fee":        pricing["fee"],
        "gst":             pricing["gst"],
        "total_amount":    pricing["total"],
        "booked_at":       datetime.utcnow().isoformat(),
        "status":          "confirmed"
    }

    # Save booking
    try:
        db_save_booking(booking)
        print(f"[{'AWS' if AWS_READY else 'LOCAL'}] Booking saved: {booking_id}")
    except Exception as e:
        flash(f"Booking failed: {e}", "error")
        return redirect(url_for("checkout"))

    # Send confirmation email
    send_confirmation_email(booking)

    session["last_booking"] = booking
    session.pop("booking_draft", None)
    flash("Booking confirmed! Your ticket is ready. 🎉", "success")
    return redirect(url_for("ticket"))


# ── Ticket Page ───────────────────────────────────────────────
@app.route("/ticket")
@login_required
def ticket():
    booking = session.get("last_booking")
    if not booking:
        flash("No booking found.", "warning")
        return redirect(url_for("home"))
    return render_template("ticket.html", booking=booking)


# ── My Bookings ───────────────────────────────────────────────
@app.route("/my-bookings")
@login_required
def my_bookings():
    try:
        bookings = db_get_user_bookings(session["user_email"])
        bookings = sorted(bookings,
                          key=lambda x: x.get("booked_at", ""),
                          reverse=True)
    except Exception as e:
        flash(f"Could not load bookings: {e}", "error")
        bookings = []
    return render_template("my_bookings.html", bookings=bookings)


# ════════════════════════════════════════════════════════════
#  RUN
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    mode = "AWS (DynamoDB + SNS)" if AWS_READY else "LOCAL (JSON files in data/)"
    print(f"\n{'='*50}")
    print(f"  TamilTix starting in {mode} mode")
    print(f"  Open: http://localhost:5000")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
