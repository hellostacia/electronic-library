import csv
import hashlib
import io
import os
import uuid
from datetime import datetime, date, time, timedelta
from xml.sax.saxutils import escape
from functools import wraps

import bleach
import markdown
from flask import (
    Flask, Response, flash, redirect, render_template, request, session,
    url_for
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required, login_user,
    logout_user
)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import event, func
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

app = Flask(__name__)
app.config["SECRET_KEY"] = "exam-library-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///library.sqlite"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Для выполнения данного действия необходимо пройти процедуру аутентификации"
login_manager.login_message_category = "warning"


book_genres = db.Table(
    "book_genres",
    db.Column("book_id", db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), primary_key=True),
    db.Column("genre_id", db.Integer, db.ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False)


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(100), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    middle_name = db.Column(db.String(100))
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)

    role = db.relationship("Role")
    reviews = db.relationship("Review", back_populates="user", cascade="all, delete-orphan")
    visits = db.relationship("BookVisit", back_populates="user")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def full_name(self):
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(part for part in parts if part)

    def has_role(self, *roles):
        return self.role is not None and self.role.name in roles


class Genre(db.Model):
    __tablename__ = "genres"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)


class Book(db.Model):
    __tablename__ = "books"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    publisher = db.Column(db.String(255), nullable=False)
    author = db.Column(db.String(255), nullable=False)
    pages = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    genres = db.relationship("Genre", secondary=book_genres, backref="books")
    cover = db.relationship("Cover", back_populates="book", cascade="all, delete-orphan", uselist=False)
    reviews = db.relationship("Review", back_populates="book", cascade="all, delete-orphan")
    visits = db.relationship("BookVisit", back_populates="book", cascade="all, delete-orphan")

    @property
    def average_rating(self):
        if not self.reviews:
            return None
        return round(sum(review.rating for review in self.reviews) / len(self.reviews), 1)

    @property
    def review_count(self):
        return len(self.reviews)

    @property
    def genres_string(self):
        return ", ".join(genre.name for genre in self.genres)


class Cover(db.Model):
    __tablename__ = "covers"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    md5_hash = db.Column(db.String(32), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False, unique=True)

    book = db.relationship("Book", back_populates="cover")


class Review(db.Model):
    __tablename__ = "reviews"
    __table_args__ = (db.UniqueConstraint("book_id", "user_id", name="unique_user_book_review"),)

    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    book = db.relationship("Book", back_populates="reviews")
    user = db.relationship("User", back_populates="reviews")


class BookVisit(db.Model):
    __tablename__ = "book_visits"

    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    visitor_id = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    book = db.relationship("Book", back_populates="visits")
    user = db.relationship("User", back_populates="visits")


class Pagination:
    def __init__(self, items, page, per_page, total):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total
        self.pages = max((total + per_page - 1) // per_page, 1)

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def prev_num(self):
        return self.page - 1

    @property
    def next_num(self):
        return self.page + 1

    def iter_pages(self):
        return range(1, self.pages + 1)


def paginate_query(query, page, per_page=10):
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return Pagination(items, page, per_page, total)


def paginate_items(items, page, per_page=10):
    total = len(items)
    start = (page - 1) * per_page
    finish = start + per_page
    return Pagination(items[start:finish], page, per_page, total)


def normalize_search_text(value):
    return (value or "").casefold()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_visitor_id():
    if "visitor_id" not in session:
        session["visitor_id"] = str(uuid.uuid4())
    return session["visitor_id"]


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(*args, **kwargs):
            if not current_user.has_role(*roles):
                flash("У вас недостаточно прав для выполнения данного действия", "warning")
                return redirect(url_for("index"))
            return view_func(*args, **kwargs)
        return wrapper
    return decorator


def can_edit_books():
    return current_user.is_authenticated and current_user.has_role("Администратор", "Модератор")


def can_delete_books():
    return current_user.is_authenticated and current_user.has_role("Администратор")


def can_create_books():
    return current_user.is_authenticated and current_user.has_role("Администратор")


@app.context_processor
def inject_permissions():
    return {
        "can_edit_books": can_edit_books,
        "can_delete_books": can_delete_books,
        "can_create_books": can_create_books,
    }


ALLOWED_TAGS = [
    "p", "br", "strong", "em", "ul", "ol", "li", "blockquote", "code", "pre",
    "h1", "h2", "h3", "h4", "a"
]
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "target", "rel"]}


def sanitize_markdown_text(text):
    return bleach.clean(text or "", tags=[], attributes={}, strip=True)


def markdown_to_html(text):
    html = markdown.markdown(text or "", extensions=["extra", "nl2br"])
    clean_html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)
    return Markup(clean_html)


@app.template_filter("markdown")
def markdown_filter(text):
    return markdown_to_html(text)


