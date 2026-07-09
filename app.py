import sys
import time
import urllib.parse
import io
import pandas as pd
import requests
import streamlit as st
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google import genai
from google.genai import types

# =====================================================================
# CONFIGURAÇÃO DE CREDENCIAIS SEGURAS (STREAMLIT SECRETS)
# =====================================================================
try:
    CREDENTIALS = {
        "developer_token": st.secrets["google_ads"]["developer_token"],
        "client_id": st.secrets["google_ads"]["client_id"],
        "client_secret": st.secrets["google_ads"]["client_secret"],
        "refresh_token": st.secrets["google_ads"]["refresh_token"],
        "use_proto_plus": "true",
    }
    CUSTOMER_ID = st.secrets["google_ads"]["customer_id"]
    
    CONSTRUCTOR_KEYS = {
        "pt": st.secrets["constructor"]["key_pt"],
        "es": st.secrets["constructor"]["key_es"]
    }
    
    # Inicializa o cliente oficial do Gemini com a chave dos secrets
    GEMINI_API_KEY = st.secrets["gemini"]["api_key"]
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    
except KeyError as e:
    st.error(f"❌ Erro: Configuração de Secrets em falta no painel ({e}).")
    st.info("Por favor, valida as credenciais 'google_ads', 'constructor' e 'gemini' nas definições do teu painel Streamlit Cloud.")
    st.stop()

# Lista de Marcas Concorrentes para Excluir
PROHIBITED_KEYWORDS = [
    "zara", "parfois", "misako", "stradivarius", "bershka", "mango",
    "pull and bear", "lefties", "h&m", "primark", "el corte ingles",
    "decathlon", "cortefiel", "springfield", "tiffosi", "bimba y lola",
    "paco martinez", "michael kors", "outlet", "louis vuitton",
    "ralph lauren", "corte ingles", "tous", "carolina herrera",
    "purificacion garcia"
]

CONSTRUCTOR_URL = "https://ac.cnstrc.com/search/"

COUNTRY_MAP = {
    "pt": {
        "LANGUAGE_PATH": "languageConstants/1014",
        "LOCATION_PATH": "geoTargetConstants/2620"
    },
    "es": {
        "LANGUAGE_PATH": "languageConstants/1003",
        "LOCATION_PATH": "geoTargetConstants/2724"
    }
}

# =====================================================================
# FUNÇÕES DE LÓGICA / PIPELINE
# =====================================================================
def get_autocomplete_suggestions(seed_keyword, country_code):
    keywords_set = set()
    modifiers = ["como", "qual", "onde", "para", "de", "com", "estilo", "look com", "casamento", "festa", "online"]
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    search_queries = [f"{m} {seed_keyword}" for m in modifiers] + [f"{seed_keyword} {m}" for m in modifiers] + [f"{seed_keyword} {l}" for l in alphabet]

    headers = {"User-Agent": "Mozilla/5.0"}
    for query in search_queries:
        encoded = urllib.parse.quote_plus(query)
        url = f"http://suggestqueries.google.com/complete/search?client=chrome&hl={country_code}&gl={country_code}&q={encoded}"
        try:
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                for s in res.json()[1]:
                    s_clean = s.lower().strip()
                    if seed_keyword in s_clean:
                        keywords_set.add(s_clean)
            time.sleep(0.05)
        except Exception:
            continue
    return list(keywords_set)

def get_google_volumes_historical(client, customer_id, keyword_list, lang_path, loc_path):
    keyword_plan_idea_service = client.get_service("KeywordPlanIdeaService")
    results_data = []
    batch_size = 50

    for i in range(0, len(keyword_list), batch_size):
        batch = keyword_list[i:i + batch_size]
        request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        request.customer_id = customer_id
        request.language = lang_path
        request.geo_target_constants.append(loc_path)
        request.keywords.extend(batch)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = keyword_plan_idea_service.generate_keyword_historical_metrics(request=request)
                for result in response.results:
                    avg_monthly_searches_12_months = result.keyword_metrics.avg_monthly_searches or 0
                    monthly_searches = [ms.monthly_searches for ms in result.keyword_metrics.monthly_search_volumes]
                    avg_monthly_searches_3_months = sum(monthly_searches[-3:]) / 3 if len(monthly_searches) >= 3 else 0

                    results_data.append({
                        "Keyword": result.text,
                        "Volume Médio Mensal (12 meses)": avg_monthly_searches_12_months,
                        "Volume Médio Mensal (3 meses)": int(avg_monthly_searches_3_months)
                    })
                break
            except Exception as e:
                if "exhausted" in str(e).lower() and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    time.sleep(wait_time)
                else:
                    st.error(f"❌ Erro API Ads: {e}")
                    break
        time.sleep(1)
    return results_data

