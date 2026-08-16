import os
import shutil
import uuid
import sqlite3
from datetime import datetime, timedelta
from calendar import monthrange
from typing import Optional, List

from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import SQLModel, Field, Relationship, Session, create_engine, select
from sqlalchemy import text
from apscheduler.schedulers.background import BackgroundScheduler
import resend
from resend.exceptions import ResendError
from dotenv import load_dotenv

from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth

# Load environment variables
load_dotenv()

NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "your-email@example.com")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def send_email_alert(
    title: str,
    due_date: str,
    amount: Optional[float],
    description: str,
    recipients: Optional[List[str]] = None,
):
    if not resend.api_key:
        print("Skipping email dispatch: RESEND_API_KEY is missing.")
        return None

    amount_str = (
        f"<p><strong>Amount Due:</strong> ${amount:.2f}</p>" if amount else ""
    )
    target_emails = recipients if recipients else [NOTIFICATION_EMAIL]

    try:
        response = resend.Emails.send(
            {
                "from": "TaskMonster <notifications@usetaskmonster.app>",
                "to": target_emails,
                "subject": title,
                "html": f"""
                <h3>😈 TaskMonster Notification</h3>
                <p><strong>Item:</strong> {title}</p>
                <p><strong>Due Date:</strong> {due_date}</p>
                {amount_str}
                <p><strong>Notes:</strong> {description or 'None'}</p>
            """,
            }
        )
        return response
    except (ResendError, Exception) as e:
        print(f"Resend notification error (non-fatal): {e}")
        return None

# --- OAuth & Session Configuration ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-brain-key-2026")

# --- Database Setup (Render PostgreSQL with Local SQLite Fallback) ---
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    sqlite_file_name = "brain.db"
    sqlite_url = f"sqlite:///{sqlite_file_name}"
    engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})

# --- Safe Automated Backup ---
def run_automated_backup():
    if not os.getenv("DATABASE_URL"):
        sqlite_file_name = "brain.db"
        if os.path.exists(sqlite_file_name):
            os.makedirs("backups", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(sqlite_file_name, f"backups/brain_backup_{timestamp}.db")

# --- Dynamic Safe Column Migrator ---
def safe_apply_migrations():
    if os.getenv("DATABASE_URL"):
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE item ADD COLUMN IF NOT EXISTS recurrence_type VARCHAR DEFAULT \'none\';'))
    else:
        sqlite_file_name = "brain.db"
        if os.path.exists(sqlite_file_name):
            conn = sqlite3.connect(sqlite_file_name)
            cursor = conn.cursor()
            
            cursor.execute("PRAGMA table_info(realm);")
            realm_cols = [col[1] for col in cursor.fetchall()]
            if 'sort_order' not in realm_cols:
                cursor.execute('ALTER TABLE realm ADD COLUMN "sort_order" INTEGER DEFAULT 0;')
            if 'user_id' not in realm_cols:
                cursor.execute('ALTER TABLE realm ADD COLUMN "user_id" INTEGER;')

            cursor.execute("PRAGMA table_info(bucket);")
            bucket_cols = [col[1] for col in cursor.fetchall()]
            if 'sort_order' not in bucket_cols:
                cursor.execute('ALTER TABLE bucket ADD COLUMN "sort_order" INTEGER DEFAULT 0;')

            cursor.execute("PRAGMA table_info(item);")
            item_cols = [col[1] for col in cursor.fetchall()]
            if 'recurrence_type' not in item_cols:
                cursor.execute('ALTER TABLE item ADD COLUMN "recurrence_type" VARCHAR DEFAULT "none";')

            conn.commit()
            conn.close()

# --- Models ---
class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    name: str = "User"
    realms: List["Realm"] = Relationship(back_populates="user")

