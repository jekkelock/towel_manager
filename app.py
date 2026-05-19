from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from typing import Iterable

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

TOWEL_DEPOSIT = Decimal("10.00")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
APP_MODE = os.getenv("APP_MODE", "client").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(INSTANCE_DIR, 'towel_manager.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.getenv("SECRET_KEY", "hotel-internal-tool")
db = SQLAlchemy(app)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    towel_stock = db.Column(db.Integer, nullable=False, default=0)
    vouchers = db.relationship("VoucherCard", backref="department", lazy=True)
    rentals = db.relationship("Rental", backref="department", lazy=True)
    transactions = db.relationship("TransactionLog", backref="department", lazy=True)


class VoucherCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    rental_links = db.relationship("RentalVoucher", backref="voucher_card", lazy=True)
    __table_args__ = (db.UniqueConstraint("department_id", "number", name="uniq_department_voucher"),)

    @property
    def assigned(self) -> bool:
        for link in self.rental_links:
            if link.rental.status == "open":
                return True
        return False


class RoomRange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(100), nullable=False)
    start_room = db.Column(db.Integer, nullable=False)
    end_room = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)


class Rental(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.Integer, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    towel_count = db.Column(db.Integer, nullable=False)
    deposit_total = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    closed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="open")
    vouchers = db.relationship("RentalVoucher", backref="rental", lazy=True, cascade="all, delete-orphan")

    def voucher_numbers(self) -> list[int]:
        return [item.voucher_card.number for item in self.vouchers]


