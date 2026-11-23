import hashlib
import os
import re
import sys
from bs4 import BeautifulSoup
import requests
from supabase import Client, create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
MODO = os.environ.get("MODO", "extrair_pendentes")  # 'extrair_pendentes' ou 'verificar_todas'

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def baixar_e_limpar_html(url: str) -> BeautifulSoup:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # Trata codificação antiga comum no Planalto
    if resp.encoding and resp.encoding.lower() in ["iso-8859-1", "latin-1"]:
        html = resp.content.decode("windows-1252", errors="replace")
    else:
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

    soup = BeautifulSoup(html, "lxml")

    # 1. Remove tags de texto riscado (<strike>, <s>, <del>)
    for tag in soup.find_all(["strike", "s", "del"]):
        tag.decompose()

    # 2. Remove elementos com CSS inline line-through (comum no Senado e Planalto)
    for tag in soup.find_all(style=re.compile(r"line-through", re.IGNORECASE)):
        tag.decompose()

    # 3. Remove classes de revogação do portal do Senado
    for tag in soup.find_all(class_=re.compile(r"(revogad|tachad|strike)", re.IGNORECASE)):
        tag.decompose()

    # 4. Remove scripts e estilos
    for tag in soup.find_all(["script", "style", "head", "nav", "footer"]):
        tag.decompose()

    return soup


def limpar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def normalizar_id(texto: str) -> str:
    limpo = re.sub(r"[^a-zA-Z0-9]", "", texto.upper())
    return limpo[:20]


def extrair_dispositivos(soup: BeautifulSoup):
    paragrafos_html = soup.find_all(["p", "div", "h1", "h2", "h3", "h4"])
    linhas_brutas = []

    for el in paragrafos_html:
        # Evita duplicar divs contêineres que possuem outros <p> dentro
        if el.name == "div" and el.find(["p", "div"]):
            continue
        txt = limpar_espacos(el.get_text(" "))
        if txt and len(txt) > 1:
            linhas_brutas.append(txt)

    # Remove linhas duplicadas consecutivas
    linhas = []
    for l in linhas_brutas:
        if not linhas or linhas[-1] != l:
            linhas.append(l)

    dispositivos = []
    art_atual = "INICIO"
    par_atual = ""
    inc_atual = ""
    contador_estrutura = 0

    # Expressões Regulares para identificação hierárquica (LC 95/1998)
    re_estrutura = re.compile(r"^(LIVRO|TÍTULO|TITULO|CAPÍTULO|CAPITULO|SEÇÃO|SECAO|SUBSEÇÃO|SUBSECAO)\b", re.IGNORECASE)
    re_artigo = re.compile(r"^(Art\.?\s*\d+[º°oO]?(?:-[A-Za-z])?)\s*[\.\-–]?\s*(.*)", re.IGNORECASE)
    re_paragrafo = re.compile(r"^(§\s*\d+[º°oO]?(?:-[A-Za-z])?|Parágrafo\s+único)\s*[\.\-–]?\s*(.*)", re.IGNORECASE)
    re_inciso = re.compile(r"^([IVXLCDM]+)\s*[-–—]\s*(.*)")
    re_alinea = re.compile(r"^([a-z])\)\s*(.*)")

    ordem = 0
    iniciou_corpo = False

    for linha in linhas:
        # Ignora cabeçalhos institucionais antes do primeiro Título/Capítulo/Artigo
        if not iniciou_corpo:
            if re_estrutura.match(linha) or re_artigo.match(linha):
                iniciou_corpo = True
            else:
                continue

        # Ignora links de navegação internos do Planalto ("Vide...", "Vigência", etc. isolados)
        if re.match(r"^\(?(Vide|Vigência|Regulamento|Produção de efeito)\)?$", linha, re.IGNORECASE):
            continue

        m_est = re_estrutura.match(linha)
        m_art = re_artigo.match(linha)
        m_par = re_paragrafo.match(linha)
        m_inc = re_inciso.match(linha)
        m_ali = re_alinea.match(linha)

        ordem += 1

        if m_est:
            contador_estrutura += 1
            slug = f"EST_{contador_estrutura}_{normalizar_id(linha)}"
            dispositivos.append({
                "slug": slug,
                "tipo": "estrutura",
                "rotulo": "",
                "texto": linha,
                "ordem": ordem
            })

        elif m_art:
            rotulo = limpar_espacos(m_art.group(1))
            corpo = limpar_espacos(m_art.group(2))
            art_atual = f"ART_{normalizar_id(rotulo)}"
            par_atual = ""
            inc_atual = ""
            slug = art_atual
            dispositivos.append({
                "slug": slug,
                "tipo": "artigo",
                "rotulo": rotulo,
                "texto": corpo,
                "ordem": ordem
            })

        elif m_par:
            rotulo = limpar_espacos(m_par.group(1))
            corpo = limpar_espacos(m_par.group(2))
            par_atual = f"PAR_{normalizar_id(rotulo)}"
            inc_atual = ""
            slug = f"{art_atual}_{par_atual}"
            dispositivos.append({
                "slug": slug,
                "tipo": "paragrafo",
                "rotulo": rotulo,
                "texto": corpo,
                "ordem": ordem
            })

        elif m_inc:
            rotulo = f"{m_inc.group(1)} -"
            corpo = limpar_espacos(m_inc.group(2))
            inc_atual = f"INC_{m_inc.group(1)}"
            base = f"{art_atual}_{par_atual}" if par_atual else art_atual
            slug = f"{base}_{inc_atual}"
            dispositivos.append({
                "slug": slug,
                "tipo": "inciso",
                "rotulo": rotulo,
                "texto": corpo,
                "ordem": ordem
            })

        elif m_ali:
            rotulo = f"{m_ali.group(1)})"
            corpo = limpar_espacos(m_ali.group(2))
            base = f"{art_atual}_{par_atual}" if par_atual else art_atual
            if inc_atual:
                base = f"{base}_{inc_atual}"
            slug = f"{base}_ALI_{m_ali.group(1).upper()}"
            dispositivos.append({
                "slug": slug,
                "tipo": "alinea",
                "rotulo": rotulo,
                "texto": corpo,
                "ordem": ordem
            })

        else:
            # Continuação de parágrafo anterior ou texto avulso dentro de artigo
            if dispositivos and dispositivos[-1]["tipo"] != "estrutura":
                dispositivos[-1]["texto"] += " " + linha
                ordem -= 1

    # Garante unicidade de slugs caso a lei tenha numeração atípica
    vistos = {}
    for d in dispositivos:
        s = d["slug"]
        if s in vistos:
            vistos[s] += 1
            d["slug"] = f"{s}_DUP{vistos[s]}"
        else:
            vistos[s] = 1

    texto_canonico = "\n".join([f"{d['slug']}:{d['texto']}" for d in dispositivos])
    hash_lei = hashlib.sha256(texto_canonico.encode("utf-8")).hexdigest()

    return dispositivos, hash_lei