@app.template_filter("datetime")
def datetime_filter(value):
    if not value:
        return ""
    return value.strftime("%d.%m.%Y %H:%M:%S")


@app.template_filter("stars")
def stars_filter(value):
    try:
        rating = int(round(float(value)))
    except (TypeError, ValueError):
        rating = 0
    rating = max(0, min(rating, 5))
    return "★" * rating + "☆" * (5 - rating)


def validate_book_form(form, is_create=True):
    errors = {}
    required_fields = {
        "title": "Название обязательно.",
        "description": "Описание обязательно.",
        "year": "Год обязателен.",
        "publisher": "Издательство обязательно.",
        "author": "Автор обязателен.",
        "pages": "Объём в страницах обязателен.",
    }
    for field, message in required_fields.items():
        if not form.get(field, "").strip():
            errors[field] = message

    try:
        year = int(form.get("year", ""))
        if year < 1 or year > datetime.now().year:
            errors["year"] = "Введите корректный год."
    except ValueError:
        errors["year"] = "Год должен быть числом."

    try:
        pages = int(form.get("pages", ""))
        if pages <= 0:
            errors["pages"] = "Количество страниц должно быть положительным числом."
    except ValueError:
        errors["pages"] = "Количество страниц должно быть числом."

    if not form.getlist("genre_ids"):
        errors["genre_ids"] = "Выберите хотя бы один жанр."

    if is_create:
        cover_file = request.files.get("cover")
        if not cover_file or not cover_file.filename:
            errors["cover"] = "Загрузите обложку книги."
        elif not (cover_file.mimetype or "").startswith("image/"):
            errors["cover"] = "Файл обложки должен быть изображением."

    return errors


def fill_book_from_form(book, form):
    book.title = form.get("title", "").strip()
    book.description = sanitize_markdown_text(form.get("description", ""))
    book.year = int(form.get("year"))
    book.publisher = form.get("publisher", "").strip()
    book.author = form.get("author", "").strip()
    book.pages = int(form.get("pages"))
    genre_ids = [int(genre_id) for genre_id in form.getlist("genre_ids")]
    book.genres = Genre.query.filter(Genre.id.in_(genre_ids)).all()


def extension_from_filename(filename):
    safe_name = secure_filename(filename)
    _, ext = os.path.splitext(safe_name)
    return ext.lower() or ".img"


def create_cover_for_book(book, uploaded_file):
    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    existing_cover = Cover.query.filter_by(md5_hash=md5_hash).first()

    if existing_cover:
        cover = Cover(
            filename=existing_cover.filename,
            mime_type=existing_cover.mime_type,
            md5_hash=md5_hash,
            book=book,
        )
        db.session.add(cover)
        return None

    cover = Cover(
        filename="pending",
        mime_type=uploaded_file.mimetype,
        md5_hash=md5_hash,
        book=book,
    )
    db.session.add(cover)
    db.session.flush()
    filename = f"{cover.id}{extension_from_filename(uploaded_file.filename)}"
    cover.filename = filename
    return file_bytes, filename


def remove_cover_file_if_unused(filename):
    if not filename:
        return
    is_used = Cover.query.filter_by(filename=filename).first() is not None
    if not is_used:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        if os.path.exists(file_path):
            os.remove(file_path)


def register_book_visit(book):
    visitor_id = get_visitor_id()
    user_id = current_user.id if current_user.is_authenticated else None
    today_start = datetime.combine(date.today(), time.min)
    today_end = datetime.combine(date.today(), time.max)

    query = BookVisit.query.filter(
        BookVisit.book_id == book.id,
        BookVisit.created_at >= today_start,
        BookVisit.created_at <= today_end,
    )
    if user_id:
        query = query.filter(BookVisit.user_id == user_id)
    else:
        query = query.filter(BookVisit.user_id.is_(None), BookVisit.visitor_id == visitor_id)

    if query.count() < 10:
        visit = BookVisit(book=book, user_id=user_id, visitor_id=visitor_id)
        db.session.add(visit)
        db.session.commit()


def get_popular_books():
    since = datetime.utcnow() - timedelta(days=90)
    return (
        db.session.query(Book, func.count(BookVisit.id).label("views_count"))
        .join(BookVisit)
        .filter(BookVisit.created_at >= since)
        .group_by(Book.id)
        .order_by(func.count(BookVisit.id).desc(), Book.title.asc())
        .limit(5)
        .all()
    )


def get_recent_books():
    visitor_id = get_visitor_id()
    query = (
        db.session.query(Book, func.max(BookVisit.created_at).label("last_visit"))
        .join(BookVisit)
    )
    if current_user.is_authenticated:
        query = query.filter(BookVisit.user_id == current_user.id)
    else:
        query = query.filter(BookVisit.user_id.is_(None), BookVisit.visitor_id == visitor_id)
    return (
        query.group_by(Book.id)
        .order_by(func.max(BookVisit.created_at).desc())
        .limit(5)
        .all()
    )


