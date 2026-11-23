import os
import re
import sys
import hashlib
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
MODO = os.environ.get("MODO_EXECUCAO", "extrair_pendentes")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERRO: Variáveis SUPABASE_URL ou SUPABASE_SERVICE_KEY não configuradas.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def baixar_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=45)
    resp.raise_for_status()
    raw_bytes = resp.content

    # Verifica se o HTML declara UTF-8 explicitamente; caso contrário, Planalto usa Windows-1252
    cabecalho = raw_bytes[:2048].lower()
    if b"charset=utf-8" in cabecalho or b'charset="utf-8"' in cabecalho:
        texto = raw_bytes.decode("utf-8", errors="replace")
    else:
        texto = raw_bytes.decode("windows-1252", errors="replace")

    # Correção preventiva caso algum caractere residual venha mal codificado
    texto = texto.replace("ş", "º").replace("Ş", "º").replace("Ę", "Ê")
    return texto

def limpar_trechos_revogados(soup: BeautifulSoup):
    for tag in soup.find_all(["strike", "s", "del"]):
        tag.decompose()

    for tag in soup.find_all(style=re.compile(r"line-through", re.I)):
        tag.decompose()

    classes_revogadas = re.compile(r"(revogad|tachad|strike|risco)", re.I)
    for tag in soup.find_all(class_=classes_revogadas):
        tag.decompose()

    for tag in soup(["script", "style", "head", "meta", "link", "noscript"]):
        tag.decompose()

def normalizar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()

def gerar_slug_estrutura(texto: str, contador: int) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", texto.upper())[:45].strip("_")
    return f"EST_{contador}_{base}"

def extrair_dispositivos(html: str):
    soup = BeautifulSoup(html, "lxml")
    limpar_trechos_revogados(soup)

    dispositivos = []
    iniciou_artigos = False
    artigo_atual = ""
    paragrafo_atual = ""
    inciso_atual = ""
    ordem = 0
    slugs_vistos = {}

    re_estrutura = re.compile(r"^(TÍTULO|LIVRO|PARTE|CAPÍTULO|SEÇÃO|SUBSEÇÃO)\s+[IVXLCDM0-9]+", re.I)
    re_artigo = re.compile(r"^(Art\.?\s*(\d+[º°o]?(?:-[A-Z])?))\s*[-–.]?\s*(.*)", re.I)
    re_paragrafo = re.compile(r"^((?:§\s*\d+[º°o]?(?:-[A-Z])?|Parágrafo\s+único))\s*[-–.]?\s*(.*)", re.I)
    re_inciso = re.compile(r"^([IVXLCDM]+)\s*[-–.]\s*(.*)")
    re_alinea = re.compile(r"^([a-z])\)\s*(.*)")

    blocos = soup.find_all(["p", "div", "h1", "h2", "h3", "h4"])
    textos_processados = set()

    for bloco in blocos:
        if bloco.name == "div" and bloco.find(["p", "div"]):
            continue

        raw = normalizar_espacos(bloco.get_text(" ", strip=True))
        if not raw or len(raw) < 2:
            continue

        if re.match(r"^\(?\s*(?:Revogad[oa]|Vetad[oa])", raw, re.I) and len(raw) < 60:
            continue

        m_art = re_artigo.match(raw)

        # Ignora todos os cabeçalhos, brasões, ementas e títulos antes do Art. 1º
        if not iniciou_artigos:
            if m_art:
                iniciou_artigos = True
            else:
                continue

        chave_dedup = f"{ordem}_{raw[:80]}"
        if chave_dedup in textos_processados:
            continue
        textos_processados.add(chave_dedup)

        tipo = None
        rotulo = ""
        texto = raw
        slug = ""

        m_est = re_estrutura.match(raw)
        m_par = re_paragrafo.match(raw)
        m_inc = re_inciso.match(raw)
        m_ali = re_alinea.match(raw)

        if m_est and len(raw) < 180:
            tipo = "estrutura"
            rotulo = ""
            texto = raw
            slug = gerar_slug_estrutura(raw, ordem)
        elif m_art:
            tipo = "artigo"
            rotulo = m_art.group(1).strip()
            num_art = re.sub(r"[^0-9A-Z]", "", m_art.group(2).upper())
            texto = m_art.group(3).strip() or raw
            artigo_atual = f"ART_{num_art}"
            paragrafo_atual = ""
            inciso_atual = ""
            slug = artigo_atual
        elif m_par:
            tipo = "paragrafo"
            rotulo = m_par.group(1).strip()
            texto = m_par.group(2).strip() or raw
            num_par = "UNICO" if "único" in rotulo.lower() else re.sub(r"[^0-9A-Z]", "", rotulo.upper())
            paragrafo_atual = f"PAR_{num_par}"
            inciso_atual = ""
            slug = f"{artigo_atual}_{paragrafo_atual}"
        elif m_inc:
            tipo = "inciso"
            rotulo = f"{m_inc.group(1).strip()} -"
            texto = m_inc.group(2).strip()
            inciso_atual = f"INC_{m_inc.group(1).strip().upper()}"
            prefixo = f"{artigo_atual}_{paragrafo_atual}" if paragrafo_atual else artigo_atual
            slug = f"{prefixo}_{inciso_atual}"
        elif m_ali:
            tipo = "alinea"
            rotulo = f"{m_ali.group(1)})"
            texto = m_ali.group(2).strip()
            prefixo = f"{artigo_atual}_{paragrafo_atual}" if paragrafo_atual else artigo_atual
            if inciso_atual:
                prefixo = f"{prefixo}_{inciso_atual}"
            slug = f"{prefixo}_ALI_{m_ali.group(1).upper()}"
        else:
            continue

        if slug in slugs_vistos:
            slugs_vistos[slug] += 1
            slug = f"{slug}_DUP{slugs_vistos[slug]}"
        else:
            slugs_vistos[slug] = 0

        ordem += 1
        dispositivos.append({
            "slug": slug,
            "tipo": tipo,
            "rotulo": rotulo,
            "texto": texto,
            "ordem": ordem
        })

    texto_canonico = "\n".join(f"{d['slug']}:{d['texto']}" for d in dispositivos)
    hash_lei = hashlib.sha256(texto_canonico.encode("utf-8")).hexdigest()
    return dispositivos, hash_lei