class Realm(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    icon: str = "🔮"
    sort_order: int = Field(default=0)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    user: Optional[User] = Relationship(back_populates="realms")
    buckets: List["Bucket"] = Relationship(back_populates="realm")

class Bucket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    icon: str = "📌"
    sort_order: int = Field(default=0)
    realm_id: int = Field(foreign_key="realm.id")
    realm: Optional[Realm] = Relationship(back_populates="buckets")
    items: List["Item"] = Relationship(back_populates="bucket")

class Item(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    amount: Optional[float] = None
    due_date: datetime
    is_completed: bool = False
    recurring_group_id: Optional[str] = Field(default=None, index=True)
    recurrence_type: Optional[str] = Field(default="none")
    bucket_id: int = Field(foreign_key="bucket.id")
    bucket: Optional[Bucket] = Relationship(back_populates="items")
    reminders: List["Reminder"] = Relationship(back_populates="item")

class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    remind_at: datetime
    email_sent: bool = False
    item_id: int = Field(foreign_key="item.id")
    item: Optional[Item] = Relationship(back_populates="reminders")

class RealmShare(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    realm_id: int = Field(foreign_key="realm.id")
    user_id: int = Field(foreign_key="users.id")

class PendingInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    realm_id: int = Field(foreign_key="realm.id")
    email: str = Field(index=True)
    
# --- FastAPI & Middleware Setup ---
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# --- OAuth Registration ---
oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# --- Scheduler ---
scheduler = BackgroundScheduler()
scheduler.start()

def check_and_send_overdue_emails():
    with Session(engine) as session:
        today = datetime.now().date()
        overdue_items = session.exec(
            select(Item).where(Item.is_completed == False, Item.due_date < datetime.now())
        ).all()

        for item in overdue_items:
            days_overdue = (today - item.due_date.date()).days
            if days_overdue > 0:
                due_str = item.due_date.strftime("%b %d, %Y")
                send_email_alert(
                    title=f"⚠️ OVERDUE ({days_overdue}d): {item.title}",
                    due_date=f"{due_str} ({days_overdue} day(s) overdue)",
                    amount=item.amount,
                    description=item.description
                )

# Daily cron at 8:00 AM for overdue notifications
scheduler.add_job(check_and_send_overdue_emails, 'cron', hour=8, minute=0)

def add_months(sourcedate: datetime, months: int) -> datetime:
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, monthrange(year, month)[1])
    return datetime(year, month, day, sourcedate.hour, sourcedate.minute, sourcedate.second)

def get_current_user(request: Request, session: Session) -> Optional[User]:
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    return session.get(User, user_id)

@app.on_event("startup")
def on_startup():
    run_automated_backup()
    safe_apply_migrations()
    SQLModel.metadata.create_all(engine)

# In main.py / OAuth login route handler
@app.get("/login")
async def login(request: Request):
    # Pass prompt="select_account" to force account chooser
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(
        request, 
        redirect_uri, 
        prompt="select_account"  # <--- FORCES ACCOUNT SELECTION SCREEN
    )

@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get('userinfo')
    if not user_info or not user_info.get('email'):
        return RedirectResponse(url="/")

    email = user_info['email'].lower()
    name = user_info.get('name', email.split('@')[0])

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        if not user:
            user = User(email=email, name=name)
            session.add(user)
            session.commit()
            session.refresh(user)

            personal = Realm(name="Personal", icon="🔮", sort_order=0, user_id=user.id)
            finance = Realm(name="Finance", icon="🔮", sort_order=1, user_id=user.id)
            session.add_all([personal, finance])
            session.commit()

        # Claim any pending invites for this email address
        pending_invites = session.exec(
            select(PendingInvite).where(PendingInvite.email == email)
        ).all()

        for invite in pending_invites:
            existing_share = session.exec(
                select(RealmShare).where(
                    RealmShare.realm_id == invite.realm_id,
                    RealmShare.user_id == user.id
                )
            ).first()
            if not existing_share:
                session.add(RealmShare(realm_id=invite.realm_id, user_id=user.id))
            session.delete(invite)

        session.commit()
        request.session['user_id'] = user.id

    return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

# --- Dashboard Route ---
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, realm_id: Optional[int] = None, bucket_id: Optional[int] = None):
    with Session(engine) as session:
        user = get_current_user(request, session)
        if not user:
            return templates.TemplateResponse(request=request, name="index.html", context={"user": None})

        owned_realms = session.exec(select(Realm).where(Realm.user_id == user.id)).all()
        shared_realm_ids = session.exec(select(RealmShare.realm_id).where(RealmShare.user_id == user.id)).all()
        shared_realms = session.exec(select(Realm).where(Realm.id.in_(shared_realm_ids))).all() if shared_realm_ids else []

        realms = list({r.id: r for r in owned_realms + shared_realms}.values())
        realms.sort(key=lambda r: r.sort_order)

        all_realm_ids = [r.id for r in realms]
        buckets = session.exec(select(Bucket).where(Bucket.realm_id.in_(all_realm_ids)).order_by(Bucket.sort_order.asc())).all() if all_realm_ids else []

        for realm in realms:
            realm.buckets.sort(key=lambda b: b.sort_order)

        query = select(Item).join(Bucket).where(Bucket.realm_id.in_(all_realm_ids)) if all_realm_ids else select(Item).where(False)
        if bucket_id:
            query = query.where(Item.bucket_id == bucket_id)
        elif realm_id:
            query = query.where(Bucket.realm_id == realm_id)

        items = session.exec(query.order_by(Item.due_date.asc())).all() if all_realm_ids else []

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "user": user,
                "realms": realms,
                "buckets": buckets,
                "items": items,
                "selected_realm_id": realm_id,
                "selected_bucket_id": bucket_id,
                "today": datetime.now().date()
            }
        )