def parse_date(value, end=False):
    if not value:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if end:
        return datetime.combine(parsed, time.max)
    return datetime.combine(parsed, time.min)


def build_view_stats_query():
    query = (
        db.session.query(Book, func.count(BookVisit.id).label("views_count"))
        .join(BookVisit)
        .filter(BookVisit.user_id.isnot(None))
    )
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    try:
        start = parse_date(date_from)
        finish = parse_date(date_to, end=True)
    except ValueError:
        flash("Дата указана в неверном формате.", "warning")
        start = finish = None

    if start:
        query = query.filter(BookVisit.created_at >= start)
    if finish:
        query = query.filter(BookVisit.created_at <= finish)

    return query.group_by(Book.id).order_by(func.count(BookVisit.id).desc(), Book.title.asc())


@app.route("/")
def index():
    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", "").strip()
    books_query = Book.query.order_by(Book.year.desc(), Book.id.desc())

    if q:
        normalized_query = normalize_search_text(q)
        matched_books = []
        for book in books_query.all():
            genre_names = " ".join(genre.name for genre in book.genres)
            search_area = " ".join([book.title, book.author, book.publisher, genre_names])
            search_area = normalize_search_text(search_area)
            if normalized_query in search_area:
                matched_books.append(book)
        books = paginate_items(matched_books, page, 10)
    else:
        books = paginate_query(books_query, page, 10)

    return render_template(
        "index.html",
        books=books,
        popular_books=get_popular_books(),
        recent_books=get_recent_books(),
        q=q,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        user = User.query.filter_by(login=login_value).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            return redirect(request.args.get("next") or url_for("index"))

        flash("Невозможно аутентифицироваться с указанными логином и паролем", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из системы.", "info")
    return redirect(request.referrer or url_for("index"))


@app.route("/books/<int:book_id>")
def show_book(book_id):
    book = db.get_or_404(Book, book_id)
    register_book_visit(book)
    own_review = None
    if current_user.is_authenticated:
        own_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
    reviews_query = Review.query.filter_by(book_id=book.id).order_by(Review.created_at.desc())
    if own_review:
        reviews_query = reviews_query.filter(Review.id != own_review.id)
    reviews = reviews_query.all()
    return render_template("show_book.html", book=book, reviews=reviews, own_review=own_review)


@app.route("/books/new", methods=["GET", "POST"])
@role_required("Администратор")
def new_book():
    genres = Genre.query.order_by(Genre.name).all()
    selected_genres = []

    if request.method == "POST":
        selected_genres = request.form.getlist("genre_ids")
        errors = validate_book_form(request.form, is_create=True)
        if errors:
            return render_template("book_form.html", book=None, genres=genres, errors=errors, form=request.form, selected_genres=selected_genres, mode="create")

        file_to_save = None
        try:
            book = Book()
            fill_book_from_form(book, request.form)
            db.session.add(book)
            db.session.flush()
            file_to_save = create_cover_for_book(book, request.files["cover"])
            db.session.commit()
            if file_to_save:
                file_bytes, filename = file_to_save
                with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as file:
                    file.write(file_bytes)
            flash("Книга успешно добавлена.", "success")
            return redirect(url_for("show_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")

    return render_template("book_form.html", book=None, genres=genres, errors={}, form=request.form, selected_genres=selected_genres, mode="create")


@app.route("/books/<int:book_id>/edit", methods=["GET", "POST"])
@role_required("Администратор", "Модератор")
def edit_book(book_id):
    book = db.get_or_404(Book, book_id)
    genres = Genre.query.order_by(Genre.name).all()
    selected_genres = [str(genre.id) for genre in book.genres]

    if request.method == "POST":
        selected_genres = request.form.getlist("genre_ids")
        errors = validate_book_form(request.form, is_create=False)
        if errors:
            return render_template("book_form.html", book=book, genres=genres, errors=errors, form=request.form, selected_genres=selected_genres, mode="edit")
        try:
            fill_book_from_form(book, request.form)
            db.session.commit()
            flash("Данные книги успешно обновлены.", "success")
            return redirect(url_for("show_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")

    return render_template("book_form.html", book=book, genres=genres, errors={}, form={}, selected_genres=selected_genres, mode="edit")


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@role_required("Администратор")
def delete_book(book_id):
    book = db.get_or_404(Book, book_id)
    filename = book.cover.filename if book.cover else None
    try:
        db.session.delete(book)
        db.session.commit()
        remove_cover_file_if_unused(filename)
        flash("Книга успешно удалена.", "success")
    except Exception:
        db.session.rollback()
        flash("При удалении книги возникла ошибка.", "danger")
    return redirect(url_for("index"))


@app.route("/books/<int:book_id>/reviews/new", methods=["GET", "POST"])
@login_required
def new_review(book_id):
    book = db.get_or_404(Book, book_id)
    existing_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
    if existing_review:
        flash("Вы уже оставляли рецензию на эту книгу.", "info")
        return redirect(url_for("show_book", book_id=book.id))

    if request.method == "POST":
        try:
            rating = int(request.form.get("rating", 5))
            text = sanitize_markdown_text(request.form.get("text", ""))
            if rating < 0 or rating > 5 or not text.strip():
                raise ValueError
            review = Review(book=book, user=current_user, rating=rating, text=text)
            db.session.add(review)
            db.session.commit()
            flash("Рецензия успешно добавлена.", "success")
            return redirect(url_for("show_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении рецензии возникла ошибка. Проверьте введённые данные.", "danger")

    return render_template("review_form.html", book=book)


@app.route("/statistics")
@role_required("Администратор")
def statistics():
    active_tab = request.args.get("tab", "logs")
    page = request.args.get("page", 1, type=int)

    logs = None
    stats = None
    if active_tab == "views":
        stats = paginate_query(build_view_stats_query(), page, 10)
    else:
        logs_query = BookVisit.query.order_by(BookVisit.created_at.desc())
        logs = paginate_query(logs_query, page, 10)
        active_tab = "logs"

    total_logs = BookVisit.query.count()
    authenticated_views = BookVisit.query.filter(BookVisit.user_id.isnot(None)).count()
    viewed_books = db.session.query(BookVisit.book_id).distinct().count()
    last_visit = BookVisit.query.order_by(BookVisit.created_at.desc()).first()

    return render_template(
        "statistics.html",
        active_tab=active_tab,
        logs=logs,
        stats=stats,
        total_logs=total_logs,
        authenticated_views=authenticated_views,
        viewed_books=viewed_books,
        last_visit=last_visit,
    )


@app.route("/statistics/export/logs")
@role_required("Администратор")
def export_logs():
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["№", "Пользователь", "Книга", "Дата и время просмотра"])
    visits = BookVisit.query.order_by(BookVisit.created_at.desc()).all()
    for index, visit in enumerate(visits, start=1):
        writer.writerow([
            index,
            visit.user.full_name if visit.user else "Неаутентифицированный пользователь",
            visit.book.title,
            visit.created_at.strftime("%d.%m.%Y %H:%M:%S"),
        ])
    filename = f"visit_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/statistics/export/views")
@role_required("Администратор")
def export_views():
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["№", "Книга", "Количество просмотров"])
    for index, row in enumerate(build_view_stats_query().all(), start=1):
        writer.writerow([index, row.Book.title, row.views_count])
    filename = f"book_views_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def split_cover_text(text, line_length=18):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= line_length:
            current = f"{current} {word}".strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:4]


def create_demo_cover(book, color, accent, label):
    title_lines = split_cover_text(book.title)
    author_lines = split_cover_text(book.author, 22)
    title_svg = "".join(
        f"<text x='210' y='{210 + index * 45}' font-size='34' font-weight='700' text-anchor='middle' fill='white' font-family='Arial'>{escape(line)}</text>"
        for index, line in enumerate(title_lines)
    )
    author_svg = "".join(
        f"<text x='210' y='{430 + index * 28}' font-size='21' text-anchor='middle' fill='white' opacity='0.95' font-family='Arial'>{escape(line)}</text>"
        for index, line in enumerate(author_lines)
    )
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='420' height='620' viewBox='0 0 420 620'>
<defs>
    <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
        <stop offset='0%' stop-color='{color}'/>
        <stop offset='100%' stop-color='{accent}'/>
    </linearGradient>
</defs>
<rect width='420' height='620' rx='18' fill='url(#g)'/>
<rect x='32' y='36' width='356' height='548' rx='14' fill='none' stroke='rgba(255,255,255,0.7)' stroke-width='3'/>
<text x='210' y='92' font-size='17' letter-spacing='3' text-anchor='middle' fill='white' opacity='0.85' font-family='Arial'>ЭЛЕКТРОННАЯ БИБЛИОТЕКА</text>
<line x1='92' y1='122' x2='328' y2='122' stroke='white' stroke-width='2' opacity='0.65'/>
{title_svg}
{author_svg}
<circle cx='210' cy='535' r='38' fill='rgba(255,255,255,0.16)'/>
<text x='210' y='544' font-size='24' font-weight='700' text-anchor='middle' fill='white' font-family='Arial'>{escape(label)}</text>
</svg>""".encode("utf-8")
    md5_hash = hashlib.md5(svg).hexdigest()
    existing_cover = Cover.query.filter_by(book_id=book.id).first()
    if existing_cover:
        return
    cover = Cover(filename="pending", mime_type="image/svg+xml", md5_hash=md5_hash, book=book)
    db.session.add(cover)
    db.session.flush()
    cover.filename = f"{cover.id}.svg"
    with open(os.path.join(app.config["UPLOAD_FOLDER"], cover.filename), "wb") as file:
        file.write(svg)


def add_review_once(book, user, rating, text):
    if book and user and not Review.query.filter_by(book_id=book.id, user_id=user.id).first():
        db.session.add(Review(book=book, user=user, rating=rating, text=sanitize_markdown_text(text)))


def create_demo_visits(users_by_login):
    books = Book.query.order_by(Book.id).all()
    if not books or BookVisit.query.count() >= 25:
        return

    now = datetime.utcnow()
    visit_plan = []
    # Много просмотров у разных книг, чтобы сразу были видны популярные книги,
    # журнал действий и статистика администратора.
    for book_index, book in enumerate(books[:10]):
        view_count = max(2, 16 - book_index)
        for i in range(view_count):
            user = [users_by_login.get("admin"), users_by_login.get("moderator"), users_by_login.get("user"), users_by_login.get("reader")][i % 4]
            is_anonymous = i % 5 == 0
            visit_plan.append({
                "book": book,
                "user": None if is_anonymous else user,
                "visitor_id": f"demo-anonymous-{book.id}-{i}" if is_anonymous else f"demo-user-{user.id}",
                "created_at": now - timedelta(days=(book_index * 3 + i) % 75, hours=i % 12, minutes=book_index * 7 + i),
            })

    for item in visit_plan:
        db.session.add(BookVisit(
            book=item["book"],
            user_id=item["user"].id if item["user"] else None,
            visitor_id=item["visitor_id"],
            created_at=item["created_at"],
        ))


def seed_data():
    db.create_all()

    roles_data = [
        ("Администратор", "Суперпользователь, имеет полный доступ к системе, включая создание и удаление книг."),
        ("Модератор", "Может редактировать данные книг и работать с содержимым библиотеки."),
        ("Пользователь", "Может просматривать книги и оставлять рецензии."),
    ]
    roles = {}
    for name, description in roles_data:
        role = Role.query.filter_by(name=name).first()
        if not role:
            role = Role(name=name, description=description)
            db.session.add(role)
        roles[name] = role
    db.session.commit()

    users_data = [
        ("admin", "Admin123!", "Админова", "Анастасия", "Сергеевна", "Администратор"),
        ("moderator", "Moderator123!", "Модеров", "Максим", "Игоревич", "Модератор"),
        ("user", "User123!", "Иванова", "Мария", "Петровна", "Пользователь"),
        ("reader", "Reader123!", "Смирнов", "Даниил", "Олегович", "Пользователь"),
        ("critic", "Critic123!", "Крылова", "Елена", "Андреевна", "Пользователь"),
        ("booklover", "Booklover123!", "Орлов", "Павел", "Николаевич", "Пользователь"),
        ("student", "Student123!", "Соколова", "Виктория", "Романовна", "Пользователь"),
    ]
    users_by_login = {}
    for login, password, last_name, first_name, middle_name, role_name in users_data:
        user = User.query.filter_by(login=login).first()
        if not user:
            user = User(
                login=login,
                last_name=last_name,
                first_name=first_name,
                middle_name=middle_name,
                role=roles[role_name],
            )
            user.set_password(password)
            db.session.add(user)
        users_by_login[login] = user
    db.session.commit()

    genre_names = [
        "Классика", "Роман", "Антиутопия", "Детектив", "Фэнтези", "Фантастика",
        "Приключения", "Драма", "Философская проза", "Американская литература",
        "Английская литература", "Французская литература", "Латиноамериканская литература"
    ]
    genres = {}
    for name in genre_names:
        genre = Genre.query.filter_by(name=name).first()
        if not genre:
            genre = Genre(name=name)
            db.session.add(genre)
        genres[name] = genre
    db.session.commit()

    legacy_titles = [
        "Мастер и Маргарита", "Пикник на обочине", "Чистый код", "Sapiens",
        "Думай медленно... решай быстро", "Грокаем алгоритмы", "Прагматичный программист"
    ]
    for legacy_book in Book.query.filter(Book.title.in_(legacy_titles)).all():
        filename = legacy_book.cover.filename if legacy_book.cover else None
        db.session.delete(legacy_book)
        db.session.commit()
        remove_cover_file_if_unused(filename)

    demo_books = [
        ("1984", "Классическая **антиутопия** о тотальном контроле, языке как инструменте власти и попытке человека сохранить свободу мышления. Роман хорошо подходит для раздела популярной зарубежной литературы, потому что его часто обсуждают в контексте политики, общества и личной ответственности.", 1949, "Secker & Warburg", "Джордж Оруэлл", 328, ["Антиутопия", "Классика", "Английская литература"], "#111827", "#4b5563", "1984"),
        ("Гордость и предубеждение", "Один из самых известных романов английской литературы. История Элизабет Беннет и мистера Дарси показывает столкновение характеров, социальных ожиданий и личного достоинства.", 1813, "T. Egerton", "Джейн Остин", 432, ["Роман", "Классика", "Английская литература"], "#6d28d9", "#c084fc", "GP"),
        ("Маленький принц", "Философская сказка-притча о дружбе, ответственности и взрослении. Небольшой объём делает книгу лёгкой для чтения, но смысловые темы остаются глубокими и универсальными.", 1943, "Reynal & Hitchcock", "Антуан де Сент-Экзюпери", 112, ["Философская проза", "Классика", "Французская литература"], "#2563eb", "#38bdf8", "MP"),
        ("Великий Гэтсби", "Роман о мечте, богатстве, любви и разочаровании. Атмосфера Америки 1920-х годов помогает показать контраст между внешним блеском и внутренней пустотой героев.", 1925, "Charles Scribner's Sons", "Фрэнсис Скотт Фицджеральд", 240, ["Роман", "Классика", "Американская литература"], "#0f766e", "#14b8a6", "VG"),
        ("Убить пересмешника", "Драматический роман о справедливости, взрослении и человеческом достоинстве. История рассказана через детское восприятие, поэтому социальные конфликты выглядят особенно остро.", 1960, "J. B. Lippincott & Co.", "Харпер Ли", 384, ["Роман", "Драма", "Американская литература"], "#92400e", "#f59e0b", "UP"),
        ("Над пропастью во ржи", "Роман о взрослении, одиночестве и внутреннем протесте. Главный герой пытается разобраться в себе и окружающем мире, поэтому книга часто воспринимается как история поиска собственного голоса.", 1951, "Little, Brown and Company", "Джером Д. Сэлинджер", 288, ["Роман", "Классика", "Американская литература"], "#b91c1c", "#fb7185", "NP"),
        ("Сто лет одиночества", "Магический реализм в истории семьи Буэндиа и города Макондо. Роман соединяет семейную хронику, мифологию, политику и тему повторяемости человеческой судьбы.", 1967, "Editorial Sudamericana", "Габриэль Гарсиа Маркес", 448, ["Роман", "Классика", "Латиноамериканская литература"], "#166534", "#86efac", "SO"),
        ("Властелин колец", "Эпическое фэнтези о путешествии, дружбе, выборе и борьбе с властью, которая разрушает человека. Роман важен для жанра фэнтези и остаётся одной из самых узнаваемых зарубежных книг.", 1954, "George Allen & Unwin", "Джон Р. Р. Толкин", 1216, ["Фэнтези", "Приключения", "Английская литература"], "#365314", "#84cc16", "VK"),
        ("Хоббит", "Приключенческая история о Бильбо Бэггинсе, путешествии к Одинокой горе и неожиданной смелости обычного героя. Книга легче по тону, чем «Властелин колец», но связана с тем же миром.", 1937, "George Allen & Unwin", "Джон Р. Р. Толкин", 320, ["Фэнтези", "Приключения", "Английская литература"], "#a16207", "#fde047", "HB"),
        ("Дюна", "Научно-фантастический роман о власти, религии, экологии и борьбе за планету Арракис. Книга сочетает приключенческий сюжет и сложное устройство мира.", 1965, "Chilton Books", "Фрэнк Герберт", 688, ["Фантастика", "Приключения", "Американская литература"], "#7c2d12", "#fdba74", "DN"),
        ("Убийство в Восточном экспрессе", "Классический детектив с Эркюлем Пуаро. Замкнутое пространство поезда, множество подозреваемых и точная логика расследования создают напряжённую интригу до последних страниц.", 1934, "Collins Crime Club", "Агата Кристи", 256, ["Детектив", "Классика", "Английская литература"], "#7f1d1d", "#ef4444", "UE"),
        ("Собака Баскервилей", "Одна из самых известных историй о Шерлоке Холмсе. В книге сочетаются готическая атмосфера, семейная легенда и рациональное расследование.", 1902, "George Newnes", "Артур Конан Дойл", 256, ["Детектив", "Классика", "Английская литература"], "#312e81", "#818cf8", "SB"),
        ("Три мушкетёра", "Приключенческий роман о дружбе, чести и смелости. История д'Артаньяна и трёх мушкетёров остаётся узнаваемой благодаря динамичному сюжету и ярким героям.", 1844, "Le Siècle", "Александр Дюма", 704, ["Приключения", "Классика", "Французская литература"], "#1d4ed8", "#60a5fa", "TM"),
        ("Джейн Эйр", "Роман о взрослении, независимости и праве женщины на собственный выбор. История соединяет социальную драму, романтическую линию и элементы готической прозы.", 1847, "Smith, Elder & Co.", "Шарлотта Бронте", 592, ["Роман", "Драма", "Английская литература"], "#831843", "#f9a8d4", "JE"),
        ("Моби Дик", "Масштабный роман о капитане Ахаве и его погоне за белым китом. Книга объединяет приключение, философские размышления и символический конфликт человека с одержимостью.", 1851, "Harper & Brothers", "Герман Мелвилл", 720, ["Приключения", "Классика", "Американская литература"], "#0e7490", "#67e8f9", "MD"),
        ("Алхимик", "Философская притча о мечте, пути и поиске личного предназначения. Текст простой по форме, но построен вокруг универсальной идеи движения к своей цели.", 1988, "HarperTorch", "Пауло Коэльо", 208, ["Роман", "Философская проза", "Приключения"], "#c2410c", "#facc15", "AL"),
    ]

    books_by_title = {}
    for title, description, year, publisher, author, pages, genre_list, color, accent, label in demo_books:
        book = Book.query.filter_by(title=title).first()
        if not book:
            book = Book(
                title=title,
                description=sanitize_markdown_text(description),
                year=year,
                publisher=publisher,
                author=author,
                pages=pages,
                genres=[genres[name] for name in genre_list],
            )
            db.session.add(book)
            db.session.flush()
            create_demo_cover(book, color, accent, label)
        books_by_title[title] = book
    db.session.commit()

    review_data = [
        ("1984", "user", 5, "Сильная антиутопия. Особенно понравилось, как через язык показан контроль над мышлением."),
        ("1984", "reader", 5, "Мрачная, но очень важная книга. После неё по-другому смотришь на свободу и информацию."),
        ("1984", "critic", 4, "Роман местами тяжёлый, но именно за счёт этого производит сильное впечатление."),
        ("1984", "booklover", 5, "Одна из тех книг, к которым хочется возвращаться, чтобы перечитать отдельные главы."),
        ("1984", "student", 4, "Интересно читать как предупреждение о том, что происходит с обществом без свободы мысли."),

        ("Гордость и предубеждение", "user", 5, "Классический роман, который держится не только на любовной линии, но и на характерах героев."),
        ("Гордость и предубеждение", "reader", 4, "Очень тонко показаны отношения, семейные ожидания и социальные правила эпохи."),
        ("Гордость и предубеждение", "critic", 5, "Роман читается легко, хотя за ним стоит точная социальная наблюдательность."),
        ("Гордость и предубеждение", "student", 4, "Мне понравилось развитие героев и спокойная ирония автора."),

        ("Маленький принц", "reader", 5, "Небольшая книга, но смыслов очень много. Хорошо подходит для повторного чтения."),
        ("Маленький принц", "user", 5, "Простая форма и очень глубокая мысль о дружбе, ответственности и взрослении."),
        ("Маленький принц", "booklover", 5, "Каждая глава воспринимается как отдельная притча, поэтому книга запоминается надолго."),
        ("Маленький принц", "critic", 4, "Очень светлая история, хотя некоторые эпизоды хочется перечитывать медленно."),
        ("Маленький принц", "student", 5, "Книга короткая, но эмоционально сильная. Особенно понравилась тема ответственности."),

        ("Великий Гэтсби", "moderator", 4, "Очень атмосферный роман о мечте и самообмане. Финал особенно запоминается."),
        ("Великий Гэтсби", "user", 4, "Понравилась атмосфера 1920-х и то, как показана иллюзия красивой жизни."),
        ("Великий Гэтсби", "critic", 3, "Стиль сильный, но эмоционально книга показалась немного холодной."),
        ("Великий Гэтсби", "booklover", 4, "Короткий роман, но после него остаётся много мыслей о мечте и одиночестве."),

        ("Убить пересмешника", "user", 5, "Трогательная и честная история о справедливости. Читается легко, но темы серьёзные."),
        ("Убить пересмешника", "reader", 5, "Очень сильная книга о взрослении, доброте и человеческом достоинстве."),
        ("Убить пересмешника", "critic", 5, "Роман хорошо показывает, как личная честность сталкивается с предрассудками общества."),
        ("Убить пересмешника", "student", 4, "Больше всего понравилось, что сложные темы показаны через детский взгляд."),

        ("Над пропастью во ржи", "reader", 4, "Книга хорошо передаёт состояние растерянности и внутреннего протеста."),
        ("Над пропастью во ржи", "student", 5, "Главный герой раздражает и одновременно кажется очень живым — из-за этого читать интересно."),
        ("Над пропастью во ржи", "critic", 3, "Важный роман о взрослении, но не всем может подойти его интонация."),

        ("Сто лет одиночества", "reader", 5, "Необычная книга с плотной атмосферой. Магический реализм здесь работает очень красиво."),
        ("Сто лет одиночества", "critic", 5, "Масштабная семейная хроника, где бытовое и фантастическое соединяются естественно."),
        ("Сто лет одиночества", "booklover", 4, "Сначала сложно привыкнуть к именам и структуре, но потом книга захватывает."),
        ("Сто лет одиночества", "moderator", 5, "Очень насыщенный роман, который создаёт ощущение целого мира."),

        ("Властелин колец", "user", 5, "Масштабный мир, много героев и сильная тема дружбы. Настоящая классика фэнтези."),
        ("Властелин колец", "reader", 5, "Книга большая, но путешествие героев и атмосфера Средиземья стоят этого времени."),
        ("Властелин колец", "booklover", 5, "Очень люблю этот роман за мир, языки, историю и ощущение настоящего эпоса."),
        ("Властелин колец", "critic", 4, "Местами повествование неспешное, но именно это делает мир более живым."),

        ("Хоббит", "reader", 4, "Более лёгкая и приключенческая книга, чем «Властелин колец». Очень уютное чтение."),
        ("Хоббит", "user", 5, "Отличная приключенческая история с юмором, дорогой и постепенным ростом героя."),
        ("Хоббит", "student", 4, "Понравилось, что книга читается легко, но всё равно оставляет ощущение большого мира."),

        ("Дюна", "moderator", 5, "Понравилось сочетание политики, экологии и фантастики. Мир прописан очень подробно."),
        ("Дюна", "critic", 5, "Сильная научная фантастика, где важен не только сюжет, но и устройство общества."),
        ("Дюна", "booklover", 4, "В начале много терминов, но потом история становится очень захватывающей."),
        ("Дюна", "student", 4, "Интереснее всего было читать про Арракис, власть и значение ресурсов."),

        ("Убийство в Восточном экспрессе", "reader", 5, "Хороший детектив с аккуратной логикой и сильной развязкой."),
        ("Убийство в Восточном экспрессе", "user", 4, "Классический камерный детектив, где почти каждая деталь оказывается важной."),
        ("Убийство в Восточном экспрессе", "critic", 5, "Развязка необычная и очень запоминающаяся, поэтому книгу легко советовать."),
        ("Убийство в Восточном экспрессе", "booklover", 4, "Пуаро как всегда точен, а атмосфера поезда добавляет напряжения."),

        ("Собака Баскервилей", "user", 4, "Атмосферное расследование. Нравится баланс мистики и рационального объяснения."),
        ("Собака Баскервилей", "reader", 4, "Хорошая готическая атмосфера и классическая работа Холмса и Ватсона."),
        ("Собака Баскервилей", "student", 5, "Читалось очень увлекательно: сначала кажется мистикой, а потом всё объясняется логикой."),

        ("Три мушкетёра", "user", 4, "Динамичное приключение о дружбе, чести и верности. История сохраняет лёгкость авантюрного романа."),
        ("Три мушкетёра", "booklover", 5, "Очень живой роман: дуэли, интриги, юмор и яркие персонажи."),
        ("Три мушкетёра", "critic", 4, "Книга местами наивная, но энергия сюжета и героев всё компенсирует."),

        ("Джейн Эйр", "reader", 5, "Сильная героиня и очень цельная история взросления. Роман понравился эмоциональностью."),
        ("Джейн Эйр", "user", 4, "История получилась драматичной, но героиня вызывает уважение своим характером."),
        ("Джейн Эйр", "critic", 5, "Роман хорошо сочетает личную историю, социальную тему и готическую атмосферу."),
        ("Джейн Эйр", "student", 4, "Понравилось, что героиня не теряет чувство собственного достоинства."),

        ("Моби Дик", "moderator", 4, "Большой и сложный роман, но в нём много интересных философских тем."),
        ("Моби Дик", "critic", 4, "Не самая лёгкая книга, зато мощная по символике и масштабу."),
        ("Моби Дик", "booklover", 3, "Были интересные главы, но темп показался слишком неровным."),

        ("Алхимик", "reader", 4, "Простая, но приятная притча о мечте и пути к цели."),
        ("Алхимик", "user", 3, "Идея хорошая, но книга показалась слишком прямолинейной."),
        ("Алхимик", "student", 4, "Лёгкое чтение с понятной мыслью о том, что важно идти к своей цели."),
    ]
    for title, login, rating, text in review_data:
        add_review_once(books_by_title.get(title), users_by_login.get(login), rating, text)
    db.session.commit()

    create_demo_visits(users_by_login)
    db.session.commit()


with app.app_context():
    seed_data()


if __name__ == "__main__":
    app.run(debug=True)