def get_constructor_products(query, constructor_key):
    params = {"key": constructor_key, "num_results_per_page": 50, "page": 1}
    try:
        encoded_query = urllib.parse.quote(query)
        response = requests.get(f"{CONSTRUCTOR_URL}{encoded_query}", params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            products = data.get("response", {}).get("results", [])
            variation_groups = []
            for p in products:
                product_data = p.get("data", {})
                vg_id = product_data.get("variation_id") or product_data.get("id") or p.get("value")
                if vg_id:
                    variation_groups.append(vg_id)
            return variation_groups
    except Exception:
        pass
    return []

# =====================================================================
# GERAÇÃO DE META DESCRIPTIONS COM IA (GEMINI 2.5 FLASH LITE)
# =====================================================================
def generate_ai_meta_description(keyword, country_code):
    """
    Chama a API gratuita do Gemini 2.5 Flash Lite para gerar uma meta description
    única, persuasiva e natural para e-commerce em tempo recorde.
    """
    idioma = "Português de Portugal (PT-PT)" if country_code == "pt" else "Espanhol moderno (ES)"
    
    prompt = f"""
    Atua como um especialista em SEO para e-commerce de moda e acessórios.
    Escreve uma Meta Description apelativa e otimizada para uma página de categoria focada na seguinte keyword: "{keyword}".
    
    Requisitos estritos:
    1. O texto deve estar escrito em {idioma}.
    2. Comprimento máximo: 155 caracteres (curto e direto).
    3. Deve incluir a keyword de forma fluida.
    4. Deve ser uma frase natural de venda (ex: mencionar tendências, novidades, envios rápidos ou estilo), evitando soar repetitiva ou robótica.
    5. Devolve APENAS a meta description final, sem aspas, sem explicações e sem introduções.
    """
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt,
        )
        return response.text.strip().replace('"', '')
    except Exception:
        if country_code == "pt":
            return f"Descubra as últimas novidades em {keyword}. Encontre designs exclusivos com envios rápidos na loja online."
        else:
            return f"Descubre las últimas novedades en {keyword}. Encuentra diseños exclusivos con envío rápido en la tienda online."

# =====================================================================
# INTERFACE INTERATIVA (STREAMLIT UI)
# =====================================================================
st.set_page_config(page_title="SEO Keyword Research Tool", page_icon="🔍", layout="wide")

st.title("🔍 SEO & Constructor.io Keyword Research")
st.markdown("Interface visual para cruzamento de dados de sugestões Google Autocomplete, volumes de pesquisa (Google Ads) e catálogo Constructor.io.")

# Barra lateral de configurações
st.sidebar.header("⚙️ Parâmetros")
selected_country_code = st.sidebar.selectbox(
    "Seleccione o País / Idioma",
    options=list(COUNTRY_MAP.keys()),
    format_func=lambda x: "🇵🇹 Portugal (PT)" if x == "pt" else "🇪🇸 Espanha (ES)"
)

LANGUAGE_PATH = COUNTRY_MAP[selected_country_code]["LANGUAGE_PATH"]
LOCATION_PATH = COUNTRY_MAP[selected_country_code]["LOCATION_PATH"]
CURRENT_CONSTRUCTOR_KEY = CONSTRUCTOR_KEYS.get(selected_country_code, CONSTRUCTOR_KEYS["pt"])

# Input de texto principal
seed_input = st.text_input("Palavra Semente (Seed Keyword):", placeholder="Ex: vestido de cerimónia")

# Inicializar estados da sessão (Cache e Dados)
if "df_results" not in st.session_state:
    st.session_state.df_results = None
if "seo_cache" not in st.session_state:
    st.session_state.seo_cache = {}