# --- Realm & Bucket Endpoints ---
@app.post("/realms/")
def create_realm(request: Request, name: str = Form(...), icon: str = Form("🔮")):
    with Session(engine) as session:
        user = get_current_user(request, session)
        if user:
            max_order = len(session.exec(select(Realm).where(Realm.user_id == user.id)).all())
            session.add(Realm(name=name, icon=icon, sort_order=max_order, user_id=user.id))
            session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/realms/update/")
def update_realm(realm_id: int = Form(...), name: str = Form(...), icon: str = Form("🔮")):
    with Session(engine) as session:
        realm = session.get(Realm, realm_id)
        if realm:
            realm.name = name
            realm.icon = icon
            session.add(realm)
            session.commit()
    return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)
@app.post("/realms/reorder/")
def reorder_realms(order: List[int] = Body(...)):
    with Session(engine) as session:
        for idx, realm_id in enumerate(order):
            realm = session.get(Realm, realm_id)
            if realm:
                realm.sort_order = idx
                session.add(realm)
        session.commit()
    return JSONResponse({"status": "ok"})

@app.post("/realms/delete/")
def delete_realm(realm_id: int = Form(...)):
    with Session(engine) as session:
        realm = session.get(Realm, realm_id)
        if realm:
            for bucket in realm.buckets:
                for item in bucket.items:
                    for reminder in item.reminders:
                        session.delete(reminder)
                    session.delete(item)
                session.delete(bucket)
            session.delete(realm)
            session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/realms/share/")
