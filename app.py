import streamlit as st
import psycopg2
import psycopg2.extras
import pandas as pd
import os
from datetime import datetime

st.set_page_config(
    page_title="DB Manager Pro",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS personnalisé ──────────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
  html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
  code, .stCode  { font-family: 'IBM Plex Mono', monospace; }
  .stButton > button {
      border-radius: 6px; font-weight: 600;
      transition: all .2s ease;
  }
  .stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.15); }
  .metric-card {
      background: linear-gradient(135deg, #1e293b, #0f172a);
      color: #e2e8f0; border-radius: 10px; padding: 16px 20px;
      border: 1px solid #334155;
  }
</style>
""", unsafe_allow_html=True)


# ── 1. Connexion ──────────────────────────────────────────────────────────────
# FIX: On n'utilise plus @st.cache_resource pour la connexion brute psycopg2,
# car une connexion en état d'erreur de transaction resterait bloquée en cache.
# On stocke plutôt la connexion dans st.session_state pour pouvoir la recréer.

def get_db_url() -> str:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise EnvironmentError(
            "La variable d'environnement DATABASE_URL est introuvable. "
            "Définissez-la avant de lancer l'application."
        )
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return db_url


def open_connection() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(get_db_url())
    conn.autocommit = False
    return conn


def get_connection() -> psycopg2.extensions.connection:
    """
    Retourne la connexion active depuis session_state.
    La recrée si elle est fermée ou dans un état d'erreur.
    """
    conn = st.session_state.get("pg_conn")
    if conn is None or conn.closed:
        conn = open_connection()
        st.session_state["pg_conn"] = conn
        return conn
    # FIX: Tester l'état de la transaction ; rollback si en erreur
    # pour éviter "InFailedSqlTransaction" sur toutes les requêtes suivantes.
    try:
        if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
            conn.rollback()          # reset d'une éventuelle transaction pendante
        conn.cursor().execute("SELECT 1")
    except Exception:
        conn = open_connection()
        st.session_state["pg_conn"] = conn
    return conn


# ── 2. Helpers base de données ────────────────────────────────────────────────
# FIX: On retire @st.cache_data sur les fonctions qui lisent la BDD pour éviter
# de renvoyer des données obsolètes après une sauvegarde.
# Le cache n'est conservé que pour get_all_tables (liste stable).

@st.cache_data(ttl=60, show_spinner=False)
def get_all_tables(_conn_id: int) -> list[str]:
    """
    _conn_id est l'id() de la connexion — paramètre factice pour que
    @st.cache_data invalide le cache si la connexion change.
    """
    conn = get_connection()
    query = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [r[0] for r in cur.fetchall()]


def get_table_info(conn, table_name: str) -> dict:
    """Retourne row_count, taille_disque, colonnes avec type. PAS de cache."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        quoted = psycopg2.extensions.quote_ident(table_name, cur)
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {quoted}")
        row_count = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT pg_size_pretty(pg_total_relation_size(quote_ident(%s))) AS sz",
            (table_name,),
        )
        size = cur.fetchone()["sz"]

        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        columns = cur.fetchall()

    return {"row_count": row_count, "size": size, "columns": list(columns)}


def get_primary_keys(conn, table_name: str) -> list[str]:
    query = """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema    = 'public'
          AND tc.table_name      = %s
        ORDER BY kcu.ordinal_position;
    """
    with conn.cursor() as cur:
        cur.execute(query, (table_name,))
        return [r[0] for r in cur.fetchall()]


# FIX: Lecture des données de table via psycopg2 pur (pas pd.read_sql)
# pour éviter les problèmes de compatibilité SQLAlchemy / transaction.
def read_table(conn, table_name: str, limit: int) -> pd.DataFrame:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        quoted = psycopg2.extensions.quote_ident(table_name, cur)
        cur.execute(f"SELECT * FROM {quoted} LIMIT %s", (limit,))
        rows = cur.fetchall()
    if not rows:
        # Récupère les noms de colonnes même si la table est vide
        info = get_table_info(conn, table_name)
        cols = [c["column_name"] for c in info["columns"]]
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)