if st.button("🚀 Iniciar Análise Corrente", type="primary"):
    if not seed_input.strip():
        st.warning("⚠️ Introduza uma palavra semente válida primeiro.")
    else:
        seed = seed_input.strip().lower()
        
        try:
            google_client = GoogleAdsClient.load_from_dict(CREDENTIALS)
        except Exception as e:
            st.error(f"❌ Falha crítica ao inicializar cliente Google Ads: {e}")
            st.stop()

        with st.spinner("🔮 A recolher sugestões no Google Autocomplete..."):
            scraped = get_autocomplete_suggestions(seed, selected_country_code)
        
        filtered_keywords = []
        for kw in scraped:
            if len(kw.split()) >= 3:
                if not any(brand in kw for brand in PROHIBITED_KEYWORDS):
                    filtered_keywords.append(kw)
        long_tails = list(set(filtered_keywords))[:300]
        
        st.info(f"💡 Foram encontradas {len(long_tails)} keywords Long-Tail que passaram nos filtros iniciais.")

        if long_tails:
            with st.spinner("📊 A extrair volumes reais do Planificador de Keywords..."):
                raw_ads = get_google_volumes_historical(google_client, CUSTOMER_ID, long_tails, LANGUAGE_PATH, LOCATION_PATH)
                df = pd.DataFrame(raw_ads).sort_values(by="Volume Médio Mensal (12 meses)", ascending=False).reset_index(drop=True)

            if not df.empty:
                top_df = df.head(50).copy()
                
                progress_text = "🔎 A mapear correspondências no índice Constructor.io..."
                progress_bar = st.progress(0, text=progress_text)
                
                product_data = []
                for idx, kw in enumerate(top_df['Keyword']):
                    refs = get_constructor_products(kw, CURRENT_CONSTRUCTOR_KEY)
                    product_data.append(", ".join(filter(None, refs)))
                    progress_bar.progress((idx + 1) / len(top_df), text=progress_text)
                
                progress_bar.empty()
                top_df['Produtos Constructor'] = product_data
                
                top_df.insert(0, "Selecionar", False)
                st.session_state.df_results = top_df
            else:
                st.warning("⚠️ A API do Google Ads não retornou dados para estas keywords.")
                st.session_state.df_results = None
        else:
            st.error("❌ Nenhuma keyword válida sobrou após a filtragem de marcas proibidas.")
            st.session_state.df_results = None

# Interface de Exibição e Edição
if st.session_state.df_results is not None:
    st.subheader("📊 Resultados de Top 50 Keywords")
    st.markdown("💡 *Ative a caixa de seleção na coluna **'Selecionar'** para gerar as descrições via AI e exportar.*")
    
    edited_df = st.data_editor(
        st.session_state.df_results,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Selecionar": st.column_config.CheckboxColumn("Selecionar", default=False),
            "Volume Médio Mensal (12 meses)": st.column_config.NumberColumn(format="%d", disabled=True),
            "Volume Médio Mensal (3 meses)": st.column_config.NumberColumn(format="%d", disabled=True),
            "Keyword": st.column_config.TextColumn(disabled=True),
            "Produtos Constructor": st.column_config.TextColumn(disabled=True)
        }
    )
    
    selected_rows = edited_df[edited_df["Selecionar"] == True].copy()
    
    if not selected_rows.empty:
        st.success(f"✅ {len(selected_rows)} linha(s) selecionada(s). A processar metadados com Gemini 2.5 Flash Lite...")
        
        h1_list = []
        desc_list = []
        
        with st.spinner("🤖 A gerar Meta Descriptions exclusivas com IA..."):
            for kw in selected_rows['Keyword']:
                # H1 mantendo exatamente a keyword, com a primeira letra em maiúscula
                h1_correto = kw.strip().capitalize()
                h1_list.append(h1_correto)
                
                # Meta Description com cache
                cache_key = f"{selected_country_code}_{kw}"
                if cache_key not in st.session_state.seo_cache:
                    st.session_state.seo_cache[cache_key] = generate_ai_meta_description(kw, selected_country_code)
                
                desc_list.append(st.session_state.seo_cache[cache_key])
            
        selected_rows['SEO H1 Gerado'] = h1_list
        selected_rows['SEO Meta Description'] = desc_list
        
        export_df = selected_rows.drop(columns=["Selecionar"])
        
        st.write("👀 **Antevisão dos Metadados SEO Gerados:**")
        st.dataframe(export_df[['Keyword', 'SEO H1 Gerado', 'SEO Meta Description']], use_container_width=True, hide_index=True)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            export_df.to_excel(writer, index=False)
        
        seed_clean = seed_input.strip().lower().replace(' ', '_') if seed_input.strip() else "keywords"
        filename = f"seo_ai_{selected_country_code}_{seed_clean}.xlsx"
        
        st.download_button(
            label="📥 Descarregar Seleção com SEO Dinâmico (.xlsx)",
            data=buffer.getvalue(),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )
    else:
        st.info("ℹ️ Selecione linhas na tabela acima para ativar a IA e gerar o ficheiro Excel.")