def share_realm(request: Request, realm_id: int = Form(...), email: str = Form(...)):
    target_email = email.strip().lower()
    api_key = os.getenv("RESEND_API_KEY")
    
    if not api_key:
        print("Skipping invite emails: RESEND_API_KEY is missing.", flush=True)
        return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)

    resend.api_key = api_key
    
    with Session(engine) as session:
        current_user = get_current_user(request, session)
        if not current_user:
            return RedirectResponse(url="/", status_code=303)

        realm = session.get(Realm, realm_id)
        if not realm:
            return RedirectResponse(url="/", status_code=303)

        target_user = session.exec(select(User).where(User.email == target_email)).first()

        if target_user:
            existing = session.exec(
                select(RealmShare).where(
                    RealmShare.realm_id == realm_id, 
                    RealmShare.user_id == target_user.id
                )
            ).first()
            if not existing:
                session.add(RealmShare(realm_id=realm_id, user_id=target_user.id))
                session.commit()
        else:
            existing_pending = session.exec(
                select(PendingInvite).where(
                    PendingInvite.realm_id == realm_id,
                    PendingInvite.email == target_email
                )
            ).first()
            if not existing_pending:
                session.add(PendingInvite(realm_id=realm_id, email=target_email))
                session.commit()

        # 1. Send Invitation Email to Recipient (Optimized for Spam Filters)
        invitation_subject = f"Collaborate with {current_user.name} on TaskMonster"
        invitation_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; line-height: 1.6; max-width: 550px; margin: 0 auto; padding: 24px; border: 1px solid #e5e7eb; border-radius: 8px;">
            <p>Hi there,</p>
            <p><strong>{current_user.name}</strong> ({current_user.email}) invited you to join the <strong>{realm.name}</strong> realm on TaskMonster so you can manage shared tasks and timelines together.</p>
            <div style="margin: 24px 0;">
                <a href="https://usetaskmonster.app/login" style="background-color: #6366f1; color: #ffffff; padding: 12px 22px; text-decoration: none; border-radius: 6px; font-weight: 600; display: inline-block;">Open {realm.name} Realm</a>
            </div>
            <p style="font-size: 13px; color: #4b5563;">
                Or copy and paste this link into your browser:<br>
                <a href="https://usetaskmonster.app/login" style="color: #6366f1;">https://usetaskmonster.app/login</a>
            </p>
            <p style="font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; padding-top: 16px; margin-top: 28px;">
                Sent via TaskMonster. You received this because {current_user.email} added your address.
            </p>
        </div>
        """
        
        try:
            resend.Emails.send({
                "from": "TaskMonster <notifications@usetaskmonster.app>",
                "reply_to": current_user.email,  # <--- CRITICAL: Tells Gmail a real person sent this
                "to": [target_email],
                "subject": invitation_subject,
                "html": invitation_body
            })
            print(f"Invite email successfully sent to {target_email}", flush=True)
        except Exception as e:
            print(f"Failed to send invite email to recipient ({target_email}): {e}", flush=True)
            
        # 2. Send Confirmation Email to Inviter
        confirmation_subject = f"Invitation Sent: {target_email} invited to '{realm.name}'"
        confirmation_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; line-height: 1.6; max-width: 550px; margin: 0 auto; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px;">
            <h2 style="color: #4f46e5; margin-top: 0;">Invitation Dispatched</h2>
            <p>Hi {current_user.name},</p>
            <p>Your invitation to <strong>{target_email}</strong> for the <strong>{realm.name}</strong> realm has been successfully sent.</p>
            <p>Once they log in with Google at <a href="https://usetaskmonster.app" style="color: #6366f1; font-weight: 600; text-decoration: none;">usetaskmonster.app</a>, the shared realm will automatically appear on their dashboard timeline.</p>
            <p style="font-size: 13px; color: #6b7280; border-top: 1px solid #e5e7eb; padding-top: 16px; margin-top: 32px;">
                TaskMonster System Notification
            </p>
        </div>
        """

        try:
            resend.Emails.send({
                "from": "TaskMonster <notifications@usetaskmonster.app>",
                "to": [current_user.email],
                "subject": confirmation_subject,
                "html": confirmation_body
            })
            print(f"Confirmation email successfully sent to inviter ({current_user.email})", flush=True)
        except Exception as e:
            print(f"Failed to send confirmation email to inviter ({current_user.email}): {e}", flush=True)

    return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)

@app.post("/buckets/")
def create_bucket(name: str = Form(...), icon: str = Form("📌"), realm_id: int = Form(...)):
    with Session(engine) as session:
        max_order = len(session.exec(select(Bucket).where(Bucket.realm_id == realm_id)).all())
        session.add(Bucket(name=name, icon=icon, realm_id=realm_id, sort_order=max_order))
        session.commit()
    return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)

@app.post("/buckets/update/")
def update_bucket(bucket_id: int = Form(...), name: str = Form(...), icon: str = Form("📌")):
    with Session(engine) as session:
        bucket = session.get(Bucket, bucket_id)
        if bucket:
            bucket.name = name
            bucket.icon = icon
            session.add(bucket)
            session.commit()
    return RedirectResponse(url=f"/?bucket_id={bucket_id}", status_code=303)

@app.post("/buckets/reorder/")
def reorder_buckets(order: List[int] = Body(...)):
    with Session(engine) as session:
        for idx, bucket_id in enumerate(order):
            bucket = session.get(Bucket, bucket_id)
            if bucket:
                bucket.sort_order = idx
                session.add(bucket)
        session.commit()
    return JSONResponse({"status": "ok"})

@app.post("/buckets/delete/")
def delete_bucket(bucket_id: int = Form(...)):
    with Session(engine) as session:
        bucket = session.get(Bucket, bucket_id)
        if bucket:
            realm_id = bucket.realm_id
            for item in bucket.items:
                for reminder in item.reminders:
                    session.delete(reminder)
                session.delete(item)
            session.delete(bucket)
            session.commit()
            return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)

