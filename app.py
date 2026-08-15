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
from apscheduler.schedulers.background import BackgroundScheduler
import resend
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY", "re_your_api_key_here")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "your-email@example.com")

# --- Database Setup (Render PostgreSQL with Local SQLite Fallback) ---
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Convert legacy postgres:// to postgresql:// required by SQLAlchemy
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    # Local SQLite fallback
    sqlite_file_name = "brain.db"
    sqlite_url = f"sqlite:///{sqlite_file_name}"
    engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})

# --- Safe Automated Backup ---
def run_automated_backup():
    # Only execute file backups when running locally with SQLite
    if not os.getenv("DATABASE_URL"):
        sqlite_file_name = "brain.db"
        if os.path.exists(sqlite_file_name):
            os.makedirs("backups", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(sqlite_file_name, f"backups/brain_backup_{timestamp}.db")

# --- Dynamic Safe Column Migrator ---
def safe_apply_migrations():
    # Only run SQLite schema patches locally
    if not os.getenv("DATABASE_URL"):
        sqlite_file_name = "brain.db"
        if os.path.exists(sqlite_file_name):
            conn = sqlite3.connect(sqlite_file_name)
            cursor = conn.cursor()
            
            # Ensure 'order' column exists in realm table
            cursor.execute("PRAGMA table_info(realm);")
            realm_cols = [col[1] for col in cursor.fetchall()]
            if 'order' not in realm_cols:
                cursor.execute("ALTER TABLE realm ADD COLUMN 'order' INTEGER DEFAULT 0;")

            # Ensure 'order' column exists in bucket table
            cursor.execute("PRAGMA table_info(bucket);")
            bucket_cols = [col[1] for col in cursor.fetchall()]
            if 'order' not in bucket_cols:
                cursor.execute("ALTER TABLE bucket ADD COLUMN 'order' INTEGER DEFAULT 0;")

            conn.commit()
            conn.close()

# --- Models ---
class Realm(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    icon: str = "🔮"
    order: int = Field(default=0)
    buckets: List["Bucket"] = Relationship(back_populates="realm")

class Bucket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    icon: str = "📌"
    order: int = Field(default=0)
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
    
    bucket_id: int = Field(foreign_key="bucket.id")
    bucket: Optional[Bucket] = Relationship(back_populates="items")
    reminders: List["Reminder"] = Relationship(back_populates="item")

class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    remind_at: datetime
    email_sent: bool = False
    
    item_id: int = Field(foreign_key="item.id")
    item: Optional[Item] = Relationship(back_populates="reminders")

# --- Scheduler ---
scheduler = BackgroundScheduler()
scheduler.start()

def send_email_alert(title: str, due_date: str, amount: Optional[float], description: str):
    amount_str = f"<p><strong>Amount Due:</strong> ${amount:.2f}</p>" if amount else ""
    resend.Emails.send({
        "from": "2nd Brain <onboarding@resend.dev>",
        "to": [NOTIFICATION_EMAIL],
        "subject": f"{title}",
        "html": f"""
            <h3>2nd Brain Reminder</h3>
            <p><strong>Item:</strong> {title}</p>
            <p><strong>Due Date:</strong> {due_date}</p>
            {amount_str}
            <p><strong>Notes:</strong> {description or 'None'}</p>
        """
    })

def add_months(sourcedate: datetime, months: int) -> datetime:
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, monthrange(year, month)[1])
    return datetime(year, month, day, sourcedate.hour, sourcedate.minute, sourcedate.second)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def on_startup():
    run_automated_backup()
    safe_apply_migrations()
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        if not session.exec(select(Realm)).first():
            personal = Realm(name="Personal", icon="🔮", order=0)
            finance = Realm(name="Finance", icon="🔮", order=1)
            session.add_all([personal, finance])
            session.commit()
            session.refresh(personal)
            session.refresh(finance)

            b1 = Bucket(name="Bills", icon="💳", realm_id=finance.id, order=0)
            b2 = Bucket(name="Maintenance", icon="🛠️", realm_id=personal.id, order=0)
            b3 = Bucket(name="Tasks", icon="📌", realm_id=personal.id, order=1)
            session.add_all([b1, b2, b3])
            session.commit()

# --- Dashboard Route ---
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, realm_id: Optional[int] = None, bucket_id: Optional[int] = None):
    with Session(engine) as session:
        realms = session.exec(select(Realm).order_by(Realm.order.asc())).all()
        buckets = session.exec(select(Bucket).order_by(Bucket.order.asc())).all()
        
        for realm in realms:
            realm.buckets.sort(key=lambda b: b.order)

        query = select(Item)
        if bucket_id:
            query = query.where(Item.bucket_id == bucket_id)
        elif realm_id:
            query = query.join(Bucket).where(Bucket.realm_id == realm_id)
            
        items = session.exec(query.order_by(Item.due_date.asc())).all()
        
        return templates.TemplateResponse(
            request=request,
            name="index.html", 
            context={
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
def create_realm(name: str = Form(...)):
    with Session(engine) as session:
        max_order = len(session.exec(select(Realm)).all())
        session.add(Realm(name=name, icon="🔮", order=max_order))
        session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/realms/update/")
def update_realm(realm_id: int = Form(...), name: str = Form(...)):
    with Session(engine) as session:
        realm = session.get(Realm, realm_id)
        if realm:
            realm.name = name
            session.add(realm)
            session.commit()
    return RedirectResponse(url=f"/?realm_id={realm_id}", status_code=303)

@app.post("/realms/reorder/")
def reorder_realms(order: List[int] = Body(...)):
    with Session(engine) as session:
        for idx, realm_id in enumerate(order):
            realm = session.get(Realm, realm_id)
            if realm:
                realm.order = idx
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

@app.post("/buckets/")
def create_bucket(name: str = Form(...), icon: str = Form("📌"), realm_id: int = Form(...)):
    with Session(engine) as session:
        max_order = len(session.exec(select(Bucket).where(Bucket.realm_id == realm_id)).all())
        session.add(Bucket(name=name, icon=icon, realm_id=realm_id, order=max_order))
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
                bucket.order = idx
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
    reminder_offset: int = Form(...),
    recurrence_type: str = Form("none"),
    interval: int = Form(1),
    weekdays: Optional[str] = Form(""),
    month_days: Optional[str] = Form(""),
    months: Optional[str] = Form(""),
    amount: Optional[float] = Form(None),
    description: Optional[str] = Form(None)
):
    base_due_date = datetime.strptime(due_date, "%Y-%m-%d")
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
            for mday in selected_month_days:
                try:
                    target_dates.append(datetime(m_date.year, m_date.month, mday, m_date.hour, m_date.minute))
                except ValueError:
                    pass
    elif recurrence_type == "yearly":
        for i in range(0, 5, interval):
            for m in selected_months:
                try:
                    target_dates.append(datetime(base_due_date.year + i, m, base_due_date.day, base_due_date.hour, base_due_date.minute))
                except ValueError:
                    pass

    target_dates = sorted(list(set(target_dates)))

    with Session(engine) as session:
        for target_due_date in target_dates:
            target_due_str = target_due_date.strftime("%Y-%m-%d")
            new_item = Item(
                title=title,
                bucket_id=bucket_id,
                due_date=target_due_date,
                amount=amount,
                description=description,
                recurring_group_id=group_id
            )
            session.add(new_item)
            session.commit()
            session.refresh(new_item)

            if reminder_offset == -1:
                for day in range(1, 4):
                    remind_time = target_due_date - timedelta(days=day)
                    session.add(Reminder(remind_at=remind_time, item_id=new_item.id))
                    scheduler.add_job(
                        send_email_alert, 'date', run_date=remind_time,
                        args=[f"⏰ Daily Reminder ({day} days left): {title}", target_due_str, amount, description]
                    )
            elif reminder_offset > 0:
                remind_time = target_due_date - timedelta(days=reminder_offset)
                session.add(Reminder(remind_at=remind_time, item_id=new_item.id))
                scheduler.add_job(
                    send_email_alert, 'date', run_date=remind_time,
                    args=[f"⏰ Reminder ({reminder_offset} days away): {title}", target_due_str, amount, description]
                )

            session.add(Reminder(remind_at=target_due_date, item_id=new_item.id))
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
def toggle_item_complete(item_id: int = Form(...)):
    with Session(engine) as session:
        item = session.get(Item, item_id)
        if item:
            item.is_completed = not item.is_completed
            session.add(item)
            session.commit()
            return RedirectResponse(url=f"/?bucket_id={item.bucket_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)
