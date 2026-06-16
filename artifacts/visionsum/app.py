"""
app.py — Main Flask application for the CCTV Video Summarization System (VisionSum).
"""

import os
import uuid
import json
import time
import logging
import threading
import io
import csv

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, send_from_directory, send_file, make_response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from utils.database import (
    init_db, create_user, get_user_by_email, get_user_by_id,
    update_last_login, update_password,
    save_video, get_video, get_user_videos, get_all_videos, delete_video,
    create_summary, get_summary, get_user_summaries, get_all_summaries, delete_summary,
    get_events, get_all_users, delete_user, get_logs, get_stats, log_action,
    get_db
)
from utils.video_processor import process_video

BASE_DIR    = os.path.dirname(__file__)
UPLOAD_DIR  = os.path.join(BASE_DIR, 'uploads')
SUMMARY_DIR = os.path.join(BASE_DIR, 'summaries')
REPORT_DIR  = os.path.join(BASE_DIR, 'reports')

for d in (UPLOAD_DIR, SUMMARY_DIR, REPORT_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

ALLOWED_EXT = {'mp4', 'avi', 'mov', 'mkv'}

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

_proc_status: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_user_by_id(session['user_id'])
        if not user or user['role'] != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if 'user_id' in session:
        return get_user_by_id(session['user_id'])
    return None


def _run_processing(video_id, summary_id, user_id, input_path, output_path, compression):
    _proc_status[video_id] = {'status': 'processing', 'progress': 0}
    result = process_video(video_id, summary_id, user_id, input_path, output_path, compression)
    _proc_status[video_id] = {
        'status':   'done' if result['success'] else 'error',
        'progress': 100,
        'error':    result.get('error', ''),
    }


# ── Init DB ───────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()
    if not get_all_users():
        create_user('admin', 'admin@cctv.local', generate_password_hash('admin123'))
        conn = get_db()
        conn.execute("UPDATE users SET role='admin' WHERE username='admin'")
        conn.commit()
        conn.close()
        logger.info("Default admin created: admin@cctv.local / admin123")


# ══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html', user=current_user())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        user     = get_user_by_email(email)
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            update_last_login(user['id'])
            flash(f'Welcome back, {user["username"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        if password != confirm:
            flash('Passwords do not match.', 'danger')
        elif len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
        else:
            if create_user(username, email, generate_password_hash(password)):
                flash('Account created! Please log in.', 'success')
                return redirect(url_for('login'))
            flash('Username or email already exists.', 'danger')
    return render_template('register.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        flash('If that email exists, a reset link has been sent (demo mode).', 'info')
    return render_template('forgot_password.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('login'))


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/dashboard')
@login_required
def dashboard():
    user      = current_user()
    stats     = get_stats(user['id'])
    videos    = get_user_videos(user['id'])[:5]
    summaries = get_user_summaries(user['id'])[:5]
    return render_template('dashboard.html',
                           user=user, stats=stats,
                           videos=videos, summaries=summaries)


# ══════════════════════════════════════════════════════════════════════════════
# ABOUT
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/about')
@login_required
def about():
    return render_template('about.html', user=current_user())


# ══════════════════════════════════════════════════════════════════════════════
# VIDEO UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    user = current_user()
    if request.method == 'POST':
        if 'video' not in request.files:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        f = request.files['video']
        if f.filename == '':
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not allowed_file(f.filename):
            flash('Unsupported file type. Use MP4, AVI, MOV, or MKV.', 'danger')
            return redirect(request.url)

        original_name = secure_filename(f.filename)
        ext           = original_name.rsplit('.', 1)[1].lower()
        unique_name   = f"{uuid.uuid4().hex}.{ext}"
        save_path     = os.path.join(UPLOAD_DIR, unique_name)
        f.save(save_path)

        file_size = os.path.getsize(save_path)
        video_id  = save_video(user['id'], unique_name, original_name, file_size)
        log_action(user['id'], video_id, 'upload', f'Uploaded {original_name}')

        flash('Video uploaded successfully!', 'success')
        return redirect(url_for('process_page', video_id=video_id))

    return render_template('upload.html', user=user)


@app.route('/upload/ajax', methods=['POST'])
@login_required
def upload_ajax():
    """XHR upload endpoint — returns JSON so the browser can show real progress."""
    user = current_user()

    if 'video' not in request.files:
        return jsonify({'error': 'No file selected.'}), 400

    f = request.files['video']

    if not f.filename:
        return jsonify({'error': 'No file selected.'}), 400

    if not allowed_file(f.filename):
        return jsonify({'error': 'Unsupported file type. Use MP4, AVI, MOV, or MKV.'}), 400

    original_name = secure_filename(f.filename)
    ext           = original_name.rsplit('.', 1)[1].lower()
    unique_name   = f"{uuid.uuid4().hex}.{ext}"
    save_path     = os.path.join(UPLOAD_DIR, unique_name)

    try:
        f.save(save_path)
    except Exception as exc:
        logger.exception("File save failed")
        return jsonify({'error': f'Save failed: {exc}'}), 500

    file_size = os.path.getsize(save_path)
    video_id  = save_video(user['id'], unique_name, original_name, file_size)
    log_action(user['id'], video_id, 'upload', f'Uploaded {original_name}')

    return jsonify({
        'ok':       True,
        'video_id': video_id,
        'redirect': url_for('process_page', video_id=video_id),
    })


# ══════════════════════════════════════════════════════════════════════════════
# PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/process/<int:video_id>', methods=['GET', 'POST'])
@login_required
def process_page(video_id):
    user  = current_user()
    video = get_video(video_id)
    if not video or video['user_id'] != user['id']:
        flash('Video not found.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        compression = int(request.form.get('compression', 50))
        out_name    = f"summary_{uuid.uuid4().hex}.mp4"
        out_path    = os.path.join(SUMMARY_DIR, out_name)
        in_path     = os.path.join(UPLOAD_DIR, video['filename'])

        summary_id = create_summary(video_id, user['id'], out_name, compression)

        t = threading.Thread(
            target=_run_processing,
            args=(video_id, summary_id, user['id'], in_path, out_path, compression),
            daemon=True,
        )
        t.start()

        return redirect(url_for('processing_status', summary_id=summary_id))

    return render_template('process.html', user=user, video=video)


@app.route('/processing/<int:summary_id>')
@login_required
def processing_status(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Summary not found.', 'danger')
        return redirect(url_for('dashboard'))
    video = get_video(summary['video_id'])
    return render_template('processing.html', user=user, summary=summary, video=video)


@app.route('/vapi/status/<int:video_id>')
@login_required
def api_status(video_id):
    status = _proc_status.get(video_id, {'status': 'unknown', 'progress': 0})
    return jsonify(status)


@app.route('/vapi/summary_status/<int:summary_id>')
@login_required
def api_summary_status(summary_id):
    summary = get_summary(summary_id)
    if not summary:
        return jsonify({'status': 'unknown'})
    return jsonify({'status': summary['status']})


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS / VIEWER
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/results/<int:summary_id>')
@login_required
def results(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Result not found.', 'danger')
        return redirect(url_for('dashboard'))
    video  = get_video(summary['video_id'])
    events = get_events(summary_id)
    return render_template('results.html', user=user, summary=summary,
                           video=video, events=events)


@app.route('/summaries')
@login_required
def summaries_list():
    user      = current_user()
    summaries = get_user_summaries(user['id'])
    return render_template('summaries.html', user=user, summaries=summaries)


@app.route('/videos')
@login_required
def videos_list():
    user   = current_user()
    videos = get_user_videos(user['id'])
    return render_template('videos.html', user=user, videos=videos)


@app.route('/delete/video/<int:video_id>', methods=['POST'])
@login_required
def delete_video_route(video_id):
    user  = current_user()
    video = get_video(video_id)
    if not video or video['user_id'] != user['id']:
        flash('Video not found.', 'danger')
        return redirect(url_for('videos_list'))
    path = os.path.join(UPLOAD_DIR, video['filename'])
    if os.path.exists(path):
        os.remove(path)
    thumb = os.path.join(os.path.dirname(__file__), 'static', 'img', 'thumbs',
                         f"thumb_{video_id}.jpg")
    if os.path.exists(thumb):
        os.remove(thumb)
    delete_video(video_id)
    flash('Video deleted.', 'success')
    return redirect(url_for('videos_list'))


@app.route('/delete/summary/<int:summary_id>', methods=['POST'])
@login_required
def delete_summary_route(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Summary not found.', 'danger')
        return redirect(url_for('summaries_list'))
    path = os.path.join(SUMMARY_DIR, summary['filename'])
    if os.path.exists(path):
        os.remove(path)
    delete_summary(summary_id)
    flash('Summary deleted.', 'success')
    return redirect(url_for('summaries_list'))


# ══════════════════════════════════════════════════════════════════════════════
# FILE SERVING
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/summaries/<path:filename>')
@login_required
def serve_summary(filename):
    return send_from_directory(SUMMARY_DIR, filename)


@app.route('/download/summary/<int:summary_id>')
@login_required
def download_summary(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Not found.', 'danger')
        return redirect(url_for('dashboard'))
    return send_from_directory(SUMMARY_DIR, summary['filename'],
                               as_attachment=True,
                               download_name=f"summary_{summary_id}.mp4")


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/reports')
@login_required
def reports():
    user      = current_user()
    summaries = get_user_summaries(user['id'])
    return render_template('report.html', user=user, summaries=summaries)


@app.route('/reports/export/csv/<int:summary_id>')
@login_required
def export_csv(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Not found.', 'danger')
        return redirect(url_for('reports'))
    events = get_events(summary_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Summary ID', 'Original Duration', 'Summary Duration',
                     'Compression %', 'Events Detected', 'Processing Time (s)',
                     'Motion Avg', 'Created At'])
    writer.writerow([
        summary['id'], summary['original_dur'], summary['summary_dur'],
        summary['compression'], summary['events_detected'],
        summary['processing_time'], summary['motion_avg'], summary['created_at'],
    ])
    writer.writerow([])
    writer.writerow(['Event Type', 'Start (s)', 'End (s)', 'Intensity'])
    for ev in events:
        writer.writerow([ev['event_type'], ev['start_time'], ev['end_time'], ev['intensity']])

    response = make_response(buf.getvalue())
    response.headers['Content-Disposition'] = f'attachment; filename=report_{summary_id}.csv'
    response.headers['Content-Type'] = 'text/csv'
    return response


@app.route('/reports/export/pdf/<int:summary_id>')
@login_required
def export_pdf(summary_id):
    user    = current_user()
    summary = get_summary(summary_id)
    if not summary or summary['user_id'] != user['id']:
        flash('Not found.', 'danger')
        return redirect(url_for('reports'))
    events = get_events(summary_id)
    video  = get_video(summary['video_id'])

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet

        buf    = io.BytesIO()
        doc    = SimpleDocTemplate(buf, pagesize=A4)
        styles = getSampleStyleSheet()
        story  = []

        story.append(Paragraph("CCTV Video Summarization Report", styles['Title']))
        story.append(Spacer(1, 12))

        meta = [
            ['Video',            video['original_name'] if video else 'N/A'],
            ['Original Duration', f"{summary['original_dur']:.1f}s"],
            ['Summary Duration',  f"{summary['summary_dur']:.1f}s"],
            ['Compression',       f"{summary['compression']}%"],
            ['Events Detected',   str(summary['events_detected'])],
            ['Processing Time',   f"{summary['processing_time']:.2f}s"],
            ['Avg Motion Score',  f"{summary['motion_avg']:.2f}"],
            ['Created',           summary['created_at']],
        ]
        t = Table(meta, colWidths=[160, 280])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F97316')),
            ('TEXTCOLOR',  (0, 0), (0, -1), colors.white),
            ('FONTNAME',   (0, 0), (-1, -1), 'Helvetica'),
            ('GRID',       (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, colors.HexColor('#FFF7ED')]),
        ]))
        story.append(t)
        story.append(Spacer(1, 20))

        if events:
            story.append(Paragraph("Detected Events", styles['Heading2']))
            ev_data = [['#', 'Event Type', 'Start (s)', 'End (s)', 'Intensity']]
            for i, ev in enumerate(events, 1):
                ev_data.append([str(i), ev['event_type'],
                                 f"{ev['start_time']:.1f}", f"{ev['end_time']:.1f}",
                                 f"{ev['intensity']:.1f}"])
            et = Table(ev_data, colWidths=[30, 180, 80, 80, 80])
            et.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F97316')),
                ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
                ('GRID',       (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (1, 0), (-1, -1), [colors.white, colors.HexColor('#FFF7ED')]),
            ]))
            story.append(et)

        doc.build(story)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f"report_{summary_id}.pdf",
                         mimetype='application/pdf')
    except Exception as e:
        flash(f'PDF generation failed: {e}', 'danger')
        return redirect(url_for('reports'))


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user = current_user()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw     = request.form.get('new_password', '')
            confirm_pw = request.form.get('confirm_password', '')
            if not check_password_hash(user['password'], current_pw):
                flash('Current password is incorrect.', 'danger')
            elif new_pw != confirm_pw:
                flash('New passwords do not match.', 'danger')
            elif len(new_pw) < 6:
                flash('Password must be at least 6 characters.', 'danger')
            else:
                update_password(user['id'], generate_password_hash(new_pw))
                flash('Password updated successfully!', 'success')
    return render_template('settings.html', user=user)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin')
@admin_required
def admin():
    stats     = get_stats()
    users     = get_all_users()
    videos    = get_all_videos()
    summaries = get_all_summaries()
    logs      = get_logs(50)
    return render_template('admin.html', user=current_user(),
                           stats=stats, users=users, videos=videos,
                           summaries=summaries, logs=logs)


@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    if user_id == session['user_id']:
        flash('Cannot delete your own account.', 'danger')
    else:
        delete_user(user_id)
        flash('User deleted.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/delete/video/<int:video_id>', methods=['POST'])
@admin_required
def admin_delete_video(video_id):
    video = get_video(video_id)
    if video:
        path = os.path.join(UPLOAD_DIR, video['filename'])
        if os.path.exists(path):
            os.remove(path)
        delete_video(video_id)
        flash('Video deleted.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/delete/summary/<int:summary_id>', methods=['POST'])
@admin_required
def admin_delete_summary(summary_id):
    summary = get_summary(summary_id)
    if summary:
        path = os.path.join(SUMMARY_DIR, summary['filename'])
        if os.path.exists(path):
            os.remove(path)
        delete_summary(summary_id)
        flash('Summary deleted.', 'success')
    return redirect(url_for('admin'))


# ══════════════════════════════════════════════════════════════════════════════
# JSON API
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/vapi/videos')
@login_required
def api_videos():
    user   = current_user()
    videos = get_user_videos(user['id'])
    return jsonify([dict(v) for v in videos])


@app.route('/vapi/summaries')
@login_required
def api_summaries():
    user      = current_user()
    summaries = get_user_summaries(user['id'])
    return jsonify([dict(s) for s in summaries])


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