def processar_lei(lei: dict, modo_atualizacao: bool = False):
    lei_id = lei["id"]
    user_id = lei["user_id"]
    url = lei["url"]

    try:
        if not modo_atualizacao:
            supabase.table("leis").update({"status_extracao": "processando"}).eq("id", lei_id).execute()

        soup = baixar_e_limpar_html(url)
        dispositivos, novo_hash = extrair_dispositivos(soup)

        if not dispositivos:
            raise ValueError("Nenhum artigo identificado na URL fornecida.")

        # Primeira extração da lei
        if not modo_atualizacao or not lei.get("hash_atual"):
            supabase.table("dispositivos").delete().eq("lei_id", lei_id).execute()

            lote = []
            for d in dispositivos:
                lote.append({
                    "lei_id": lei_id,
                    "user_id": user_id,
                    "slug": d["slug"],
                    "tipo": d["tipo"],
                    "rotulo": d["rotulo"],
                    "texto": d["texto"],
                    "ordem": d["ordem"],
                    "versao_status": "vigente"
                })

            # Insere em lotes de 500
            for i in range(0, len(lote), 500):
                supabase.table("dispositivos").insert(lote[i:i+500]).execute()

            supabase.table("leis").update({
                "hash_atual": novo_hash,
                "hash_pendente": None,
                "tem_atualizacao": False,
                "status_extracao": "concluido",
                "mensagem_erro": None,
                "ultima_verificacao": "now()"
            }).eq("id", lei_id).execute()
            print(f"[OK] Lei '{lei['titulo']}' extraída com {len(dispositivos)} dispositivos.")

        # Verificação diária de alteração legislativa
        else:
            if novo_hash != lei["hash_atual"]:
                # Salva como versão pendente para o usuário revisar e confirmar no site
                supabase.table("dispositivos").delete().eq("lei_id", lei_id).eq("versao_status", "pendente").execute()

                lote = []
                for d in dispositivos:
                    lote.append({
                        "lei_id": lei_id,
                        "user_id": user_id,
                        "slug": d["slug"],
                        "tipo": d["tipo"],
                        "rotulo": d["rotulo"],
                        "texto": d["texto"],
                        "ordem": d["ordem"],
                        "versao_status": "pendente"
                    })

                for i in range(0, len(lote), 500):
                    supabase.table("dispositivos").insert(lote[i:i+500]).execute()

                supabase.table("leis").update({
                    "hash_pendente": novo_hash,
                    "tem_atualizacao": True,
                    "ultima_verificacao": "now()"
                }).eq("id", lei_id).execute()
                print(f"[ALERTA] Alteração detectada na lei '{lei['titulo']}'!")
            else:
                supabase.table("leis").update({"ultima_verificacao": "now()"}).eq("id", lei_id).execute()
                print(f"[SEM ALTERAÇÃO] Lei '{lei['titulo']}' intacta.")

    except Exception as e:
        print(f"[ERRO] Falha ao processar '{lei['titulo']}': {e}")
        if not modo_atualizacao:
            supabase.table("leis").update({
                "status_extracao": "erro",
                "mensagem_erro": str(e)
            }).eq("id", lei_id).execute()


def main():
    if MODO == "verificar_todas":
        res = supabase.table("leis").select("*").eq("status_extracao", "concluido").execute()
        for lei in res.data:
            processar_lei(lei, modo_atualizacao=True)
    else:
        res = supabase.table("leis").select("*").eq("status_extracao", "pendente").execute()
        for lei in res.data:
            processar_lei(lei, modo_atualizacao=False)


if __name__ == "__main__":
    main()
