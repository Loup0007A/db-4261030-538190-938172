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

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
  html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
  code, .stCode { font-family: 'IBM Plex Mono', monospace; }
  .stButton > button { border-radius: 6px; font-weight: 600; transition: all .2s ease; }
  .stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.15); }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# CONNEXION
# ═══════════════════════════════════════════════════════

def open_connection():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise EnvironmentError("DATABASE_URL est introuvable.")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    return conn


def get_conn():
    """Connexion persistante dans session_state, recréée si morte."""
    conn = st.session_state.get("pg_conn")
    if conn is None or conn.closed:
        conn = open_connection()
        st.session_state["pg_conn"] = conn
        return conn
    try:
        if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
        conn.cursor().execute("SELECT 1")
    except Exception:
        conn = open_connection()
        st.session_state["pg_conn"] = conn
    return conn


# ═══════════════════════════════════════════════════════
# HELPERS DB
# ═══════════════════════════════════════════════════════

def db_fetch(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def get_all_tables(conn):
    rows = db_fetch(conn, """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    return [r["table_name"] for r in rows]


def get_columns(conn, table_name):
    rows = db_fetch(conn, """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    return list(rows)


def get_primary_keys(conn, table_name):
    rows = db_fetch(conn, """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
    """, (table_name,))
    return [r["column_name"] for r in rows]


def get_table_stats(conn, table_name):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        quoted = psycopg2.extensions.quote_ident(table_name, cur)
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {quoted}")
        row_count = cur.fetchone()["cnt"]
        cur.execute(
            "SELECT pg_size_pretty(pg_total_relation_size(quote_ident(%s))) AS sz",
            (table_name,)
        )
        size = cur.fetchone()["sz"]
    conn.commit()
    return row_count, size


def read_table(conn, table_name, limit):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        quoted = psycopg2.extensions.quote_ident(table_name, cur)
        cur.execute(f"SELECT * FROM {quoted} LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.commit()
    if not rows:
        cols = get_columns(conn, table_name)
        return pd.DataFrame(columns=[c["column_name"] for c in cols])
    df = pd.DataFrame(rows)
    # Convertir objets complexes (UUID, JSON…) en str pour l'éditeur
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].apply(lambda x: str(x) if x is not None else None)
    return df


def safe_val(v):
    """Convertit une valeur en None si elle est NaN/NaT, sinon la renvoie."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


# ═══════════════════════════════════════════════════════
# SAUVEGARDE PAR DELTA (clé du fix)
# ═══════════════════════════════════════════════════════

def apply_editor_changes(conn, table_name, original_df, editor_state, pk_cols):
    """
    Lit le DELTA du data_editor (edited_rows / added_rows / deleted_rows)
    et génère des UPDATE/INSERT/DELETE ciblés.

    Pourquoi cette approche ?
    st.data_editor retourne un DataFrame complet à chaque re-run Streamlit.
    Quand l'utilisateur clique sur "Enregistrer", Streamlit re-exécute tout
    le script : la valeur de retour de data_editor est recalculée AVANT que
    le bouton soit traité, donc elle contient déjà les données originales.
    En revanche, st.session_state[editor_key] conserve le delta exact tapé
    par l'utilisateur jusqu'au prochain rechargement explicite.
    """
    edited_rows  = editor_state.get("edited_rows",  {})
    added_rows   = editor_state.get("added_rows",   [])
    deleted_rows = editor_state.get("deleted_rows", [])

    total_ops = len(edited_rows) + len(added_rows) + len(deleted_rows)
    if total_ops == 0:
        return 0

    cols = list(original_df.columns)

    with conn.cursor() as cur:
        qt = psycopg2.extensions.quote_ident(table_name, cur)

        # ── UPDATE ────────────────────────────────────────────────────────────
        for row_idx_str, changes_dict in edited_rows.items():
            row_idx = int(row_idx_str)
            if row_idx >= len(original_df):
                continue

            if pk_cols:
                set_parts, set_vals = [], []
                for col, new_val in changes_dict.items():
                    set_parts.append(f"{psycopg2.extensions.quote_ident(col, cur)} = %s")
                    set_vals.append(safe_val(new_val))

                where_parts, where_vals = [], []
                for pk in pk_cols:
                    where_parts.append(f"{psycopg2.extensions.quote_ident(pk, cur)} = %s")
                    where_vals.append(safe_val(original_df.iloc[row_idx][pk]))

                cur.execute(
                    f"UPDATE {qt} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}",
                    set_vals + where_vals,
                )
            else:
                # Pas de PK : on mémorise pour TRUNCATE+INSERT plus bas
                pass

        # ── INSERT ────────────────────────────────────────────────────────────
        if added_rows:
            q_cols = ", ".join(psycopg2.extensions.quote_ident(c, cur) for c in cols)
            ph     = ", ".join(["%s"] * len(cols))
            for new_row in added_rows:
                vals = [safe_val(new_row.get(c)) for c in cols]
                cur.execute(f"INSERT INTO {qt} ({q_cols}) VALUES ({ph})", vals)

        # ── DELETE ────────────────────────────────────────────────────────────
        if deleted_rows:
            if pk_cols:
                for row_idx in deleted_rows:
                    if row_idx >= len(original_df):
                        continue
                    where_parts, where_vals = [], []
                    for pk in pk_cols:
                        where_parts.append(f"{psycopg2.extensions.quote_ident(pk, cur)} = %s")
                        where_vals.append(safe_val(original_df.iloc[row_idx][pk]))
                    cur.execute(
                        f"DELETE FROM {qt} WHERE {' AND '.join(where_parts)}",
                        where_vals,
                    )
            else:
                st.warning("⚠️ Suppression ignorée : pas de clé primaire pour identifier les lignes.")

        # ── Fallback sans PK : TRUNCATE + INSERT ─────────────────────────────
        if not pk_cols and edited_rows:
            final_df = original_df.copy()
            for row_idx_str, changes_dict in edited_rows.items():
                row_idx = int(row_idx_str)
                for col, val in changes_dict.items():
                    final_df.at[row_idx, col] = val

            cur.execute(f"TRUNCATE TABLE {qt} RESTART IDENTITY CASCADE")
            q_cols = ", ".join(psycopg2.extensions.quote_ident(c, cur) for c in cols)
            ph     = ", ".join(["%s"] * len(cols))
            for _, row in final_df.iterrows():
                cur.execute(
                    f"INSERT INTO {qt} ({q_cols}) VALUES ({ph})",
                    [safe_val(v) for v in row],
                )

    conn.commit()
    return total_ops


def execute_raw_sql(conn, sql):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        if cur.description:
            rows = cur.fetchall()
            conn.commit()
            return pd.DataFrame(rows)
        conn.commit()
        return f"{cur.rowcount} ligne(s) affectée(s)."


# ═══════════════════════════════════════════════════════
# HISTORIQUE
# ═══════════════════════════════════════════════════════

def log_action(action, detail=""):
    if "history" not in st.session_state:
        st.session_state.history = []
    st.session_state.history.insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action, "detail": detail,
    })
    st.session_state.history = st.session_state.history[:50]


# ═══════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════

st.title("🗄️ Dashboard Admin PostgreSQL")

try:
    conn = get_conn()
except Exception as e:
    st.error(f"Connexion impossible : {e}")
    st.stop()

try:
    all_tables = get_all_tables(conn)
except Exception as e:
    st.error(f"Impossible de lire les tables : {e}")
    st.stop()

if not all_tables:
    st.warning("Aucune table dans le schéma `public`.")
    st.stop()

# ── Sidebar ───────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")
    search = st.text_input("🔍 Filtrer les tables", placeholder="ex: user, order…")
    filtered = [t for t in all_tables if search.lower() in t.lower()] if search else all_tables
    selected_table = st.selectbox("Table à éditer :", filtered)

    st.divider()
    row_limit    = st.slider("Lignes à charger", 50, 5000, 500, step=50)
    show_schema  = st.toggle("Afficher le schéma", value=False)
    show_history = st.toggle("Afficher l'historique", value=False)
    st.divider()

    if st.button("🔄 Rafraîchir", use_container_width=True):
        try:
            st.session_state["pg_conn"].close()
        except Exception:
            pass
        for key in list(st.session_state.keys()):
            if key.startswith("df_") or key.startswith("editor_"):
                del st.session_state[key]
        if "pg_conn" in st.session_state:
            del st.session_state["pg_conn"]
        st.rerun()

    if show_history and "history" in st.session_state:
        st.subheader("📋 Historique")
        for h in st.session_state.history:
            st.caption(f"`{h['time']}` **{h['action']}** {h['detail']}")

if not selected_table:
    st.info("Sélectionnez une table.")
    st.stop()

# ── Métriques ─────────────────────────────────────────
try:
    row_count, size = get_table_stats(conn, selected_table)
    pk_cols  = get_primary_keys(conn, selected_table)
    col_defs = get_columns(conn, selected_table)
except Exception as e:
    st.error(f"Métadonnées inaccessibles : {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("📊 Lignes",        f"{row_count:,}")
c2.metric("💾 Taille",        size)
c3.metric("🔑 Clés primaires", ", ".join(pk_cols) if pk_cols else "—")
c4.metric("📋 Colonnes",      len(col_defs))

if show_schema:
    with st.expander("🏗️ Schéma", expanded=True):
        st.dataframe(pd.DataFrame(col_defs), use_container_width=True, hide_index=True)

st.divider()

tab_edit, tab_sql, tab_export = st.tabs(["✏️ Édition", "💻 SQL libre", "📤 Export"])

# ════════════════════════════════════════════════════════
# ONGLET ÉDITION
# ════════════════════════════════════════════════════════
with tab_edit:
    st.subheader(f"Table : `{selected_table}`")

    load_gen   = st.session_state.get("load_gen", 0)
    editor_key = f"editor_{selected_table}_{load_gen}"

    with st.expander("🔎 Filtrer", expanded=False):
        filter_col = st.selectbox(
            "Colonne", ["— aucun —"] + [c["column_name"] for c in col_defs]
        )
        filter_val = st.text_input("Valeur contient") if filter_col != "— aucun —" else ""

    # DataFrame de référence en session_state (stable jusqu'au prochain reload)
    df_key = f"df_{selected_table}_{load_gen}"
    if df_key not in st.session_state:
        try:
            st.session_state[df_key] = read_table(conn, selected_table, row_limit)
        except Exception as e:
            st.error(f"Lecture impossible : {e}")
            st.stop()

    original_df = st.session_state[df_key]

    display_df = original_df.copy()
    if filter_col != "— aucun —" and filter_val and filter_col in display_df.columns:
        display_df = display_df[
            display_df[filter_col].astype(str).str.contains(filter_val, case=False, na=False)
        ]

    st.caption(f"{len(display_df):,} ligne(s) affichées (limite : {row_limit})")

    if not pk_cols:
        st.warning(
            "⚠️ Pas de clé primaire — les modifications utilisent TRUNCATE + INSERT.",
            icon="⚠️",
        )

    # L'éditeur enregistre ses changements dans st.session_state[editor_key]
    st.data_editor(
        display_df,
        use_container_width=True,
        num_rows="dynamic",
        key=editor_key,
    )

    col_save, col_cancel, _ = st.columns([1, 1, 4])

    with col_save:
        if st.button("💾 Enregistrer", type="primary", use_container_width=True):
            # On lit le DELTA depuis session_state, pas la valeur de retour
            editor_state = st.session_state.get(editor_key, {})
            try:
                n = apply_editor_changes(
                    conn, selected_table, original_df, editor_state, pk_cols
                )
                if n == 0:
                    st.info("Aucune modification détectée.")
                else:
                    log_action("SAVE", f"→ `{selected_table}` ({n} op.)")
                    st.success(f"✅ {n} opération(s) appliquée(s) !")
                    st.session_state["load_gen"] = load_gen + 1
                    st.rerun()
            except Exception as e:
                conn.rollback()
                st.error(f"❌ Erreur SQL : {e}")
                if "foreign key" in str(e).lower():
                    st.info("💡 Contrainte FK — modifiez d'abord les tables liées.")

    with col_cancel:
        if st.button("↩️ Annuler", use_container_width=True):
            st.session_state["load_gen"] = load_gen + 1
            st.rerun()


# ════════════════════════════════════════════════════════
# ONGLET SQL LIBRE
# ════════════════════════════════════════════════════════
with tab_sql:
    st.subheader("💻 Exécution SQL libre")
    st.warning("⚠️ `DELETE`, `DROP`, `UPDATE` sans `WHERE` sont irréversibles.", icon="⚠️")

    sql_input = st.text_area(
        "Requête SQL :",
        value=f'SELECT * FROM "{selected_table}" LIMIT 100;',
        height=150,
    )

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
                    if any(k in sql_input.upper() for k in ("UPDATE", "INSERT", "DELETE", "TRUNCATE")):
                        st.session_state["load_gen"] = st.session_state.get("load_gen", 0) + 1
            except Exception as e:
                conn.rollback()
                st.error(f"❌ {e}")


# ════════════════════════════════════════════════════════
# ONGLET EXPORT
# ════════════════════════════════════════════════════════
with tab_export:
    st.subheader("📤 Exporter les données")
    export_limit = st.number_input("Lignes max", min_value=1, value=10000, step=1000)
    fmt = st.radio("Format", ["CSV", "JSON", "Excel (.xlsx)"], horizontal=True)

    if st.button("⬇️ Générer le fichier"):
        with st.spinner("Préparation…"):
            try:
                export_df = read_table(conn, selected_table, int(export_limit))
                ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename  = f"{selected_table}_{ts}"

                if fmt == "CSV":
                    data     = export_df.to_csv(index=False).encode("utf-8")
                    mime     = "text/csv"
                    filename += ".csv"
                elif fmt == "JSON":
                    data     = export_df.to_json(orient="records", force_ascii=False).encode("utf-8")
                    mime     = "application/json"
                    filename += ".json"
                else:
                    import io
                    buf = io.BytesIO()
                    export_df.to_excel(buf, index=False)
                    data     = buf.getvalue()
                    mime     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    filename += ".xlsx"

                log_action("EXPORT", f"{selected_table} → {fmt} ({len(export_df)} lignes)")
                st.download_button(f"⬇️ Télécharger {filename}", data, filename, mime)
            except Exception as e:
                st.error(f"Erreur export : {e}")