def salvar_dispositivos_em_lotes(lei_id: str, user_id: str, dispositivos: list, status_versao: str):
    registros = [
        {
            "lei_id": lei_id,
            "user_id": user_id,
            "slug": d["slug"],
            "tipo": d["tipo"],
            "rotulo": d["rotulo"],
            "texto": d["texto"],
            "ordem": d["ordem"],
            "versao_status": status_versao
        }
        for d in dispositivos
    ]
    tamanho_lote = 500
    for i in range(0, len(registros), tamanho_lote):
        lote = registros[i:i + tamanho_lote]
        supabase.table("dispositivos").insert(lote).execute()

def processar_lei(lei: dict):
    lei_id = lei["id"]
    user_id = lei["user_id"]
    url = lei["url"]
    print(f"Processando: {lei['titulo']} ({url})")

    try:
        html = baixar_html(url)
        dispositivos, novo_hash = extrair_dispositivos(html)

        if not dispositivos:
            raise ValueError("Nenhum dispositivo normativo extraído da URL.")

        print(f"Total extraído: {len(dispositivos)} dispositivos.")

        if not lei.get("hash_atual"):
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).execute()
            salvar_dispositivos_em_lotes(lei_id, user_id, dispositivos, "vigente")
            supabase.table("leis").update({
                "hash_atual": novo_hash,
                "status_extracao": "concluido",
                "tem_atualizacao": False,
                "mensagem_erro": None
            }).eq("id", lei_id).execute()
            print(f"Extração inicial concluída com sucesso ({len(dispositivos)} itens).")

        elif novo_hash != lei.get("hash_atual"):
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq("versao_status", "pendente").execute()
            salvar_dispositivos_em_lotes(lei_id, user_id, dispositivos, "pendente")
            supabase.table("leis").update({
                "hash_pendente": novo_hash,
                "tem_atualizacao": True,
                "status_extracao": "concluido",
                "mensagem_erro": None
            }).eq("id", lei_id).execute()
            print("Alteração legislativa detectada e salva como pendente!")

        else:
            supabase.table("leis").update({
                "status_extracao": "concluido",
                "mensagem_erro": None
            }).eq("id", lei_id).execute()
            print("Nenhuma alteração na fonte oficial.")

    except Exception as e:
        print(f"Erro ao processar {lei['titulo']}: {e}")
        supabase.table("leis").update({
            "status_extracao": "erro",
            "mensagem_erro": str(e)
        }).eq("id", lei_id).execute()

def main():
    query = supabase.table("leis").select("*")
    if MODO == "extrair_pendentes":
        res = query.in_("status_extracao", ["pendente", "processando"]).execute()
    else:
        res = query.execute()

    leis = res.data or []
    print(f"Encontradas {len(leis)} lei(s) para processar no modo '{MODO}'.")
    for lei in leis:
        processar_lei(lei)

if __name__ == "__main__":
    main()