class RentalVoucher(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    rental_id = db.Column(db.Integer, db.ForeignKey("rental.id"), nullable=False)
    voucher_card_id = db.Column(db.Integer, db.ForeignKey("voucher_card.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("rental_id", "voucher_card_id", name="uniq_rental_voucher"),)


class TransactionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_type = db.Column(db.String(20), nullable=False)
    room_number = db.Column(db.Integer, nullable=False)
    towel_count = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, unique=True)
    value = db.Column(db.Text, nullable=False)


def ensure_schema_updates() -> None:
    # Minimal in-place migration for existing sqlite files.
    columns = db.session.execute(db.text("PRAGMA table_info(department)")).all()
    column_names = {row[1] for row in columns}
    if "towel_stock" not in column_names:
        db.session.execute(db.text("ALTER TABLE department ADD COLUMN towel_stock INTEGER NOT NULL DEFAULT 0"))
        db.session.commit()


def is_admin_mode() -> bool:
    return APP_MODE == "admin"


def get_setting(key: str, default: str = "") -> str:
    item = AppSetting.query.filter_by(key=key).first()
    if item is None:
        return default
    return item.value


def set_setting(key: str, value: str) -> None:
    item = AppSetting.query.filter_by(key=key).first()
    if item is None:
        db.session.add(AppSetting(key=key, value=value))
    else:
        item.value = value


def get_admin_password() -> str:
    return get_setting("admin_password", ADMIN_PASSWORD)


def set_admin_password(new_password: str) -> None:
    set_setting("admin_password", new_password)


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not is_admin_mode():
            abort(404)
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    return wrapped


def bootstrap_defaults() -> None:
    if Department.query.count() == 0:
        db.session.add(Department(name="Beach Bar", code="BEACH"))
        db.session.add(Department(name="Pool Bar", code="POOL"))
    if RoomRange.query.count() == 0:
        db.session.add(RoomRange(label="Default Block A", start_room=1001, end_room=1010))
    if AppSetting.query.filter_by(key="admin_password").first() is None:
        db.session.add(AppSetting(key="admin_password", value=ADMIN_PASSWORD))
    db.session.commit()


def active_room_ranges() -> list[RoomRange]:
    return RoomRange.query.filter_by(active=True).order_by(RoomRange.start_room.asc()).all()


def room_is_allowed(room_number: int) -> bool:
    ranges = active_room_ranges()
    if not ranges:
        return True
    for allowed in ranges:
        if allowed.start_room <= room_number <= allowed.end_room:
            return True
    return False


def available_vouchers(department_id: int) -> list[VoucherCard]:
    candidates = (
        VoucherCard.query.filter_by(department_id=department_id, active=True)
        .order_by(VoucherCard.number.asc())
        .all()
    )
    return [voucher for voucher in candidates if not voucher.assigned]


def parse_int(name: str, value: str | None) -> int:
    if value is None or value.strip() == "":
        raise ValueError(f"{name} is required.")
    return int(value.strip())


def parse_voucher_list(raw: str) -> list[int]:
    raw = re.sub(r"[.;\s]+", ",", raw.strip())
    numbers: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            numbers.append(int(token))
    if len(numbers) != len(set(numbers)):
        raise ValueError("Voucher numbers contain duplicates.")
    return sorted(numbers)


@app.route("/")
def dashboard():
    departments = Department.query.order_by(Department.name.asc()).all()
    open_rentals = Rental.query.filter_by(status="open").order_by(Rental.created_at.desc()).all()
    total_open_towels = sum(item.towel_count for item in open_rentals)
    total_open_deposit = sum(Decimal(item.deposit_total) for item in open_rentals)
    if not is_admin_mode():
        department = current_client_department()
        if department is None:
            return redirect(url_for("select_department"))
        department_open_rentals = (
            Rental.query.filter_by(status="open", department_id=department.id)
            .order_by(Rental.created_at.desc())
            .all()
        )
        department_open_towels = sum(item.towel_count for item in department_open_rentals)
        department_open_deposit = sum(Decimal(item.deposit_total) for item in department_open_rentals)
        return render_template(
            "dashboard_client.html",
            department=department,
            open_rentals=department_open_rentals,
            available_voucher_numbers=[card.number for card in available_vouchers(department.id)],
            towel_deposit=TOWEL_DEPOSIT,
            given_out_towels=department_open_towels,
            total_open_towels=department_open_towels,
            total_open_deposit=department_open_deposit,
        )

    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_login"))

    room_ranges = RoomRange.query.order_by(RoomRange.start_room.asc()).all()
    room_filter_raw = (request.args.get("room_number") or "").strip()
    start_date_raw = (request.args.get("start_date") or "").strip()
    end_date_raw = (request.args.get("end_date") or "").strip()
    page = request.args.get("page", default=1, type=int)

    tx_query = TransactionLog.query
    if room_filter_raw:
        try:
            tx_query = tx_query.filter(TransactionLog.room_number == int(room_filter_raw))
        except ValueError:
            flash("Room filter must be a number.", "error")

    if start_date_raw:
        try:
            start_date = datetime.strptime(start_date_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            tx_query = tx_query.filter(TransactionLog.created_at >= start_date)
        except ValueError:
            flash("Start date format is invalid.", "error")
    if end_date_raw:
        try:
            end_exclusive = datetime.strptime(end_date_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            tx_query = tx_query.filter(TransactionLog.created_at < end_exclusive)
        except ValueError:
            flash("End date format is invalid.", "error")

    tx_pagination = tx_query.order_by(TransactionLog.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    tx_logs = tx_pagination.items
    voucher_status: dict[int, dict[str, int]] = {}
    for department in departments:
        all_cards = VoucherCard.query.filter_by(department_id=department.id).all()
        available_count = len([v for v in all_cards if v.active and not v.assigned])
        assigned_count = len([v for v in all_cards if v.active and v.assigned])
        voucher_status[department.id] = {
            "total": len(all_cards),
            "available": available_count,
            "assigned": assigned_count,
        }

    return render_template(
        "dashboard_admin.html",
        departments=departments,
        room_ranges=room_ranges,
        open_rentals=open_rentals,
        tx_logs=tx_logs,
        towel_deposit=TOWEL_DEPOSIT,
        total_open_towels=total_open_towels,
        total_open_deposit=total_open_deposit,
        voucher_status=voucher_status,
        tx_pagination=tx_pagination,
        room_filter=room_filter_raw,
        start_date_filter=start_date_raw,
        end_date_filter=end_date_raw,
    )


@app.route("/department/select", methods=["GET", "POST"])
def select_department():
    if is_admin_mode():
        abort(404)
    departments = Department.query.order_by(Department.name.asc()).all()
    if request.method == "POST":
        department_id = parse_int("Department", request.form.get("department_id"))
        department = Department.query.filter_by(id=department_id).first()
        if department is None:
            flash("Department not found.", "error")
            return render_template("department_select.html", departments=departments)
        session["client_department_id"] = department.id
        flash(f"Department selected: {department.name}", "success")
        return redirect(url_for("dashboard"))
    return render_template("department_select.html", departments=departments)


@app.post("/department/logout")
def department_logout():
    if is_admin_mode():
        abort(404)
    session.pop("client_department_id", None)
    flash("Department cleared.", "success")
    return redirect(url_for("select_department"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not is_admin_mode():
        abort(404)
    if request.method == "POST":
        submitted = (request.form.get("password") or "").strip()
        if submitted == get_admin_password():
            session["admin_authenticated"] = True
            flash("Admin access granted.", "success")
            return redirect(url_for("dashboard"))
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    if not is_admin_mode():
        abort(404)
    session.pop("admin_authenticated", None)
    flash("Logged out.", "success")
    return redirect(url_for("admin_login"))


@app.post("/admin/theme/toggle")
@admin_required
def admin_toggle_theme():
    session["admin_dark_mode"] = not bool(session.get("admin_dark_mode"))
    return redirect(url_for("dashboard", **request.args))


@app.post("/admin/change-password")
@admin_required
def admin_change_password():
    try:
        current_password = (request.form.get("current_password") or "").strip()
        new_password = (request.form.get("new_password") or "").strip()
        confirm_password = (request.form.get("confirm_password") or "").strip()

        if current_password != get_admin_password():
            raise ValueError("Current password is incorrect.")
        if len(new_password) < 4:
            raise ValueError("New password must be at least 4 characters.")
        if new_password != confirm_password:
            raise ValueError("New password and confirm password do not match.")

        set_admin_password(new_password)
        db.session.commit()
        flash("Admin password updated.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/issue")
def issue_towels():
    if is_admin_mode():
        abort(404)
    try:
        department = current_client_department()
        if department is None:
            raise ValueError("Select a department first.")
        room_number = parse_int("Room number", request.form.get("room_number"))
        towel_count = parse_int("Towel count", request.form.get("towel_count"))
        voucher_numbers_raw = (request.form.get("voucher_numbers") or "").strip()

        if towel_count <= 0:
            raise ValueError("Towel count must be greater than zero.")
        if not room_is_allowed(room_number):
            raise ValueError("Room number is outside configured ranges.")
        if department.towel_stock < towel_count:
            raise ValueError(f"Not enough towels in stock for {department.name}.")

        free_vouchers = available_vouchers(department.id)
        if len(free_vouchers) < towel_count:
            raise ValueError("Not enough available voucher cards in the selected department.")
        free_by_number = {voucher.number: voucher for voucher in free_vouchers}
        selected_vouchers: list[VoucherCard]
        if voucher_numbers_raw:
            selected_numbers = parse_voucher_list(voucher_numbers_raw)
            if len(selected_numbers) != towel_count:
                raise ValueError("Voucher count must match towel count.")
            selected_vouchers = []
            for number in selected_numbers:
                selected = free_by_number.get(number)
                if selected is None:
                    raise ValueError(f"Voucher {number} is not available in this department.")
                selected_vouchers.append(selected)
        else:
            selected_vouchers = free_vouchers[:towel_count]
        deposit_total = TOWEL_DEPOSIT * towel_count
        rental = Rental(
            room_number=room_number,
            department_id=department.id,
            towel_count=towel_count,
            deposit_total=deposit_total,
            status="open",
        )
        db.session.add(rental)
        db.session.flush()
        department.towel_stock -= towel_count

        for voucher in selected_vouchers:
            db.session.add(RentalVoucher(rental_id=rental.id, voucher_card_id=voucher.id))

        voucher_numbers = ", ".join(str(v.number) for v in selected_vouchers)
        db.session.add(
            TransactionLog(
                transaction_type="issue",
                room_number=room_number,
                towel_count=towel_count,
                amount=deposit_total,
                department_id=department.id,
                details=f"Assigned vouchers: {voucher_numbers}. Stock now: {department.towel_stock}",
            )
        )
        db.session.commit()
        flash(
            f"Issued {towel_count} towels. Deposit: EUR {deposit_total}. Stock left: {department.towel_stock}",
            "success",
        )
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/return")
def return_towels():
    if is_admin_mode():
        abort(404)
    try:
        department = current_client_department()
        if department is None:
            raise ValueError("Select a department first.")
        rental_id = parse_int("Rental", request.form.get("rental_id"))
        vouchers_raw = request.form.get("voucher_numbers", "")
        returned_vouchers = parse_voucher_list(vouchers_raw)
        if not returned_vouchers:
            raise ValueError("Enter at least one returned voucher.")
        rental = Rental.query.get_or_404(rental_id)
        if rental.status != "open":
            raise ValueError("Rental is already closed.")
        if rental.department_id != department.id:
            raise ValueError("This rental belongs to another department.")

        rental_links = {link.voucher_card.number: link for link in rental.vouchers}
        invalid = [number for number in returned_vouchers if number not in rental_links]
        if invalid:
            raise ValueError(f"Vouchers not part of this rental: {', '.join(map(str, invalid))}")

        for number in returned_vouchers:
            db.session.delete(rental_links[number])

        returned_count = len(returned_vouchers)
        rental.towel_count -= returned_count
        rental.deposit_total = TOWEL_DEPOSIT * rental.towel_count
        if rental.towel_count == 0:
            rental.status = "closed"
            rental.closed_at = datetime.now(timezone.utc)
        refund = TOWEL_DEPOSIT * returned_count
        remaining_vouchers = sorted([number for number in rental_links if number not in returned_vouchers])
        db.session.add(
            TransactionLog(
                transaction_type="return",
                room_number=rental.room_number,
                towel_count=returned_count,
                amount=refund * -1,
                department_id=rental.department_id,
                details=(
                    f"Partial return vouchers: {', '.join(map(str, returned_vouchers))}. "
                    f"Remaining vouchers: {', '.join(map(str, remaining_vouchers)) or 'none'}. "
                    "Returned towels marked as dirty (not added to clean stock)."
                ),
            )
        )
        db.session.commit()
        if rental.status == "closed":
            flash(
                f"Rental closed. Refund due: EUR {refund}. Returned towels are not clean stock yet.",
                "success",
            )
        else:
            flash(
                f"Partial return recorded. Refunded EUR {refund}. "
                f"Remaining towels in rental: {rental.towel_count}. Returned towels are not clean stock yet.",
                "success",
            )
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/stock/adjust")
def adjust_stock():
    if is_admin_mode():
        abort(404)
    try:
        department = current_client_department()
        if department is None:
            raise ValueError("Select a department first.")
        quantity = parse_int("Towel quantity", request.form.get("quantity"))
        new_total = department.towel_stock + quantity
        if new_total < 0:
            raise ValueError("Stock cannot go below zero.")
        department.towel_stock = new_total

        db.session.add(
            TransactionLog(
                transaction_type="stock_adjust",
                room_number=0,
                towel_count=abs(quantity),
                amount=Decimal("0.00"),
                department_id=department.id,
                details=f"Stock adjusted by {quantity}. New stock: {department.towel_stock}",
            )
        )
        db.session.commit()
        flash(f"Stock updated. {department.name} towels available: {department.towel_stock}", "success")
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/admin/room-ranges")
@admin_required
def add_room_range():
    try:
        label = (request.form.get("label") or "").strip()
        start_room = parse_int("Start room", request.form.get("start_room"))
        end_room = parse_int("End room", request.form.get("end_room"))
        if start_room > end_room:
            raise ValueError("Start room must be less than or equal to end room.")
        if not label:
            label = f"Block {start_room}-{end_room}"
        db.session.add(RoomRange(label=label, start_room=start_room, end_room=end_room, active=True))
        db.session.commit()
        flash("Room range saved.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/admin/vouchers")
@admin_required
def add_voucher_range():
    try:
        department_id = parse_int("Department", request.form.get("department_id"))
        start_number = parse_int("Start voucher", request.form.get("start_number"))
        end_number = parse_int("End voucher", request.form.get("end_number"))
        if start_number > end_number:
            raise ValueError("Start voucher number must be less than or equal to end number.")

        created = 0
        skipped = 0
        for number in range(start_number, end_number + 1):
            exists = VoucherCard.query.filter_by(department_id=department_id, number=number).first()
            if exists:
                skipped += 1
                continue
            db.session.add(VoucherCard(department_id=department_id, number=number, active=True))
            created += 1

        db.session.commit()
        flash(f"Voucher import complete. Added: {created}, skipped existing: {skipped}.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.post("/admin/vouchers/<int:voucher_id>/toggle")
@admin_required
def toggle_voucher(voucher_id: int):
    try:
        voucher = VoucherCard.query.get_or_404(voucher_id)
        if voucher.assigned and voucher.active:
            raise ValueError("Cannot deactivate a voucher currently assigned to an open rental.")
        voucher.active = not voucher.active
        db.session.commit()
        state = "active" if voucher.active else "inactive"
        flash(f"Voucher {voucher.number} is now {state}.", "success")
    except Exception as exc:  # pylint: disable=broad-except
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.get("/export/transactions.csv")
@admin_required
def export_csv():
    logs = TransactionLog.query.order_by(TransactionLog.created_at.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "timestamp_utc", "type", "room_number", "towel_count", "amount_eur", "department", "details"])
    for item in logs:
        writer.writerow(
            [
                item.id,
                item.created_at.isoformat(),
                item.transaction_type,
                item.room_number,
                item.towel_count,
                str(item.amount),
                item.department.name if item.department else "",
                item.details or "",
            ]
        )
    data = io.BytesIO(output.getvalue().encode("utf-8"))
    return send_file(
        data,
        as_attachment=True,
        download_name="transactions.csv",
        mimetype="text/csv",
    )


@app.get("/export/summary.pdf")
@admin_required
def export_pdf():
    open_rentals = Rental.query.filter_by(status="open").all()
    logs = TransactionLog.query.order_by(TransactionLog.created_at.desc()).limit(25).all()
    total_deposit = sum(Decimal(r.deposit_total) for r in open_rentals)
    total_open_towels = sum(r.towel_count for r in open_rentals)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(40, y, "Hotel Towel Service - Snapshot Report")
    y -= 25
    pdf.setFont("Helvetica", 11)
    pdf.drawString(40, y, f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}")
    y -= 25
    pdf.drawString(40, y, f"Open rentals: {len(open_rentals)}")
    y -= 18
    pdf.drawString(40, y, f"Open towels: {total_open_towels}")
    y -= 18
    pdf.drawString(40, y, f"Open deposit total: EUR {total_deposit}")
    y -= 28
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(40, y, "Recent Transactions")
    y -= 20
    pdf.setFont("Helvetica", 10)
    for tx in logs:
        line = (
            f"{tx.created_at.strftime('%Y-%m-%d %H:%M')} | {tx.transaction_type.upper()} "
            f"| Room {tx.room_number} | Towels {tx.towel_count} | EUR {tx.amount}"
        )
        pdf.drawString(40, y, line[:105])
        y -= 15
        if y < 60:
            pdf.showPage()
            y = height - 50
            pdf.setFont("Helvetica", 10)
    pdf.save()

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="towel-service-summary.pdf",
        mimetype="application/pdf",
    )


def list_vouchers_by_department(department: Department) -> Iterable[VoucherCard]:
    return (
        VoucherCard.query.filter_by(department_id=department.id)
        .order_by(VoucherCard.number.asc())
        .all()
    )


def current_client_department() -> Department | None:
    department_id = session.get("client_department_id")
    if not department_id:
        return None
    return Department.query.filter_by(id=department_id).first()


@app.context_processor
def inject_helpers():
    selected_department = None
    if not is_admin_mode():
        selected_department = current_client_department()
    return {
        "list_vouchers_by_department": list_vouchers_by_department,
        "app_mode": APP_MODE,
        "admin_authenticated": bool(session.get("admin_authenticated")),
        "selected_department": selected_department,
        "admin_dark_mode": bool(session.get("admin_dark_mode")),
    }


with app.app_context():
    db.create_all()
    ensure_schema_updates()
    bootstrap_defaults()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