def execute_raw_sql(conn, sql: str) -> "pd.DataFrame | str":
    """Exécute du SQL libre ; retourne un DataFrame pour SELECT, sinon un message."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        if cur.description:
            rows = cur.fetchall()
            conn.commit()
            return pd.DataFrame(rows)
        conn.commit()
        return f"{cur.rowcount} ligne(s) affectée(s)."


# ── 3. Sauvegarde intelligente (upsert ou truncate+insert) ────────────────────
def save_table(conn, table_name: str, edited_df: pd.DataFrame, pk_cols: list[str]):
    """
    Si des clés primaires existent → UPSERT (INSERT … ON CONFLICT DO UPDATE).
    Sinon → TRUNCATE + INSERT (comportement originel).
    """
    with conn.cursor() as cur:
        q_table = psycopg2.extensions.quote_ident(table_name, cur)
        q_cols = [psycopg2.extensions.quote_ident(c, cur) for c in edited_df.columns]
        col_names = ", ".join(q_cols)
        placeholders = ", ".join(["%s"] * len(edited_df.columns))

        if pk_cols:
            non_pk = [c for c in edited_df.columns if c not in pk_cols]
            if non_pk:
                update_clause = ", ".join(
                    f"{psycopg2.extensions.quote_ident(c, cur)} = EXCLUDED.{psycopg2.extensions.quote_ident(c, cur)}"
                    for c in non_pk
                )
                conflict_action = f"DO UPDATE SET {update_clause}"
            else:
                conflict_action = "DO NOTHING"

            conflict_cols = ", ".join(
                psycopg2.extensions.quote_ident(c, cur) for c in pk_cols
            )
            sql = (
                f"INSERT INTO {q_table} ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_cols}) {conflict_action}"
            )
            for _, row in edited_df.iterrows():
                values = tuple(None if pd.isna(v) else v for v in row)
                cur.execute(sql, values)
        else:
            cur.execute(f"TRUNCATE TABLE {q_table} RESTART IDENTITY CASCADE")
            if not edited_df.empty:
                sql = f"INSERT INTO {q_table} ({col_names}) VALUES ({placeholders})"
                for _, row in edited_df.iterrows():
                    values = tuple(None if pd.isna(v) else v for v in row)
                    cur.execute(sql, values)

    conn.commit()   # FIX: commit explicite ici (était déjà présent, on s'assure)


# ── 4. Historique des actions ─────────────────────────────────────────────────
def log_action(action: str, detail: str = ""):
    if "history" not in st.session_state:
        st.session_state.history = []
    st.session_state.history.insert(
        0,
        {"time": datetime.now().strftime("%H:%M:%S"), "action": action, "detail": detail},
    )
    st.session_state.history = st.session_state.history[:50]


# ══════════════════════════════════════════════════════════════════════════════
# UI principale
# ══════════════════════════════════════════════════════════════════════════════
st.title("🗄️ Dashboard Admin PostgreSQL")

# Connexion
try:
    conn = get_connection()
except EnvironmentError as e:
    st.error(str(e))
    st.stop()
except Exception as e:
    st.error(f"Erreur de connexion : {e}")
    st.stop()

# Récupération des tables
try:
    all_tables = get_all_tables(id(conn))
except Exception as e:
    st.error(f"Impossible de lire la liste des tables : {e}")
    st.stop()

if not all_tables:
    st.warning("Aucune table trouvée dans le schéma `public`.")
    st.stop()

# ── Barre latérale ────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")

    search = st.text_input("🔍 Filtrer les tables", placeholder="ex: user, order…")
    filtered = [t for t in all_tables if search.lower() in t.lower()] if search else all_tables

    selected_table = st.selectbox("Table à éditer :", filtered)

    st.divider()

    st.subheader("Options")
    row_limit = st.slider("Lignes à charger", 50, 5000, 500, step=50)
    show_schema = st.toggle("Afficher le schéma", value=False)
    show_history = st.toggle("Afficher l'historique", value=False)

    st.divider()

    if st.button("🔄 Rafraîchir le cache", use_container_width=True):
        get_all_tables.clear()
        # FIX: Invalider aussi la connexion pour forcer une reconnexion propre
        if "pg_conn" in st.session_state:
            try:
                st.session_state["pg_conn"].close()
            except Exception:
                pass
            del st.session_state["pg_conn"]
        st.rerun()

    if show_history and "history" in st.session_state:
        st.subheader("📋 Historique")
        for h in st.session_state.history:
            st.caption(f"`{h['time']}` **{h['action']}** {h['detail']}")

# ── Zone principale ───────────────────────────────────────────────────────────
if not selected_table:
    st.info("Sélectionnez une table dans la barre latérale.")
    st.stop()

try:
    info = get_table_info(conn, selected_table)
    pk_cols = get_primary_keys(conn, selected_table)
except Exception as e:
    st.error(f"Impossible de lire les métadonnées : {e}")
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("📊 Lignes", f"{info['row_count']:,}")
col2.metric("💾 Taille", info["size"])
col3.metric("🔑 Clés primaires", ", ".join(pk_cols) if pk_cols else "—")
col4.metric("📋 Colonnes", len(info["columns"]))

if show_schema:
    with st.expander("🏗️ Schéma de la table", expanded=True):
        schema_df = pd.DataFrame(info["columns"])
        st.dataframe(schema_df, use_container_width=True, hide_index=True)

st.divider()

tab_edit, tab_sql, tab_export = st.tabs(["✏️ Édition", "💻 SQL libre", "📤 Export"])

# ── Onglet Édition ────────────────────────────────────────────────────────────
with tab_edit:
    st.subheader(f"Table : `{selected_table}`")

    with st.expander("🔎 Filtrer / Trier", expanded=False):
        filter_col = st.selectbox(
            "Colonne de filtre",
            ["— aucun —"] + [c["column_name"] for c in info["columns"]],
        )
        filter_val = st.text_input("Valeur contient") if filter_col != "— aucun —" else ""

    # FIX: Lecture via read_table() (psycopg2 pur) au lieu de pd.read_sql()
    try:
        df = read_table(conn, selected_table, row_limit)
    except Exception as e:
        st.error(f"Lecture impossible : {e}")
        st.stop()

    if filter_col != "— aucun —" and filter_val and filter_col in df.columns:
        df = df[df[filter_col].astype(str).str.contains(filter_val, case=False, na=False)]

    st.caption(f"{len(df):,} ligne(s) affichées (limite : {row_limit})")

    # FIX: La clé de l'éditeur intègre un compteur de sauvegarde pour forcer
    # le rechargement du widget après chaque save (sinon Streamlit garde
    # l'ancien état et les modifications semblent ne pas être appliquées).
    save_count = st.session_state.get("save_count", 0)
    editor_key = f"editor_{selected_table}_{save_count}"

    edited_df = st.data_editor(
        df,
        use_container_width=True,
        num_rows="dynamic",
        key=editor_key,
    )

    col_save, col_reset = st.columns([1, 5])

    with col_save:
        if st.button("💾 Enregistrer", type="primary", use_container_width=True):
            with st.spinner("Application des changements…"):
                try:
                    save_table(conn, selected_table, edited_df, pk_cols)
                    # FIX: Incrémenter le compteur pour invalider la clé de l'éditeur
                    # et forcer Streamlit à recharger les données fraîches depuis la BDD.
                    st.session_state["save_count"] = save_count + 1
                    log_action("SAVE", f"→ `{selected_table}` ({len(edited_df)} lignes)")
                    st.success(f"✅ `{selected_table}` mise à jour avec succès !")
                    st.rerun()
                except Exception as e:
                    conn.rollback()
                    st.error(f"❌ Erreur SQL : {e}")
                    if "foreign key" in str(e).lower():
                        st.info(
                            "💡 Contrainte de clé étrangère détectée. "
                            "Modifiez d'abord les tables enfants ou désactivez temporairement la contrainte."
                        )

    with col_reset:
        if st.button("↩️ Annuler les modifications", use_container_width=False):
            # FIX: Incrémenter aussi le compteur pour forcer le rechargement
            st.session_state["save_count"] = save_count + 1
            st.rerun()

# ── Onglet SQL Libre ──────────────────────────────────────────────────────────
with tab_sql:
    st.subheader("💻 Exécution SQL libre")
    st.warning(
        "⚠️ Attention : les requêtes `DELETE`, `DROP` ou `UPDATE` sans `WHERE` sont irréversibles.",
        icon="⚠️",
    )

    default_sql = f'SELECT * FROM "{selected_table}" LIMIT 100;'
    sql_input = st.text_area("Requête SQL :", value=default_sql, height=150)

    if st.button("▶️ Exécuter", type="primary"):
        with st.spinner("Exécution…"):
            try:
                result = execute_raw_sql(conn, sql_input)
                log_action("SQL", sql_input[:80])
                if isinstance(result, pd.DataFrame):
                    st.success(f"{len(result)} ligne(s) retournée(s).")
                    st.dataframe(result, use_container_width=True)
                else:
                    st.success(result)
            except Exception as e:
                conn.rollback()
                st.error(f"❌ Erreur : {e}")

# ── Onglet Export ─────────────────────────────────────────────────────────────
with tab_export:
    st.subheader("📤 Exporter les données")

    export_limit = st.number_input("Nombre de lignes max", min_value=1, value=10000, step=1000)
    fmt = st.radio("Format", ["CSV", "JSON", "Excel (.xlsx)"], horizontal=True)

    if st.button("⬇️ Générer le fichier"):
        with st.spinner("Préparation…"):
            try:
                # FIX: Utilise read_table() ici aussi pour la cohérence
                export_df = read_table(conn, selected_table, int(export_limit))
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{selected_table}_{ts}"

                if fmt == "CSV":
                    data = export_df.to_csv(index=False).encode("utf-8")
                    mime = "text/csv"
                    filename += ".csv"
                elif fmt == "JSON":
                    data = export_df.to_json(orient="records", force_ascii=False).encode("utf-8")
                    mime = "application/json"
                    filename += ".json"
                else:
                    import io
                    buf = io.BytesIO()
                    export_df.to_excel(buf, index=False)
                    data = buf.getvalue()
                    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    filename += ".xlsx"

                log_action("EXPORT", f"{selected_table} → {fmt} ({len(export_df)} lignes)")
                st.download_button(
                    label=f"⬇️ Télécharger {filename}",
                    data=data,
                    file_name=filename,
                    mime=mime,
                )
            except Exception as e:
                st.error(f"Erreur lors de l'export : {e}")
