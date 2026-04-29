import streamlit as st
import psycopg2
import pandas as pd
import os

# Configuration de la page
st.set_page_config(page_title="DB Admin", layout="wide")

# 1. Fonction de connexion via variable d'environnement
def get_connection():
    # Récupère l'URL depuis les variables d'environnement de Render
    db_url = os.environ.get("DATABASE_URL")
    
    if not db_url:
        st.error("Erreur : La variable d'environnement DATABASE_URL est manquante.")
        st.stop()
    
    # Correction pour Render : SQLAlchemy/psycopg2 préfèrent postgresql:// à postgres://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    return psycopg2.connect(db_url)

st.title("🚀 Dashboard de Modification PostgreSQL")

# 2. Interface de sélection
table_name = st.text_input("Nom de la table à éditer :", value="utilisateurs")

try:
    conn = get_connection()
    
    # Chargement des données
    query = f"SELECT * FROM {table_name}"
    df = pd.read_sql(query, conn)

    st.write(f"Modification de la table : **{table_name}**")
    
    # 3. Éditeur de données
    # 'num_rows="dynamic"' permet d'ajouter/supprimer des lignes directement
    edited_df = st.data_editor(df, use_container_width=True, num_rows="dynamic")

    # 4. Bouton de sauvegarde
    if st.button("Enregistrer les modifications"):
        with st.spinner("Mise à jour de la base de données..."):
            try:
                cursor = conn.cursor()
                
                # Méthode "Overwrite" (On vide et on remplace)
                # Attention : Pour de très grosses tables, préférez des UPDATE ciblés
                cursor.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE")
                
                # Construction de la requête d'insertion dynamique
                cols = ",".join([f'"{c}"' for c in edited_df.columns])
                placeholders = ",".join(["%s"] * len(edited_df.columns))
                insert_query = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"
                
                for _, row in edited_df.iterrows():
                    # Conversion en tuple en gérant les types (None pour NULL)
                    cursor.execute(insert_query, tuple(row))
                
                conn.commit()
                st.success("✅ Modifications enregistrées avec succès !")
                st.balloons()
                
            except Exception as e:
                conn.rollback()
                st.error(f"Erreur lors de l'écriture : {e}")
            finally:
                cursor.close()

except Exception as e:
    st.error(f"Erreur de connexion ou de lecture : {e}")
finally:
    if 'conn' in locals():
        conn.close()
