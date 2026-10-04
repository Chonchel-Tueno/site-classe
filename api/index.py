"""API du site de classe 1GH : port Python (Flask) de l'ancien api.js, pour Vercel."""
import base64
import os

from flask import Flask, jsonify, request
from supabase import create_client

app = Flask(__name__)

MAX_UPLOAD_BYTES = 3 * 1024 * 1024  # Vercel limite le corps d'une requête à ~4,5 Mo (base64 inclus)
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx", "odt", "ppt", "pptx", "txt"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise ApiError(500, f"Variable d'environnement manquante sur le serveur : {name}")
    return value


def admin_client():
    """Client avec la clé service_role (côté serveur uniquement)."""
    return create_client(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))


def public_client():
    # Client neuf à chaque appel : la connexion d'un utilisateur ne doit jamais "fuiter" vers un autre.
    return create_client(_env("SUPABASE_URL"), _env("SUPABASE_ANON_KEY"))


def get_profile(db, user_id):
    rows = db.table("profiles").select("*").eq("id", user_id).limit(1).execute().data
    return rows[0] if rows else None


def get_auth_context(db):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.split(" ", 1)[1]
    try:
        res = db.auth.get_user(token)
        user = res.user if res else None
    except Exception:
        return None
    if not user:
        return None
    return {"user": user, "profile": get_profile(db, user.id)}


def rows(query):
    return query.execute().data


# ----------------------------------------------------------------------------
# Routes publiques
# ----------------------------------------------------------------------------
def login(db, body):
    try:
        res = public_client().auth.sign_in_with_password(
            {"email": body.get("email"), "password": body.get("password")}
        )
    except Exception as e:
        raise ApiError(400, getattr(e, "message", str(e)))
    return {
        "token": res.session.access_token,
        "user": res.user.model_dump(mode="json"),
        "profile": get_profile(db, res.user.id),
    }


def list_posts(db, body):
    return rows(db.table("posts").select("*").order("created_at", desc=True))


def approved_sheets(db, body):
    return rows(db.table("revision_sheets").select("*").eq("status", "approved").order("created_at", desc=True))


PUBLIC = {
    ("POST", "auth/login"): login,
    ("GET", "posts"): list_posts,
    ("GET", "sheets/approved"): approved_sheets,
}


# ----------------------------------------------------------------------------
# Routes pour tout utilisateur connecté
# ----------------------------------------------------------------------------
def me(db, body, ctx):
    return {"user": ctx["user"].model_dump(mode="json"), "profile": ctx["profile"]}


def change_password(db, body, ctx):
    try:
        db.auth.admin.update_user_by_id(ctx["user"].id, {"password": body.get("new_password")})
    except Exception as e:
        raise ApiError(400, getattr(e, "message", str(e)))
    return {"message": "Mot de passe mis à jour."}


def my_results(db, body, ctx):
    return rows(db.table("student_results").select("*").eq("student_id", ctx["user"].id).order("updated_at", desc=True))


def my_sheets(db, body, ctx):
    return rows(db.table("revision_sheets").select("*").eq("created_by", ctx["user"].id).order("created_at", desc=True))


def upload_sheet(db, body, ctx):
    title, subject, file_name = body.get("title"), body.get("subject"), body.get("fileName") or ""
    raw = body.get("fileBase64") or ""
    if not title or not raw:
        raise ApiError(400, "Titre et fichier requis.")

    header, sep, payload = raw.partition(",")
    content_type = header[5:].split(";")[0] if sep and header.startswith("data:") else "application/octet-stream"
    data = base64.b64decode(payload if sep else raw)

    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "Fichier trop volumineux (3 Mo maximum).")
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ALLOWED_EXT:
        raise ApiError(400, f"Type de fichier non autorisé (.{ext}).")

    import time
    storage_path = f"{ctx['user'].id}/{int(time.time() * 1000)}.{ext}"
    try:
        db.storage.from_("revision_files").upload(storage_path, data, {"content-type": content_type})
    except Exception as e:
        raise ApiError(500, getattr(e, "message", str(e)))
    public_url = db.storage.from_("revision_files").get_public_url(storage_path)

    db.table("revision_sheets").insert({
        "title": title, "subject": subject, "file_url": public_url, "file_name": file_name,
        "created_by": ctx["user"].id, "status": "pending", "rejection_reason": None,
    }).execute()
    return {"message": "Fiche envoyée en modération."}


def my_intake(db, body, ctx):
    data = rows(db.table("delegate_intake_responses").select("*").eq("student_id", ctx["user"].id))
    return data[0] if data else None


def submit_intake(db, body, ctx):
    fields = ["info", "has_whatsapp", "whatsapp_handle", "has_snapchat", "snapchat_handle",
              "has_instagram", "instagram_handle"]
    record = {"student_id": ctx["user"].id, **{f: body.get(f) for f in fields}}
    db.table("delegate_intake_responses").upsert(record, on_conflict="student_id").execute()
    return {"message": "Réponses enregistrées avec succès."}


AUTHED = {
    ("GET", "auth/me"): me,
    ("POST", "auth/change-password"): change_password,
    ("GET", "results/my"): my_results,
    ("GET", "sheets/my"): my_sheets,
    ("POST", "sheets/upload"): upload_sheet,
    ("GET", "intake/my"): my_intake,
    ("POST", "intake/submit"): submit_intake,
}


# ----------------------------------------------------------------------------
# Routes admin (admin + webmaster)
# ----------------------------------------------------------------------------
def create_post(db, body, ctx):
    db.table("posts").insert({"title": body.get("title"), "content": body.get("content")}).execute()
    return {"message": "Annonce publiée."}