# --- Item Endpoints ---
@app.post("/items/")
def create_item(
    title: str = Form(...),
    bucket_id: int = Form(...),
    due_date: str = Form(...),
    due_time: Optional[str] = Form(None),
    reminder_offset: int = Form(...),
    recurrence_type: str = Form("none"),
    interval: int = Form(1),
    weekdays: Optional[str] = Form(""),
    month_days: Optional[str] = Form(""),
    months: Optional[str] = Form(""),
    amount: Optional[float] = Form(None),
    description: Optional[str] = Form(None)
):
    # Parse due_time if provided, otherwise default to 9:00 AM
    hour, minute = 9, 0
    if due_time and due_time.strip():
        try:
            time_obj = datetime.strptime(due_time.strip(), "%H:%M")
            hour, minute = time_obj.hour, time_obj.minute
        except ValueError:
            pass

    # Anchor to specified time (or default 9:00 AM) to eliminate UTC/Local midnight timezone day shifts
    base_due_date = datetime.strptime(due_date, "%Y-%m-%d").replace(hour=hour, minute=minute, second=0)
    group_id = str(uuid.uuid4()) if recurrence_type != "none" else None
    interval = max(1, interval)

    selected_weekdays = [int(x) for x in weekdays.split(",") if x.strip()] if weekdays else [(base_due_date.weekday() + 1) % 7]
    selected_month_days = [int(x) for x in month_days.split(",") if x.strip()] if month_days else [base_due_date.day]
    selected_months = [int(x) for x in months.split(",") if x.strip()] if months else [base_due_date.month]

    target_dates = []
    if recurrence_type == "none":
        target_dates.append(base_due_date)
    elif recurrence_type == "daily":
        curr = base_due_date
        max_date = base_due_date + timedelta(days=180)
        while curr <= max_date:
            target_dates.append(curr)
            curr += timedelta(days=interval)
    elif recurrence_type == "weekly":
        curr = base_due_date
        max_date = base_due_date + timedelta(days=365)
        while curr <= max_date:
            wday = (curr.weekday() + 1) % 7
            if wday in selected_weekdays:
                target_dates.append(curr)
            curr += timedelta(days=1)
            if wday == 6 and interval > 1:
                curr += timedelta(weeks=interval - 1)
    elif recurrence_type == "monthly":
        for i in range(0, 12, interval):
            m_date = add_months(base_due_date, i)
            max_day_in_month = monthrange(m_date.year, m_date.month)[1]
            for mday in selected_month_days:
                actual_day = min(mday, max_day_in_month)
                target_dates.append(datetime(m_date.year, m_date.month, actual_day, hour, minute, 0))
    elif recurrence_type == "yearly":
        for i in range(0, 5, interval):
            target_year = base_due_date.year + i
            for m in selected_months:
                max_day = monthrange(target_year, m)[1]
                actual_day = min(base_due_date.day, max_day)
                target_dates.append(datetime(target_year, m, actual_day, hour, minute, 0))

    target_dates = sorted(list(set(target_dates)))

    with Session(engine) as session:
        for target_due_date in target_dates:
            target_due_str = target_due_date.strftime("%Y-%m-%d %I:%M %p") if due_time and due_time.strip() else target_due_date.strftime("%Y-%m-%d")
            new_item = Item(
                title=title,
                bucket_id=bucket_id,
                due_date=target_due_date,
                amount=amount,
                description=description,
                recurring_group_id=group_id,
                recurrence_type=recurrence_type
            )
            session.add(new_item)
            session.commit()
            session.refresh(new_item)

            if reminder_offset == -1:
                for day in range(1, 4):
                    remind_time = target_due_date - timedelta(days=day)
                    session.add(Reminder(remind_at=remind_time, item_id=new_item.id))
                    if remind_time > datetime.now():
                        scheduler.add_job(
                            send_email_alert, 'date', run_date=remind_time,
                            args=[f"⏰ Daily Reminder ({day} days left): {title}", target_due_str, amount, description]
                        )
            elif reminder_offset > 0:
                remind_time = target_due_date - timedelta(days=reminder_offset)
                session.add(Reminder(remind_at=remind_time, item_id=new_item.id))
                if remind_time > datetime.now():
                    scheduler.add_job(
                        send_email_alert, 'date', run_date=remind_time,
                        args=[f"⏰ Reminder ({reminder_offset} days away): {title}", target_due_str, amount, description]
                    )

            session.add(Reminder(remind_at=target_due_date, item_id=new_item.id))
            if target_due_date > datetime.now():
                scheduler.add_job(
                    send_email_alert, 'date', run_date=target_due_date,
                    args=[f"🚨 Due Today: {title}", target_due_str, amount, description]
                )

        session.commit()

    return RedirectResponse(url=f"/?bucket_id={bucket_id}", status_code=303)

