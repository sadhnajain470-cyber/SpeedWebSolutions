"""CampusFlow school portal.

Database tables expected by the existing project:
teacher(id, tclass, tsec, password), student(id, student_name, password, tid, year, st_class, sec),
attendece(id, student_id, status, day, year), marks(id, student_id, subject, marks, term, session).
The legacy tsec and student.sec columns are retained in the database but are not used by the website.
"""

import os
from pathlib import Path
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, join_room
import psycopg
from psycopg.rows import dict_row
from psycopg import Error


def load_local_environment():
    """Load simple KEY=value entries from .env without requiring python-dotenv."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
        return
    except ImportError:
        pass

    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_local_environment()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "local-dev-change-this-secret"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)
socketio = SocketIO(app, async_mode="threading")  # Same-origin clients only; no wildcard CORS.

TERMS = ("Term 1", "Term 2", "Term 3")


def connect_db():
    """Create a fresh connection per operation instead of keeping a stale global one."""
    # Render may need Supabase's IPv4 shared pooler while local development uses
    # the direct IPv6 URL. Set DATABASE_POOLER_URL only on IPv4-only hosts.
    database_url = os.getenv("DATABASE_POOLER_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise psycopg.OperationalError(
            "DATABASE_URL is not configured. Add your Supabase Postgres connection string."
        )
    try:
        return psycopg.connect(database_url, connect_timeout=10, sslmode="require")
    except psycopg.OperationalError as error:
        hostname = urlsplit(database_url).hostname or ""
        network_error = str(error).lower()
        if (hostname.startswith("db.") and "supabase.co" in hostname
                and any(message in network_error for message in
                        ("network is unreachable", "no route to host", "timed out"))):
            raise psycopg.OperationalError(
                "Supabase direct database host is IPv6-only, but this host cannot reach IPv6. "
                "Set DATABASE_POOLER_URL to the Session pooler connection string from "
                "Supabase Dashboard > Connect > Session pooler, then redeploy."
            ) from error
        raise


def fetch_all(sql, params=()):
    connection = connect_db()
    cursor = connection.cursor(row_factory=dict_row)
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def next_table_id(cursor, table):
    """Allocate an ID for migrated tables that have no Postgres identity/default."""
    if table not in {"student", "attendece", "marks"}:
        raise ValueError("Unsupported table for ID allocation.")
    # The current Supabase tables have no PK/default sequence. Serialize writers
    # from this app while calculating MAX(id)+1 so simultaneous requests do not collide.
    cursor.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE")
    cursor.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM {table}")
    row = cursor.fetchone()
    return int(row["next_id"] if isinstance(row, dict) else row[0])


def emit_data_changed(entity, operation, teacher_id=None, student_ids=()):
    """Send metadata-only refresh events to the affected teacher and students."""
    payload = {"entity": entity, "operation": operation}
    try:
        if teacher_id is not None:
            socketio.emit("data_changed", payload, to=f"teacher:{teacher_id}")
        for student_id in set(student_ids):
            socketio.emit("data_changed", payload, to=f"student:{student_id}")
    except Exception:
        app.logger.exception("A database change was saved, but its live notification failed")


def login_required(role):
    key = "tid" if role == "teacher" else "stid"

    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if key not in session:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("teacher_login" if role == "teacher" else "student_login"))
            return view(*args, **kwargs)
        return wrapped
    return decorate


def parse_school_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.route("/", methods=["GET"], endpoint="home")
def home_page():
    return render_template("selectype.html")


@app.route("/logout", endpoint="logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/tlogin", methods=["GET", "POST"], endpoint="teacher_login")
def teacher_login_page():
    if request.method == "POST":
        teacher_id = request.form.get("id", "").strip()
        password = request.form.get("pwd", "")
        if not teacher_id or not password:
            flash("Enter your teacher ID and password.", "error")
        else:
            try:
                teacher = fetch_all(
                    "SELECT id FROM teacher WHERE id=%s AND password=%s LIMIT 1",
                    (teacher_id, password),
                )
                if teacher:
                    session.clear()
                    session.permanent = True
                    session["tid"] = str(teacher[0]["id"])
                    return redirect(url_for("teacher_home"))
                flash("Teacher ID or password is incorrect.", "error")
            except Error:
                app.logger.exception("Teacher login database error")
                flash("Supabase database connection failed. Check the app terminal for the exact error.", "error")
    return render_template("login.html", role="Teacher", endpoint="teacher_login")


@app.route("/teacherhome", endpoint="teacher_home")
@login_required("teacher")
def teacher_home_page():
    year = date.today().year
    stats = {"students": 0, "records": 0, "marks": 0}
    attendance_chart, term_chart = [], []
    try:
        student_count = fetch_all("SELECT COUNT(*) AS total FROM student WHERE tid=%s", (session["tid"],))
        stats["students"] = student_count[0]["total"] if student_count else 0
        recent = fetch_all(
            "SELECT a.day, SUM((LOWER(a.status)='present')::int) AS present, COUNT(*) AS total "
            "FROM attendece AS a JOIN student AS s ON s.id=a.student_id "
            "WHERE s.tid=%s AND a.year=%s AND a.day IS NOT NULL "
            "GROUP BY a.day ORDER BY a.day DESC LIMIT 7",
            (session["tid"], year),
        )
        recent.reverse()
        for row in recent:
            try:
                label = parse_school_date(str(row["day"])[:10]).strftime("%d %b")
            except (TypeError, ValueError):
                app.logger.warning("Skipping invalid attendance date in dashboard chart")
                continue
            attendance_chart.append({"label": label, "value": int(row["present"] or 0),
                                     "total": int(row["total"] or 0)})
        stats["records"] = sum(row["total"] for row in attendance_chart)
        mark_rows = fetch_all(
            "SELECT m.term,m.marks FROM marks AS m "
            "JOIN student AS s ON s.id=m.student_id "
            'WHERE s.tid=%s AND m."session"=%s',
            (session["tid"], year),
        )
        stats["marks"] = len(mark_rows)
        buckets = {"term1": [], "term2": [], "term3": []}
        for row in mark_rows:
            key = str(row["term"] or "").lower().replace(" ", "")
            if key in buckets and row["marks"] is not None:
                buckets[key].append(int(row["marks"]))
        term_chart = [{"label": f"Term {index}", "value": round(sum(buckets[f"term{index}"]) / len(buckets[f"term{index}"])) if buckets[f"term{index}"] else 0,
                       "count": len(buckets[f"term{index}"])} for index in range(1, 4)]
    except Error:
        app.logger.exception("Teacher dashboard chart query failed")
        flash("Some dashboard charts could not load from the database.", "error")
    return render_template("teacherhome.html", stats=stats, attendance_chart=attendance_chart,
                           term_chart=term_chart, dashboard_year=year)


@app.route("/admission", methods=["GET", "POST"], endpoint="admission")
def student_admission_page():
    teacher_id = session.get("tid")
    teacher_assignments = []
    try:
        if teacher_id:
            teacher_assignments = fetch_all(
                "SELECT DISTINCT tclass FROM teacher WHERE id=%s AND tclass IS NOT NULL ORDER BY tclass",
                (teacher_id,),
            )
        else:
            teacher_assignments = fetch_all(
                "SELECT DISTINCT tclass FROM teacher WHERE tclass IS NOT NULL ORDER BY tclass"
            )
    except Error:
        app.logger.exception("Could not load admission class choices")
        flash("Class choices could not be loaded.", "error")

    if request.method == "POST":
        fields = {
            "student_name": request.form.get("student_name", "").strip(),
            "father_name": request.form.get("father_name", "").strip(),
            "mother_name": request.form.get("mother_name", "").strip(),
            "phone_number": request.form.get("phone_number", "").strip(),
            "adhar_id": request.form.get("adhar_id", "").strip(),
            "gender": request.form.get("gender", "").strip(),
            "st_class": "",
            "adress": request.form.get("adress", "").strip(),
            "password": request.form.get("password", ""),
            "lastfeemonth": request.form.get("lastfeemonth", "").strip(),
        }
        try:
            class_number = int(request.form.get("class", ""))
            fields["st_class"] = str(class_number)
            last_fee_year = int(request.form.get("lastfeeyear", date.today().year))
            school_year = int(request.form.get("year", date.today().year))
            required = ("student_name", "father_name", "phone_number", "gender", "st_class", "password")
            if any(not fields[key].strip() if key == "password" else not fields[key] for key in required):
                raise ValueError("Fill in the required student, guardian, class and password fields.")
            limits = {"student_name": 50, "father_name": 50, "mother_name": 50, "phone_number": 12,
                      "adhar_id": 19, "gender": 50, "st_class": 50,
                      "adress": 500, "password": 30, "lastfeemonth": 255}
            if any(len(str(fields[key])) > limit for key, limit in limits.items()):
                raise ValueError("One or more fields exceed the maximum length allowed by the student table.")
            if not fields["phone_number"].isdigit() or not 7 <= len(fields["phone_number"]) <= 12:
                raise ValueError("Phone number must contain 7 to 12 digits.")
            if fields["adhar_id"]:
                if not fields["adhar_id"].isdigit() or int(fields["adhar_id"]) > 9223372036854775807:
                    raise ValueError("Aadhaar / ID number must be numeric and fit the database number field.")
            if not 2000 <= last_fee_year <= 2100 or not 2000 <= school_year <= 2100:
                raise ValueError("School and fee years must be between 2000 and 2100.")

            connection = connect_db()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id, tclass FROM teacher WHERE tclass=%s "
                    + ("AND id=%s " if teacher_id else "")
                    + "ORDER BY id LIMIT 1",
                    (int(fields["st_class"]), teacher_id) if teacher_id else (int(fields["st_class"]),),
                )
                assigned_teacher = cursor.fetchone()
                if not assigned_teacher:
                    raise ValueError(
                        f"Class {fields['st_class']} is not assigned to your account."
                    )
                teacher_id = assigned_teacher[0]
                assigned_id = next_table_id(cursor, "student")
                cursor.execute(
                    "INSERT INTO student (id,student_name,father_name,mother_name,phone_number,adhar_id,gender,"
                    "st_class,adress,password,lastfeemonth,lastfeeyear,tid,year) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (assigned_id, fields["student_name"], fields["father_name"], fields["mother_name"] or None,
                     int(fields["phone_number"]), int(fields["adhar_id"]) if fields["adhar_id"] else None,
                     fields["gender"], int(fields["st_class"]), fields["adress"] or None,
                     fields["password"], fields["lastfeemonth"] or None,
                     last_fee_year, teacher_id, school_year),
                )
                connection.commit()
                emit_data_changed("student", "created", teacher_id=teacher_id)
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()

            flash(f"Admission completed. Student ID: {assigned_id} | Teacher ID: {teacher_id}", "success")
            return redirect(url_for("admission"))
        except ValueError as error:
            flash(str(error), "error")
        except Error:
            app.logger.exception("Student admission database error")
            flash("Admission could not be saved. Check the student table and database connection.", "error")

    return render_template("admission.html", teacher_assignments=teacher_assignments,
                           school_year=date.today().year)


@app.route("/attendece", methods=["GET", "POST"], endpoint="attendance")
@login_required("teacher")
def attendance_page():
    # Existing attendece.html is intentionally unchanged:
    # Load form sends action=load; Save form sends no action field.
    selected_date = request.values.get("date", date.today().isoformat()).strip()
    students = []

    try:
        selected_day = parse_school_date(selected_date)
        # The migrated Supabase schema stores attendece.day as text.
        selected_day_text = selected_day.isoformat()
        selected_year = selected_day.year
        teacher_id = int(session["tid"])

        connection = connect_db()
        cursor = connection.cursor(row_factory=dict_row)
        try:
            if request.method == "POST" and request.form.get("action") != "load":
                # Re-query students on the save request; Flask requests do not
                # retain the students list from the previous Load request.
                cursor.execute(
                    "SELECT id FROM student WHERE tid=%s AND year=%s ORDER BY id",
                    (teacher_id, selected_year),
                )
                assigned_students = cursor.fetchall()

                if not assigned_students:
                    flash("No students are assigned to your account for this year.", "info")
                else:
                    attendance_updates = []
                    for student in assigned_students:
                        student_id = student["id"]
                        status = request.form.get(str(student_id), "").strip().lower()
                        if status not in ("present", "absent"):
                            raise ValueError(
                                f"Missing or invalid attendance status for student ID {student_id}."
                            )
                        attendance_updates.append((student_id, status))

                    if attendance_updates:
                        first_attendance_id = next_table_id(cursor, "attendece")
                        values_sql = ",".join(["(%s,%s)"] * len(attendance_updates))
                        values = [value for update in attendance_updates for value in update]
                        cursor.execute(
                            f"UPDATE attendece AS a SET status=v.status "
                            f"FROM (VALUES {values_sql}) AS v(student_id,status) "
                            "WHERE a.student_id=v.student_id AND a.day=%s AND a.year=%s",
                            (*values, selected_day_text, selected_year),
                        )
                        cursor.execute(
                            f"INSERT INTO attendece (id,student_id,status,day,year) "
                            f"SELECT %s + ROW_NUMBER() OVER (ORDER BY v.student_id) - 1, "
                            f"v.student_id,v.status,%s,%s FROM (VALUES {values_sql}) AS v(student_id,status) "
                            "WHERE NOT EXISTS (SELECT 1 FROM attendece AS a "
                            "WHERE a.student_id=v.student_id AND a.day=%s AND a.year=%s)",
                            (first_attendance_id, selected_day_text, selected_year, *values,
                             selected_day_text, selected_year),
                        )

                    connection.commit()
                    emit_data_changed(
                        "attendance", "saved", teacher_id=teacher_id,
                        student_ids=(student["id"] for student in assigned_students),
                    )
                    flash("Attendance saved successfully.", "success")

            cursor.execute(
                """
                SELECT
                    s.id AS student_id,
                    s.student_name AS name,
                    COALESCE(a.status, 'absent') AS status
                FROM student AS s
                LEFT JOIN attendece AS a
                    ON a.student_id = s.id
                    AND a.day = %s
                    AND a.year = %s
                WHERE s.tid = %s
                    AND s.year = %s
                ORDER BY s.id
                """,
                (selected_day_text, selected_year, teacher_id, selected_year),
            )
            students = cursor.fetchall()

        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    except ValueError as error:
        flash(str(error), "error")
    except Error:
        app.logger.exception("Attendance database error")
        flash("Attendance could not be processed. Check the database connection and table columns.", "error")

    return render_template(
        "attendece.html",
        students=students,
        selected_date=selected_date,
    )


@app.route("/deleteattendece", methods=["GET", "POST"], endpoint="delete_attendance")
@login_required("teacher")
def delete_attendance_page():
    if request.method == "POST":
        raw_student_id = request.form.get("student_id", "").strip()
        raw_day = request.form.get("day", "")
        try:
            student_id = int(raw_student_id)
            if student_id < 1:
                raise ValueError
            day = parse_school_date(raw_day)
            # A teacher can delete records only for students assigned to them.
            connection = connect_db()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM attendece AS a USING student AS s "
                    "WHERE s.id=a.student_id AND s.tid=%s AND s.id=%s AND a.day=%s",
                    (session["tid"], student_id, day.isoformat()),
                )
                deleted = cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()
            if deleted:
                emit_data_changed("attendance", "deleted", teacher_id=session["tid"],
                                  student_ids=(student_id,))
            flash("Attendance record deleted." if deleted else "No matching record was found for your class.",
                  "success" if deleted else "info")
        except ValueError:
            flash("Enter a valid student ID and date.", "error")
        except Error:
            app.logger.exception("Delete attendance database error")
            flash("The attendance record could not be deleted.", "error")
    return render_template("deleteatt.html")


@app.route("/successatt", methods=["GET"], endpoint="success_att")
@login_required("teacher")
def attendance_success_page():
    return render_template("success.html", title="Attendance saved", message="Your class attendance has been saved.", back="attendance")


def read_mark_form():
    raw_student_id = request.form.get("student_id", "").strip()
    subject = request.form.get("subject", "").strip()
    raw_session = request.form.get("session", "").strip()
    term = request.form.get("term", "")
    try:
        marks = int(request.form.get("marks", ""))
    except (TypeError, ValueError):
        raise ValueError("Marks must be a whole number from 0 to 100.")
    try:
        school_session = int(raw_session)
    except (TypeError, ValueError):
        raise ValueError("School session must be a year, for example 2026.")
    if not raw_student_id or not subject or not raw_session:
        raise ValueError("Fill in the student ID, subject and school session.")
    try:
        student_id = int(raw_student_id)
    except ValueError:
        raise ValueError("Student ID must be numeric.")
    if student_id < 1:
        raise ValueError("Student ID must be a positive number.")
    if not subject or len(subject) > 50:
        raise ValueError("Subject is required and must be 50 characters or fewer.")
    if term not in TERMS:
        raise ValueError("Choose Term 1, Term 2 or Term 3.")
    if not 2000 <= school_session <= 2100:
        raise ValueError("School session must be between 2000 and 2100.")
    if not 0 <= marks <= 100:
        raise ValueError("Marks must be a whole number from 0 to 100.")
    return student_id, subject, marks, term, school_session


@app.route("/marks", methods=["GET", "POST"], endpoint="marks_entry")
@login_required("teacher")
def marks_entry_page():
    if request.method == "POST":
        try:
            student_id, subject, marks, term, school_session = read_mark_form()
            connection = connect_db()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id FROM student WHERE id=%s AND tid=%s LIMIT 1",
                    (student_id, session["tid"]),
                )
                student = cursor.fetchone()
                if not student:
                    connection.rollback()
                    flash("That student is not assigned to your teacher account.", "error")
                else:
                    student_id = student[0]
                    mark_id = next_table_id(cursor, "marks")
                    cursor.execute(
                        'INSERT INTO marks (id,student_id,subject,marks,term,"session") VALUES (%s,%s,%s,%s,%s,%s)',
                        (mark_id, student_id, subject, marks, term, school_session),
                    )
                    connection.commit()
                    emit_data_changed("marks", "created", teacher_id=session["tid"],
                                      student_ids=(student_id,))
                    flash("Marks saved successfully.", "success")
                    return redirect(url_for("marks_entry"))
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()
        except ValueError as error:
            flash(str(error), "error")
        except Error:
            app.logger.exception("Add marks database error")
            flash("Marks could not be saved. Check the database settings and table columns.", "error")
    return render_template("marks.html", terms=TERMS)


@app.route("/updatemarks", methods=["GET", "POST"], endpoint="update_marks")
@login_required("teacher")
def update_marks_page():
    if request.method == "POST":
        try:
            student_id, subject, marks, term, school_session = read_mark_form()
            connection = connect_db()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id FROM student WHERE id=%s AND tid=%s LIMIT 1",
                    (student_id, session["tid"]),
                )
                if not cursor.fetchone():
                    connection.rollback()
                    flash("That student is not assigned to your teacher account.", "error")
                    return render_template("updatemarks.html", terms=TERMS)

                cursor.execute(
                    "SELECT id FROM marks WHERE student_id=%s AND subject=%s "
                    'AND LOWER(REPLACE(term,\' \',\'\'))=%s AND "session"=%s LIMIT 1',
                    (student_id, subject, term.lower().replace(" ", ""), school_session),
                )
                if not cursor.fetchone():
                    connection.rollback()
                    flash("No matching mark record was found.", "info")
                    return render_template("updatemarks.html", terms=TERMS)

                cursor.execute(
                    "UPDATE marks SET marks=%s, term=%s WHERE student_id=%s "
                    'AND subject=%s AND LOWER(REPLACE(term,\' \',\'\'))=%s AND "session"=%s',
                    (marks, term, student_id, subject,
                     term.lower().replace(" ", ""), school_session),
                )
                count = cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.close()
            if count:
                emit_data_changed("marks", "updated", teacher_id=session["tid"],
                                  student_ids=(student_id,))
            flash("Marks updated." if count else "Marks were already set to that value.", "success")
            return redirect(url_for("update_marks"))
        except ValueError as error:
            flash(str(error), "error")
        except Error:
            app.logger.exception("Update marks database error")
            flash("Marks could not be updated. Check the database settings and table columns.", "error")
    return render_template("updatemarks.html", terms=TERMS)


@app.route("/sucessmarks", methods=["GET"], endpoint="success_marks")
@login_required("teacher")
def marks_success_page():
    return render_template("success.html", title="Marks saved", message="The student result has been saved.", back="marks_entry")


@app.route("/stdlogin", methods=["GET", "POST"], endpoint="student_login")
def student_login_page():
    if request.method == "POST":
        student_id = request.form.get("id", "").strip()
        password = request.form.get("pwd", "")
        if not student_id or not password:
            flash("Enter your student ID and password.", "error")
        else:
            try:
                student = fetch_all(
                    "SELECT id FROM student WHERE id=%s AND password=%s LIMIT 1",
                    (student_id, password),
                )
                if student:
                    session.clear()
                    session.permanent = True
                    session["stid"] = int(student[0]["id"])
                    return redirect(url_for("student_home"))
                flash("Student ID or password is incorrect.", "error")
            except Error:
                app.logger.exception("Student login database error")
                flash("Supabase database connection failed. Check the app terminal for the exact error.", "error")
    return render_template("login.html", role="Student", endpoint="student_login")


@app.route("/studenthome", endpoint="student_home")
@login_required("student")
def student_home_page():
    year = date.today().year
    attendance_rows, marks_rows = [], []
    stats = {"present": 0, "days": 0, "average": 0}
    try:
        attendance_rows = fetch_all(
            "SELECT status FROM attendece WHERE year=%s AND student_id=%s",
            (year, session["stid"]),
        )
        stats["days"] = len(attendance_rows)
        stats["present"] = sum(str(row["status"]).lower() == "present" for row in attendance_rows)
        marks_rows = fetch_all(
            'SELECT term,marks FROM marks WHERE "session"=%s AND student_id=%s',
            (year, session["stid"]),
        )
        buckets = {"term1": [], "term2": [], "term3": []}
        for row in marks_rows:
            key = str(row["term"] or "").lower().replace(" ", "")
            if key in buckets and row["marks"] is not None:
                buckets[key].append(int(row["marks"]))
        averages = ([{"label": f"Term {index}", "value": round(sum(buckets[f"term{index}"]) / len(buckets[f"term{index}"])) if buckets[f"term{index}"] else 0,
                      "count": len(buckets[f"term{index}"])} for index in range(1, 4)] if marks_rows else [])
        all_marks = [int(row["marks"]) for row in marks_rows if row["marks"] is not None]
        stats["average"] = round(sum(all_marks) / len(all_marks)) if all_marks else 0
    except Error:
        app.logger.exception("Student dashboard chart query failed")
        flash("Some progress charts could not load from the database.", "error")
        averages = []
    return render_template("studenthome.html", stats=stats, attendance_chart={"present": stats["present"], "total": stats["days"]},
                           term_chart=averages, dashboard_year=year)


@app.route("/attstd", methods=["GET", "POST"], endpoint="student_attendance")
@login_required("student")
def student_attendance_page():
    total = present = 0
    selected_year = str(request.form.get("year", date.today().year)).strip()
    if request.method == "POST":
        if not selected_year.isdigit() or len(selected_year) != 4:
            flash("Enter a valid four-digit year.", "error")
        else:
            try:
                rows = fetch_all(
                    "SELECT status FROM attendece WHERE year=%s AND student_id=%s",
                    (int(selected_year), session["stid"]),
                )
                total = len(rows)
                present = sum(str(row["status"]).lower() == "present" for row in rows)
                if not total:
                    flash("No attendance records were found for that year.", "info")
            except Error:
                app.logger.exception("Student attendance database error")
                flash("Attendance could not be loaded. Check the database settings.", "error")
    return render_template("attstd.html", total=total, present=present, selected_year=selected_year)


@app.route("/stdmarks", methods=["GET", "POST"], endpoint="student_marks")
@login_required("student")
def student_marks_page():
    rows = []
    trend_chart = []
    term = request.form.get("term", TERMS[0])
    raw_session = request.form.get("session", "").strip()
    if request.method == "POST":
        if term not in TERMS:
            flash("Choose Term 1, Term 2 or Term 3.", "error")
        else:
            try:
                school_session = int(raw_session)
                rows = fetch_all(
                    'SELECT subject,marks FROM marks WHERE "session"=%s '
                    'AND LOWER(REPLACE(term,\' \',\'\'))=%s AND student_id=%s ORDER BY subject',
                    (school_session, term.lower().replace(" ", ""), session["stid"]),
                )
                all_rows = fetch_all(
                    'SELECT term,marks FROM marks WHERE "session"=%s AND student_id=%s',
                    (school_session, session["stid"]),
                )
                buckets = {"term1": [], "term2": [], "term3": []}
                for result in all_rows:
                    key = str(result["term"] or "").lower().replace(" ", "")
                    if key in buckets and result["marks"] is not None:
                        buckets[key].append(int(result["marks"]))
                trend_chart = [{"label": f"Term {index}",
                                "value": round(sum(buckets[f"term{index}"]) / len(buckets[f"term{index}"])) if buckets[f"term{index}"] else 0,
                                "count": len(buckets[f"term{index}"])} for index in range(1, 4)]
                if not rows:
                    flash("No marks found for that term and school session.", "info")
            except ValueError:
                flash("School session must be a year, for example 2026.", "error")
            except Error:
                app.logger.exception("Student marks database error")
                flash("Marks could not be loaded. Check the database settings.", "error")
    return render_template(
        "stdmarks.html", list1=rows, terms=TERMS,
        selected_term=term, selected_session=raw_session,
        chart_data=[{"label": row["subject"], "value": int(row["marks"] or 0)} for row in rows],
        average_mark=round(sum(int(row["marks"] or 0) for row in rows) / len(rows)) if rows else 0,
        trend_chart=trend_chart,
        score_mix=[
            {"label": "Strong · 80–100", "value": sum(int(row["marks"] or 0) >= 80 for row in rows), "color": "#58f3df"},
            {"label": "On track · 60–79", "value": sum(60 <= int(row["marks"] or 0) < 80 for row in rows), "color": "#aa7aff"},
            {"label": "Keep building · 0–59", "value": sum(int(row["marks"] or 0) < 60 for row in rows), "color": "#ff68c9"},
        ],
    )


@socketio.on("connect")
def socket_connected(auth=None):
    if session.get("tid"):
        join_room(f"teacher:{session['tid']}")
        app.logger.info("A teacher connected to CampusFlow live updates")
    elif session.get("stid"):
        join_room(f"student:{session['stid']}")
        app.logger.info("A student connected to CampusFlow live updates")
    else:
        return False


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    socketio.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=debug,
        allow_unsafe_werkzeug=debug,
    )