def update_post(db, body, ctx):
    db.table("posts").update({"title": body.get("title"), "content": body.get("content")}).eq("id", body.get("id")).execute()
    return {"message": "Annonce mise à jour."}


def delete_post(db, body, ctx):
    db.table("posts").delete().eq("id", body.get("id")).execute()
    return {"message": "Annonce supprimée."}


def pending_sheets(db, body, ctx):
    return rows(db.table("revision_sheets").select("*").eq("status", "pending").order("created_at", desc=True))


def approve_sheet(db, body, ctx):
    db.table("revision_sheets").update({"status": "approved", "rejection_reason": None}).eq("id", body.get("sheetId")).execute()
    return {"message": "Fiche approuvée."}


def reject_sheet(db, body, ctx):
    reason = body.get("reason") or "Aucune raison spécifiée."
    db.table("revision_sheets").update({"status": "rejected", "rejection_reason": reason}).eq("id", body.get("sheetId")).execute()
    return {"message": "Fiche refusée avec motif."}


def delete_sheet(db, body, ctx):
    db.table("revision_sheets").delete().eq("id", body.get("sheetId")).execute()
    return {"message": "Fiche supprimée."}


def students(db, body, ctx):
    return rows(db.table("profiles").select("id, email, full_name, role").neq("role", "webmaster"))


def student_results(db, body, ctx):
    return rows(db.table("student_results").select("*").eq("student_id", body.get("studentId")).order("updated_at", desc=True))


def add_result(db, body, ctx):
    db.table("student_results").insert({
        "student_id": body.get("studentId"),
        "title": body.get("title") or "Conseil de classe",
        "appreciation": body.get("appreciation"),
    }).execute()
    return {"message": "Résultat ajouté."}


def intake_responses(db, body, ctx):
    responses = rows(db.table("delegate_intake_responses").select("*").order("created_at", desc=True))
    profiles = {p["id"]: p for p in rows(db.table("profiles").select("id, full_name, email"))}
    unknown = {"full_name": "Élève inconnu", "email": ""}
    return [{**r, "profiles": profiles.get(r["student_id"], unknown)} for r in responses]


ADMIN = {
    ("POST", "posts/create"): create_post,
    ("POST", "posts/update"): update_post,
    ("POST", "posts/delete"): delete_post,
    ("GET", "sheets/pending"): pending_sheets,
    ("POST", "sheets/approve"): approve_sheet,
    ("POST", "sheets/reject"): reject_sheet,
    ("POST", "sheets/delete"): delete_sheet,
    ("GET", "admin/students"): students,
    ("POST", "admin/student-results"): student_results,
    ("POST", "admin/add-result"): add_result,
    ("GET", "intake/responses"): intake_responses,
}


# ----------------------------------------------------------------------------
# Routes webmaster
# ----------------------------------------------------------------------------
def create_user(db, body, ctx):
    try:
        res = db.auth.admin.create_user({
            "email": body.get("email"), "password": body.get("password"), "email_confirm": True,
            "user_metadata": {"full_name": body.get("name")},
        })
    except Exception as e:
        raise ApiError(400, getattr(e, "message", str(e)))
    db.table("profiles").update({"role": body.get("role")}).eq("id", res.user.id).execute()
    return {"message": "Utilisateur créé avec succès."}


def all_profiles(db, body, ctx):
    return rows(db.table("profiles").select("*").order("email"))


def update_profile(db, body, ctx):
    db.table("profiles").update({"full_name": body.get("name"), "role": body.get("role")}).eq("id", body.get("userId")).execute()
    return {"message": "Profil mis à jour."}


def reset_password(db, body, ctx):
    try:
        db.auth.admin.update_user_by_id(body.get("userId"), {"password": body.get("newPassword")})
    except Exception as e:
        raise ApiError(500, getattr(e, "message", str(e)))
    return {"message": "Mot de passe réinitialisé."}


WEBMASTER = {
    ("POST", "webmaster/create-user"): create_user,
    ("GET", "webmaster/profiles"): all_profiles,
    ("POST", "webmaster/update-profile"): update_profile,
    ("POST", "webmaster/reset-password"): reset_password,
}


# ----------------------------------------------------------------------------
# Routage
# ----------------------------------------------------------------------------
@app.route("/api/<path:path>", methods=["GET", "POST"])
def dispatch(path):
    # Vercel réécrit /api/xxx vers /api/index : le vrai chemin arrive dans ?route=xxx (voir vercel.json).
    route = request.args.get("route") or path
    key = (request.method, route.strip("/"))
    body = request.get_json(silent=True) or {}
    try:
        db = admin_client()

        if key in PUBLIC:
            return jsonify(PUBLIC[key](db, body))

        ctx = get_auth_context(db)
        if not ctx:
            raise ApiError(401, "Non autorisé. Veuillez vous connecter.")

        role = (ctx["profile"] or {}).get("role")
        is_webmaster = role == "webmaster"
        is_admin = role in ("admin", "webmaster")

        if key in AUTHED:
            return jsonify(AUTHED[key](db, body, ctx))
        if key in ADMIN and is_admin:
            return jsonify(ADMIN[key](db, body, ctx))
        if key in WEBMASTER and is_webmaster:
            return jsonify(WEBMASTER[key](db, body, ctx))

        raise ApiError(404, "Route non trouvée ou privilèges insuffisants.")
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    except Exception as e:  # erreur inattendue : on renvoie du JSON, jamais une page HTML
        return jsonify({"error": str(e)}), 500