@app.post("/items/delete/")
def delete_item(item_id: int = Form(...), delete_series: bool = Form(False)):
    with Session(engine) as session:
        item = session.get(Item, item_id)
        if item:
            bucket_id = item.bucket_id
            if delete_series and item.recurring_group_id:
                series_items = session.exec(select(Item).where(Item.recurring_group_id == item.recurring_group_id)).all()
                for series_item in series_items:
                    for reminder in series_item.reminders:
                        session.delete(reminder)
                    session.delete(series_item)
            else:
                for reminder in item.reminders:
                    session.delete(reminder)
                session.delete(item)
            session.commit()
            return RedirectResponse(url=f"/?bucket_id={bucket_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)

@app.post("/items/toggle-complete/")
def toggle_item_complete(request: Request, item_id: int = Form(...)):
    with Session(engine) as session:
        item = session.get(Item, item_id)
        if item:
            was_completed = item.is_completed
            item.is_completed = not was_completed
            session.add(item)

            # Send email notification when marking complete
            if not was_completed:
                current_user = get_current_user(request, session)
                completed_by = current_user.name if current_user else "A team member"
                
                recipients = []
                bucket = session.get(Bucket, item.bucket_id) if item.bucket_id else None
                realm = session.get(Realm, bucket.realm_id) if bucket and bucket.realm_id else None
                
                if realm:
                    if realm.user_id:
                        owner = session.get(User, realm.user_id)
                        if owner and owner.email:
                            recipients.append(owner.email)
                    
                    shared_records = session.exec(
                        select(RealmShare).where(RealmShare.realm_id == realm.id)
                    ).all()
                    for share in shared_records:
                        shared_user = session.get(User, share.user_id)
                        if shared_user and shared_user.email:
                            recipients.append(shared_user.email)

                recipients = list(set(recipients))

                if recipients:
                    send_email_alert(
                        title=f"✅ Task Completed: {item.title}",
                        due_date=item.due_date.strftime("%b %d, %Y"),
                        amount=item.amount,
                        description=f"Completed by {completed_by}. Notes: {item.description or 'None'}",
                        recipients=recipients
                    )

            session.commit()
            return RedirectResponse(url=f"/?bucket_id={item.bucket_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)
    
# --- Edit Task Endpoint ---
@app.post("/items/update/")
def update_item(
    item_id: int = Form(...),
    title: str = Form(...),
    due_date: str = Form(...),
    due_time: Optional[str] = Form(None),
    amount: Optional[float] = Form(None),
    description: Optional[str] = Form(None),
    bucket_id: Optional[int] = Form(None),
    update_series: bool = Form(False)
):
    hour, minute = 9, 0
    if due_time and due_time.strip():
        try:
            time_obj = datetime.strptime(due_time.strip(), "%H:%M")
            hour, minute = time_obj.hour, time_obj.minute
        except ValueError:
            pass

    new_due_date = datetime.strptime(due_date, "%Y-%m-%d").replace(hour=hour, minute=minute, second=0)

    with Session(engine) as session:
        item = session.get(Item, item_id)
        if not item:
            return RedirectResponse(url="/", status_code=303)

        target_bucket_id = bucket_id if bucket_id else item.bucket_id

        # Update entire recurring series if requested
        if update_series and item.recurring_group_id:
            series_items = session.exec(
                select(Item).where(Item.recurring_group_id == item.recurring_group_id)
            ).all()

            for series_item in series_items:
                series_item.title = title
                series_item.amount = amount
                series_item.description = description
                series_item.bucket_id = target_bucket_id
                # Adjust time of day across the series while keeping each item's scheduled date
                series_item.due_date = series_item.due_date.replace(hour=hour, minute=minute, second=0)
                session.add(series_item)
        else:
            # Update single item
            item.title = title
            item.due_date = new_due_date
            item.amount = amount
            item.description = description
            item.bucket_id = target_bucket_id
            session.add(item)

        session.commit()
        return RedirectResponse(url=f"/?bucket_id={target_bucket_id}", status_code=303)
